# ADR-0009 — Qdrant as the only vector backend

**Status:** Accepted · **Date:** 2026-09-24 · **Deciders:** Product, Architecture
**Amends:** [ADR-0004](ADR-0004-vector-store-abstraction.md) ("ship pgvector and Qdrant")

## Context

ADR-0004 planned pgvector as the default and Qdrant for scale. The product owner has
decided that every deployment runs one vector backend, Qdrant. No deployment — Cairn or
the company fork — holds vector data that must survive the switch, so no migration tool
is needed; indexes are rebuildable from chunks (ADR-0007).

Two things pgvector provided must be replaced:

- **Lexical retrieval.** The hybrid search's second stage was PostgreSQL full-text with the
  `simple` configuration, which cannot segment Chinese.
- **Exact filter semantics** from `vectorstore.filters`, which Qdrant's filter language
  expresses almost, but not entirely.

## Decision

1. `VectorStore` keeps its protocol; the only driver is `cairn.vectorstore.qdrant`.
   Connection details are deployment settings (`CAIRN_QDRANT__URL`,
   `CAIRN_QDRANT__API_KEY`); vector bindings carry no config.
2. **One collection per namespace** (`cairn_vec_{kb}_v{n}`): a named dense vector
   `dense` and a named sparse vector `sparse` with the `idf` modifier. A shared collection
   was rejected because Qdrant computes IDF across the whole collection, which would rank
   one knowledge base's terms by every other knowledge base's vocabulary.
3. **BM25 sparse vectors** (`cairn.embedding.sparse`, `bm25-jieba-v1`): jieba search-mode
   segmentation for CJK, word and code-token splitting for everything else, 32-bit hashed
   indices, BM25 term-frequency saturation for documents and unit weights for queries;
   Qdrant supplies IDF. `VectorQuery.text` is removed — full-text is a sparse query.
4. **Filters:** `$exists` is exact through a hidden `_paths` keyword field written on every
   point and stripped on read; numeric equality is a closed range so `3 == 3.0`; ordered
   comparisons with a non-numeric operand raise `UnsupportedFilter` instead of diverging.
5. PostgreSQL returns to the official image, pinned to `postgres:16-bookworm`: the pgvector image
   was built on Debian bookworm (glibc 2.36), and `postgres:16` now tracks trixie (glibc 2.41),
   whose collation changes would silently reorder existing text indexes on an in-place upgrade.
   Migration `0013_qdrant` drops the extension and any per-namespace tables and repoints bindings.

## Consequences

**Positive**
- Native sparse retrieval with CJK segmentation, in the same system as dense retrieval.
- One vector implementation to operate, test and tune.
- The route to learned sparse vectors (bge-m3) and quantization is native.

**Negative**
- Qdrant is a required service in every deployment, including the smallest.
- Collection-per-namespace caps a node at a few hundred live index versions.
- One recorded filter divergence: an ordered comparison against an array of numbers
  matches when any element does.
- BM25 is lexical, not learned. Learned sparse vectors and hosted embedding vendors are
  follow-on work.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Keep pgvector as the default, Qdrant optional | Contradicts the product decision; two drivers to keep identical |
| Shared Qdrant collection with a tenant index | Collection-wide IDF pollutes BM25 ranking across knowledge bases |
| Qdrant full-text payload index for lexical search | Filters only; produces no relevance score to rank or fuse |
