# Data Plane API — the Retrieval Contract

**Document:** `03-api/02-data-plane-api.md`
**Status:** Normative — **this is the product surface**
**Date:** 2026-08-28
**Owned by:** [M09 Retrieval](../02-modules/M09-retrieval.md)

Everything an external Agent needs: a service URL, an API key, and a JSON payload.

---

## 1. `POST /v1/retrieval/query`

The primary endpoint. `kb:query` required on **every** target.

### Request

```jsonc
{
  // ── WHAT to search ────────────────────────────────────────────────────────
  "targets": [                                   // 1–10 knowledge bases  (FR-H-04)
    { "knowledge_base_id": "kb_01HQZX3N9K2M5P7R8T", "weight": 1.0 },
    { "knowledge_base_id": "kb_02KMV8B4C6D9F1G3H5", "weight": 0.6 }
  ],

  // ── WHAT to look for ──────────────────────────────────────────────────────
  "query": "How do I rotate the signing key?",   // 1–8192 chars
  "query_vector": null,                          // supply your own embedding  (FR-H-13)

  // ── HOW to search ─────────────────────────────────────────────────────────
  "top_k": 5,                                    // 1–100,  default from KB config
  "candidate_k": 100,                            // pre-rerank pool, 1–1000
  "search_mode": "hybrid",                       // vector | fulltext | hybrid
  "fusion": { "method": "rrf", "k": 60 },        // rrf | weighted
  "weights": { "dense": 0.7, "sparse": 0.3 },    // weighted fusion only
  "score_threshold": 0.35,
  "rerank": {
    "enabled": true,
    "model": "bge-reranker-v2-m3",               // omit to use the KB default
    "top_n": 5
  },

  // ── WHAT to exclude ───────────────────────────────────────────────────────
  "filters": {
    "metadata": {                                 // FR-H-05
      "lang": "en",
      "tags":       { "$in": ["security", "ops"] },
      "version":    { "$gte": 3 },
      "deprecated": { "$ne": true }
    },
    "document_ids": null,                         // FR-H-06
    "created_after":  "2025-01-01T00:00:00Z",
    "created_before": null
  },

  // ── WHAT to return ────────────────────────────────────────────────────────
  "options": {
    "expand_parent": true,                        // return parent windows   (FR-H-10)
    "include_highlights": true,
    "include_metadata": true,
    "max_context_tokens": 4000,                   // server-side trimming    (FR-H-09)
    "dedupe": "by_document",                      // none | by_chunk | by_document
    "mmr": { "enabled": false, "lambda": 0.5 },   // FR-H-12
    "explain": false                              // FR-H-08
  },

  "strict": false                                 // fail if any target fails (FR-H-17)
}
```

**Only `targets` and one of `query` / `query_vector` are required.** Everything else falls back
to the KB's configured defaults. The minimal request is:

```json
{ "targets": [{"knowledge_base_id": "kb_01HQZX3N9K2M5P7R8T"}],
  "query": "How do I rotate the signing key?" }
```

### Response — `200 OK`

```jsonc
{
  "request_id": "req_01HQZX3N9K2M5P7R8T",
  "results": [
    {
      "chunk_id": "chk_01HQZX3N9K2M5P7R8T",
      "document_id": "doc_01HQZX3N9K2M5P7R8T",
      "knowledge_base_id": "kb_01HQZX3N9K2M5P7R8T",

      "content": "Key rotation is performed via the `cairn-admin rotate-key` command…",
      "token_count": 418,

      "score": 0.873,                            // final ranking score
      "scores": {                                // per-stage, for debugging
        "dense": 0.81, "sparse": 12.4, "fused": 0.0323, "rerank": 0.873
      },

      "highlights": [ { "start": 0, "end": 12 } ],
      "matched_child_ids": ["chk_01HQ…"],        // present when expand_parent applied

      "metadata": {
        "heading_path": ["Security", "Key Management"],
        "page": 14,
        "lang": "en",
        "tags": ["security"]
      },
      "source": {
        "title": "Ops Handbook 2026",
        "url": "https://docs.internal/ops-handbook#key-management",
        "type": "pdf",
        "updated_at": "2026-06-02T09:11:00Z"
      }
    }
  ],

  "usage": {
    "embedding_tokens": 11,
    "rerank_units": 100,
    "cached_embedding": true,
    "index_versions": { "kb_01HQZX3N9K2M5P7R8T": 3 },
    "latency_ms": { "auth": 2, "embed": 9, "search": 21,
                    "fuse": 1, "rerank": 47, "assemble": 3, "total": 94 }
  },

  "degraded": [],                                 // ALWAYS present  (FR-H-16)
  "partial_failures": [],                         // FR-H-17
  "truncated_to_token_budget": false,             // FR-H-09
  "explain": null                                 // populated when options.explain
}
```

### Degradation — never silent (`FR-H-16`)

```jsonc
{
  "results": [ /* fused but not reranked */ ],
  "degraded": [
    { "stage": "rerank", "reason": "timeout",
      "detail": "Reranking exceeded 1500 ms; results are ranked by fusion score." }
  ]
}
```

```jsonc
{
  "results": [ /* from the two KBs that succeeded */ ],
  "partial_failures": [
    { "knowledge_base_id": "kb_02KMV8B4C6D9F1G3H5",
      "code": "STORAGE_BINDING_UNAVAILABLE",
      "detail": "The vector store for this knowledge base is unreachable." }
  ]
}
```

With `"strict": true` the second case is a `502` instead.

> **Contract commitment:** any reduction in result quality relative to the requested
> configuration appears in `degraded` or `partial_failures`. An Agent can therefore detect,
> alert, and adapt. Silent degradation is a defect, not a fallback.

### Explain output (`FR-H-08`)

```jsonc
{
  "explain": {
    "plan": {
      "mode": "hybrid", "candidate_k": 100, "ef_search": 100,
      "fusion": "rrf(k=60)", "rerank_model": "bge-reranker-v2-m3",
      "score_threshold": 0.35, "expand_parent": true,
      "targets": [{"kb": "kb_01HQ…", "weight": 1.0, "index_version": 3}]
    },
    "stages": [
      { "stage": "embed",  "latency_ms": 9,  "cache_hit": true, "dim": 1024 },
      { "stage": "dense",  "latency_ms": 21, "returned": 100,
        "top": [{"chunk_id":"chk_9f…","score":0.81,"rank":1,
                 "preview":"Key rotation is performed via…"}] },
      { "stage": "sparse", "latency_ms": 18, "returned": 100,
        "top": [{"chunk_id":"chk_2a…","score":12.4,"rank":1,
                 "matched_terms":["rotate","signing","key"]}] },
      { "stage": "fuse",   "latency_ms": 1, "method": "rrf", "unique": 143,
        "rank_changes":[{"chunk_id":"chk_2a…","dense_rank":7,"sparse_rank":1,"fused_rank":2}] },
      { "stage": "filter", "latency_ms": 0, "removed_by_threshold": 91 },
      { "stage": "rerank", "latency_ms": 47, "scored": 100, "returned": 5,
        "rank_changes":[{"chunk_id":"chk_2a…","before":2,"after":1,"score":0.873}] },
      { "stage": "expand", "latency_ms": 3, "children_collapsed": 8, "parents_returned": 5 },
      { "stage": "budget", "latency_ms": 1, "dropped": 0, "total_tokens": 2087 }
    ]
  }
}
```

RAG failures are otherwise undiagnosable. This one flag answers "was it never indexed, ranked
400th, filtered by threshold, or dropped by the budget?" in a single request.
`explain` costs ~5 ms and is rate-limited more aggressively.

---

## 2. `POST /v1/retrieval/batch` (`FR-H-14`)

```jsonc
{ "queries": [ { /* RetrievalRequest */ }, { /* … */ } ] }   // max 20
```

```jsonc
{ "request_id": "req_…",
  "responses": [ { /* RetrievalResponse */ }, { /* … */ } ],
  "usage": { "embedding_tokens": 34, "latency_ms": { "total": 118 } } }
```

Queries share one embedding batch and run concurrently. Individual failures appear in that
response's `partial_failures`; the batch itself still returns 200.

---

## 3. `GET /v1/knowledge-bases` — discovery (`FR-I-12`)

Lets a key holder find out what it can reach, which is what makes MCP tool descriptions and
self-configuring clients possible.

```jsonc
{
  "items": [
    { "id": "kb_01HQZX3N9K2M5P7R8T", "name": "Ops Handbook",
      "description": "Internal operations documentation and runbooks.",
      "document_count": 412, "chunk_count": 128_440,
      "last_indexed_at": "2026-08-27T02:14:00Z",
      "embedding_model": "bge-m3", "permissions": ["kb:query"] }
  ],
  "next_cursor": null, "has_more": false
}
```

Visible with `kb:query` **or** `kb:read`. A `kb:query`-only key sees the KB exists and can search
it, but cannot list its documents.

---

## 4. `GET /v1/chunks/{chunk_id}` and `GET /v1/documents/{id}/content`

Citation resolution and expansion after retrieval. Both require `kb:read` — deliberately more
than `kb:query`, so a search-only key cannot walk the corpus chunk by chunk.

---

## 5. `POST /retrieval` — Dify External Knowledge API (`NFR-C-01`)

Implements Dify's contract exactly, so any Dify deployment can use Cairn with a URL and a key and
**no code**.

```jsonc
// request
{ "knowledge_id": "kb_01HQZX3N9K2M5P7R8T",
  "query": "How do I rotate the signing key?",
  "retrieval_setting": { "top_k": 5, "score_threshold": 0.35 } }

// response
{ "records": [
    { "content": "Key rotation is performed via…",
      "score": 0.873,
      "title": "Ops Handbook 2026",
      "metadata": { "path": "Security > Key Management", "page": 14,
                    "document_id": "doc_01HQ…", "chunk_id": "chk_01HQ…" } } ] }
```

Errors follow Dify's `{"error_code": 1001, "error_msg": "…"}` shape on this endpoint only.

---

## 6. Client examples

### curl

```bash
curl -sS -X POST https://cairn.example.com/v1/retrieval/query \
  -H "Authorization: Bearer $CAIRN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": [{"knowledge_base_id": "kb_01HQZX3N9K2M5P7R8T"}],
    "query": "How do I rotate the signing key?",
    "top_k": 5,
    "options": {"max_context_tokens": 4000}
  }'
```

### Python

```python
from cairn import Client

cairn = Client(base_url="https://cairn.example.com", api_key=os.environ["CAIRN_API_KEY"])

result = cairn.retrieval.query(
    targets=["kb_01HQZX3N9K2M5P7R8T"],
    query="How do I rotate the signing key?",
    top_k=5,
    max_context_tokens=4000,
)
for hit in result.results:
    print(f"[{hit.score:.3f}] {hit.source.title} p.{hit.metadata.page}")
    print(hit.content)

if result.degraded:
    logger.warning("retrieval degraded: %s", result.degraded)
```

### TypeScript

```ts
import { CairnClient } from "@cairn/client";

const cairn = new CairnClient({ baseUrl: "https://cairn.example.com",
                              apiKey: process.env.CAIRN_API_KEY! });

const { results, degraded } = await cairn.retrieval.query({
  targets: [{ knowledgeBaseId: "kb_01HQZX3N9K2M5P7R8T" }],
  query: "How do I rotate the signing key?",
  topK: 5,
  options: { maxContextTokens: 4000 },
});
```

---

## 7. Service level objectives

Published as part of the contract (`NFR-P-01`, `NFR-P-02`, `NFR-R-01`):

| Metric | Objective | Conditions |
| --- | --- | --- |
| p50 latency | < 60 ms | hybrid, no rerank, top_k ≤ 10, ≤ 1M vectors |
| p95 latency | < 150 ms | same |
| p99 latency | < 300 ms | same |
| p95 with rerank | < 400 ms | 100 candidates, local reranker |
| Availability | ≥ 99.9% monthly | data plane |

Measured server-side, excluding network transit. Degradation is announced, not silent.

---

## 8. Client guidance

| Do | Don't |
| --- | --- |
| Check `degraded` and log it | Assume every 200 is a full-quality result |
| Set `max_context_tokens` to your model's budget | Reimplement trimming client-side |
| Send `query_vector` if you already embedded | Pay for a second embedding |
| Use `Idempotency-Key` on writes | Retry blindly |
| Respect `RateLimit-Remaining` | Retry into a 429 wall |
| Branch on `code`, not `detail` | Parse human-readable messages |
| Ignore unknown response fields | Break on additive changes |
| Use `explain` while developing | Leave it on in production |
