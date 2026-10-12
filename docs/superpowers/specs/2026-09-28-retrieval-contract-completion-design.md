# Retrieval Contract Completion (Phase 2 closure) — Design

**Date:** 2026-09-28 · **Status:** Draft for independent review · **Owner goal:** finish the
unfinished development; design deviations need a second agent's agreement before code changes.

`docs/03-api/02-data-plane-api.md` is the normative product surface. Today
`POST /v1/retrieval/query` rejects `filters`, `include_highlights`, `dedupe: by_document`,
`mmr.enabled` and `explain` (`retrieval/service.py::_validate_supported`), the response `explain`
is typed `None`, `source.title/type/updated_at` are never filled, and `/v1/retrieval/batch`,
`/v1/chunks/{id}`, `/v1/documents/{id}/content` do not exist. KB-configured MMR and document
dedupe are reported as degraded. This design closes those gaps.

## 1. Read model v2 (ADR-0005 conformance)

ADR-0005 already specifies that the point payload carries `created_at` and `meta` (document
metadata). The index stage never wrote them, which is why metadata/time filters were impossible.

- Payload additions written by `_points` and by chunk re-embed (`_chunk_reembed_payload`):
  - `meta`: the document's `metadata` JSON (fixed per document revision — no endpoint edits it;
    a replaced document is a new revision and is re-indexed).
  - `created_at`: document `created_at` as integer epoch seconds.
  - `doc_title`, `doc_type` (file extension from the stored name/MIME, e.g. `pdf`),
    `doc_updated_at` (epoch seconds of the indexed revision) → fill the response `source`.
  Sources: `IngestionRun` and `ChunkReembedTarget` gain `document_metadata`, `document_title`,
  `document_type`, `document_created_at`, `document_updated_at`, loaded from the `document` row
  where these DTOs are already built.
- These keys are added to the service's `internal` set so `metadata` in responses does not
  duplicate them; `meta` is flattened into the response `metadata` (user metadata is what callers
  expect there, e.g. `lang`, `tags`), chunk metadata keys win on collision.
- **Marker:** `IndexVersionConfig.payload_schema: int = 1` (old snapshots parse as 1). Versions
  captured after this change record `2`. `KnowledgeBaseRuntime.payload_schema` carries the
  ACTIVE version's value (projection default 1).
- A metadata or time filter against a target whose ACTIVE version has `payload_schema < 2` is a
  `422 FILTER_REQUIRES_REBUILD` naming the knowledge base ("rebuild the index to enable metadata
  and date filters"). Never a silent empty result. `document_ids` works on every version
  (`document_id` has always been in the payload).

## 2. Filters (FR-H-05, FR-H-06)

`FilterSpec` → one `FilterNode` per request, applied to both the dense and the sparse search of
every target (pushed down to Qdrant via the existing exact-semantics translation):

- `metadata`: parsed by `parse_filter` with every top-level field path prefixed by `meta.`
  (logical operators `$and/$or/$not` recurse; dotted user paths become `meta.a.b`). Operator
  semantics are the normative ones in `vectorstore/filters.py`.
- `document_ids`: decoded `doc_…` IDs → `document_id $in [uuid strings]` (max 1000; invalid ID →
  400 `INVALID_ID`).
- `created_after` / `created_before` → `created_at $gte / $lt` epoch seconds (timezone-aware
  input required; naive → 422).
- Bounded: filter JSON ≤ 16 KB, depth ≤ 8, ≤ 256 leaf conditions (422 otherwise).

## 3. Post-fusion stages, in spec order (M09 §4: filter → rerank → expand → dedupe → MMR → budget)

- **Threshold** unchanged.
- **Dedupe** (`options.dedupe` or KB `retrieval_config.dedupe`): `by_chunk` (current behaviour:
  unique chunk IDs, including collapsed parents) and `by_document` (keep the best-scoring hit per
  `document_id`, stable order). Applied after parent expansion, before `top_k`.
- **MMR** (`options.mmr` or KB config; FR-H-12): greedy MMR over the post-dedupe candidates,
  selecting `top_k`: `λ·rel(d) − (1−λ)·max sim(d, selected)`, where `rel` is the final score
  min-max normalised to [0,1] and `sim` is cosine similarity of the candidates' **BM25 term
  vectors** (`cairn.embedding.sparse.encode_document` of the content, computed locally).
  Rationale: works identically for vector, fulltext and hybrid hits, needs no protocol change to
  return dense vectors from the store, and targets the redundancy users see (near-duplicate
  passages). Deterministic tie-breaking. Result `scores["mmr"]` records the MMR objective.
  Removes the `mmr`/`dedupe` "unsupported" degradation notices.
- **Highlights** (`include_highlights`): character spans `{start, end}` in the returned `content`
  for query terms. Terms come from the same jieba search-mode segmentation as the BM25 query
  encoder (`sparse.query_terms`, a small new public helper), matched case-insensitively
  (NFKC-folded), overlapping spans merged, at most 64 spans per hit. Vector-only requests with
  only `query_vector` → no highlights (empty list), no error.
- **Token budget** unchanged.

## 4. Explain (FR-H-08)

`options.explain: true` returns `explain: {plan, stages}` shaped as in the API doc:
`plan` (mode per target, candidate_k, fusion, rerank model, threshold, expand_parent, dedupe,
mmr, targets with weight and index_version, filter summary) and `stages` (`embed`, `dense`,
`sparse`, `fuse`, `filter` (removed_by_threshold), `rerank`, `expand`, `dedupe`, `mmr`,
`budget`), each with `latency_ms`, counts, and `top` ≤ 10 entries (`chunk_id`, `score`, `rank`,
120-char `preview`, `matched_terms` for sparse) and `rank_changes` ≤ 20. Multi-target stages
carry `knowledge_base_id`. Collected by a small `ExplainRecorder` threaded through the service;
when explain is off the recorder is a no-op so the hot path pays nothing. `usage.latency_ms` gains
the per-stage totals always (cheap).

Explain is rate-limited separately: `rl:explain:{principal}` at `CAIRN_RETRIEVAL__EXPLAIN_RPM`
(default 30/min) in addition to the normal limit; 429 when exceeded.

## 5. `POST /v1/retrieval/batch` (FR-H-14)

`{"queries": [RetrievalRequest, …]}` (1–20). Queries run concurrently (bounded to 4 at a time);
the embedding service cache and per-model grouping already de-duplicate identical encodings.
Authorization and request validation are checked for **all** queries before any runs: a 400/403/
422 in any query fails the whole batch (the caller sent a malformed batch). A runtime failure of
one query (every target unavailable, strict failure, timeout) does not fail the batch: that
entry is a response with `results: []` and one `partial_failures` entry per target carrying the
error code. Response `{request_id, responses, usage: {embedding_tokens, latency_ms.total}}`.
Explain inside batch is rejected (422) — use the single query endpoint.

## 6. Citation endpoints (API doc §4)

`GET /v1/chunks/{chunk_id}` and `GET /v1/documents/{document_id}/content`, both `kb:read` on the
owning KB (resolved from the row, 404 for foreign workspaces and for callers without `kb:read`
so existence is not leaked). Chunk: `{chunk_id, document_id, knowledge_base_id, content,
token_count, ordinal, parent_id, metadata, index_version}` from the ACTIVE version. Document
content: the parsed text (`parsed_object_key` artifact → plain text of blocks joined by blank
lines), `text/plain; charset=utf-8`, bounded to 20 MB. These read PostgreSQL and object storage,
so they are mounted with the catalog (control/`all` roles), not the data-only role —
**deviation from the API doc's placement, recorded there**: they are not on the retrieval hot path
and the data-only role deliberately has no catalog/object-store dependency (ADR-0002).

## 7. Documentation corrections (doc/code design drift found by the audit)

Code is shipped and the web client depends on it, so the docs are corrected to the code:
nested document/chunk paths under `/v1/knowledge-bases/{kb_id}/…`; multipart upload at
`…/documents/upload`; model test response `{healthy, dimensions, tokens, sparse_terms}`; pgvector
references in architecture/plan/quickstart replaced by Qdrant; stale "Phase 0/Phase 2 only"
docstrings in `apps/api/main.py`, `core/health.py`, `modelgw/__init__.py`.

## 8. Web

Search console: filter editor (metadata JSON, document picker, date range), dedupe/MMR/highlight
toggles, highlighted snippets, an **Explain** panel (plan + per-stage table with rank changes) and
**Copy as curl** (TC-M16-09/10). zh-CN/en-US strings.

## 9. Verification

Unit: filter translation (prefixing, logical ops, bounds, time), dedupe, MMR diversity (a
near-duplicate pair is split by MMR, TC-M09-19), highlight spans (CJK + ASCII, merging), explain
stage completeness (TC-M09-15), schema-1 refusal, batch semantics, explain rate limit.
Contract: filters on `meta.*` and `created_at` agree between fake and Qdrant drivers.
Integration (PostgreSQL + Redis + Qdrant): upload with metadata → filtered hybrid query; batch;
citation endpoints with kb:read vs kb:query-only keys; a pre-existing schema-1 KB refuses metadata
filters and accepts them after rebuild. Web: unit tests for console pieces, vue-tsc, i18n,
api:check, build. Full backend suite green before commit.

## Out of scope here

Discovery on the data-only role (FR-I-12; the control/`all` role already serves it — separate
increment), Dify endpoint, load shedding, performance SLO measurement, S3 driver.

## Review resolutions (independent review 2026-09-28: APPROVE WITH CHANGES — all adopted)

1. **Rolling upgrades.** The index stage asserts `meta` and `created_at` on every point when the
   run's `payload_schema >= 2` (fail the task, never write a schema-2 point without them). Ops
   note: upgrade workers before control replicas.
2. **Stable revision time.** New column `document.revised_at` (set at registration and whenever
   the revision increments; migration backfills `created_at`). `doc_updated_at` = `revised_at`.
3. **Bounded document metadata.** At registration: JSON ≤ 8 KB, depth ≤ 4, ≤ 64 keys per object.
   Indexing a legacy document whose metadata exceeds this fails with `DOCUMENT_METADATA_TOO_LARGE`
   (no silent truncation).
4. System keys (`meta`, `created_at`, `doc_*`) are applied in the final `payload.update` so chunk
   metadata cannot override them.
5. Qdrant: integer payload index on `created_at` and keyword index on `document_id` when a
   namespace is created.
6. Filters: size/depth bounds on the raw JSON before `parse_filter`; prefix applied to the parsed
   tree; path segments must be non-empty and free of `[`, `]`, `"`; translatability checked up
   front (ordered comparison on strings, list/dict equality) → 422 `FILTER_UNSUPPORTED`. The API
   doc states that `filters.metadata` addresses document metadata only.
7. Parent results carry the child's `meta`-derived metadata and `source`.
8. Stage order is a **recorded deviation** from M09 §2: threshold → rerank → expand → dedupe → MMR
   → top_k → budget (dedupe needs expanded parents). M09 diagram updated.
9. **After rerank the final `score` is the rerank score** (`scores.fused` keeps the fused one);
   MMR runs only over the reranked prefix when rerank ran.
10. MMR pool ≤ max(3·top_k, 50) candidates, similarity computed on the first 4 000 characters of
    each candidate, run in a worker thread; BM25 cosine kept (dense vectors are not comparable
    across federated targets with different models, and would need a protocol change).
11. Conflicting per-KB `dedupe`/`mmr` defaults across targets without an explicit request option
    → 422 asking for an explicit option (same rule as rerank models).
12. Highlights: spans are Python code-point offsets into the returned `content`; term matching is
    done on a case-folded copy with a per-character offset map back to the original text.
13. `by_document` keeps the first hit per document in current ranking order.
14. Explain: `matched_terms` only for BM25 targets (omitted for learned sparse); the explain limit
    is checked after body validation.
15. Batch: a planning phase loads runtimes and resolves every query's plan (all 400/403/422s)
    before execution; each query counts against the rate limit; overall batch deadline =
    `request_timeout_s × 2`; identical (model, text) encodings across the batch are computed once
    (TC-M09-21 amended from "one embedding batch" to "each distinct encoding once").
16. Citation endpoints: migration adds an index on `chunk(id)`; document content over 20 MB → 413.
