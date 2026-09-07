# System Architecture

**Document:** `01-architecture/01-system-architecture.md`
**Status:** Baseline
**Date:** 2026-08-28

---

## 1. Architectural style

**Modular monolith + independently scaled worker fleet, deployed as two roles of one binary.**

| Decision | Rationale | ADR |
| --- | --- | --- |
| Modular monolith, not microservices | A 6-person team cannot afford distributed-systems overhead before product-market fit. Module boundaries are enforced statically, so extraction later is mechanical. | [ADR-0001](05-adr/ADR-0001-modular-monolith.md) |
| Control plane / data plane split | Retrieval is SLO-bound and read-only; management is transactional and latency-tolerant. They must not share a failure domain or a scaling signal. | [ADR-0002](05-adr/ADR-0002-control-data-plane-split.md) |
| PostgreSQL-backed durable task queue | Transactional enqueue eliminates the dual-write class of bug; job state becomes SQL-queryable; one fewer system to operate. | [ADR-0003](05-adr/ADR-0003-postgres-task-queue.md) |
| Pluggable vector store | `FR-C-03` makes storage backend a user-facing option; pgvector for simplicity, Qdrant for scale. | [ADR-0004](05-adr/ADR-0004-vector-store-abstraction.md) |
| Denormalized read model in the vector store | Single round trip on the hot path; removes Postgres from retrieval entirely. | [ADR-0005](05-adr/ADR-0005-denormalized-read-model.md) |

---

## 2. Context view (C4 level 1)

```
        ┌──────────────────┐   ┌─────────────────┐   ┌───────────────────┐
        │ Platform Admin   │   │ Knowledge Eng.  │   │ Content Contrib.  │
        │  (browser)       │   │  (browser)      │   │  (browser)        │
        └────────┬─────────┘   └────────┬────────┘   └─────────┬─────────┘
                 │                      │                      │
                 └──────────────┬───────┴──────────────────────┘
                                │  HTTPS (session cookie)
                 ┌──────────────▼───────────────────────────────────────┐
                 │                                                       │
    ┌────────────┤                    Z F K S                            ├────────────┐
    │            │      Knowledge Base Platform / RAG Infra Server        │            │
    │            └───────────────────────────────────────────────────────┘            │
    │ HTTPS + API key                                              outbound HTTPS      │
    │                                                                                  │
┌───▼──────────────┐  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────────▼┐
│ External Agent   │  │ MCP client       │  │ Dify / LangChain │  │ Model providers    │
│ (custom app)     │  │ (Claude Code…)   │  │ (compat surface) │  │ OpenAI, Anthropic, │
└──────────────────┘  └──────────────────┘  └──────────────────┘  │ Gemini, Qwen,      │
                                                                   │ DeepSeek, Ollama…  │
                                                                   └────────────────────┘
                                             ┌──────────────────┐
                                             │ Web sites        │
                                             │ (crawl targets)  │
                                             └──────────────────┘
```

**Trust boundary:** everything inside the Cairn box is trusted; everything crossing the box
edge is untrusted, including crawled content and retrieved chunks fed to LLM nodes
(`NFR-SEC-09`, `NFR-SEC-10`).

---

## 3. Container view (C4 level 2)

```
                    ┌────────────────── Ingress: Nginx / Traefik ──────────────────┐
                    │  TLS · path routing · edge rate limit · SPA static serving   │
                    └───┬──────────────────────────────────────────────────┬───────┘
   /v1/retrieval/*      │                                                  │   everything else
   /mcp                 │                                                  │
   /v1/knowledge-bases  │ (GET, discovery)                                 │
                        ▼                                                  ▼
   ┌────────────────────────────────────┐          ┌────────────────────────────────────┐
   │  api-data          CAIRN_ROLE=data  │          │  api-control    CAIRN_ROLE=control  │
   │  ─────────────────────────────     │          │  ─────────────────────────────     │
   │  M09 retrieval                     │          │  M01 identity   M03 catalog        │
   │  M13 mcp-server                    │          │  M02 authz      M10 modelgw        │
   │  M02 keyauth (cache-only path)     │          │  M11 pipelines  M12 functions      │
   │                                    │          │  M14 evaluation M15 platform       │
   │  read-only · SLO-bound             │          │  M06 tasks (enqueue side)          │
   │  scales on RPS + p95               │          │  transactional · 2 replicas        │
   │  replicas: 2 … 32                  │          │  scales on nothing much            │
   └───┬────────────────┬───────────────┘          └────────────┬───────────────────────┘
       │                │                                       │
       │                │                                       │
       ▼                ▼                                       ▼
 ┌───────────┐   ┌──────────────────┐                ┌────────────────────────┐
 │ Redis 7   │   │ Vector store     │◀───────────────│ PostgreSQL 16          │
 │           │   │  pgvector |      │  derived from  │ ── SOURCE OF TRUTH ──  │
 │ principal │   │  Qdrant |        │                │ workspace, user, grant │
 │ kb config │   │  Elasticsearch   │                │ knowledge_base, doc,   │
 │ embed $   │   │                  │                │ chunk, task, audit,    │
 │ ratelimit │   │ READ MODEL:      │                │ pipeline, function     │
 │ pubsub    │   │ vec+sparse+text  │                │                        │
 └───────────┘   │ +filter payload  │                │ + read replicas        │
                 └──────────────────┘                └───────┬────────────────┘
                          ▲                                   │ claim via
                          │ upsert                            │ FOR UPDATE SKIP LOCKED
                          │                                   ▼
       ┌──────────────────┴───────────────────────────────────────────────────────┐
       │                       WORKER FLEET (CAIRN_ROLE=worker)                     │
       │  one Deployment per queue — separate replica counts, separate autoscaling │
       │                                                                            │
       │  worker-fetch    asyncio  ~200 conc   crawl, download        (I/O)         │
       │  worker-parse    process  = cores     PDF/DOCX/layout        (CPU) ← bottleneck
       │  worker-ocr      process  2–4         scanned docs           (CPU/GPU)     │
       │  worker-embed    asyncio  batch 64    vectorization          (GPU/API)     │
       │  worker-index    asyncio  16–32       vector upsert          (I/O)         │
       │  worker-maintain process  1–2         recrawl, GC, reindex   (mixed)       │
       └───────────┬──────────────────────────────────────┬───────────────────────┘
                   │                                       │
                   ▼                                       ▼
       ┌────────────────────────┐              ┌──────────────────────────┐
       │ embed-server           │              │ sandbox-runner            │
       │ TEI / Infinity         │              │ gVisor (runsc) pool       │
       │ bge-m3 + reranker      │              │ user Functions            │
       │ dynamic batching       │              │ no egress · ephemeral     │
       │ GPU optional           │              │ internal network only     │
       └────────────────────────┘              └──────────────────────────┘

       ┌────────────────────────┐
       │ Object store           │  MinIO / S3 / OSS
       │ originals, covers,     │
       │ parsed artifacts       │
       └────────────────────────┘
```

### Why "same binary, two roles"

`api-data` and `api-control` run the **identical image**. `CAIRN_ROLE` selects which routers
are mounted at startup:

```python
# apps/api/main.py
ROUTERS = {
    "data":    [retrieval_router, mcp_router, discovery_router],
    "control": [auth_router, users_router, grants_router, keys_router,
                kb_router, documents_router, models_router, pipelines_router,
                functions_router, eval_router, platform_router],
}
for r in ROUTERS[settings.role]:
    app.include_router(r)
```

Benefits: zero code duplication, one build, one test suite, and independent scaling and blast
radius. A runaway admin export cannot degrade the retrieval SLO (`NFR-R-02`).

A `CAIRN_ROLE=all` mode exists for local development and the `small` deployment preset.

---

## 4. Component view — module map

Full detail in [`04-module-boundaries.md`](04-module-boundaries.md).

```
                        ┌─────────────────────────────────────┐
   DATA PLANE  ★        │  M09 retrieval    M13 mcp-server    │
                        │  M02 keyauth (cache-only subset)    │
                        └───────────────┬─────────────────────┘
                                        │  may use ↓ only
   SHARED SERVICES      ┌───────────────▼─────────────────────┐
                        │ M05 vectorstore   M08 embedding     │
                        │ M04 objectstore   M10 modelgw       │
                        │ M06 tasks         M00 core          │
                        └───────────────▲─────────────────────┘
                                        │  may use ↑
   CONTROL PLANE        ┌───────────────┴─────────────────────┐
                        │ M01 identity  M02 authz  M03 catalog│
                        │ M11 pipelines M12 functions         │
                        │ M14 evaluation M15 platform         │
                        └─────────────────────────────────────┘
   WORKER               ┌─────────────────────────────────────┐
                        │ M07 ingestion  (uses shared + M03)  │
                        └─────────────────────────────────────┘
   FRONTEND             ┌─────────────────────────────────────┐
                        │ M16 web-ui  (HTTP client only)      │
                        └─────────────────────────────────────┘
```

**The one inviolable rule:** data-plane modules MUST NOT import control-plane modules.
Enforced by `import-linter` in CI (`NFR-M-02`).

---

## 5. Request paths

### 5.1 Retrieval — the hot path (target p95 < 150 ms)

```
 1. Ingress                     TLS, edge rate limit by key prefix        ~1 ms
 2. api-data: keyauth           Redis GET key_hash → Principal            ~1 ms   ← no Postgres
 3. api-data: authz             in-memory check kb_id ∈ principal.kbs     ~0 ms
 4. api-data: kb config         Redis GET kb config (version-stamped)     ~1 ms
 5. api-data: embed query       Redis GET embedding cache
                                  hit  → 0 ms
                                  miss → embed-server / provider          ~8–90 ms
 6. api-data: search            dense ∥ sparse issued concurrently        ~20 ms
                                  (asyncio.gather — cost is max, not sum)
 7. api-data: fuse              RRF over ranked lists                     ~1 ms
 8. api-data: rerank            cross-encoder over 100 candidates         ~45 ms
 9. api-data: assemble          parent expansion, dedupe, token budget    ~3 ms
10. response                    JSON envelope + RateLimit headers         ~1 ms
                                                                   ────────────
                                                       total, cache hit:  ~72 ms
                                                       total, cache miss: ~150 ms
```

**Postgres is not on this path.** Steps 2 and 4 are Redis; steps 5–8 are the vector store,
embed-server, and reranker. On a Redis miss, a fallback to Postgres repopulates the cache —
that path is slower and is measured separately as `cairn_authz_cache_miss_total`.

### 5.2 Ingestion — the throughput path

```
1. api-control receives upload
2. ONE TRANSACTION:  INSERT document (state='registered')
                     INSERT task    (queue='parse', state='ready')
                     COMMIT                              ← no dual-write window (NFR-R-03)
3. NOTIFY cairn_task_parse
4. worker-parse claims via FOR UPDATE SKIP LOCKED, takes a 10-min lease
5. parse → normalized Markdown + layout metadata → object store
6. ONE TRANSACTION:  UPDATE document SET state='parsed'
                     INSERT task (queue='chunk')  COMMIT
7. … chunk → embed → index, each stage the same shape
8. final: UPDATE document SET state='indexed', indexed_at=now()
```

Each stage transition is atomic. A crash anywhere resumes from the last committed stage
(`NFR-R-05`). Every stage is idempotent on `content_hash` (`NFR-R-06`).

### 5.3 Blue/green reindex

```
1. kb.building_index_version = active + 1
2. worker-maintain fans out re-embed + re-index tasks into namespace (kb_id, v_new)
3. queries continue served from (kb_id, v_active) throughout
4. on completion:  UPDATE knowledge_base
                      SET active_index_version = building_index_version,
                          building_index_version = NULL
                   → one atomic switch, config cache version bumped
5. after retention window (default 24 h) the old namespace is dropped
```

---

## 6. Failure domains

| Component fails | Effect on retrieval | Effect on ingestion | Mitigation |
| --- | --- | --- | --- |
| `api-control` | **None** | Cannot accept new uploads | Independent deployment (`NFR-R-02`) |
| `api-data` | Retrieval down | None | ≥ 2 replicas, HPA |
| PostgreSQL primary | **None while caches are warm** (TTL 60 s), then degraded | Halted; tasks resume on recovery | Read replica, connection pool, cache TTL |
| Redis | Degraded — falls back to Postgres, latency ↑ ~15 ms | Minor | Cache is never source of truth |
| Vector store | Retrieval fails for affected KBs | Index stage backs off | Per-KB isolation; `FR-H-17` partial results |
| embed-server | Cache hits still served; misses fail | Embed queue backs up | Provider API fallback (`FR-K-08`) |
| One worker | None | Lease expires, task reclaimed ≤ 2× lease | `NFR-R-04` |
| Model provider | Only if used for query embedding | That KB's embed stage backs off | Circuit breaker (`FR-K-07`) |
| sandbox-runner | None | Function nodes fail with clear error | Contained by design (`FR-M-09`) |

---

## 7. Cross-cutting flows

### Configuration and cache invalidation

Every cacheable entity carries a version counter in Postgres. Cache keys embed the version:

```
authz:principal:{key_hash}:{perm_version}
kb:config:{kb_id}:{config_version}
```

A grant or config change bumps the counter, which changes the key, which makes stale entries
unreachable and lets them expire naturally. **Never enumerate keys to invalidate** (`FR-B-12`).

### Secrets

```
CAIRN_MASTER_KEY (env / KMS)
        │ derives
        ▼
   KEK ──encrypts──▶ per-secret DEK ──encrypts──▶ ciphertext in Postgres
```

Decryption happens only in-process at point of use. Secrets never enter API responses, logs,
traces, or error strings (`NFR-SEC-02`, `NFR-SEC-03`).

### Correlation

`X-Request-Id` is accepted or generated at the ingress, propagated through every log line,
every span, and into `task.payload` so a worker's logs correlate back to the originating HTTP
request (`FR-I-08`, `NFR-O-01`).

---

## 8. Deployment topologies

| Preset | Shape | Capacity |
| --- | --- | --- |
| `small` | Single container `CAIRN_ROLE=all` + Postgres/pgvector + Redis + MinIO. 4 vCPU / 16 GB. | ~5k docs, ~10 RPS |
| `medium` | Split roles, 8 parse workers, Qdrant, local embed-server. 16 vCPU / 64 GB. | ~100k docs, ~200 RPS |
| `large` | K8s, HPA on `api-data`, KEDA on queue depth, GPU embed node, Qdrant cluster. | ~5M docs, ~2k RPS |

Detail in [`../06-ops/01-deployment.md`](../06-ops/01-deployment.md).

---

## 9. Scaling axes

| Axis | Mechanism | Trigger |
| --- | --- | --- |
| Retrieval throughput | `api-data` replicas, HPA on RPS + p95 | p95 > 60% of SLO |
| Ingestion throughput | `worker-parse` replicas, KEDA on `parse` backlog | backlog > 5× throughput |
| Embedding throughput | embed-server replicas; separate ingest and query fleets | queue wait > 1 s |
| Metadata capacity | pgbouncer → read replicas → partition `chunk`/`task`/`audit` → shard by `workspace_id` | > 200 conn or `chunk` > 50M rows |
| Vector capacity | pgvector → Qdrant single → quantization → Qdrant cluster sharded by `kb_id` | > 5M / > 50M vectors |
| KB count | hybrid collection layout: shared collection with tenant-partitioned payload index for small KBs, dedicated collections for large ones | > 200 KBs |

**Note on the KB-count axis:** collection-per-KB is the intuitive layout and it degrades badly
past a few hundred collections in Qdrant. The `Namespace` abstraction hides which layout is in
use, so promoting a KB from shared to dedicated is an operational action, not a migration
(`NFR-S-07`). See [M05](../02-modules/M05-vectorstore.md) §6.

---

## 10. What we are explicitly not doing, and why

| Not doing | Why |
| --- | --- |
| Microservices | Team size. Boundaries are enforced statically instead; extraction stays cheap. |
| Message broker (Kafka/RabbitMQ/Celery) | Dual-write bug class; opaque job state; fair scheduling is painful. Postgres queue is sufficient to ~10k claims/s. |
| Event sourcing | Audit log covers the compliance need at a fraction of the complexity. |
| GraphQL | The primary consumer is a machine calling one endpoint. REST + OpenAPI generates better SDKs. |
| gRPC on the public API | HTTP/JSON is what agent frameworks speak. gRPC internally only if a bottleneck appears. |
| Multi-region active-active | No requirement. Design permits read replicas later. |
| Kubernetes as the primary target | Most deployments are single-node. Compose first, Helm in Phase 5. |
