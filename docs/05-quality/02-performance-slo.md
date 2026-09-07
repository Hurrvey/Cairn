# Performance and Service Level Objectives

**Document:** `05-quality/02-performance-slo.md`
**Status:** Normative — published as part of the API contract
**Date:** 2026-08-28

An infrastructure product without published numbers is not infrastructure. These are commitments,
measured continuously, not aspirations.

---

## 1. Published SLOs

Measured server-side, excluding network transit, on the `medium` preset.

| SLI | Objective | Conditions | Req |
| --- | --- | --- | --- |
| Retrieval latency p50 | **< 60 ms** | hybrid, no rerank, top_k ≤ 10, ≤ 1M vectors | NFR-P-01 |
| Retrieval latency p95 | **< 150 ms** | same | NFR-P-01 |
| Retrieval latency p99 | **< 300 ms** | same | NFR-P-01 |
| Retrieval latency p95, reranked | **< 400 ms** | 100 candidates, local cross-encoder | NFR-P-02 |
| Retrieval availability | **≥ 99.9%** monthly | data plane | NFR-R-01 |
| Retrieval throughput | **≥ 200 RPS** | per 4-vCPU `api-data` replica at p95 SLO | NFR-P-03 |
| Ingestion throughput | **≥ 120 docs/hour/core** | typical 100-page PDF | NFR-P-04 |
| Embedding throughput | **≥ 2000 chunks/s** | bge-m3, L4 GPU, batch 64 | NFR-P-05 |
| Authorization overhead | **< 5 ms p99** | cache hit | NFR-P-06 |

**Error budget:** 99.9% monthly = 43 minutes. Consumed budget pauses feature work on the data
plane in favour of reliability work.

---

## 2. Latency budget

Per-stage p95 targets, cached embedding. Each stage has its own histogram, so a regression is
attributable without profiling.

| Stage | Budget | Metric label |
| --- | --- | --- |
| Ingress + TLS | 2 ms | (edge) |
| API key auth | 3 ms | `stage="auth"` |
| Authorization | < 1 ms | `stage="authz"` |
| KB config load | 2 ms | `stage="config"` |
| Query embedding, cache hit | 5 ms | `stage="embed"` |
| Query embedding, cache miss | +85 ms | `stage="embed"` |
| Dense ∥ sparse search | 40 ms | `stage="search"` |
| Fusion | 3 ms | `stage="fuse"` |
| Rerank (when enabled) | 250 ms | `stage="rerank"` |
| Parent expansion | 5 ms | `stage="expand"` |
| Dedupe, MMR, budget | 5 ms | `stage="assemble"` |
| Serialization | 5 ms | `stage="serialize"` |
| **Total, cache hit, no rerank** | **~66 ms** | |
| **Total, cache miss, no rerank** | **~151 ms** | |
| **Total, cache hit, rerank** | **~316 ms** | |

Dense and sparse are `asyncio.gather`-ed, so their cost is the **maximum**, not the sum. This is
worth 20 ms and is asserted by `TC-M09-04`.

---

## 3. Capacity model

### Vector memory, 1024-d, HNSW m=16

| Mode | Bytes/vector | 1M | 10M | 100M |
| --- | --- | --- | --- | --- |
| float32 | ~4.2 KB | 4.2 GB | 42 GB | 420 GB |
| int8 scalar + rescore | ~1.1 KB | 1.1 GB | 11 GB | 110 GB |
| binary + rescore | ~0.15 KB | 0.15 GB | 1.5 GB | 15 GB |

**Rule of thumb:** a 16 GB Qdrant node comfortably serves ~10M vectors at int8 — roughly
30k–50k typical documents. Scalar quantization costs ~1% recall and should be the default above
1M vectors. Binary is for > 50M only.

### Ingestion — parse dominates by an order of magnitude

A 100-page PDF ≈ 300 chunks.

| Stage | Cost | Throughput per unit |
| --- | --- | --- |
| Parse (layout + tables) | 15–30 s CPU | 2–4 docs/min/core |
| OCR (scanned) | 1–3 s/page | 0.5 docs/min/core |
| Embed (L4, batch 64) | ~0.15 s / 300 chunks | 2–3k chunks/s |
| Index (bulk upsert) | ~10 ms / 300 chunks | ~30k chunks/s |

| Corpus | 8 parse cores | 32 cores | 64 cores |
| --- | --- | --- | --- |
| 10k docs | 7 h | 1.8 h | 0.9 h |
| 100k docs | 70 h | 17 h | 9 h |
| 1M docs | 700 h | 175 h | 87 h |

**Parse workers are the scaling unit.** The number to publish is documents/hour/core. Embedding
becomes the bottleneck only when using a hosted provider — another argument for local TEI.

### Postgres

| Table | Row size | 100k docs | 1M docs |
| --- | --- | --- | --- |
| `document` | ~1 KB | 100 MB | 1 GB |
| `chunk` | ~1.4 KB | 42 GB | 420 GB |
| `task` (transient) | ~0.5 KB | < 1 GB | < 5 GB |

Partition `chunk` by `kb_id` hash from day one (16 partitions) — retrofitting is a rewrite.

---

## 4. Sizing presets (`NFR-D-06`)

| Preset | Hardware | Vector backend | Capacity | Ingest rate |
| --- | --- | --- | --- | --- |
| `small` | 4 vCPU / 16 GB / 200 GB | pgvector | ~5k docs, ~1.5M vectors, ~10 RPS | ~8 docs/h |
| `medium` | 16 vCPU / 64 GB / 1 TB | Qdrant, int8 | ~100k docs, ~30M vectors, ~200 RPS | ~500 docs/h |
| `large` | K8s: 4× api-data, 16 parse workers, GPU embed, Qdrant cluster | Qdrant sharded | ~5M docs, ~1.5B vectors, ~2k RPS | ~8k docs/h |

Each preset ships as a Compose or Helm values file with measured, not estimated, figures.

---

## 5. Load profiles

| Profile | Shape | Asserts |
| --- | --- | --- |
| `steady` | 200 RPS for 10 min | p95 SLO sustained without drift |
| `burst` | 50 → 500 RPS in 10 s | Autoscaling and load shedding behave |
| `mixed` | 80% search, 15% batch, 5% explain | Realistic composition |
| `cold` | No cache warm-up | Cache-miss path stays within budget |
| `concurrent_ingest` | Retrieval load during a bulk ingest | **Retrieval SLO holds** — the point of ADR-0002 |
| `large_kb` | 10M vectors, single KB | Scale claim (`NFR-S-03`) |

`concurrent_ingest` is the one that validates the whole architecture: if a bulk ingest degrades
retrieval, the control/data plane split has failed somewhere.

---

## 6. Optimization tactics, in order of leverage

| # | Tactic | Effect | Where |
| --- | --- | --- | --- |
| 1 | Postgres off the hot path | Removes a whole dependency from the SLO chain | ADR-0002, ADR-0005 |
| 2 | Embedding cache | 30–60% of queries skip the provider entirely | M08 §3.1 |
| 3 | Concurrent dense + sparse | Cost is max, not sum: ~20 ms | M09 §4.3 |
| 4 | Local embed/rerank server | 40–120 ms → ~8 ms, and no rate limit | M08, M10 |
| 5 | Two-stage retrieval | Cheap ANN wide, expensive cross-encoder narrow | M09 §4.5 |
| 6 | Scalar quantization | 4× memory, ~1% recall | M05 §8 |
| 7 | Batched parent fetch | One round trip instead of N | M09 §4.7 |
| 8 | Shared `httpx.AsyncClient` | Avoids per-request TLS handshake | M00 |
| 9 | Per-provider semaphores | One slow provider cannot exhaust the pool | M10 §4.2 |
| 10 | Partial index on `task` | Claim query stays fast regardless of table size | M06 |

---

## 7. Load shedding

Degrade **quality** before availability, and announce every degradation (`FR-H-16`).

| Trigger | Action | Reported as |
| --- | --- | --- |
| p95 > 80% of SLO | Disable rerank | `degraded: [{stage:"rerank", reason:"load_shed"}]` |
| p95 > 150% of SLO | Cap `candidate_k` at 50 | `degraded: [{stage:"search", reason:"load_shed"}]` |
| Over quota | 429 with `Retry-After` | `RATE_LIMIT_EXCEEDED` |
| Vector store unavailable | Fail that target only | `partial_failures[]` |

> Silent degradation destroys trust in infrastructure faster than an outage. An Agent that is
> told it received lower-quality results can retry, alert, or fall back. One that is not, cannot.

---

## 8. Continuous verification

| When | What |
| --- | --- |
| Every PR | Micro-benchmarks: authz, embedding cache, fusion, filter translation |
| Every merge to main | Full load suite; **10% regression fails the build** |
| Nightly | `concurrent_ingest`, `large_kb`, chaos |
| Pre-release | All profiles on all three presets; results published in release notes |

Latency regressions accumulate invisibly one PR at a time. Measuring only at release means
discovering a 3× regression with no idea which of 400 commits caused it.

---

## 9. Monitoring and alerting

| Alert | Condition | Severity |
| --- | --- | --- |
| Retrieval SLO breach | p95 > 150 ms for 5 min | page |
| Retrieval errors | 5xx rate > 1% for 5 min | page |
| Error budget burn | > 10× normal rate | page |
| Queue backlog | `oldest_ready_age` > 30 min | ticket |
| Lease expiries rising | `lease_expired_total` increasing | ticket |
| Embedding cache collapse | Hit rate < 15% for 1 h | ticket |
| Provider errors | > 10% for 10 min | ticket |
| Read-model drift | `drift_total` > 0 | ticket |
| Disk | > 80% | ticket |
| Quota | Workspace > 90% | notify owner |

Dashboards and alert rules ship as provisioned Grafana JSON (`T-OPS-15`), so a new deployment is
observable on day one rather than after someone builds dashboards by hand.
