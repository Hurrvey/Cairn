# M10 — Model Gateway

| | |
| --- | --- |
| **Package** | `cairn.modelgw` |
| **Layer** | L2/L3 shared service |
| **Phase** | 3 |
| **Owner** | Backend eng. D |
| **Depends on** | M00, M15 (facade) |
| **Depended on by** | M03, M07, M08, M11, M14 (and M09 indirectly via M08) |
| **Tables owned** | `model_provider`, `model`, `secret` |
| **Requirements owned** | FR-K-01..09, NFR-SEC-02, NFR-C-04 |

---

## 1. Purpose and scope

One typed interface to every LLM, embedding, and rerank provider, with credentials that never
leak.

**In scope:** provider and model registration, credential encryption, a unified call interface,
capability typing, connectivity testing, usage and cost accounting, concurrency limits, retries,
circuit breaking, fallback chains, streaming.

**Out of scope:** the embedding cache and batching (M08), deciding which model to use (callers),
hosting inference.

---

## 2. Provider support (`FR-K-02`)

| Family | Capabilities | Notes |
| --- | --- | --- |
| `openai` | chat, embedding | Also Azure OpenAI via `base_url` |
| `anthropic` | chat | Claude; native Messages API for tool use and streaming fidelity |
| `google` | chat, embedding | Gemini |
| `dashscope` | chat, embedding, rerank | Qwen. 25-item embedding batch cap |
| `deepseek` | chat | OpenAI-compatible |
| `ollama` | chat, embedding | Self-hosted; Llama and others |
| `vllm` | chat, embedding | Self-hosted, OpenAI-compatible |
| `tei` / `infinity` | embedding, rerank | Self-hosted, the recommended production path |
| `cohere` / `jina` | embedding, rerank | |
| `openai_compatible` | chat, embedding | **Any** endpoint speaking the OpenAI schema (`NFR-C-04`) |

Implementation uses **LiteLLM** for breadth, wrapped so its types never escape:

```python
# ✅ our domain type
async def chat(self, model: ModelRef, messages: list[Message], **opts) -> ChatResponse
# ❌ never exposed
litellm.completion(...) -> ModelResponse
```

The wrapper is not ceremony. LiteLLM's shapes shift between versions; a leaked type means every
consumer breaks on upgrade, and it makes swapping in a hand-written adapter for a provider with
awkward behaviour impossible.

---

## 3. Public interface

```python
class ModelGatewayService:
    # registration
    async def create_provider(self, actor, spec: ProviderSpec) -> ProviderView: ...
    async def update_provider(self, actor, provider_id, spec) -> ProviderView: ...
    async def delete_provider(self, actor, provider_id) -> None: ...
    async def list_providers(self, workspace_id) -> list[ProviderView]: ...
    async def discover_models(self, provider_id) -> list[DiscoveredModel]: ...
    async def register_model(self, actor, spec: ModelSpec) -> ModelView: ...
    async def list_models(self, workspace_id, capability=None) -> list[ModelView]: ...
    async def test_model(self, model_id) -> ModelTestResult: ...          # FR-K-05

    # references for other modules — NEVER include secrets
    async def get_ref(self, model_id: UUID) -> ModelRef: ...

    # invocation
    async def chat(self, ref: ModelRef, messages, **opts) -> ChatResponse: ...
    async def chat_stream(self, ref: ModelRef, messages, **opts) -> AsyncIterator[ChatChunk]: ...
    async def embed(self, ref: ModelRef, texts: Sequence[str], *, purpose) -> list[list[float]]: ...
    async def rerank(self, ref: ModelRef, query: str, docs: Sequence[str]) -> list[float]: ...

@dataclass(frozen=True)
class ModelRef:
    """The only model representation crossing a module boundary. No credentials, ever."""
    id: UUID; provider_family: str; model_key: str
    capability: Literal["chat","embedding","rerank"]
    dimension: int | None; max_input_tokens: int | None
    normalize: bool; query_prefix: str | None
    optimal_batch_size: int; tokenizer_id: str
```

---

## 4. Behaviour

### 4.1 Secret handling (`FR-K-04`, `NFR-SEC-02`) — envelope encryption

```
CAIRN_MASTER_KEY (env or KMS)
      │ HKDF
      ▼
     KEK ──AES-256-GCM──▶ per-secret DEK ──AES-256-GCM──▶ ciphertext in `secret`
```

```python
async def _store_secret(self, session, workspace_id, purpose, plaintext: str) -> UUID:
    dek   = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    ct    = AESGCM(dek).encrypt(nonce, plaintext.encode(), aad=purpose.encode())
    wrapped = AESGCM(self._kek).encrypt(nonce, dek, aad=str(workspace_id).encode())
    return await self.repo.insert_secret(session, ciphertext=ct, nonce=nonce,
                                         wrapped_dek=wrapped, key_version=self._key_version)
```

Rules:
- Decryption happens **only** at the point of the outbound call, never earlier.
- Plaintext is never returned by any API — the UI shows `sk-…a1e` masks only.
- Providers with a stored secret are updated by replacement, never by read-modify-write.
- KEK rotation re-wraps DEKs without touching ciphertexts (`key_version` tracks which KEK).
- The AAD binds a secret to its workspace and purpose, so a stolen row cannot be replayed
  elsewhere.

### 4.2 Resilience (`FR-K-07`)

| Control | Default | Rationale |
| --- | --- | --- |
| Per-provider semaphore | 8 concurrent | **One slow provider must not exhaust the worker pool.** Without this, a hanging provider consumes every task slot and stalls unrelated queues. |
| Timeout | 30 s chat, 30 s embed, **2 s query embed** | Hot path fails fast |
| Retries | 3, exponential + jitter, `Retry-After` honoured | |
| Circuit breaker | 5 failures / 60 s → open 30 s → half-open probe | Fail fast instead of queueing into a dead provider |
| Fallback chain (`FR-K-08`) | opt-in per model | Falls to a secondary; the response records which model actually served |

### 4.3 Connectivity test (`FR-K-05`)

```python
async def test_model(self, model_id) -> ModelTestResult:
    # capability-appropriate minimal probe
    #   chat      → "Reply with OK."
    #   embedding → embed "test", assert dim == model.dimension   ← catches misconfiguration
    #   rerank    → score one document
    # returns: reachable, latency_ms, error, and for embeddings the OBSERVED dimension
```

The embedding probe is the valuable one: a mismatch between the configured and observed
dimension is caught at registration rather than as a mysterious failure during a large ingest.

### 4.4 Usage accounting (`FR-K-06`)

Every call writes a `usage_record` (via M15) with workspace, KB, principal, model, token counts,
and cost from `model.cost_per_1k_*`. Written **asynchronously** on a buffered flush so accounting
never adds latency to the call path — and buffered records are flushed on shutdown.

### 4.5 Streaming (`FR-K-09`)

`chat_stream` yields `ChatChunk` over SSE. Used by pipeline LLM nodes and the model test console.
Cancellation propagates: a disconnected client aborts the upstream request rather than paying for
tokens nobody will read.

---

## 5. API endpoints owned

| Method | Path | Permission |
| --- | --- | --- |
| GET/POST | `/v1/model-providers` | `platform:models` |
| PATCH/DELETE | `/v1/model-providers/{id}` | `platform:models` |
| GET | `/v1/model-providers/{id}/discover` | `platform:models` |
| GET/POST | `/v1/models` | `model:use` / `platform:models` |
| PATCH/DELETE | `/v1/models/{id}` | `platform:models` |
| POST | `/v1/models/{id}/test` | `platform:models` |
| POST | `/v1/models/{id}/chat` (test console, SSE) | `model:use` |

---

## 6. Errors owned

| Code | HTTP | Meaning |
| --- | --- | --- |
| `PROVIDER_UNREACHABLE` | 502 | Connectivity failure |
| `PROVIDER_AUTH_FAILED` | 502 | Credentials rejected upstream |
| `PROVIDER_RATE_LIMITED` | 429 | Upstream 429, `Retry-After` propagated |
| `PROVIDER_CIRCUIT_OPEN` | 503 | Breaker open; fast-fail |
| `MODEL_CAPABILITY_MISMATCH` | 400 | e.g. a chat model selected for embedding |
| `MODEL_DIMENSION_MISMATCH` | 400 | Observed dimension ≠ configured |
| `MODEL_DISABLED` | 400 | |

---

## 7. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M10-01 | Provider registration encrypts credentials; ciphertext differs per secret | FR-K-04 |
| TC-M10-02 | **No API path returns a decrypted credential** | FR-K-04 |
| TC-M10-03 | Credentials never appear in logs, traces, or `repr()` | NFR-SEC-03 |
| TC-M10-04 | KEK rotation re-wraps DEKs; old secrets remain readable | §4.1 |
| TC-M10-05 | A secret cannot be decrypted with another workspace's AAD | §4.1 |
| TC-M10-06 | Each provider family performs a successful chat and embed against a mock | FR-K-02 |
| TC-M10-07 | Capability mismatch is rejected | FR-K-03 |
| TC-M10-08 | Embedding test detects a dimension mismatch at registration | FR-K-05 |
| TC-M10-09 | Semaphore bounds concurrency per provider | FR-K-07 |
| TC-M10-10 | **A hanging provider does not block calls to a different provider** | FR-K-07 |
| TC-M10-11 | Circuit breaker opens, fails fast, and recovers via half-open | FR-K-07 |
| TC-M10-12 | Retries honour `Retry-After` | FR-K-07 |
| TC-M10-13 | Fallback chain switches and records the serving model | FR-K-08 |
| TC-M10-14 | Usage records are written with correct token counts and cost | FR-K-06 |
| TC-M10-15 | Usage accounting adds < 1 ms to the call path | §4.4 |
| TC-M10-16 | Streaming yields chunks; client disconnect aborts upstream | FR-K-09 |
| TC-M10-17 | `ModelRef` contains no credential field (structural assertion) | §3 |

Coverage target: **85%**.

---

## 8. Acceptance criteria

- [ ] All 17 test cases pass
- [ ] Every listed provider family verified against a recorded-cassette mock
- [ ] Secret-scanning CI finds no plaintext credential path
- [ ] LiteLLM types confined to `cairn/modelgw/_adapters/` (import check)
- [ ] Journey J7 model comparison works end to end

---

## 9. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M10-01 | ORM + migration: `model_provider`, `model`, `secret` | 0.5 |
| T-M10-02 | **Envelope encryption + KEK rotation** | 2.0 |
| T-M10-03 | `ModelRef` + provider/model CRUD | 1.5 |
| T-M10-04 | LiteLLM adapter layer (chat, embed, rerank) | 2.0 |
| T-M10-05 | Native adapters where fidelity matters (Anthropic, DashScope) | 1.5 |
| T-M10-06 | Local server adapters (TEI, Infinity, Ollama, vLLM) | 1.0 |
| T-M10-07 | Semaphores, timeouts, retries, circuit breaker | 1.5 |
| T-M10-08 | Fallback chains | 1.0 |
| T-M10-09 | Model discovery + connectivity test | 1.0 |
| T-M10-10 | Usage accounting with buffered flush | 1.0 |
| T-M10-11 | Streaming + SSE + cancellation | 1.0 |
| T-M10-12 | Routers | 1.0 |
| T-M10-13 | Tests TC-M10-01..17 | 2.0 |
| | **Total** | **17.0** |
