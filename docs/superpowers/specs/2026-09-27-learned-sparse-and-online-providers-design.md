# Learned Sparse Vectors (bge-m3) and Online Embedding Providers — Design

**Date:** 2026-09-27 · **Status:** Approved direction (user: bge-m3 via FlagEmbedding + torch
sidecar; online vendors such as Alibaba and Volcengine; "choose what works best, effort is fine")
· **Sub-projects:** 2 and 3 of 3 (sub-project 1: [Qdrant](2026-09-23-qdrant-vector-backend-design.md))

Both sub-projects change the same path — provider → model registry → ingestion embed stage →
index → query encoding — so they share one design and are implemented in phases.

## 1. Goals

1. A knowledge base's lexical ("sparse") retrieval uses the best source available:
   the embedding model's own learned sparse output → a separately registered learned-sparse
   model (the local bge-m3 sidecar, or DashScope) → built-in BM25 (sub-project 1).
2. Hosted embedding vendors work end to end: Alibaba Cloud Model Studio / DashScope (dense +
   sparse, query/document asymmetry), Volcengine Ark (dense), and any OpenAI-compatible
   endpoint (OpenAI, Zhipu, SiliconFlow, vLLM, Ollama…).
3. Vendor API keys are stored encrypted, never logged, never returned, and never placed in
   Redis in plaintext.

## 2. Provider families

| Family | Wire | Dense | Sparse | Key | Default base URL | Default batch |
| --- | --- | --- | --- | --- | --- | --- |
| `tei` | TEI `POST /embed` | ✓ | – | optional | required | 16 |
| `infinity` | `POST /embeddings` | ✓ | – | optional | required | 16 |
| `bge_m3` | sidecar `POST /v1/encode` | ✓ | ✓ | optional | `http://bge-m3:8000` | 16 |
| `dashscope` | native `POST /services/embeddings/text-embedding/text-embedding` | ✓ | ✓ (v3/v4) | **required** | `https://dashscope.aliyuncs.com/api/v1` | 10 |
| `volcengine` | OpenAI-compatible `POST /embeddings` | ✓ | – | **required** | `https://ark.cn-beijing.volces.com/api/v3` | 16 |
| `openai_compatible` | `POST /embeddings` | ✓ | – | optional | required | 16 |

Wire details (confirmed from the vendors' official SDK sources, `dashscope` 1.27.7 and
`volcengine-python-sdk` 5.0.50):

- DashScope body `{"model", "input": {"texts": [...]}, "parameters": {"text_type":
  "query"|"document", "output_type": "dense"|"sparse"|"dense&sparse", "dimension"?}}`; response
  `output.embeddings[] = {text_index, embedding, sparse_embedding: [{index, value, token}]}`.
  `dimension`: 64–1024 (v3/v4), 1536/2048 (v4 only).
- Volcengine / OpenAI-compatible body `{"model", "input": [...], "encoding_format": "float",
  "dimensions"?}`; response `data[] = {index, embedding}`.

Hosted families (`dashscope`, `volcengine`) never allow private addresses (SSRF guard); local
families may opt in, as today. `purpose` maps to DashScope `text_type`; other families rely on
the model's `query_prefix` for query/document asymmetry.

## 3. Model registry

New `model` columns (migration `0014_sparse_and_providers`), mirrored on `ModelRef` with
defaults so existing snapshots stay valid:

- `sparse: bool` — the model returns learned sparse vectors. Allowed only for `bge_m3` and
  `dashscope`; the model test verifies non-empty sparse output.
- `send_dimension: bool` — send the registered dimension to the provider (`dimension` for
  DashScope, `dimensions` for OpenAI-compatible/Volcengine). Lets one model serve several widths.

Registration accepts `dimension` omitted: the service probes the provider once and records the
observed width, so hosted models with unfamiliar widths need no guessing.

Hosted vendors do not publish their tokenizers. Their models use a configured tokenizer as a
budget estimate; the bundled `bge-m3` tokenizer (XLM-R, multilingual) is the default suggestion.
Chunk sizes are far below vendor limits (8192 tokens for DashScope v3/v4).

## 4. Credentials (NFR-SEC-02)

`cairn.core.secrets` implements the existing `secret` table's envelope scheme:

- KEK = HKDF-SHA256(`CAIRN_MASTER_KEY`, info `cairn/secret-kek/v1`); a random 256-bit DEK per
  secret; AES-256-GCM for the payload and for wrapping the DEK. AAD binds
  `workspace_id:purpose:secret_id`, so a row lifted into another workspace or provider fails.
- `model_provider.secret_ref` points at the row. Keys are write-only in the API
  (`api_key` on create; `PUT /v1/model-providers/{id}/credentials` to rotate); responses expose
  only `has_credentials`.
- Workers decrypt through the model catalog. The data plane receives the **sealed** secret in
  its Redis provider projection and opens it with the master key it already holds; Redis never
  sees plaintext. Rotation republishes the projection, which changes its fingerprint and
  replaces the data plane's client.

## 5. Sparse source per knowledge base

`cairn.core.sparse.SparseSpec` (credential-free, shared by both planes):

```
kind: "bm25" | "model"
model: ModelRef | None        # set iff kind == "model"; model.sparse must be true
modifier -> "idf" (bm25) | "none" (learned weights)
```

- `knowledge_base` gains `sparse_kind` and `sparse_model_id` (FK to `model`, `RESTRICT`): the
  desired source for the next build. `IndexVersionConfig.sparse` snapshots it per version
  (default BM25, so existing snapshots parse unchanged). `KnowledgeBaseRuntime.sparse` carries it
  to the data plane.
- Choice at creation: `auto` (default), `bm25`, or `model` + `model_id`. `auto` resolves to the
  embedding model when it is sparse-capable; otherwise the best enabled sparse model in the
  workspace, preferring the local `bge_m3` family over paid `dashscope`; otherwise BM25.
- Changing the source is a rebuild (`ReindexRequest.sparse`), exactly like changing the
  embedding model — a new index version with the right Qdrant sparse modifier.
- A model referenced as a sparse source cannot be deleted (FK plus snapshot scan).

## 6. Embedding service and providers

- Providers gain `embed_hybrid(model, texts, *, purpose, dense, sparse)` where supported;
  `build_provider(family, …)` replaces the three duplicated if/else constructions (workers, query
  runtime, probe).
- `EmbeddingService.embed_documents_hybrid` / `embed_query_hybrid`: dense vectors keep their
  cache keys; sparse vectors are cached under `sps:v1:` with the same identity hash. Per text
  only the missing outputs are requested, so when dense and sparse come from one model a single
  provider call returns both (one forward pass on bge-m3, one billed call on DashScope).
- Sparse validation: integer indices in `[0, 2^32)`, finite positive weights, duplicates merged
  by maximum, sorted, at most 16 384 terms.

## 7. Ingestion and query

- Embed stage: dense as before; sparse per the version's `SparseSpec` — BM25 locally, the
  embedding model's own output in the same call, or the separate sparse model. The embedding
  manifest records `sparse_encoder` (identity) and `sparse` vectors; unchanged content reuses
  both from the previous manifest when identities match.
- Index stage writes the manifest's sparse vectors; namespaces are created with the version's
  modifier (`idf` or `none`). Chunk re-embed and deleted-document cleanup use the same spec.
- Query: encodings are grouped by model across targets so one call per model serves both dense
  and sparse; BM25 is computed once locally. In `hybrid`, a failed sparse encoding degrades to
  vector search with a notice; in `fulltext` it is a target failure.

## 8. bge-m3 sidecar (`apps/bge_m3`)

- FastAPI + FlagEmbedding 1.4.2 `BGEM3FlagModel`, weights mounted read-only from
  `data/models/bge-m3` (pinned revision `5617a9f61b028005a4858fdac845db406aefb181`, SHA-256
  verified by `scripts/prepare_embedding_model.py --model bge-m3`).
- `GET /health`; `POST /v1/encode {inputs, dense, sparse}`; TEI-compatible `POST /embed` and
  `POST /embed_sparse`. One inference at a time in a worker thread; bounded batch and input
  size; optional bearer key.
- Image `deploy/docker/Dockerfile.bge-m3`: CPU torch by default (`TORCH_INDEX_URL` build arg);
  `docker-compose.gpu.yml` switches to CUDA torch, fp16 and an NVIDIA device reservation.
  Compose profile `bge-m3`. The bge-m3 tokenizer is prepared for every deployment (it is small)
  so model registration can reference it without the weights.

## 9. Web

- Provider sheet: family presets (base URL, batch size, key field for hosted families, private
  networking only for local ones). Provider menu: replace API key.
- Model sheet: presets per family (bge-m3; DashScope v4/v3; OpenAI text-embedding-3), sparse and
  send-dimension options, dimension may be left empty for auto-detection.
- Knowledge base creation: "Keyword search" choice (Automatic / BM25 / a sparse model), with the
  automatic choice explained; settings show the active source; rebuild can change it.

## 10. Verification

- Unit: secrets (round trip, tamper and cross-workspace failures), every wire format with mock
  transports (including DashScope sparse parsing and error mapping), hybrid service caching and
  call grouping, sparse validation, auto resolution, query grouping and degradation.
- Contract: Qdrant `none` modifier; namespace spec mismatch refused.
- Integration: pipeline + retrieval with a deterministic hybrid provider on PostgreSQL, Redis and
  Qdrant; encrypted key storage and data-plane decryption.
- Live: bge-m3 sidecar container (Chinese/English dense + sparse); Compose stack with the sidecar,
  browser acceptance, and an API acceptance of a bge-m3 knowledge base. Hosted vendors run live
  only when `CAIRN_TEST_DASHSCOPE_API_KEY` / `CAIRN_TEST_ARK_API_KEY` are set — none are available
  in this environment, so their adapters are verified against the SDK-confirmed wire formats.

## Out of scope

ColBERT multi-vectors (≈2 MB per 512-token chunk), rerank models from hosted vendors, key
rotation of the master key itself, quotas/billing.
