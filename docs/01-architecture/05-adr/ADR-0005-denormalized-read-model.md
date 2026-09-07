# ADR-0005 — Denormalized read model in the vector store

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture

## Context

Retrieval returns chunk **text**. Where should that text live?

**Option A — normalized.** Vector store holds `chunk_id` + vector only; text is fetched from
Postgres after search.

```
search → [chunk_id × 100] → rerank needs text → SELECT … WHERE id = ANY($1) → assemble
```

**Option B — denormalized.** Vector store payload carries the text; Postgres remains the source
of truth.

```
search → [Hit with text × 100] → rerank → assemble
```

Option A is cleaner and was the initial instinct. It has two problems for an infrastructure
product whose read path is the deliverable:

1. **It puts PostgreSQL on the hot path.** A primary-key `IN` lookup of 100 rows is 2–5 ms —
   acceptable in isolation, but it means every retrieval consumes a database connection. At 200
   RPS that is 200 connections/s of pressure on the same primary handling ingestion writes, and
   it makes retrieval availability depend on control-plane database health, defeating ADR-0002.
2. **Reranking needs the text before ranking.** So the hydration cannot be deferred to the
   final top-5 — it must fetch all 100 candidates.

## Decision

**Option B.** The vector store payload carries a denormalized copy of chunk text and filterable
metadata. PostgreSQL `chunk` remains the source of truth. The read model is written **only** by
the indexer (`M07` index stage).

```jsonc
{
  "id": "<chunk_id>",
  "vectors": { "dense": [...], "sparse": {...} },
  "payload": {
    "kb_id": "...", "document_id": "...", "index_version": 3,
    "parent_id": "...", "ordinal": 17,
    "content": "Key rotation is performed via …",   // ← denormalized
    "token_count": 418, "heading_path": [...], "page": 14,
    "source_url": "...", "created_at": 1735689600,
    "meta": { "lang": "en", "tags": ["security"] }
  }
}
```

## Consequences

**Positive**
- Retrieval is a **single round trip**. Postgres leaves the hot path entirely.
- Completes ADR-0002: the data plane can survive a Postgres primary outage while caches are warm.
- Rerank gets its text for free.
- ~4 ms saved per request and, more importantly, one whole dependency removed from the SLO chain.

**Negative**
- **Storage duplication.** ~1.2 KB per chunk duplicated. At 10M chunks that is ~12 GB.
  Mitigated: Qdrant `on_disk_payload: true` keeps payload off the heap, so RAM impact is
  negligible; pgvector stores it in the same table anyway, so duplication there is nil.
- **Write amplification.** A chunk edit (`FR-F-09`) must update both stores. Mitigated: edits
  always flow through the indexer, which already writes both. Direct writes to the read model
  are forbidden.
- **Consistency window.** Postgres and the read model can diverge if an index task fails
  mid-write. Mitigated: index tasks are idempotent upserts keyed on `chunk_id`; a nightly
  `maintain` reconciliation compares counts per `(kb_id, index_version)` and re-enqueues drift.

## Implementation requirements

1. **Only `M07`'s index stage writes the read model.** No other code path may call
   `VectorStore.upsert`. Enforced by review; violations are a correctness bug, not a style issue.
2. Payload size cap of 32 KB per point. Chunks exceeding it store a truncation marker and the
   full text is fetched from Postgres for those rare cases — a documented, measured fallback.
3. A `maintain` task reconciles `count(chunk)` in Postgres against `VectorStore.count()` per
   namespace nightly, emitting `cairn_readmodel_drift_total` and re-enqueuing repairs.
4. The rebuild path is always available: a namespace can be dropped and rebuilt from Postgres,
   which is exactly the blue/green flow of ADR-0007. **The read model is always disposable.**

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Normalized (Option A) | Puts Postgres on the hot path; breaks data-plane independence |
| Text in Redis | Cache is not a source of truth; eviction produces silent empty results |
| Two-phase: search → rerank on IDs → hydrate top-5 | Reranking requires the text of all candidates |
| Separate read-model store | A third system for something the vector store already does |
