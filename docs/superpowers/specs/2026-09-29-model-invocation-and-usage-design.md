# Model invocation and usage — continuation design

Status: approved by persistent design advisor after eight resolutions; implementation starting. Depends on acceptance of September 29
retrieval/MCP/Dify/S3 increment. The persistent advisor identified this as the next complete
vertical slice before quotas/crawler/evaluation.

## Product behavior

Administrators register chat or rerank models as well as embeddings. A capability-specific
test validates each model. Authorized users can send messages from a model test console and
see streamed output, serving model and usage. Usage is visible by workspace/model/principal/KB,
with explicit unknown/estimated token counts rather than false zeros. Provider keys remain
write-only, encrypted and never reach responses or traces.

## Runtime boundary

Separate wire DTOs/runtime from ORM administration. The existing model catalog resolves model
identity, workspace, capability and provider projection. HTTP console remains control-plane;
pure gateway runtime accepts ModelRef + ProviderRuntimeProjection and never accesses PostgreSQL.
Retrieval can later consume this runtime using its published snapshot. Revise import-linter to
allow only runtime/schema, never management/model ORM.

Use SafeTransport-backed native adapters, not an unguarded LiteLLM transport. First support
OpenAI-compatible chat and SSE (including DeepSeek/vLLM-style hosts), Anthropic Messages,
Gemini generateContent/streamGenerateContent and Ollama chat. DashScope compatible chat uses
its documented compatible endpoint. Registered TEI/Infinity rerank shares existing protocol
semantics. Provider capability matrix rejects unsupported combinations before invocation.
All providers share per-provider semaphore8, 30s chat deadline, bounded response/SSE buffers,
retry on connection/429/5xx before any stream output, and circuit breaker. Never retry after
yielding stream content; cancellation closes upstream immediately. Fallback is explicit and
must record serving model, with no cross-workspace references.

## Authorization

`model:use` on the specific model (or workspace-wide grant) is required for user invocation;
platform:models permits administrative probes/console tests. Permission and workspace checks
precede provider projection/decryption. API-key permissions remain the intersection with owner;
do not implicitly broaden KB-only keys to model invocation.

## Usage path

Shared core UsageEvent/sink protocol with event UUID, request/task attempt, workspace, principal,
KB optional, model/provider, operation, outcome, token counts nullable, estimated flag and price
snapshot. Gateway publishes completion/failure/cancellation without message text. A bounded
process queue feeds a Redis stream asynchronously; drain on shutdown with a deadline. If queue
or Redis is unavailable, increment a dropped-event metric and log only safe identifiers. This
first reporting slice explicitly has a crash-loss window and is not a strict quota ledger.

Maintain worker consumes/reclaims stream events and inserts by event UUID with ON CONFLICT DO
NOTHING, then acknowledges; never acknowledge before database commit. Add usage_record migration
and query/summary endpoints. Administrative queries scope to current workspace and bound date
range/pagination. Cost uses Decimal and the event's price snapshot; unknown tokens/cost stay null.

## Validation and follow-ups

Mock HTTP contract for each supported provider, per-provider isolation, retries/Retry-After,
timeout, circuit recovery, stream disconnect, credential redaction, grant/tenant checks.
Real Redis/Postgres tests verify idempotent event replay, reclaim, commit-before-ack and summary.
Browser test registers a local deterministic chat endpoint, streams a response and inspects usage.
Commercial providers need separate operator credentials for live inference acceptance.

Strict quota reservations/reconciliation, generated golden-set review, crawler/archive import,
pure DAG editor/executor, production function sandbox and temp-index evaluation remain separately
tracked deliverables; do not mark them complete when this slice passes.

## Persistent advisor review resolutions

1. First slice console accepts user sessions only: model:use or administrative testing access.
   API-key model invocation remains unavailable until resolver/resource scope contracts are
   extended and tested; KB-only keys never acquire invocation privileges implicitly.
2. Fallback may apply only to chat/rerank. Embedding never crosses immutable model identity.
   Usage records each billed provider attempt; final response separately names serving model.
3. Bound stream length/retention and consumer batches; reclaim pending entries, isolate poison
   events, and count any losses caused by retention. This reporting stream is explicitly not a
   quota ledger and cannot claim strict delivery after crash/trim.
4. First usage_record is not partitioned; event_uuid primary key supplies unambiguous dedup.
   A future partition migration must preserve dedup using a separate key table or a stable
   composite key, with explicit retention semantics.
5. Invocation snapshots freeze enabled/capability state, provider credentials/config fingerprint,
   prices (unit and currency) and request context after authorization. Only Cairn DTOs cross the
   interface. Initial messages are text-only; images/tool-use are rejected explicitly. Streams
   use delta/completion/error/usage events with no provider headers/raw error passthrough.
6. Semaphore waiting, retries and fallback share one absolute deadline. No retry/fallback after
   first delta; cancellation closes upstream before nonblocking usage publication. Retrying a
   disconnected attempt may be billed twice; record per-attempt known/unknown usage honestly.
7. Reporting exposes completeness, unknown/estimated counts and cost coverage; never sum unlike
   currencies or treat missing tokens/prices as zero. Cache estimates are separate from provider
   spend. Bounded dead-letter captures safe event identity and error category, never prompts/keys.
8. Provider matrix is per capability. DeepSeek/vLLM are compatible protocol targets, not verified
   proprietary extensions. DashScope chat uses a capability-specific compatible endpoint without
   modifying existing embedding base URLs. Live commercial acceptance requires operator keys.
