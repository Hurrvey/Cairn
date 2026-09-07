# Observability and Runbooks

**Document:** `06-ops/02-observability-runbook.md`
**Status:** Normative
**Date:** 2026-08-28

---

## 1. The four numbers that matter

Everything else is diagnostic. If a dashboard shows only four things, show these:

| # | Signal | Metric | Healthy |
| --- | --- | --- | --- |
| 1 | **Retrieval latency** | `cairn_retrieval_latency_seconds` p95 | < 150 ms |
| 2 | **Queue backlog** | `cairn_task_oldest_ready_age_seconds` | < 5 min |
| 3 | **Ingestion throughput** | `rate(cairn_ingestion_documents_total{outcome="indexed"})` | > 0 while work is queued |
| 4 | **Provider error rate** | `cairn_provider_requests_total{status!="ok"}` ratio | < 1% |

---

## 2. Metrics

Naming: `cairn_<subsystem>_<measure>_<unit>`.

### Retrieval (data plane)

```
cairn_retrieval_latency_seconds{stage,mode,kb_id}          histogram   ← per-stage attribution
cairn_retrieval_requests_total{status,mode}                counter
cairn_retrieval_results_returned                           histogram
cairn_retrieval_degraded_total{stage,reason}               counter     ← watch this
cairn_retrieval_partial_failures_total{code}               counter
```

`cairn_retrieval_degraded_total` rising means users are silently getting worse results — except
they are not silent, because `degraded` is in the response. Alert on it anyway.

### Tasks and ingestion

```
cairn_task_queue_depth{queue}                              gauge       ← KEDA scales on this
cairn_task_oldest_ready_age_seconds{queue}                 gauge       ← the starvation signal
cairn_task_running{queue}                                  gauge
cairn_task_duration_seconds{queue,kind,outcome}            histogram
cairn_task_lease_expired_total{queue}                      counter     ← worker instability
cairn_task_dead_lettered_total{queue,error_code}           counter
cairn_ingestion_documents_total{stage,outcome}             counter
cairn_ingestion_stage_duration_seconds{stage}              histogram
```

### Embedding, models, storage

```
cairn_embedding_cache_hits_total / _misses_total           counter     ← target > 30% hit rate
cairn_embedding_batch_size                                 histogram
cairn_provider_requests_total{provider,model,status}       counter
cairn_provider_latency_seconds{provider,model}             histogram
cairn_provider_circuit_state{provider}                     gauge       0 closed 1 open 2 half
cairn_vectorstore_operation_seconds{driver,op}             histogram
cairn_vectorstore_points{kb_id,index_version}              gauge
cairn_readmodel_drift_total                                counter     ← must stay 0
```

### Auth and platform

```
cairn_authz_cache_hits_total / _misses_total               counter     ← target > 99%
cairn_auth_login_total{outcome}                            counter
cairn_ratelimit_rejected_total{principal_type}             counter
cairn_quota_exceeded_total{workspace_id,kind}              counter
cairn_sandbox_executions_total{outcome}                    counter
```

**Cardinality rule:** never label by `principal_id`, `document_id`, `chunk_id`, or query text.
`kb_id` only where the deployment has < 1000 KBs.

---

## 3. Tracing

OpenTelemetry, OTLP export. Two trace shapes:

```
Retrieval:  ingress → api-data.retrieval.query
              ├── authz.resolve            (cache hit/miss attribute)
              ├── embedding.encode         (cache hit/miss)
              ├── vectorstore.search.dense    ┐ concurrent
              ├── vectorstore.search.sparse   ┘
              ├── retrieval.fuse
              ├── retrieval.rerank
              └── retrieval.assemble

Ingestion:  ingress → api-control.document.upload
              └── (correlation_id in task.payload)
                   ├── worker.parse       ← separate trace, LINKED by correlation_id
                   ├── worker.chunk
                   ├── worker.embed
                   └── worker.index
```

Trace context propagates into `task.payload` so a worker's spans link back to the originating
HTTP request. Without this, debugging "why did this upload never index" means correlating logs
by timestamp.

**Never** put query text, chunk content, or credentials in span attributes.

---

## 4. Dashboards

Shipped as provisioned Grafana JSON (`T-OPS-15`) so a fresh deployment is observable on day one.

| Dashboard | Panels |
| --- | --- |
| **Overview** | The four numbers, request rate, error rate, active KBs, total vectors |
| **Retrieval** | Latency by stage (stacked), RPS by mode, degradation events, cache hit rates, top KBs |
| **Ingestion** | Queue depths, oldest-ready age, documents by state, stage durations, failures by error code |
| **Models** | Provider latency and error rate, circuit states, token usage, estimated cost |
| **Storage** | Vector counts by KB, Postgres size by table, object store usage, drift counter |
| **Security** | Login failures, authz denials, rate-limit rejections, break-glass events, sandbox failures |

---

## 5. Alerts

| Alert | Condition | Severity | Runbook |
| --- | --- | --- | --- |
| `RetrievalSLOBreach` | p95 > 150 ms for 5 min | **page** | §6.1 |
| `RetrievalErrors` | 5xx > 1% for 5 min | **page** | §6.2 |
| `DataPlaneDown` | `up{role="data"} == 0` | **page** | §6.2 |
| `ErrorBudgetBurn` | > 10× normal burn rate | **page** | §6.1 |
| `QueueStarvation` | `oldest_ready_age` > 30 min | ticket | §6.3 |
| `LeaseExpiriesRising` | `lease_expired_total` increasing 10 min | ticket | §6.4 |
| `IngestionStalled` | Backlog > 0 and throughput = 0 for 15 min | ticket | §6.3 |
| `EmbeddingCacheCollapse` | Hit rate < 15% for 1 h | ticket | §6.5 |
| `ProviderErrors` | > 10% for 10 min | ticket | §6.6 |
| `CircuitOpen` | `circuit_state == 1` for 5 min | ticket | §6.6 |
| `ReadModelDrift` | `drift_total > 0` | ticket | §6.7 |
| `DiskPressure` | > 80% | ticket | — |
| `AuthzCacheDegraded` | Hit rate < 95% for 15 min | ticket | — |
| `SandboxFailures` | > 5% failure rate | ticket | §6.8 |
| `BreakGlassOpened` | Any event | notify | — |

---

## 6. Runbooks

### 6.1 Retrieval latency SLO breach

```
1. Which stage?  cairn_retrieval_latency_seconds by {stage}
     embed high     → cache hit rate? provider latency? → §6.5
     search high    → vector store CPU/memory; index size; ef_search too high
     rerank high    → reranker saturated; scale embed-server, or accept load shedding
     assemble high  → unusual; check parent expansion fan-out
2. Concurrent bulk ingest?  If yes, retrieval SHOULD be unaffected (ADR-0002) — if it is
   not, that is an architecture defect, not a capacity problem. Investigate.
3. Scale api-data replicas.
4. Confirm load shedding engaged: cairn_retrieval_degraded_total{reason="load_shed"}
5. If the vector store is the bottleneck: enable quantization, or shard the KB.
```

### 6.2 Data plane down or erroring

```
1. /readyz on each replica — which dependency is failing?
2. Redis down  → auth and config fall back to Postgres; latency rises; NOT fatal
3. Vector store down → affected KBs fail; others fine; check partial_failures
4. Postgres down → cached requests still serve (~60 s); then degraded. Restore Postgres.
5. All replicas down → check recent deploys; roll back images.
6. Note: control-plane failure MUST NOT cause this. If it did, the boundary was violated.
```

### 6.3 Ingestion stalled

```
1. Workers running?          docker compose ps / kubectl get pods
2. Backlog by queue?         cairn_task_queue_depth
3. Everything stuck in one queue → that worker pool is down or starved
4. Workspace fairness cap?   SELECT * FROM workspace_runtime WHERE running >= concurrency_limit
5. Quota pause?              cairn_quota_exceeded_total; check the workspace's quota
6. Dead-lettered?            SELECT error_code, count(*) FROM task WHERE state='failed' GROUP BY 1
7. Provider circuit open?    → §6.6
8. Resume: raise the quota, restart workers, or retry failed documents from the UI.
```

### 6.4 Lease expiries rising

Means workers are dying mid-task — OOM, eviction, or crash.

```
1. Which queue?  cairn_task_lease_expired_total by {queue}
2. Almost always `parse` or `ocr`: a large document exceeded the memory limit.
3. Check container memory limits and RSS. Parse workers should stay under 2 GB.
4. Identify the culprit:
     SELECT document_id, attempt FROM task
      WHERE error_code='LEASE_EXPIRED' ORDER BY attempt DESC LIMIT 20;
5. Raise the memory limit, or add a size guard for pathological documents.
No work is lost — the reaper requeues. But repeated expiry burns attempts toward dead-letter.
```

### 6.5 Embedding cache collapse

```
1. Redis healthy? Memory pressure causing eviction?
2. Did the KB's embedding model change? Cache keys include model_id — a change invalidates
   everything by design.
3. Traffic pattern shift: many unique queries genuinely means a low hit rate. Check whether
   the query distribution changed rather than assuming a bug.
4. Raise Redis maxmemory or the cache TTL if eviction is the cause.
```

### 6.6 Provider errors / circuit open

```
1. Which provider and model?  cairn_provider_requests_total{status!="ok"}
2. Check the provider's status page.
3. Auth failure (PROVIDER_AUTH_FAILED) → credential expired or rotated upstream. Re-enter it.
4. Rate limited → lower CAIRN_EMBEDDING__MAX_CONCURRENCY, or upgrade the provider tier.
5. Configure a fallback chain (FR-K-08) to avoid a repeat.
6. Circuit reopens automatically via half-open probe. Do not restart to "clear" it —
   that just removes the protection.
```

### 6.7 Read-model drift

```
1. Which KB?  Reconciliation logs name (kb_id, index_version) and the delta.
2. Small drift (< 0.1%): the repair task re-enqueues automatically. Verify it converges.
3. Large drift: something wrote to the vector store outside the index stage, or an index
   task failed silently. Check for failed index tasks.
4. Definitive fix: trigger a blue/green reindex. The read model is disposable by design.
```

### 6.8 Sandbox failures

```
1. Which outcome?  cairn_sandbox_executions_total{outcome}
     timeout / oom → a user function is misbehaving; identify it and notify the author
     unavailable   → the sandbox-runner service is down; restart it
     escape_denied → SECURITY EVENT — see below
2. On ANY suspected escape:
     a. Disable the Function Library platform-wide (feature flag)
     b. Preserve the sandbox logs and the function source
     c. Escalate to the security reviewer immediately
     d. Do not re-enable until reviewed
```

---

## 7. Log queries

Structured JSON, so these are direct field queries.

```
# every log line for one request, across API and workers
request_id = "req_01HQZX3N9K2M5P7R8T"

# why did this document fail?
document_id = "doc_01HQ…" AND level >= "WARNING"

# slow retrievals
event = "retrieval.completed" AND latency_ms > 500

# authorization denials for one principal
event = "authz.denied" AND principal_id = "key_01HQ…"

# every degradation in the last hour
event = "retrieval.*.degraded"
```

---

## 8. Capacity review

Monthly, 30 minutes:

| Check | Action if breached |
| --- | --- |
| Vector count vs. node memory | Enable quantization, or plan a shard |
| Postgres size and growth | Review retention; plan partitioning |
| Object store growth | Review retention |
| Queue backlog p95 | Add parse workers |
| Retrieval p95 trend | Scale `api-data`, or investigate a regression |
| Embedding token spend | Review enrichment settings; check for reindex loops |
| Error budget consumed | If > 50% at mid-month, pause data-plane feature work |

---

## 9. Observability requirements for new code

Every new operation ships with:

- [ ] A structured log at INFO on completion, with duration
- [ ] A structured log at WARNING or ERROR on failure, with the error code
- [ ] A metric — counter for events, histogram for durations
- [ ] A trace span if it performs I/O
- [ ] Request context bound (`request_id`, `workspace_id`, `principal_id`)
- [ ] No secrets, no content, no PII in any of the above

A feature that cannot be observed cannot be operated, and will eventually be the thing nobody
can debug at 3 a.m.
