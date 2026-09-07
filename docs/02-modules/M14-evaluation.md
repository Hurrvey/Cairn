# M14 — Evaluation and Model Testing

| | |
| --- | --- |
| **Package** | `cairn.evaluation` |
| **Layer** | L3 control plane |
| **Phase** | 5 |
| **Owner** | Backend eng. C |
| **Depends on** | M00, M02, M03 (facade), M05, M06, M08, M09 (facade), M10 (facade) |
| **Tables owned** | `golden_set`, `golden_item`, `eval_run`, `eval_result` |
| **Requirements owned** | FR-N-01..09 |

---

## 1. Purpose and scope

Answer one question with evidence: **did that change make retrieval better?**

**In scope:** golden sets, retrieval metrics, A/B configuration comparison, temporary index
versions for chunking/embedding experiments, run history, export.

**Out of scope:** a general LLM playground (ADR-0008). The chat test console belongs to M10/M16.

> Without this module, every retrieval tuning decision is a guess. With it, "we switched
> rerankers and nDCG@10 went from 0.61 to 0.68 at +38 ms p95" is a statement someone can act on.
> This is the module that turns retrieval quality from an opinion into a number.

---

## 2. Public interface

```python
class EvaluationService:
    # golden sets
    async def create_golden_set(self, actor, kb_id, spec) -> GoldenSetView: ...
    async def add_items(self, actor, gs_id, items: Sequence[GoldenItemSpec]) -> int: ...
    async def import_items(self, actor, gs_id, file: UploadFile, fmt) -> ImportReport: ...
    async def generate_items(self, actor, gs_id, spec: GenerateSpec) -> TaskRef: ...   # FR-N-02
    async def review_item(self, actor, item_id, approved: bool, edits=None) -> GoldenItemView: ...

    # runs
    async def start_run(self, actor, spec: EvalRunSpec) -> EvalRunView: ...
    async def get_run(self, run_id) -> EvalRunView: ...
    async def compare(self, run_ids: Sequence[UUID]) -> ComparisonReport: ...          # FR-N-04
    async def export_run(self, run_id, fmt) -> bytes: ...

@dataclass
class EvalRunSpec:
    golden_set_id: UUID
    label: str
    retrieval_config: RetrievalConfig | None       # query-time changes: no reindex
    chunk_config: ChunkConfig | None               # requires a temporary index
    embedding_model_id: UUID | None                # requires a temporary index
```

---

## 3. Metrics (`FR-N-03`)

| Metric | Definition | Reads as |
| --- | --- | --- |
| `recall@k` | fraction of relevant chunks appearing in the top k | "did we find it at all" |
| `precision@k` | fraction of the top k that are relevant | "how much noise" |
| `mrr` | mean reciprocal rank of the first relevant hit | "how fast to the first good answer" |
| `ndcg@k` | discounted cumulative gain, normalized | **the headline metric** — rank-aware, supports graded relevance |
| `hit_rate@k` | fraction of queries with ≥ 1 relevant hit in top k | easiest to explain to non-specialists |
| `latency_p50/p95` | per-query retrieval latency | the cost side of any quality gain |

Reported at k ∈ {1, 3, 5, 10, 20}. Graded relevance (`golden_item.relevance`) is used when
present; otherwise binary.

**nDCG@10 is the headline.** Report it alongside p95 latency always — a quality gain that
triples latency is a trade, not a win, and presenting them together prevents optimizing one into
the ground.

---

## 4. Behaviour

### 4.1 Run classification — the important branch

```python
async def start_run(self, actor, spec) -> EvalRunView:
    needs_reindex = spec.chunk_config is not None or spec.embedding_model_id is not None

    if needs_reindex:
        # Build a TEMPORARY index version. The KB's active version is untouched,
        # so production retrieval is unaffected throughout the experiment. (FR-N-06)
        temp_version = await self.catalog.allocate_temp_index_version(spec.kb_id)
        await self._build_temp_index(spec, temp_version)
        run.temp_index_version = temp_version
    else:
        # Query-time only (search mode, weights, reranker, top_k) — reuse the active index.
        # Cheap: seconds, not hours.
        run.temp_index_version = None
```

The distinction matters enormously in practice: comparing rerankers takes seconds; comparing
chunk sizes takes hours and provider spend. The UI must tell the user which they are asking for,
with an estimate, before the run starts.

Temporary versions are always cleaned up — on completion, on failure, and by a `maintain` sweep
for any orphaned by a crash.

### 4.2 Execution

Queries run concurrently (bounded at 8) against `M09` with the run's configuration, bypassing the
HTTP layer via the service facade so measured latency is the retrieval pipeline itself, not
network and serialization.

Progress is reported per query so a 500-query run is observable rather than an opaque wait.

### 4.3 Comparison (`FR-N-04`)

```jsonc
{
  "runs": [
    { "id": "run_A", "label": "bge-reranker-v2-m3", "metrics": { "ndcg@10": 0.61, "p95_ms": 340 } },
    { "id": "run_B", "label": "jina-reranker-v2",   "metrics": { "ndcg@10": 0.68, "p95_ms": 378 } }
  ],
  "delta": { "ndcg@10": { "absolute": 0.07, "relative": 0.115,
                          "significant": true, "p_value": 0.003 },
             "p95_ms":  { "absolute": 38, "relative": 0.112 } },
  "per_query": [
    { "query": "how do I rotate the signing key",
      "run_A": { "ndcg@10": 0.31 }, "run_B": { "ndcg@10": 0.89 }, "verdict": "improved" }
  ],
  "summary": { "improved": 71, "unchanged": 34, "regressed": 15 }
}
```

Significance uses a paired bootstrap over per-query nDCG. **Reporting significance is not
statistical decoration** — with a 50-query golden set, a 3% nDCG difference is noise, and teams
routinely ship changes based on exactly that noise.

`per_query` with regressions listed first is the most actionable part of the report: aggregate
metrics hide the case where a change helps 71 queries and destroys 15.

### 4.4 Golden set generation (`FR-N-02`)

An LLM proposes `(query, relevant_chunk_ids)` pairs from sampled chunks. Every generated item
starts `reviewed = false` and is **excluded from runs until a human approves it**. Generated
golden sets that grade themselves measure the generator, not the retriever — the review gate is
what makes them worth anything.

### 4.5 LLM-as-judge (`FR-N-08`, P2)

Optional end-to-end mode: retrieve, generate an answer, have a judge model score faithfulness
and relevance against the golden answer. Reported separately from retrieval metrics and clearly
labelled as model-dependent.

---

## 5. API endpoints owned

| Method | Path | Permission |
| --- | --- | --- |
| GET/POST | `/v1/golden-sets` | `eval:read` / `kb:manage` |
| GET/PATCH/DELETE | `/v1/golden-sets/{id}` | |
| POST | `/v1/golden-sets/{id}/items` · `/import` · `/generate` | `eval:run` |
| PATCH | `/v1/golden-items/{id}` (review) | `eval:run` |
| GET/POST | `/v1/eval-runs` | `eval:read` / `eval:run` |
| GET | `/v1/eval-runs/{id}` · `/results` · `/export` | `eval:read` |
| POST | `/v1/eval-runs/compare` | `eval:read` |

---

## 6. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M14-01 | Each metric matches a reference implementation on a known fixture | FR-N-03 |
| TC-M14-02 | nDCG handles graded relevance correctly | FR-N-03 |
| TC-M14-03 | Metrics are correct when a query has zero relevant chunks | FR-N-03 |
| TC-M14-04 | Query-time-only runs reuse the active index (no reindex) | §4.1 |
| TC-M14-05 | **Chunk-config runs build a temp index; the active one is untouched** | FR-N-06 |
| TC-M14-06 | **Temp index is cleaned up on success, failure, and after a crash** | FR-N-06 |
| TC-M14-07 | Comparison computes correct deltas and significance | FR-N-04 |
| TC-M14-08 | Per-query breakdown identifies regressions | FR-N-04 |
| TC-M14-09 | CSV and JSONL import with a validation report | FR-N-02 |
| TC-M14-10 | **Generated items are excluded from runs until reviewed** | FR-N-02 |
| TC-M14-11 | Run history is retained and exportable | FR-N-07 |
| TC-M14-12 | Progress is reported during a long run | §4.2 |
| TC-M14-13 | 500-query run completes within 10 minutes (query-time config) | §4.2 |

Coverage target: **85%**.

---

## 7. Acceptance criteria

- [ ] All 13 test cases pass
- [ ] Journey J7 completes end to end
- [ ] A reference golden set ships with the product for self-testing
- [ ] Metrics validated against `pytrec_eval` on a public IR fixture
- [ ] The UI states clearly whether a run requires reindexing, with a cost estimate

---

## 8. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M14-01 | ORM + migration: `golden_set`, `golden_item`, `eval_run`, `eval_result` | 0.5 |
| T-M14-02 | Metric implementations + reference validation | 2.0 |
| T-M14-03 | Golden set CRUD, import, review gate | 1.5 |
| T-M14-04 | LLM-assisted item generation | 1.5 |
| T-M14-05 | Run orchestration (query-time path) | 1.5 |
| T-M14-06 | **Temp index build + guaranteed cleanup** | 2.0 |
| T-M14-07 | Comparison + paired bootstrap significance | 1.5 |
| T-M14-08 | Export (CSV/JSONL) | 0.5 |
| T-M14-09 | LLM-as-judge mode | 1.5 |
| T-M14-10 | Routers | 1.0 |
| T-M14-11 | Tests TC-M14-01..13 | 1.5 |
| | **Total** | **15.0** |
