# Embedding providers, keyword search and the bge-m3 sidecar

Status: introduced 2026-09-27 ([ADR-0010](../01-architecture/05-adr/ADR-0010-learned-sparse-and-online-providers.md)).

## Provider types

| Type (family) | Where it runs | API key | Keyword weights | Notes |
| --- | --- | --- | --- | --- |
| TEI | local (`http://embedding:80`) | – | – | The bundled MiniLM (`--profile local-model`) |
| Infinity | local | – | – | Any Infinity server |
| bge-m3 | local (`http://bge-m3:8000`) | optional | ✓ | The bundled sidecar (`--profile bge-m3`) |
| Alibaba Cloud Model Studio (`dashscope`) | hosted | required | ✓ (v3/v4) | `https://dashscope.aliyuncs.com/api/v1`; 10 texts per request |
| Volcengine Ark (`volcengine`) | hosted | required | – | `https://ark.cn-beijing.volces.com/api/v3`; model name or endpoint ID (`ep-…`) |
| OpenAI-compatible | hosted or local | optional | – | OpenAI, Zhipu, SiliconFlow, vLLM, Ollama… |

Add providers under **Models → Add provider**. Hosted types need an API key; it is stored
encrypted with `CAIRN_MASTER_KEY`, never shown again, and can be replaced from the provider's
menu. Keep the master key backed up with the database: without it stored keys cannot be read and
must be entered again.

Register a model with a preset or by hand. Leave **Vector dimensions** empty to have the server
ask the provider once and record the width. Tick **Returns keyword weights** for bge-m3 and
DashScope v3/v4; **Test** then reports the number of keyword terms returned. Hosted tokenizers are
not published; pick the `bge-m3` tokenizer, which is used as a token-budget estimate.

## Keyword search per knowledge base

Full-text and hybrid search use sparse vectors. Each knowledge base chooses their source when it
is created (**Keyword search**):

- **Automatic** (default): the embedding model's own keyword weights; otherwise an enabled model
  that returns them, preferring the local bge-m3 service over paid vendors; otherwise BM25.
- **BM25** (built in, Chinese via jieba) — no model calls.
- A specific model with keyword weights.

The choice is part of each index version. To change it, open the knowledge base's **Settings →
Keyword search**, choose a source and rebuild; search keeps using the current version until the
new one is active. When a model provides both dense vectors and keyword weights, one call
produces both.

## bge-m3 sidecar (optional, on demand)

The sidecar is **off by default**. The normal start command does not run it, and nothing needs it
unless a knowledge base uses bge-m3: without it, keyword search uses built-in BM25 and dense
vectors come from the provider you registered (for example the bundled TEI MiniLM).

```text
docker compose --profile local-model up -d          # normal start: no bge-m3
```

Start it only when you want bge-m3 dense vectors or learned keyword weights:

1. Once: download the weights (2.3 GB, SHA-256 verified).

   ```text
   uv run python scripts/prepare_embedding_model.py --model bge-m3
   ```

2. Start it alongside the running stack (builds the 2.4 GB image the first time; startup takes
   one to three minutes while the weights load):

   ```text
   docker compose --profile local-model --profile bge-m3 up -d --build bge-m3
   docker compose ps bge-m3                             # wait for "healthy"
   ```

3. Once: under **Models**, add a provider of type **bge-m3** (default URL `http://bge-m3:8000`,
   private networking on), register the **bge-m3** preset and press **Test**.

4. Create the knowledge base with **Keyword search → Automatic** (it picks bge-m3) or choose
   bge-m3 explicitly; for an existing knowledge base change the source and rebuild.

To stop it again: `docker compose stop bge-m3`. Knowledge bases that use it then cannot index:
their embedding tasks retry and finally fail (retry them from the document page after starting
the sidecar again), and their hybrid search falls back to vector search with a notice. Before
stopping it for good, switch those knowledge bases to BM25 (Settings → Keyword search, rebuild),
then press **Test** on the bge-m3 model while the sidecar is stopped: it is marked unavailable and
**Automatic** stops choosing it for new knowledge bases (or delete the model once nothing uses it).
Leaving it stopped frees about 3 GB of memory.

The CPU image is about 2.4 GB; the service keeps the model
in memory (about 3 GB resident, limit 6 GB) — the weights are copied out of the read-only mount
at startup, because reading memory-mapped weights through a Docker Desktop bind mount made every
query ~25 times slower.

Measured on this project's 8-thread CPU: about 0.2 s per query and about 0.4 chunks per second
for 500-token Chinese/English chunks. Queries fit the default two-second query deadline; bulk
ingestion of a large corpus should use a GPU. If query encoding competes with ingestion on a busy
CPU, raise `CAIRN_EMBEDDING__QUERY_TIMEOUT_S` (and `CAIRN_RETRIEVAL__REQUEST_TIMEOUT_S`, at most 30)
rather than letting hybrid search fall back to vector search.

Give Docker enough memory for everything you run: one full stack plus the sidecar needs roughly
10 GB; the kernel stops the largest process when the VM runs out.

NVIDIA GPU (CUDA torch, fp16):

```text
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile bge-m3 up -d --build bge-m3
```

Settings: `CAIRN_BGE_M3_DIR` (weights, default `./data/models/bge-m3`), `BGE_M3_DEVICE`
(`auto`/`cpu`/`cuda`), `BGE_M3_API_KEY` (optional bearer key; set the same key on the provider).
Health: `docker compose ps bge-m3`.

## Verification

- Unit: `uv run python -m pytest tests/unit` (wire formats, encryption, hybrid caching).
- Live sidecar: set `CAIRN_TEST_BGE_M3_URL` and `CAIRN_TEST_BGE_M3_TOKENIZER`, run
  `tests/integration/test_bge_m3_live.py`.
- Stack: `CAIRN_ACCEPTANCE_PASSWORD=… uv run python scripts/sparse_acceptance.py` against a
  disposable stack with the sidecar running.
