# Deployment

**Document:** `06-ops/01-deployment.md`
**Status:** Normative
**Date:** 2026-08-28

---

## 1. Deployment model

| Target | Mechanism | Phase |
| --- | --- | --- |
| **Single node (primary)** | Docker Compose v2 with profiles | 0 |
| Cluster | Kubernetes + Helm | 5 |
| Development | `docker compose --profile dev` (`CAIRN_ROLE=all`) | 0 |

The bar: **`docker compose up -d` produces a working system with no manual steps beyond reading
the printed admin password** (`NFR-D-01`).

---

## 2. Compose topology

```yaml
# docker-compose.yml (abridged — see deploy/compose/ for the full file)
services:
  postgres:                    # pgvector/pgvector:pg16 + zhparser
    healthcheck: {test: ["CMD-SHELL","pg_isready -U cairn"], interval: 5s, retries: 10}
    volumes: [pgdata:/var/lib/postgresql/data]

  redis:      {command: redis-server --appendonly yes, volumes: [redisdata:/data]}
  minio:      {command: server /data --console-address ":9001", volumes: [miniodata:/data]}

  qdrant:     {profiles: [scale],  volumes: [qdrantdata:/qdrant/storage]}
  embed:      {profiles: [gpu]}    # TEI/Infinity: bge-m3 + bge-reranker-v2-m3

  migrate:                     # ONE-SHOT. Alembic under pg_advisory_lock, then bootstrap admin.
    command: ["cairn","migrate","--and-bootstrap"]
    depends_on: {postgres: {condition: service_healthy}}
    restart: "no"

  api-control:
    environment: {CAIRN_ROLE: control}
    depends_on: {migrate: {condition: service_completed_successfully}}
    deploy: {replicas: 1}

  api-data:                    # ← the one you scale
    environment: {CAIRN_ROLE: data}
    depends_on: {migrate: {condition: service_completed_successfully}}
    deploy: {replicas: 2}

  worker-fetch:    {environment: {CAIRN_ROLE: worker, CAIRN_TASKS__QUEUE: fetch}}
  worker-parse:    {environment: {CAIRN_ROLE: worker, CAIRN_TASKS__QUEUE: parse}, deploy: {replicas: 4}}
  worker-ocr:      {environment: {CAIRN_ROLE: worker, CAIRN_TASKS__QUEUE: ocr}, profiles: [ocr]}
  worker-embed:    {environment: {CAIRN_ROLE: worker, CAIRN_TASKS__QUEUE: embed}}
  worker-index:    {environment: {CAIRN_ROLE: worker, CAIRN_TASKS__QUEUE: index}}
  worker-maintain: {environment: {CAIRN_ROLE: worker, CAIRN_TASKS__QUEUE: maintain}}

  sandbox:                     # gVisor runtime; internal network only, NO egress
    profiles: [functions]
    runtime: runsc
    networks: [sandbox_net]    # deliberately NOT on the default network

  nginx:                       # TLS, SPA, path routing
    ports: ["80:80","443:443"]

volumes: {pgdata: , redisdata: , miniodata: , qdrantdata: }
networks:
  default: 
  sandbox_net: {internal: true}     # no route to postgres/redis/minio/qdrant
```

### Non-obvious details that matter

| Detail | Why |
| --- | --- |
| **Migrations in a one-shot container** | Running them in the API entrypoint means N replicas race. `service_completed_successfully` gates everything else. (`NFR-D-03`) |
| **Advisory lock inside `migrate`** | Belt and braces — also protects against two `docker compose up` invocations |
| **Bootstrap runs in `migrate`** | Exactly once, regardless of API replica count (`FR-A-02`) |
| **Profiles for qdrant/gpu/ocr/functions** | The base stack must be small; optional services are opt-in (`NFR-D-02`) |
| **`sandbox_net` is `internal: true`** | The sandbox container physically cannot reach the data stores (`FR-M-04`) |
| **Real healthchecks** | `depends_on: service_healthy` prevents the "API starts, Postgres isn't ready, crash loop" first-run experience |
| **Named volumes** | Bind mounts break on Windows and SELinux hosts |

### Path routing

```nginx
location /v1/retrieval/  { proxy_pass http://api-data;    }
location /mcp            { proxy_pass http://api-data;    }
location /retrieval      { proxy_pass http://api-data;    }   # Dify compat
location /v1/            { proxy_pass http://api-control; }
location /               { root /usr/share/nginx/html;    }   # SPA
```

`client_max_body_size 200m;` and `proxy_read_timeout 300s;` for uploads. SSE endpoints need
`proxy_buffering off;`.

---

## 3. First run

```bash
git clone https://github.com/…/cairn && cd cairn
./install.sh                 # generates .env with CAIRN_MASTER_KEY and DB passwords
docker compose up -d
docker compose logs migrate  # ← the admin password is here, printed once
```

```
╔══════════════════════════════════════════════════════════════════╗
║  Cairn — initial administrator account created                   ║
║    username:  admin                                              ║
║    password:  7Kq2-mVx9RtL4pZsN3wY                               ║
║  This password is displayed ONCE and is not recoverable.         ║
║  You will be required to change it at first login.               ║
╚══════════════════════════════════════════════════════════════════╝
```

Then `http://localhost` → log in → forced credential dialog → done.

For automated deployment, set `CAIRN_INITIAL_ADMIN_PASSWORD` and nothing is printed; the forced
change still applies.

---

## 4. Sizing presets (`NFR-D-06`)

| Preset | Hardware | Configuration | Capacity |
| --- | --- | --- | --- |
| `small` | 4 vCPU / 16 GB / 200 GB | `CAIRN_ROLE=all`, pgvector, 2 parse workers, no GPU | ~5k docs, ~10 RPS |
| `medium` | 16 vCPU / 64 GB / 1 TB | Split roles, Qdrant, 8 parse workers, local embed | ~100k docs, ~200 RPS |
| `large` | K8s | 4× api-data, 16 parse, GPU embed node, Qdrant cluster | ~5M docs, ~2k RPS |

Shipped as `compose.small.yml`, `compose.medium.yml`, and `values-large.yaml`, with **measured**
figures from `T-OPS-17`, not estimates.

---

## 5. Kubernetes (Phase 5)

| Workload | Scaling | Notes |
| --- | --- | --- |
| `api-data` | HPA on RPS + p95 latency, 2–32 | The SLO-bound tier |
| `api-control` | Fixed 2 | HA only; it does not need to scale |
| `worker-parse` | **KEDA, PostgreSQL scaler on `parse` backlog**, 2–64 | Backlog is the correct signal |
| `worker-embed` | KEDA on `embed` backlog, 1–8 | GPU node selector where available |
| other workers | KEDA on their queue, 1–8 | |
| `sandbox-runner` | Fixed pool; `NetworkPolicy` denying all egress | |

```yaml
# KEDA — scale on backlog, not CPU
triggers:
  - type: postgresql
    metadata:
      query: "SELECT count(*) FROM task WHERE queue='parse' AND state='ready'"
      targetQueryValue: "20"
```

> A queue consumer's CPU is a **lagging** indicator — it only rises after work is already
> delayed, so CPU-based autoscaling always responds one task-duration too late. Backlog depth is
> the leading indicator (`NFR-S-06`).

Also required: `PodDisruptionBudget` on `api-data`; `terminationGracePeriodSeconds: 60` on
workers so leases are released on shutdown rather than waiting out expiry; `NetworkPolicy`
isolating `sandbox-runner`; Postgres via CloudNativePG or a managed service.

---

## 6. Configuration reference

Complete list in [M00 §4](../02-modules/M00-core.md). Essentials:

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `CAIRN_ROLE` | | `all` | `control` \| `data` \| `worker` \| `all` |
| `CAIRN_DATABASE_URL` | ✅ | — | |
| `CAIRN_REDIS_URL` | ✅ | — | |
| `CAIRN_MASTER_KEY` | ✅ | — | 32 bytes base64. **Losing it makes every stored secret unrecoverable.** |
| `CAIRN_INITIAL_ADMIN_PASSWORD` | | — | Automated deploy; suppresses the banner |
| `CAIRN_ENVIRONMENT` | | `dev` | `prod` enables stricter defaults |
| `CAIRN_TASKS__QUEUE` | worker | — | Which queue this worker serves |
| `CAIRN_TASKS__CONCURRENCY` | | per-queue | |
| `CAIRN_EMBEDDING__LOCAL_SERVER_URL` | | — | TEI/Infinity endpoint |
| `CAIRN_TELEMETRY__OTLP_ENDPOINT` | | — | Tracing off if unset |

**The process refuses to start on invalid configuration** rather than failing at first request.

---

## 7. Upgrades

```
1. Read the release notes for migration warnings
2. Back up: pg_dump + object store snapshot
3. docker compose pull
4. docker compose up -d migrate        # migrations run once, under the advisory lock
5. docker compose up -d                # rolling restart of API and workers
6. Verify /readyz on every replica
```

Migrations are **forward-compatible with the previous release** (`NFR-R-09`), so the old code can
run against the new schema during a rolling deploy. Breaking changes use expand → migrate →
contract across three releases.

Rollback: revert images and re-deploy. Because migrations are forward-compatible, the previous
release runs against the new schema. Migrations are **not** rolled back — that is a restore, not
an upgrade.

---

## 8. Backup and restore (`NFR-R-08`)

| Component | Method | Frequency |
| --- | --- | --- |
| PostgreSQL | `pg_dump -Fc`, or WAL archiving for PITR | Daily / continuous |
| Object store | MinIO replication or `mc mirror` | Daily |
| Vector store | **Not backed up — rebuildable** | — |
| Configuration | `.env` and Compose files in a secret manager | On change |
| `CAIRN_MASTER_KEY` | Offline, separately from backups | Once |

> The vector store is deliberately not backed up. It is a **derived read model** (ADR-0005) and
> is fully rebuildable from Postgres via blue/green reindex (ADR-0007). Backing it up doubles
> storage cost for data that can be regenerated. Restore procedure: restore Postgres and the
> object store, then trigger a reindex per KB.

**The restore drill (`T-OPS-16`) must be executed and documented before GA.** An untested backup
is not a backup.

---

## 9. Production hardening checklist

- [ ] TLS with a real certificate at the ingress
- [ ] `CAIRN_ENVIRONMENT=prod`
- [ ] `CAIRN_MASTER_KEY` from a secret manager, not `.env`
- [ ] Postgres on a managed service or with replication
- [ ] pgbouncer if > 200 connections
- [ ] Object store on S3/OSS rather than single-node MinIO
- [ ] Backups scheduled **and a restore rehearsed**
- [ ] `CAIRN_TELEMETRY__OTLP_ENDPOINT` set; dashboards imported
- [ ] Alert rules loaded and routing verified
- [ ] Resource limits set on every container
- [ ] `sandbox_net` isolation verified if functions are enabled
- [ ] Rate limits tuned for expected traffic
- [ ] Log shipping configured with retention
- [ ] Admin password changed; break-glass policy confirmed
- [ ] `docker compose config` reviewed for accidental port exposure

---

## 10. Troubleshooting first-run

| Symptom | Cause | Fix |
| --- | --- | --- |
| No admin password in logs | An admin already exists, or `CAIRN_INITIAL_ADMIN_PASSWORD` is set | `docker compose logs migrate`; otherwise reset via CLI |
| `api` crash-loops | Postgres not ready, or invalid config | Check `depends_on` healthcheck; read the startup error — it names the setting |
| Documents stuck in `registered` | No parse worker running | `docker compose ps worker-parse` |
| Retrieval returns nothing | KB has no `active_index_version` | Check index progress; documents may still be processing |
| `EMBEDDING_DIMENSION_MISMATCH` | Model dimension ≠ KB configuration | Reindex with the correct model; test the model first |
| Slow retrieval | Cold embedding cache, or `ef_search` too high | Check `cairn_embedding_cache_hits_total`; tune `ef_search` |
| MinIO unreachable | Wrong endpoint in the storage binding | Test the binding from the UI |
