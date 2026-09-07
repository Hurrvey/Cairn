# M09 — Retrieval ★

| | |
| --- | --- |
| **Package** | `cairn.retrieval` |
| **Layer** | L3 **data plane** — SLO-bound |
| **Phase** | 2 |
| **Owner** | **Backend lead or strongest available engineer** |
| **Depends on** | M00, `M02.dataplane`, M05, M06 (read-only), M08, `M11.runtime` |
| **Forbidden imports** | M01, M03, M07, M10, M12, M14, M15 (CI-enforced, `NFR-M-02`) |
| **Tables owned** | none — reads the vector store read model only |
| **Requirements owned** | FR-H-01..17, FR-I-01..03, FR-I-12, NFR-P-01, NFR-P-02, NFR-P-03, NFR-R-02 |

---

## 1. Purpose and scope

**This module is the product.** Everything else exists to make it better. It gets the strongest
contract guarantees, the tightest budgets, and the most tests.

**In scope:** query planning, dense/sparse/hybrid search, fusion, reranking, filtering, parent
expansion, deduplication, MMR, token budgeting, explain mode, federated multi-KB search, the
public retrieval API, graceful degradation.

**Out of scope:** anything that writes. This module is strictly read-only.

---

## 2. The pipeline

```
                     ┌─────────────────────────────────────────────┐
  request ──────────▶│ 1. authenticate    M02.dataplane (Redis)    │  ~1 ms
                     │ 2. authorize       kb:query on every target │  ~0 ms
                     │ 3. load config     KnowledgeBaseRuntime     │  ~1 ms
                     │ 4. plan            merge defaults+overrides │  ~0 ms
                     ├─────────────────────────────────────────────┤
                     │ 5. embed query     M08 (cache-first)        │  0–90 ms
                     ├─────────────────────────────────────────────┤
                     │ 6. search    ┌── dense  ─┐                  │
                     │              ├── sparse ─┤ asyncio.gather   │  ~20 ms
                     │              └── per target KB ─┘           │  (max, not sum)
                     ├─────────────────────────────────────────────┤
                     │ 7. fuse            RRF / weighted           │  ~1 ms
                     │ 8. filter          threshold, dedupe        │  ~0 ms
                     │ 9. rerank          cross-encoder            │  ~45 ms
                     │10. diversify       MMR (optional)           │  ~2 ms
                     │11. expand          parent windows           │  ~3 ms
                     │12. budget          trim to max_context      │  ~1 ms
                     └─────────────────────────────────────────────┘
                             total: ~72 ms cached · ~150 ms uncached
```

---

## 3. Public interface

```python
class RetrievalService:
    async def query(self, principal: Principal, req: RetrievalRequest) -> RetrievalResponse: ...
    async def query_batch(self, principal: Principal,
                          reqs: Sequence[RetrievalRequest]) -> list[RetrievalResponse]: ...
    async def list_accessible_kbs(self, principal: Principal) -> list[KbSummary]: ...
```

```python
class RetrievalRequest(BaseModel):
    targets: list[RetrievalTarget] = Field(min_length=1, max_length=10)
    query: str = Field(min_length=1, max_length=8192)
    query_vector: list[float] | None = None            # FR-H-13
    top_k: int = Field(5, ge=1, le=100)
    candidate_k: int | None = Field(None, ge=1, le=1000)
    search_mode: Literal["vector","fulltext","hybrid"] | None = None
    fusion: FusionSpec | None = None
    weights: SearchWeights | None = None
    score_threshold: float | None = None
    rerank: RerankSpec | None = None
    filters: FilterSpec | None = None
    options: RetrievalOptions = RetrievalOptions()
    strict: bool = False                               # FR-H-17

class RetrievalTarget(BaseModel):
    knowledge_base_id: str
    weight: float = Field(1.0, ge=0.0, le=10.0)

class RetrievalOptions(BaseModel):
    expand_parent: bool | None = None
    include_highlights: bool = False
    include_metadata: bool = True
    max_context_tokens: int | None = Field(None, ge=100, le=200_000)
    dedupe: Literal["none","by_chunk","by_document"] | None = None
    mmr: MmrSpec | None = None
    explain: bool = False
```

---

## 4. Behaviour

### 4.1 Configuration precedence

```
request field  >  KB retrieval_config  >  system default
```

Resolved once into an immutable `QueryPlan` before any I/O, so every stage sees one consistent
configuration and the plan can be logged verbatim for debugging.

### 4.2 Authorization (`FR-B-07`, `FR-H-04`)

```python
for target in req.targets:
    kb_id = decode_id("kb", target.knowledge_base_id)
    if not principal.can("kb:query", kb_id):
        raise PermissionError_(f"This key does not have kb:query on {target.knowledge_base_id}.")
```

Checked against the **precomputed in-memory** `Principal` — no database access. `kb:query` is
deliberately narrower than `kb:read`: a key may search a KB without being able to enumerate or
export it.

### 4.3 Hybrid search — concurrency is the point

```python
async def _search_targets(self, plan: QueryPlan) -> list[list[ScoredHit]]:
    tasks = []
    for target in plan.targets:
        store = await self.registry.for_binding(target.runtime.vector_binding)
        if plan.mode in ("vector", "hybrid"):
            tasks.append(("dense", target, store.search(target.runtime.namespace,
                          VectorQuery(dense=plan.query_vector, top_k=plan.candidate_k,
                                      filter=plan.filter, ef_search=plan.ef_search))))
        if plan.mode in ("fulltext", "hybrid"):
            tasks.append(("sparse", target, store.search(target.runtime.namespace,
                          VectorQuery(sparse=plan.query_sparse, top_k=plan.candidate_k,
                                      filter=plan.filter))))

    results = await asyncio.gather(*(t[2] for t in tasks), return_exceptions=True)
    # cost is the MAX of the searches, not the sum
```

`return_exceptions=True` implements `FR-H-17`: a failing target degrades to partial results with
the failure named, unless `strict: true`.

### 4.4 Fusion (`FR-H-02`)

**Reciprocal Rank Fusion is the default** because dense and sparse scores are not on comparable
scales — cosine similarity lives in [0,1] while BM25 is unbounded and corpus-dependent. RRF uses
only rank, so it needs no normalization and no per-corpus tuning.

```python
def rrf_fuse(lists: Sequence[RankedList], k: int = 60) -> list[ScoredHit]:
    scores: dict[UUID, float] = defaultdict(float)
    for rl in lists:
        for rank, hit in enumerate(rl.hits, start=1):
            scores[hit.id] += rl.weight / (k + rank)
    return sorted(...)
```

Per-target `weight` multiplies that target's contribution, implementing weighted federated
search (`FR-H-04`).

Weighted-score fusion is available for callers who have tuned normalization themselves, but is
not the default — it is a footgun without corpus-specific calibration.

### 4.5 Reranking (`FR-H-03`)

```python
async def _rerank(self, plan, candidates: list[ScoredHit]) -> list[ScoredHit]:
    if not plan.rerank.enabled or not candidates:
        return candidates
    try:
        async with asyncio.timeout(plan.rerank.timeout_s):     # default 1.5 s
            scores = await self.reranker.score(
                plan.query, [c.content for c in candidates], model=plan.rerank.model)
    except (TimeoutError, UpstreamError) as exc:
        # DEGRADE, DO NOT FAIL. Fused results are still useful; a 502 is not.
        plan.degraded.append(DegradationNotice(
            stage="rerank", reason=type(exc).__name__,
            detail="Reranking was skipped; results are ranked by fusion score."))
        log.warning("retrieval.rerank.degraded", error=str(exc))
        return candidates
    ...
```

Two-stage retrieval is the shape: **retrieve wide (100 candidates) with cheap ANN, rank narrow
(top 5) with an expensive cross-encoder.** Rerank typically improves nDCG@10 by 15–30% over
fusion alone and is the single highest-value quality stage.

### 4.6 Graceful degradation (`FR-H-16`) — a product principle, not an implementation detail

```python
class RetrievalResponse(BaseModel):
    request_id: str
    results: list[RetrievalHit]
    usage: UsageInfo
    degraded: list[DegradationNotice] = []      # ← ALWAYS present, never silently empty
    partial_failures: list[TargetFailure] = []
    truncated_to_token_budget: bool = False
    explain: ExplainOutput | None = None
```

| Situation | Behaviour |
| --- | --- |
| Reranker times out | Return fused results, `degraded: ["rerank"]` |
| One of three KBs fails | Return the other two, `partial_failures: [...]`, unless `strict` |
| Under load shedding | Disable rerank, `degraded: ["rerank:load_shed"]` |
| Token budget exceeded | Trim, `truncated_to_token_budget: true` |
| Index mid-rebuild | Serve the active version; note `index_version` in `usage` |

> Silent degradation destroys trust in infrastructure faster than an outage does. An Agent
> receiving worse results with no signal cannot adapt, cannot alert, and cannot debug. One that
> is told can do all three.

### 4.7 Parent expansion (`FR-H-10`)

Matched child chunks are replaced by their parents, deduplicated (several children of one
parent collapse to a single result), keeping the best child's score and recording
`matched_child_ids` so the caller can highlight the precise match inside the larger window.

Parents are fetched in **one batched** `VectorStore.fetch(parent_ids)` call, never per hit.

### 4.8 Token budgeting (`FR-H-09`)

```python
def _apply_token_budget(self, hits, budget: int) -> tuple[list[RetrievalHit], bool]:
    total, kept = 0, []
    for hit in hits:                       # already ranked best-first
        if total + hit.token_count > budget:
            return kept, True              # truncated
        kept.append(hit); total += hit.token_count
    return kept, False
```

Whole hits only — never a partially truncated chunk, which produces mid-sentence cuts that
degrade the LLM's answer. Every agent framework reimplements this badly; doing it server-side,
where the exact per-chunk token counts are already known, is strictly better.

### 4.9 Explain mode (`FR-H-08`) — the feature engineers tell each other about

```jsonc
{
  "explain": {
    "plan": { "mode":"hybrid", "candidate_k":100, "ef_search":100,
              "rerank_model":"bge-reranker-v2-m3", "targets":[…] },
    "stages": [
      { "stage":"embed",  "latency_ms":9,  "cache_hit":true },
      { "stage":"dense",  "latency_ms":21, "returned":100,
        "top": [{"chunk_id":"chk_…","score":0.81,"rank":1,"preview":"Key rotation…"}] },
      { "stage":"sparse", "latency_ms":18, "returned":100,
        "top": [{"chunk_id":"chk_…","score":12.4,"rank":1,"matched_terms":["rotate","signing"]}] },
      { "stage":"fuse",   "latency_ms":1, "method":"rrf", "returned":143,
        "rank_changes":[{"chunk_id":"chk_…","dense_rank":7,"sparse_rank":1,"fused_rank":2}] },
      { "stage":"rerank", "latency_ms":47, "returned":5,
        "rank_changes":[{"chunk_id":"chk_…","before":3,"after":1,"score":0.873}] },
      { "stage":"expand", "latency_ms":3, "expanded":5 }
    ]
  }
}
```

RAG failures are otherwise undiagnosable: a user says "it didn't find the obvious document" and
there is no way to tell whether the chunk was never indexed, ranked 400th in dense, filtered by
threshold, or dropped by the token budget. Explain answers that in one request.

`explain: true` costs ~5 ms extra and is **rate-limited more aggressively** than normal queries
because it returns substantially more data.

### 4.10 Load shedding (`NFR-P-03`)

```python
if self.load_monitor.p95_latency > self.slo.p95 * 0.8:
    plan.rerank.enabled = False
    plan.degraded.append(DegradationNotice(stage="rerank", reason="load_shed"))
if self.load_monitor.p95_latency > self.slo.p95 * 1.5:
    plan.candidate_k = min(plan.candidate_k, 50)
```

Degrade **quality** before availability, and always announce it.

---

## 5. API endpoints owned

| Method | Path | Permission | Req |
| --- | --- | --- | --- |
| POST | `/v1/retrieval/query` | `kb:query` on every target | FR-I-02 |
| POST | `/v1/retrieval/batch` | same | FR-H-14 |
| GET | `/v1/knowledge-bases` (discovery projection) | `kb:read` or `kb:query` | FR-I-12 |
| GET | `/v1/chunks/{id}` | `kb:read` | — |
| POST | `/retrieval` (Dify compatibility) | `kb:query` | NFR-C-01 |

Full contract in [`../03-api/02-data-plane-api.md`](../03-api/02-data-plane-api.md).

---

## 6. Performance requirements — the SLO

| Metric | Target | Conditions |
| --- | --- | --- |
| p50 latency | **< 60 ms** | hybrid, no rerank, top_k ≤ 10, ≤ 1M vectors |
| p95 latency | **< 150 ms** | same (`NFR-P-01`) |
| p99 latency | **< 300 ms** | same |
| p95 with rerank | **< 400 ms** | 100 candidates, local reranker (`NFR-P-02`) |
| Throughput | ≥ 200 RPS per 4-vCPU replica (`NFR-P-03`) | |
| Auth overhead | < 5 ms p99 (`NFR-P-06`) | |
| Availability | ≥ 99.9% monthly (`NFR-R-01`) | |

### Per-stage budget (p95, cached embedding)

| Stage | Budget |
| --- | --- |
| auth + authorize | 3 ms |
| config load | 2 ms |
| query embed (cache hit) | 5 ms |
| dense ∥ sparse | 40 ms |
| fuse | 3 ms |
| rerank | 250 ms (only when enabled) |
| expand + dedupe + budget | 10 ms |
| serialize | 5 ms |

Each stage emits its own histogram. A regression is attributable to a stage without profiling.

---

## 7. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M09-01 | Dense search returns semantically relevant results | FR-H-01 |
| TC-M09-02 | Sparse search finds exact terms dense misses (product codes, acronyms) | FR-H-01 |
| TC-M09-03 | **Hybrid outperforms both on the reference golden set** | FR-H-02 |
| TC-M09-04 | Dense and sparse execute concurrently (elapsed ≈ max, not sum) | §4.3 |
| TC-M09-05 | RRF fusion matches the reference implementation | FR-H-02 |
| TC-M09-06 | Per-target weights shift ranking as expected | FR-H-04 |
| TC-M09-07 | Rerank improves nDCG@10 on the reference set | FR-H-03 |
| TC-M09-08 | **Rerank timeout degrades, not fails, and reports it** | FR-H-16 |
| TC-M09-09 | Federated search across 3 KBs merges correctly | FR-H-04 |
| TC-M09-10 | **Missing `kb:query` on ONE target rejects the whole request** | FR-B-07 |
| TC-M09-11 | `kb:query` without `kb:read` permits retrieval | FR-B-07 |
| TC-M09-12 | Every filter operator behaves per the normative semantics | FR-H-05 |
| TC-M09-13 | Document-ID and time-range filters | FR-H-06 |
| TC-M09-14 | Score threshold excludes; **zero results is a 200, not an error** | FR-H-07 |
| TC-M09-15 | Explain output contains all stages with rank changes | FR-H-08 |
| TC-M09-16 | **Token budget trims to whole hits and sets the flag** | FR-H-09 |
| TC-M09-17 | Parent expansion returns parents, dedupes siblings, keeps best score | FR-H-10 |
| TC-M09-18 | `by_document` dedupe returns one hit per document | FR-H-11 |
| TC-M09-19 | MMR increases result diversity measurably | FR-H-12 |
| TC-M09-20 | `query_vector` bypasses embedding entirely | FR-H-13 |
| TC-M09-21 | Batch query shares one embedding batch | FR-H-14 |
| TC-M09-22 | **One failing target returns partial results with the failure named** | FR-H-17 |
| TC-M09-23 | `strict: true` fails the whole request instead | FR-H-17 |
| TC-M09-24 | **Retrieval reads only `active_index_version` during a rebuild** | ADR-0007 |
| TC-M09-25 | KB config change takes effect within the cache TTL | — |
| TC-M09-26 | A `deleting` KB rejects retrieval immediately | — |
| TC-M09-27 | **`import-linter`: no control-plane import** | NFR-M-02 |
| TC-M09-28 | **No PostgreSQL connection is opened on the happy path** | ADR-0002 |
| TC-M09-29 | **Load test: p95 < 150 ms at 200 RPS on 1M vectors** | NFR-P-01/03 |
| TC-M09-30 | Load test with rerank: p95 < 400 ms | NFR-P-02 |
| TC-M09-31 | Load shedding disables rerank and reports it | §4.10 |
| TC-M09-32 | Malformed requests return RFC 9457 with field-level errors | FR-I-04 |

Coverage target: **90%** — this is the product.

---

## 8. Acceptance criteria

- [ ] All 32 test cases pass
- [ ] Load test TC-M09-29 sustained for 10 minutes with no p95 regression
- [ ] `import-linter` confirms data-plane isolation
- [ ] TC-M09-28 verified by instrumenting the connection pool
- [ ] Journey J4 completes in under 15 minutes for a first-time developer
- [ ] Every stage has its own latency histogram
- [ ] `degraded` is populated in every degradation path (no silent degradation anywhere)
- [ ] OpenAPI documents the full contract with working examples

---

## 9. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M09-01 | Request/response schemas + validation | 1.0 | T-M00-03 |
| T-M09-02 | `QueryPlan` resolution (precedence merge) | 1.0 | T-M09-01 |
| T-M09-03 | Authentication + authorization via `M02.dataplane` | 0.5 | T-M02-05 |
| T-M09-04 | KB runtime config loading + cache + invalidation subscribe | 1.0 | T-M03-06 |
| T-M09-05 | Filter spec → `FilterNode` translation | 1.0 | T-M05-02 |
| T-M09-06 | **Concurrent dense + sparse execution** | 1.5 | T-M05-06/07 |
| T-M09-07 | **RRF + weighted fusion** | 1.0 | T-M09-06 |
| T-M09-08 | Reranker client + timeout degradation | 1.5 | T-M10 |
| T-M09-09 | Threshold, dedupe, MMR | 1.0 | T-M09-07 |
| T-M09-10 | Parent expansion (batched fetch) | 1.0 | T-M09-07 |
| T-M09-11 | Token budgeting | 0.5 | T-M09-10 |
| T-M09-12 | **Explain mode** | 1.5 | T-M09-07 |
| T-M09-13 | Federated multi-KB + partial failure handling | 1.5 | T-M09-06 |
| T-M09-14 | Batch query endpoint | 1.0 | T-M09-06 |
| T-M09-15 | Discovery endpoint | 0.5 | T-M09-03 |
| T-M09-16 | Load monitor + shedding | 1.0 | T-M09-02 |
| T-M09-17 | Per-stage metrics + tracing | 1.0 | T-M00-09 |
| T-M09-18 | Router, error mapping, rate limit headers | 1.0 | T-M09-01 |
| T-M09-19 | Dify compatibility endpoint | 0.5 | T-M09-18 |
| T-M09-20 | Golden-set fixture + quality tests | 2.0 | T-M09-08 |
| T-M09-21 | Load-test harness + TC-M09-29/30 | 2.0 | all |
| T-M09-22 | Tests TC-M09-01..32 | 3.0 | all |
| | **Total** | **26.0** | |
