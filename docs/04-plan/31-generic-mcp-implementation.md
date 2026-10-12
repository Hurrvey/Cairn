# Generic read-only MCP implementation plan

Date: 2026-09-17. Status: SEARCH MCP ACCEPTED AND DEPLOYED. Agent changes remain deferred.

**Goal:** Expose the existing authenticated retrieval service through interoperable Streamable HTTP
MCP and verify it with an actual MCP client, without coupling Cairn to any particular Agent.

**Architecture:** A small `cairn.mcpserver` adapter shares `RetrievalService`, authz principals and
runtime projections with HTTP retrieval. The official Python MCP SDK owns protocol/transport.
Stateless JSON responses avoid per-process session affinity. API composition mounts the endpoint
only for data/all roles. No catalog/identity/modelgw imports in the MCP domain package.

**Tech stack:** existing Python/FastAPI/Redis/pgvector plus locked official MCP Python SDK1.x,
explicit2025-11-25 protocol compatibility baseline. No assertion of support for every newer draft.

## Approved scope and limits

- First deliverable: `search_knowledge_base`, not the entire historical M13 three-tool/resource plan.
- Streamable HTTP `/mcp`, bearer `cairn_sk_...` only, existing key/owner intersection, revocation,
  IP restrictions and rate limits. Cookie sessions must not authorize MCP, including role=all.
- Search arguments: query, optional knowledge_base_id (infer only with exactly one authorized KB),
  top_k1..20/default5, max_context_tokens100..20000/default4000. No unsupported filters/rerank inputs.
- Results contain readable source-labeled passages and structured retrieval output. Tool validation,
  denied KBs and retrieval failures become safe `isError` results; transport authentication remains401.
- Tools are read-only annotations, not upload/delete/admin. Untrusted retrieved text is data, not
  executable instruction. Do not disclose inaccessible KB IDs, secrets or raw exceptions.
- Request size/timeout bounds, strict allowed origins (absent Origin accepted for server clients),
  correct HTTP methods/content negotiation, cancellation/shutdown and no stale cross-key context.
- Keep existing HTTP and UI behavior. No change to `D:/code/ai-smart-screen-digital-human`.
- No new branch or commit; preserve dirty workspace and live user services. Disposable DB only for tests.

## Tasks and ownership

### A. Tool adapter (sole sol/high subagent)

Files: `src/cairn/mcpserver/tools.py`, `tests/unit/mcpserver/test_tools.py`.
Interface: `KnowledgeSearchTools(retrieval)` with `list_tools(principal)` and
`call_tool(principal, name, arguments, request_id)` returning SDK tool/result objects.

- [x] RED valid schema/read-only descriptor, caller-bound KB IDs, no-KB/ambiguous defaults.
- [x] Implement bounded arguments -> RetrievalRequest(hybrid, rerank=false, expand_parent=false).
- [x] RED/GREEN formatted citations+structuredContent, denied scopes, sanitized tool errors,
  invalid/unknown arguments, cancellation propagation and output bounds.
- [x] Unit tests/Ruff/mypy, final diff and coordinator review. No container/DB access by worker.

### B. HTTP protocol and application wiring (coordinator)

Files: SDK dependency/lock, `src/cairn/mcpserver/server.py`, package init, API composition/config,
boundary contracts and focused protocol tests.

- [x] Inspect pinned SDK transport/lifecycle/request-context APIs before wiring.
- [x] RED unauthenticated/cookie/Origin/body/method tests, SDK initialize/list/call compatibility.
- [x] Build stateless server and lifespan; authenticate every HTTP request before transport parsing.
- [x] Add origin config and finite envelope bounds; keep data-plane rate limits and no ORM imports.
- [x] Mount exact `/mcp` without redirect, role-specific meta capability, audit route guards.

### C. Independent acceptance and deployment/docs (coordinator)

Files: `tests/integration/test_mcp_acceptance.py`, `docs/06-ops/05-mcp-quickstart.md`, ledger/map.

- [x] Actual SDK client against network server: initialize, list tools, call search, close; two keys
  concurrently, forbidden KB, revoked key and no-access principal; real PG/Redis retrieval.
- [x] SDK/network/Compose validation without touching user DB; full regression/static/boundary checks.
- [x] Record exact protocol/auth compatibility limits, config example and unsupported M13 features.
- [x] Cleanup only owned disposable services. No Agent repo changes or undocumented completion claim.

## Journal

- M02: Official SDK1.30.0 selected and pinned in dependency lock. Eight transport tests first failed
  with expected404 (control-only404 already passed); after SDK lifecycle/response integration all9
  passed. Parent independently tested actual SDK ClientSession over a loopback Uvicorn socket with
  real PostgreSQL/Redis indexed data; search, citations, denied target, revoked key, cookie rejection
  and rate limit passed. Concurrent disjoint key scopes now pass without descriptor/context leakage.
- M03: Sole Bohr delivered23 unit tool tests; parent reviewed and required safe class-level error
  titles (never raw exception detail), strict explicit-null rejection, and512KiB serialized result
  cap. Parent found cookie-carried API keys could inherit api_key auth_source: new RED guard test then
  enforced actual Authorization Bearer header. Transport suite13 passed including chunked body cap,
  badOrigin/Host/version/Accept, malformedRPC, notifications and cancellation of a timed-out tool.
  Worker explicitly closed; no subagent remains. Parent final acceptance continues.
- M04: Permission-change test exposed inherited authz semantics: key revocation evicts immediately,
  while ordinary grant drift is bounded by the existing60-second principal-cache TTL; perm_version
  alone is not checked on a warm cache hit. MCP adds no independent descriptor cache. Do not claim
  instantaneous ordinary-grant propagation or full historical TC-M13-15. Acceptance shortens only
  the test TTL to1s and verifies newly resolved permissions remove the KB and deny subsequent calls.
  This release documents the inherited bound rather than redesigning authz under MCP scope.
- M05: Isolated Compose `cairn-mcp-acceptance` (port38881) built/runs API/data/web/all workers/TEI
  against its own volumes and DB. HTTP created provider/model/storage/KB/upload, actual workers
  indexed the document, then official MCP ClientSession over nginx discovered/search-called the tool
  and returned the Saturn passage. Temporary key was revoked in finally, never printed/saved.
  Dedicated integration PG39170/Redis38694/TEI38742 use labelmcp-20260917, separate from user services.
- M06: One live semantic regression's original HTTP retrieval timed out while image builds and
  extra model startup contended for CPU. No timeout/assertion weakened. Repeated the same three
  real-model cases plus MCP acceptance/units when builds finished:41 passed in30.28s. Read-only
  guarded GET now also checks Origin/Host (new RED405-vs403 then GREEN). Latest transport+tool+
  data-plane import/role subset44 passed. Final regression and final-image verification remain.
- M07: Final-image Compose acceptance repeated successfully, including SDK search and Origin
  rejection through nginx. Full backend954 passed,0 assertion failures/skips,2 deferred PDF/OCR
  collection errors,4 existing Alembic warnings,294.64s,coverage86.29% (80% gate unchanged).
  One additional cross-replica stateless test was added after full-suite collection; final transport
  suite15 passed separately. Frontend36 tests/typecheck/API-drift passed; actual backend OpenAPI still
  exactly equals exported spec (MCP is separately described, not an OpenAPI JSON endpoint).
  Ruff222 files, mypy130,7 boundaries and62-route guard audit passed. Agent worktree is still clean
  at662972d. Updating only user's API/control-data and web images, not DB/model/workers/volumes.
- M08: User-facing local `/mcp` now deployed. Actual official client inside the API container called
  nginx -> api-data using one temporary scoped query key, queried existing content and returned5
  hits with protocol2025-11-25 and no session ID. Key revoked in finally; no raw key/query/source text
  printed. Before/after user document count1, indexed count1. API/web/stores healthy; other services
  and volumes preserved. No Agent repo files modified. MCP module combined statement/branch coverage
  is93% (224 statements/60 branches), above85% target for this bounded adapter.
- M09 FINAL: Final focused adapter/transport/network/real-model matrix43 passed in27.58s. Verified
  labels before removing only3mcp-20260917 integration containers and12cairn-mcp-acceptance Compose
  containers/network (down without-v; its disposable volumes and local evidence retained). Confirmed
  user's11Cairn services still running, APIs/web/stores healthy, original M08 containers untouched.
  Live `/mcp` rejects anonymous calls401 and `/v1/meta` reports MCP. No test process/subagent left.
  `git diff --check` passes; no commit/push. Agent worktree remains clean and unchanged at662972d.

## Acceptance and honest limits

The approved **Cairn-only generic read-only search MCP** is complete. The actual endpoint is
`http://127.0.0.1:8080/mcp`, with static bearer key support, not a vendor-specific Agent integration.
Final focused report: `data/acceptance-mcp-20260917/final-focused.xml`. Full regression report:
`backend.xml`, coverage: `coverage.xml`, in the same ignored directory. The full command remains
nonzero solely due to the existing unimplemented PDF/OCR draft collection errors.

Do not mark all historical M13 acceptance criteria complete: this increment has one tool, no
list/get/resources, no OAuth discovery, no interactive third-party client certification, no KB-name
projection/description cache and no claim of instant ordinary-grant propagation. Protocol baseline
is2025-11-25 with official SDK1.30.0, not an unverified future/draft version. Actual official SDK
network and Compose interoperability are verified. Agent client work remains for a separate request.

- M01: Read M13, current retrieval DTO/service, role composition and auth middleware. Existing `/mcp`
  nginx proxy is not an implementation. Project had no MCP SDK installed. User selected Cairn-only
  search MCP; historical list/get/resources and Agent client are explicitly not claimed complete.
  Official references: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
  and https://modelcontextprotocol.io/specification/2025-11-25/server/tools ; official SDK v1.x source
  at https://github.com/modelcontextprotocol/python-sdk/tree/v1.x .
