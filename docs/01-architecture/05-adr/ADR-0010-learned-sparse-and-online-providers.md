# ADR-0010 — Learned sparse vectors and online embedding providers

**Status:** Accepted · **Date:** 2026-09-27 · **Deciders:** Product, Architecture
**Builds on:** [ADR-0009](ADR-0009-qdrant-only-vector-backend.md) (Qdrant, BM25 sparse vectors)

## Context

ADR-0009 made keyword search a BM25 sparse query. Learned sparse encoders (bge-m3's lexical
weights, DashScope text-embedding-v3/v4) outperform BM25 on multilingual retrieval, and the
product owner asked for hosted embedding vendors — Alibaba Cloud Model Studio (DashScope),
Volcengine Ark and OpenAI-compatible endpoints — with the instruction to use whatever works
best. Hosted vendors need API keys; until now the provider `secret` table existed but nothing
wrote or read it, and every provider path refused credentials.

## Decision

1. **Provider families** with runtime adapters: `tei`, `infinity`, `bge_m3` (bundled sidecar),
   `dashscope` (native API: dense + sparse, query/document `text_type`), `volcengine` and
   `openai_compatible` (OpenAI `/embeddings`). One factory builds them; hosted families require a
   key and never reach private addresses.
2. **Keys** are envelope-encrypted (`cairn.core.secrets`): HKDF-derived KEK from
   `CAIRN_MASTER_KEY`, per-secret AES-256-GCM DEK, associated data binding
   workspace, purpose and secret id. Write-only in the API. Workers open them through the catalog;
   the data plane receives the **sealed** secret in its Redis projection and opens it with the
   master key — Redis never stores plaintext.
3. **Sparse source per knowledge base** (`SparseSpec`): BM25, or a registered model with
   `sparse=true`. `auto` (default) prefers the embedding model's own sparse output, then an
   enabled sparse model (local bge-m3 before paid vendors), then BM25. The source is snapshotted
   per index version; changing it is a rebuild. Learned weights are stored with the Qdrant sparse
   modifier `none`, BM25 with `idf`; a namespace built one way refuses to be ensured the other.
4. **One call per model**: when dense and sparse come from the same model, one provider call
   returns both (one forward pass on bge-m3, one billed request on DashScope), at ingestion and at
   query time. Sparse vectors are cached (`sps:v1:`) beside dense ones and recorded in the
   embedding manifest for reuse.
5. **bge-m3 sidecar** (`apps/bge_m3`): FlagEmbedding + torch, CPU image by default, CUDA via
   `docker-compose.gpu.yml`; weights pinned by revision and SHA-256.

## Consequences

**Positive**
- Multilingual learned keyword matching (Chinese and English) without an external service.
- Hosted models usable end to end, with credentials never exposed to Redis, logs or responses.
- Hybrid search degrades to vector search, with a notice, when a sparse encoder is down.

**Negative**
- The sidecar image is large (2.4 GB CPU, several GB with CUDA) and CPU inference is slow for
  bulk ingestion; GPUs are recommended for large corpora.
- Hosted tokenizers are unpublished; their token budgets use the bge-m3 tokenizer as an estimate.
- Losing `CAIRN_MASTER_KEY` makes stored provider keys unrecoverable (re-enter them).
- Hosted adapters were verified against the vendors' SDK-confirmed wire formats; live calls need
  real keys (`CAIRN_TEST_DASHSCOPE_API_KEY`, `CAIRN_TEST_ARK_API_KEY`).

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Always BM25 | Measurably weaker multilingual keyword retrieval than learned weights |
| Always local bge-m3 for sparse | Forces every deployment to run a 2 GB+ model service |
| Plaintext keys in the Redis projection | Any Redis read would leak vendor credentials |
| ColBERT multi-vectors from bge-m3 | ~2 MB per 512-token chunk; storage cost out of proportion |
