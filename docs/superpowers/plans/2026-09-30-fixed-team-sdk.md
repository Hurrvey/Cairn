# Fixed-team continuation and SDK acceptance plan

## Execution ownership

User direction September30: `/root` designs, coordinates and accepts; all source/test changes
are implemented by the one persistent `/root/developer` (gpt-5.6-sol/high). The existing
`/root/design_advisor` remains the only reviewer. Reuse these agents; do not create replacements
per task. Coordinator may maintain plans and evidence, but does not implement source changes.

## Gate 1 — close gateway regression

Current evidence: `data/modelgw-full.xml`:1319 passed,1 failed,1 setup error,84.60% coverage.
These are not accepted as a passing run.

1. Reproduce `test_create_kb_rejects_a_chat_model`: historical OpenAI fixture lacks the now
   required credential. Reviewer determines correct fixture update, preserving the negative
   KB capability assertion and production credential enforcement.
2. Reproduce `test_deadline_cancels_pending_tool`: actual HTTP200 versus expected503. Inspect
   MCP envelope and cancellation, distinguish a timing issue from lost cancellation/error.
   Do not weaken assertions or extend deadlines without evidence.
3. Developer implements reviewer-agreed fixes and runs focused tests, then full combined
   service-backed suite when stable. Coordinator checks exact XML failure/skip counts and
   coverage. Record underlying code changes separately from fixture compatibility updates.

## Gate 2 — Python/TypeScript SDKs

October8 accepted timeout clarification: sync Python has soft aggregate plus per-I/O limits,
with up to one in-progress read timeout of overrun; async Python/TS require hard cancellation.
The deadline rows below use this documented distinction. Real loopback slow-drip/stall tests
must cover it; no new retry starts after the aggregate deadline. Reviewer approved this scoped
deviation instead of changing injected-client semantics via an async bridge.

Design: `../specs/2026-09-30-client-sdks-design.md`.

| Deliverable | Contract | Required evidence |
| --- | --- | --- |
| Python package | `CairnClient`, `AsyncCairnClient`; generated DTOs without server imports | Wheel builds, clean-environment import, sync/async lifecycle tests |
| TypeScript package | fetch client, generated OpenAPI types, actual private key storage | Typecheck, npm tarball, clean Node import, JSON.stringify reveals no key |
| Retrieval | query/batch preserve usage/degraded/partial_failures/explain/truncation | Same fixture and real KB response IDs in both languages |
| Discovery | actual CursorPageKnowledgeBaseResponse, bounded per-page iteration | Query-only key sees authorized KBs; advancing/repeated cursor tests |
| Citations | CitedChunkResponse; document Markdown plus request ID/revision | Read key succeeds, query-only gets404; text length bound and cancellation |
| CRUD/upload | exact nested server paths and multipart metadata/file | Real isolated upload → indexed → query → cleanup; no automatic mutation retry |
| Failures | status/code/request_id/Retry-After; safe bounded error data | seconds/date429, HTML failure, transport/timeout distinct; key not exposed |
| Retries | default0; opt-in only declared read calls within total deadline | no premature Retry-After retry; batch200 partial failure not retried |
| Origin control | explicit unified base URL, no redirects or alternate-origin request | hostile ID/path/Location cannot redirect authenticated request |
| Response bounds | JSON/error/Markdown caps, actual stream/reader cancellation | oversized response and timeout abort actual work |

Typed low-level request can cover admin surfaces without implying API-key admin permissions.
Generated SecretStr inputs require deliberate wire serialization: secret masking used by repr
must not silently replace a write-only credential in a request. Prefer ordinary input mappings
for credential operations if generated model serialization cannot safely preserve intent.

Generation must pin tool versions and use stable formatting/no timestamps. A schema change
without regeneration fails a dedicated SDK drift check. Keep SDK dependencies out of server lock.

## Gate 3 — optional retriever adapters

Use actual supported LangChain/LlamaIndex base classes in separate optional extras/packages.
Sync and async paths use corresponding SDK clients. Preserve native scores, citations and
degradation notices in framework metadata; zero hits with failures cannot silently look like
clean no-results. Define explicit strict default/error behavior. Test imports and retrieval with
real framework installations, then through the isolated Compose endpoint. Do not publish.

## Acceptance sequence

Developer reports each bounded deliverable, reviewer checks contracts/security, coordinator
verifies evidence and asks the same developer to resolve findings. Do not modify backend during
its frozen full run; SDK work is isolated under sdk/. Only one integration test process may own
the disposable PG/Redis/Qdrant environment. Product Compose data is never a truncation target.

After gates: update ledger/README with exact scope, version/build/test counts and remaining
project tasks. SDK acceptance does not close quotas, crawling/archive, configurable pipelines,
sandbox, evaluation, annotated PDFs or production scale gates.
