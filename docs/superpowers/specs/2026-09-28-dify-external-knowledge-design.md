# Dify External Knowledge API (`POST /retrieval`, T-M09-19, NFR-C-01) — Design

**Date:** 2026-09-28 · **Status:** Draft for independent review · **Contract:**
`docs/03-api/02-data-plane-api.md` §5; nginx already routes `/retrieval` to `api-data`;
`/v1/meta` reports `dify_compat: false`.

## Request / response (Dify's contract)

```
POST /retrieval   Authorization: Bearer cairn_sk_…
{ "knowledge_id": "kb_…", "query": "…",
  "retrieval_setting": {"top_k": 5, "score_threshold": 0.5},
  "metadata_condition": {"logical_operator": "and", "conditions": [...]}   // optional (Dify ≥ 1.1)
}
→ 200 { "records": [ {"content", "score", "title", "metadata": {path, page, document_id,
                                                               chunk_id, knowledge_base_id, …}} ] }
```

Errors on this path only: `{"error_code": int, "error_msg": str}` — `1001` malformed
`Authorization` header (403), `1002` authorization failed: missing/invalid key, IP not allowed, no
`kb:query` on the KB (403), `2001` knowledge does not exist or is not ready (404). Anything else
keeps its HTTP status and uses it as `error_code` (400 validation, 429 rate limit with
`Retry-After`, 502/503 upstream). Rendered by the route and by the two global error renderers
(`ErrorHandlingMiddleware`, the app exception handlers) when `request.url.path == "/retrieval"`.

## Mapping

- One target, the KB's configured retrieval defaults (mode, fusion, rerank, parent expansion,
  dedupe, MMR), `top_k` from the request (1–100), `strict: true`.
- `metadata_condition` → the document-metadata filter: `is`/`=` → `$eq`, `is not`/`≠` → `$ne`,
  `>`/`<`/`≥`/`≤` → range, `empty` → `$exists: false`, `not empty` → `$exists: true`,
  `contains` on a list field → `$eq` (containment) — other operators (`start with`,
  `end with`, substring `contains`/`not contains`, `before`/`after` on strings) → `400` naming the
  operator. `name` is Dify's list of field names; each becomes one condition (`or` across names).
- `title` = `source.title`; `metadata.path` = heading path joined with " > "; the rest of the
  hit's metadata is passed through.

## Score scale — the one real decision

Dify treats `score` as relevance in `[0, 1]` and filters with `score_threshold` (often 0.5).
Cairn's final scores are on the scale of their last stage: RRF (≈0.01–0.03), weighted fusion
(0–1), cosine (−1–1), BM25 (unbounded), reranker (model-specific). Passing them through would make
a default hybrid KB return nothing to Dify.

Proposal: this endpoint reports and thresholds a **normalised relevance**:
- reranked → the reranker score through a logistic if outside `[0, 1]`, else as is;
- weighted fusion → as is (already in `[0, 1]`);
- RRF → score ÷ the best score possible for that request (`Σ weights / (k + 1)`), i.e. 1.0 means
  "ranked first by every retriever that ran"; rank-based, documented as such;
- vector only → cosine clamped to `[0, 1]` (dot/L2 → logistic);
- full text only → BM25 ÷ the top BM25 score of the result (relative; documented).
Threshold applied after normalisation; `metadata.cairn_score` carries the native score. The HTTP
API is unchanged.

## Verification

Unit: request/response mapping, every error code path (header formats, invalid/foreign KB, no
grant, rate limit shape), `metadata_condition` mapping and refusals, normalisation per stage;
integration over HTTP with a real indexed KB (default hybrid KB + Dify threshold 0.5 returns the
relevant passage); `/v1/meta` `dify_compat: true` on data/all roles.

## Review resolutions (independent review 2026-09-29: APPROVE WITH CHANGES; §Score rejected and replaced — all adopted)

Checked against Dify `main` (`external_knowledge_service.py`, `metadata_entities.py`,
`dataset_retrieval.py`, `external-knowledge-api.mdx`).

1. **Score = what Dify reports for its own knowledge.** If a rerank ran: the rerank score clamped to
   `[0, 1]` (TEI rerank is called with `raw_scores=false`, already `[0, 1]`). Otherwise the dense
   **cosine similarity** clamped to `[0, 1]` (dot = cosine for normalised embeddings; L2 → `1 − d²/2`
   from Cairn's stored `−d`). Hits without a dense score (keyword-only hits in hybrid, full-text-only
   KBs) get one from a second dense query restricted to their IDs (`VectorQuery.ids` → Qdrant
   `HasIdCondition`; fake: id filter). Cairn's ranking order is kept; Dify's `score_threshold` is
   applied to this score **after** the pipeline and is never passed into the request's own
   `score_threshold` (which would replace the KB's configured threshold). `metadata.cairn_score`
   carries the native score.
2. **Registration probe.** Dify registers an endpoint by sending `knowledge_id: ""`, `query: ""`
   with the key: a bad key must be `403`, a good key `400` (anything but 403/404/502 passes). The key
   is checked in a route dependency (runs before body validation); an empty/malformed
   `knowledge_id` → `400`.
3. **Lenient request model** (Dify adds fields in minor releases): extra fields ignored; `null`
   accepted for `metadata_condition`, `logical_operator`, `conditions`, `score_threshold`; `top_k`
   capped at 100 rather than rejected.
4. **`metadata_condition`:** `conditions[].name` is a string. `is`/`=` → `$eq`; `is not`/`≠` →
   `$ne` AND `$exists: true` (Dify excludes documents without the field); `>`/`<`/`≥`/`≤` → range;
   `before`/`after` → `$lt`/`$gt` (numeric timestamps); `empty` → `$exists: false`; `not empty` →
   `$exists: true`; `in`/`not in` → `$in`/`$nin` (list or comma-separated string); `contains`,
   `not contains`, `start with`, `end with` → `400` naming the operator (substring matches, not
   expressible over Qdrant payloads). Conditions with a `null` value are skipped. At most 64
   conditions.
5. **Errors:** a custom `APIRoute` class for this route renders `RequestValidationError`,
   `CairnError`, `HTTPException` and unexpected exceptions as `{"error_code", "error_msg"}`;
   `ErrorHandlingMiddleware` uses the same renderer for errors raised in middleware on this path
   (IP allow-list, rate limit, deadline). `application/json`; `Retry-After`, `X-Request-Id` kept.
   Every 401 → `1002`/403 (Dify treats only 403 as an authorization failure).
6. **Security/order:** the route builds a one-target `RetrievalRequest` and calls the service, so
   `kb:query` is checked before any lookup: no grant → `1002` whether or not the KB exists. A KB the
   key holds a grant on but that has no published runtime (missing or not ready) → `2001`/404 via a
   new `KbRuntimeNotPublished(KbRuntimeUnavailable)`; a Redis failure stays `5xx`.
7. **Missing pieces:** `RetrievalDeadlineMiddleware` covers `/retrieval`; nginx `limit_req` on
   `location = /retrieval`; degradation made visible (`X-Cairn-Degraded` header,
   `metadata.cairn_degraded`, metric); OpenAPI documents the error shape and the web client types
   are regenerated; `/v1/meta` `dify_compat: true` where served. Tests: registration probe, name as
   string, in/not in, nulls, L2/dot scores, keyword-only hits, refusals.
