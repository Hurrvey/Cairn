# PDF/OCR and extended retrieval

PDF and PNG/JPEG/single-page TIFF uploads follow the normal document lifecycle.
The runtime image includes Tesseract English/simplified/traditional Chinese packs.
Use the document progress/error display for failed or encrypted files; retry is
appropriate after correcting a runtime failure, not for invalid source bytes.
Text PDF extraction does not require OCR. Images and image-only PDF pages do.

Environment settings (restart parse workers after changes):

```dotenv
CAIRN_INGESTION__OCR_ENABLED=true
CAIRN_INGESTION__OCR_LANGUAGES=eng+chi_sim+chi_tra
CAIRN_INGESTION__OCR_TIMEOUT_S=45
CAIRN_INGESTION__OCR_MAX_CONCURRENCY=1
```

The company fork uses `DATA_PLATFORM_` instead of `CAIRN_`. Multi-page TIFF is
explicitly rejected to avoid silently dropping pages; convert it to PDF first.
Complex/merged tables remain a documented quality limitation. See the September21
quality report, which distinguishes successful extraction from measured accuracy.

## Reranking endpoint configuration

Reranking uses a separate TEI or Infinity server hosting a cross-encoder model.
The bundled MiniLM embedding server cannot rerank. Endpoint URLs and credentials
are operator configuration, never supplied by retrieval clients. Each configured
endpoint requires an explicit workspace UUID allowlist.

```dotenv
CAIRN_RETRIEVAL__RERANK_ENDPOINTS={"company-reranker":{"base_url":"http://reranker:80","dialect":"tei","workspace_ids":["00000000-0000-0000-0000-000000000001"],"allow_private":true,"max_candidates":100}}
```

Replace the illustrative workspace UUID with the actual owning workspace; do not
reuse it verbatim. For Infinity, set `dialect` to `infinity` and `model_name` to the
deployed model key. The optional `api_key` remains in the private environment file.
Configure API control/data and MCP processes consistently. Private addresses are
opt-in for trusted operator-owned model services. Requests use pinned-DNS SSRF
checks, no redirects, a response cap, and a per-stage deadline.

Request selection uses the configured name, for example:

```json
{"rerank":{"enabled":true,"model_id":"company-reranker","top_n":5,"timeout_s":1.5}}
```

An unavailable optional stage retains base retrieval with an explicit degradation
notice. Workspace authorization failures remain permission failures. Tests of the
adapter protocol do not imply that an external cross-encoder has been deployed
or that retrieval-quality improvements have been measured.

## Weighted fusion and parent context

RRF remains the default. To choose weighted fusion, provide:

```json
{"search_mode":"hybrid","fusion":{"method":"weighted"},"weights":{"dense":0.7,"sparse":0.3}}
```

Each nonempty score list is min-max normalized separately; equal-score lists
receive normalized score 1. Component weights and the per-knowledge-base target
weight multiply the contribution. This normalization is candidate-set dependent;
scores are not universal relevance probabilities. Both component weights cannot
be zero, and explicit component weights cannot be combined with RRF.

`options.expand_parent=true` returns the broader context for matching child
chunks and groups sibling hits with their `matched_child_ids`. Parent context
is stored as a version/revision-scoped snapshot in indexed child payloads, so
the data plane requires no catalog database lookup. New ingestion/rebuilds create
the snapshots; old indexes lacking them return children with a degradation notice.
After a manual chunk edit, parent expansion is conservatively disabled for that
active knowledge base to avoid reintroducing stale text through unedited siblings.
The post-expansion token budget counts the actual returned parent content.

Primary protocol references:
- https://huggingface.co/docs/text-embeddings-inference/quick_tour
- https://github.com/michaelfeil/infinity/blob/main/docs/assets/openapi.json

## Filters, explain and batch (2026-09-28)

Queries can filter by document metadata, document IDs and the date a document was added; they can
also deduplicate by document, diversify with MMR, highlight query terms and explain the ranking.
`POST /v1/retrieval/batch` runs up to 20 queries. The contract is in
[the data plane API](../03-api/02-data-plane-api.md).

Upgrading an existing deployment:

1. Upgrade **workers before control-plane replicas** (Compose recreates both together, which is
   fine). Migration `0015_read_model_documents` adds `document.revised_at` and an index on
   `chunk(id)`.
2. Existing index versions do not carry document metadata. Metadata and date filters against them
   answer `400 FILTER_REQUIRES_REBUILD` with the knowledge base IDs; rebuild those knowledge bases
   (Settings → Rebuild) to enable them. Unfiltered search and `document_ids` filters keep working
   without a rebuild.
3. Metadata is attached when a document is added: the `metadata` part of the multipart upload (the
   web upload area has a "Document metadata" field) or `metadata` when registering stored objects.
   To change a document's metadata, upload it again as a new document.

Settings: `CAIRN_RETRIEVAL__EXPLAIN_RPM` (default 30) is the per-key budget for `explain` queries,
separate from the key's normal rate limit. The citation reads `GET /v1/chunks/{id}` and
`GET /v1/documents/{id}/content` need `kb:read` and are served by the control-plane role.

Load shedding (M09 §4.10): each data-plane process tracks the p95 latency of its recent queries.
Above 80 % of `CAIRN_RETRIEVAL__SLO_P95_MS` (default 150; `0` turns shedding off) reranking is
skipped, above 150 % the candidate pool is also limited to 50 per search; both are reported in
`degraded` with reason `load_shed`. Metrics: `cairn_retrieval_stage_seconds{stage}` and
`cairn_retrieval_load_shed_total{level}`. The MCP service applies the same limits and timeouts as
`/v1/retrieval/query`.

Ingestion retries a transient vector-store failure (Qdrant unreachable, HTTP 5xx or 429) with
backoff instead of failing the document; a rejected request (other 4xx) or a misconfigured binding
still fails at once.
