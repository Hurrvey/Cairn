# Model invocation and usage implementation plan

Approved design: `../specs/2026-09-29-model-invocation-and-usage-design.md`.
Execution: primary agent inline, same persistent design advisor only.

## Contracts and sequence

1. **Pure invocation runtime** (`modelgw/runtime.py`, `modelgw/invocation.py`, tests/unit/modelgw):
   ChatMessage(role/content), ChatRequest(messages/max_tokens/temperature/stream), ChatResponse
   and ChatChunk; InvocationSnapshot(ModelRef/provider/prices). HTTP-native adapters enforce text
   messages, bounded bodies/SSE, one deadline including semaphore/retries, safe credentials and
   provider-specific usage conversion. Test normal and streaming output/cancellation, invalid
   upstream shapes, retry before output only, cross-provider isolation. Keep ORM out of runtime.
2. **Usage delivery** (`core/usage.py`, `platform/usage.py`, migration0016, worker maintain):
   `UsageEvent` immutable UUID/time, nullable token/cost fields; bounded async queue → Redis
   consumer group → deduplicated SQL commit → ACK. Test replay/reclaim/poison/retention/stop and
   workspace-scoped summary. No strict quota claim.
3. **Control facade and APIs** (model schemas/service/router/catalog): capability-aware registry,
   session-only permission-before-lookup invocation snapshot, chat/rerank probes, usage routes.
   Tests model grant/admin/API-key/foreign-workspace cases with real DB and local HTTP provider.
4. **UI/deployment/docs** (ModelsView, API types/locales, usage panel, lifespan/worker wiring):
   select capability, chat console with streamed deltas/cancel and usage results, summary view.
   Test actual browser against deterministic local streaming provider and Redis/PG persistence;
   full static/boundary/auth/schema/frontend gates and affected backend integration.

Each deliverable starts with a failing behavior test, then minimal implementation and focused
green verification. Subsequent complete regression follows when combined source is stable.
Commercial live APIs, strict quota and later modules remain explicit separate work.

Progress: [ ] runtime [ ] usage [ ] API integration [ ] UI/acceptance
