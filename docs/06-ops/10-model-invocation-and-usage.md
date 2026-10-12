# Chat/rerank invocation and usage reporting

September 30 increment. This adds gateway model calls and reporting; strict workspace quotas,
embedding/retrieval usage attribution, fallback chains and production vendor live acceptance
remain distinct follow-up work.

## Register and use a model

Administrators create a provider under Models, then register a model with capability **Text chat**
or **Reranking**. Chat does not require an embedding dimension/tokenizer. Optional input/output
prices are USD per 1,000 tokens; leave them blank when unknown. Prices are copied into usage
events when the call starts, so later configuration does not rewrite historical costs.

Text chat adapters: OpenAI-compatible (including compatible DeepSeek/vLLM endpoints), OpenAI,
Anthropic Messages, Gemini and Ollama. DashScope chat uses its compatible-mode endpoint while
existing embedding configuration retains its original endpoint. Registered rerank supports
TEI and Infinity. Incompatible capability/provider registration is refused explicitly. Adapters
currently accept text messages only; image/tool/thought blocks are not silently treated as text.

Use **Test** for a short capability-specific probe. **Try chat** opens the streaming console.
The separate **Model console** page lists only chat models the current user can use. Ordinary
users need a `model:use` grant; administrators can test via `platform:models`. This first gateway
surface requires a user session. Existing knowledge-base API keys cannot call it, even when their
owner is an administrator. Every lookup remains workspace-scoped.

Endpoints:

- `GET /v1/models/usable` — authorized session's usable chat models.
- `POST /v1/models/{id}/chat` — messages/max_tokens/temperature/stream; JSON or SSE response.
- `POST /v1/models/{id}/rerank` — query/documents; index-aligned scores.
- `GET /v1/usage` — administrator's workspace report, optional model_id/principal_id filters.

SSE events are Cairn `delta`, `completed` or `error`; provider raw errors/headers are not exposed.
Stopping the console cancels the upstream stream. A bounded producer queue prevents a slow client
from causing unbounded buffering. One 30-second deadline covers admission, all retries and output.
Retries honor Retry-After and happen only before output; a failed stream is never restarted after
text is delivered. A provider-wide semaphore isolates concurrency and a circuit breaker fails fast
after repeated errors. The same policy covers registered rerank calls.

## Usage is best-effort reporting

Gateway chat/rerank attempts emit content-free events: workspace/principal/model, request and
attempt identity, outcome, latency, token counts and price snapshot. No prompts, output text or
plaintext credentials enter usage events. Failed/cancelled attempts may have unknown usage.
Unknown tokens/prices remain null; totals are labelled **reported**, with unknown counts and cost
coverage. Currency totals are separate. This report does not claim to be a strict billing ledger.

The API owns a bounded process queue (1,024 events) and publishes to Redis stream
`cairn:usage:events` (100,000 retained events). `worker-maintain` reclaims pending events, writes
`usage_record` by UUID, commits, then ACKs. Duplicate delivery is harmless. Invalid/oversized or
unrepresentable events enter a bounded dead-letter stream; the raw invalid payload is not logged.
Migration0016 creates an unpartitioned table to support global UUID dedup. Queues, retention and
shutdown may lose events; `cairn_usage_delivery_total{outcome}` reports published/persisted/dropped
outcomes. Shutdown drains for at most5 seconds. Redis loss can lose pending reporting data; quotas
must use a separate durable reservation design.

## Validation scope

Tests cover native provider request/response/stream contracts with controlled transports, shared
deadline, Retry-After, cancellation, malformed/nontext outputs, backpressure and close failures.
Real local HTTP/SSE, Redis and PostgreSQL tests verify the full invocation/reporting chain,
deduplication, poison handling and workspace authorization. Browser acceptance uses the disposable
deterministic local provider; it is not evidence of commercial inference quality or availability.

Primary wire references checked during implementation:
[OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat),
[Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming),
[Gemini generateContent](https://ai.google.dev/api/generate-content),
[Ollama chat](https://docs.ollama.com/api/chat).
