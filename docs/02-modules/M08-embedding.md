# M08 — Embedding Service

| | |
| --- | --- |
| **Package** | `cairn.embedding` |
| **Layer** | L2 domain service — importable by the **data plane** |
| **Phase** | 2 |
| **Owner** | Backend eng. C |
| **Depends on** | M00, M10 (facade) |
| **Depended on by** | M03, M07, M09 ★, M11, M14 |
| **Tables owned** | none |
| **Requirements owned** | FR-G-01, FR-G-02, FR-G-03, FR-G-11, FR-F-11, NFR-P-05, NFR-P-07 |

---

## 1. Purpose and scope

Text → vectors, batched and cached, with tokenizer parity.

**In scope:** the embedding interface, batching, the embedding cache, tokenization and token
counting, sparse-vector generation, local server clients (TEI/Infinity), normalization,
dimension guarantees.

**Out of scope:** provider credentials and registration (M10), storing vectors (M05), deciding
what to embed (M07).

M09 imports this module for **query** embedding, so it is on the hot path and held to
data-plane latency standards.

---

## 2. Public interface

```python
class EmbeddingService:
    async def embed_query(self, model: ModelRef, text: str) -> Vector:
        """Hot path. Cache-first. Budget < 10 ms cached, < 90 ms uncached."""
    async def embed_documents(self, model: ModelRef, texts: Sequence[str],
                              *, batch_size: int | None = None) -> list[Vector]:
        """Throughput path. Batches, caches, and preserves input order."""
    async def embed_sparse(self, text: str, *, language: str) -> SparseVector: ...

    def count_tokens(self, model: ModelRef, text: str) -> int:
        """Uses the MODEL'S tokenizer (FR-F-11). Never len(text.split())."""
    def truncate_to_tokens(self, model: ModelRef, text: str, limit: int) -> str: ...

@dataclass(frozen=True)
class Vector:
    values: list[float]
    dim: int
    normalized: bool
```

---

## 3. Behaviour

### 3.1 Query embedding — the hot path

```python
async def embed_query(self, model: ModelRef, text: str) -> Vector:
    normalized = _normalize_text(text)                   # NFKC, collapse whitespace, strip
    cache_key = f"emb:{model.id}:{sha256(normalized).hexdigest()[:32]}"

    if (cached := await self.cache.get(cache_key)) is not None:
        self.metrics.cache_hit()
        return Vector.from_bytes(cached, dim=model.dimension)

    self.metrics.cache_miss()
    async with self._provider_semaphore(model):          # bounded concurrency
        raw = await self.provider.embed(model, [normalized], purpose="query")

    vector = self._postprocess(raw[0], model)
    await self.cache.set(cache_key, vector.to_bytes(), ttl=self.settings.cache_ttl)
    return vector
```

**The cache is worth more than it looks.** Agent traffic is highly repetitive — clients retry,
rephrase minimally, and re-issue the same queries across sessions. Observed hit rates of 30–60%
(`NFR-P-07`) turn a 40–90 ms provider round trip into a 1 ms Redis GET on a large fraction of
requests, and remove a hard availability dependency on the provider for that fraction.

Cache entries are raw `float32` bytes, not JSON: 4 KB instead of ~20 KB for 1024 dims, and no
parse cost.

Some models (including bge-m3 in asymmetric mode) require a query prefix such as
`"Represent this sentence for searching relevant passages: "`. The prefix is part of `ModelRef`
and is applied **before** hashing, so query and document embeddings never collide in the cache.

### 3.2 Document embedding — the throughput path

```python
async def embed_documents(self, model, texts, *, batch_size=None) -> list[Vector]:
    batch_size = batch_size or model.optimal_batch_size

    keys   = [self._cache_key(model, t) for t in texts]
    cached = await self.cache.mget(keys)
    misses = [(i, t) for i, (t, c) in enumerate(zip(texts, cached)) if c is None]

    results: list[Vector | None] = [
        Vector.from_bytes(c, model.dimension) if c else None for c in cached]

    for batch in _chunked(misses, batch_size):
        oversized = [(i, t) for i, t in batch
                     if self.count_tokens(model, t) > model.max_input_tokens]
        for i, t in oversized:
            batch[batch.index((i, t))] = (i, self.truncate_to_tokens(model, t,
                                                                     model.max_input_tokens))
            log.warning("embedding.truncated", index=i,
                        original_tokens=self.count_tokens(model, t))

        async with self._provider_semaphore(model):
            raw = await self.provider.embed(model, [t for _, t in batch], purpose="document")
        for (i, t), v in zip(batch, raw):
            results[i] = self._postprocess(v, model)

    await self.cache.mset({...})
    return results          # order preserved — callers rely on positional correspondence
```

Order preservation is a hard contract: `M07` zips the returned vectors against its chunk list.

### 3.3 Batching parameters

| Provider type | Batch size | Concurrency | Notes |
| --- | --- | --- | --- |
| Local TEI/Infinity | 64 | 4 | Server does dynamic batching; more parallelism helps little |
| OpenAI | 256 | 8 | Large batches supported; watch the 8192-token-per-item limit |
| DashScope / Qwen | 25 | 4 | Hard per-request item cap |
| Cohere / Jina | 96 | 4 | |
| Ollama | 32 | 2 | Typically CPU-bound |

Values live in `model.config` and are tunable without a code change.

### 3.4 Normalization

If `model.normalize`, L2-normalize so cosine similarity reduces to a dot product — measurably
faster in every backend. `Vector.normalized` records what happened; `M05` asserts consistency
between the vector and the namespace metric, because mixing normalized and unnormalized vectors
in one index silently distorts every score.

### 3.5 Sparse vectors (`FR-G-05`)

Two paths:

1. **Model-produced** (bge-m3 returns learned sparse weights alongside dense) — preferred when
   available.
2. **Computed** — tokenize (jieba for CJK, regex for Latin), remove stopwords, compute BM25
   term weights against corpus statistics maintained per KB.

Corpus statistics (document frequency per term) are maintained incrementally per
`(kb_id, index_version)` and cached. They must be **rebuilt on reindex**, since a new index
version may have entirely different chunk boundaries.

### 3.6 Tokenization (`FR-F-11`)

```python
def count_tokens(self, model: ModelRef, text: str) -> int:
    return len(self._tokenizer_for(model).encode(text))
```

Uses the **embedding model's own tokenizer** — tiktoken for OpenAI, HF tokenizers for
bge/Qwen. Never a whitespace heuristic: for Chinese, `len(text.split())` is off by roughly 100×,
which turns "512-token chunks" into "50,000-token chunks" that the provider rejects.

Tokenizers are loaded once at startup and cached; loading one per call costs ~200 ms.

### 3.7 Failure handling

| Failure | Behaviour |
| --- | --- |
| Provider 429 | Exponential backoff with jitter; respect `Retry-After`; retryable |
| Provider 5xx | Retry ×3, then `EMBED_PROVIDER_ERROR` (retryable at the task level) |
| Timeout | 30 s documents, **2 s queries** (hot path must fail fast, not hang) |
| Dimension mismatch from the provider | **Terminal** — indicates misconfiguration; alert |
| Circuit breaker | 5 failures in 60 s opens for 30 s; fail fast rather than queue up |

---

## 4. Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `CAIRN_EMBEDDING__CACHE_TTL_S` | `604800` | 7 days |
| `CAIRN_EMBEDDING__CACHE_ENABLED` | `true` | |
| `CAIRN_EMBEDDING__QUERY_TIMEOUT_S` | `2` | Hot path |
| `CAIRN_EMBEDDING__DOCUMENT_TIMEOUT_S` | `30` | |
| `CAIRN_EMBEDDING__MAX_CONCURRENCY` | `8` | Per provider |
| `CAIRN_EMBEDDING__LOCAL_SERVER_URL` | none | TEI/Infinity endpoint |
| `CAIRN_EMBEDDING__SPARSE_METHOD` | `auto` | `auto` \| `model` \| `bm25` |

---

## 5. Performance requirements

| Operation | Budget | Req |
| --- | --- | --- |
| `embed_query` cache hit | **< 5 ms p99** | NFR-P-01 |
| `embed_query` miss, local server | < 20 ms p95 | |
| `embed_query` miss, hosted provider | < 120 ms p95 | |
| `embed_documents` throughput, local GPU | ≥ 2000 chunks/s | NFR-P-05 |
| `count_tokens` on 2 KB | < 1 ms | |
| Cache hit rate, steady state | > 30% | NFR-P-07 |

---

## 6. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M08-01 | `embed_query` returns a vector of the model's dimension | FR-G-01 |
| TC-M08-02 | Identical text hits the cache; the provider is called once | FR-G-02 |
| TC-M08-03 | Whitespace/unicode variants normalize to the same cache key | FR-G-02 |
| TC-M08-04 | Query and document prefixes produce distinct cache keys | §3.1 |
| TC-M08-05 | **`embed_documents` preserves input order with partial cache hits** | §3.2 |
| TC-M08-06 | Batching respects the provider's per-request item cap | §3.3 |
| TC-M08-07 | Over-length input is truncated and warned, not rejected | §3.2 |
| TC-M08-08 | Normalized vectors have L2 norm 1.0 ± 1e-6 | §3.4 |
| TC-M08-09 | **Chinese token counting matches the model tokenizer, not whitespace** | FR-F-11 |
| TC-M08-10 | Sparse vectors from bge-m3 and from BM25 both produce valid output | FR-G-05 |
| TC-M08-11 | BM25 statistics rebuild on reindex | §3.5 |
| TC-M08-12 | Provider 429 backs off and eventually succeeds | §3.7 |
| TC-M08-13 | Circuit breaker opens after 5 failures and recovers | §3.7 |
| TC-M08-14 | Query timeout is 2 s and fails fast | §3.7 |
| TC-M08-15 | Provider dimension mismatch is terminal, not retried | §3.7 |
| TC-M08-16 | Benchmark: ≥ 2000 chunks/s on the reference GPU | NFR-P-05 |
| TC-M08-17 | Benchmark: cache-hit query p99 < 5 ms | NFR-P-01 |

Coverage target: **85%**.

---

## 7. Acceptance criteria

- [ ] All 17 test cases pass
- [ ] Benchmarks met on the `medium` preset
- [ ] Cache hit/miss metrics exported and visible on the dashboard
- [ ] Tokenizer parity verified against each supported embedding model
- [ ] No unbounded provider concurrency anywhere

---

## 8. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M08-01 | `EmbeddingService` interface, `Vector`, `ModelRef` | 0.5 | |
| T-M08-02 | Tokenizer registry + `count_tokens` + truncation | 1.0 | |
| T-M08-03 | **Embedding cache: keying, normalization, binary encoding** | 1.0 | T-M00-06 |
| T-M08-04 | `embed_query` hot path | 0.5 | T-M08-03 |
| T-M08-05 | `embed_documents` batching with order preservation | 1.5 | T-M08-03 |
| T-M08-06 | Local server client (TEI / Infinity) | 1.0 | |
| T-M08-07 | Sparse: model-produced + BM25 with per-KB statistics | 2.0 | T-M08-02 |
| T-M08-08 | Normalization + metric consistency assertions | 0.5 | |
| T-M08-09 | Retries, circuit breaker, semaphores | 1.0 | |
| T-M08-10 | Metrics | 0.5 | |
| T-M08-11 | Tests TC-M08-01..17 + benchmarks | 2.0 | all |
| | **Total** | **11.5** | |
