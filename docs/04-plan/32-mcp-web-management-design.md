# MCP web management design proposal

Date: 2026-09-17. Status: ACCEPTED AND DEPLOYED.

## User request

Manage MCP from the Cairn web interface: configurable port, actual running status, start/stop/restart,
and logs. Agent-side integration remains explicitly deferred. Preserve all live knowledge-base data.

## Current implementation and constraint

MCP currently shares API-data/all process lifecycle and port with HTTP retrieval. Restarting the
container to implement an MCP restart would also interrupt other endpoints. Host ports in Compose
are deployment mappings; saving an application field cannot change an existing Docker port mapping.
The control API currently has no Docker socket access and must not receive unrestricted daemon access.

## Alternatives

1. Recommended: an isolated managed MCP listener, with an internal supervisor/control channel and
   a small deployment-published port range (proposed8081..8090). Web users select one port within the
   range, independently start/stop/restart MCP and see real listener readiness. Expand the published
   range through deployment configuration, not arbitrary web-issued shell/Docker commands.
2. Deployment-port mode: keep arbitrary port changes in Compose and provide browser-generated
   configuration/instructions, exposing only application enable/disable in the web UI. Simpler but
   does not fully meet the user's requested direct port/start/stop management.
3. Restricted host/Docker control helper: dynamically recreate a designated container for arbitrary
   host ports. This adds powerful infrastructure privileges and failure/recovery surface; not proposed
   for the first implementation, and never an unrestricted Docker socket in the web/API container.

## Proposed page

- Admin-only MCP service page: desired state, observed state, configured/effective port, actual
  endpoint URL, last health observation, last operation result, runtime uptime, protocol/tool summary.
- Settings: select a deployment-allowed port, allowed Host/Origin, finite timeout/body limits,
  optional autostart. Saving is not displayed as applied until supervisor acknowledges readiness.
- Start/stop/restart with in-progress state, duplicate-operation prevention and explicit error state.
  Stop blocks new MCP requests and uses a bounded drain/cancel policy; main UI/retrieval/workers stay up.
- Bounded persistent operational/request logs, level/time filters, live follow and capped downloads.
  Include request ID, method/tool, status and duration; exclude keys, full prompts and document text.
- Separate service readiness from dependency/search readiness. Running container is not sufficient
  to claim a usable MCP listener; unreachable supervisor is unknown/unreachable, never simply stopped.
- Preserve existing8080/mcp client compatibility only if its forwarding/admission reflects the same
  managed state. Never leave an alternate uncontrolled MCP endpoint usable after web Stop succeeds.

## Required implementation/acceptance after design approval

Persist desired configuration and operation generation; reconcile after crash/restart; protect
management with admin permission, CSRF, audit and private authenticated supervisor transport. Do
not expose control operations as public MCP tools. Deny invalid/disallowed/in-use ports without
pretending a change succeeded; preserve or clearly report the last working configuration.

Tests must cover port changes through actual Docker host mappings, startup failure, concurrent
commands, stale acknowledgements, process failure, restart persistence, stop admission, log bounds/
redaction, non-admin denial, and actual MCP SDK calls through both supported entrypoints. Use only
disposable test services before any verified user deployment update. One sol/high subagent at a time
if delegated; coordinator independently reviews. Record every stage in this document/ledger.

## Journal

- W02: User approved proposal. Parent wrote durable singleton service/config/generation+bounded-log
  schema migration0012 and admin guarded routes. Two initial RED integration tests (missing service/
  route) became GREEN against separate PG39170/Redis38694 withmcpui-20260917 labels. No user migration.
  Sole sol/high Boyle delivered the page/adapter/navigation and12 new frontend tests (48total); closed.
- W03: Parent implemented socket listener/supervisor, private PostgreSQL command channel, advisory
  leader, generation-fenced acknowledgements, health probe and stable compatibility gateway. Gateway
  dispatches to the same guarded ASGI app in-process (not an extra unauthenticated HTTP hop), preserving
  peer/header context and using identical admission/lifecycle. Embedded API MCP disabled in managed
  mode. Four real-socket unit cases passed, including actual SDK lifecycle restart and port collision.
  Tests use no user data. Independent log-retention and UI/Compose validation now in progress.
- W04: Parent real Chromium verified admin page at isolatedport38882: observed running, Stop closed
  the direct published38081 socket and compatibility/mcp returned503, changed port to38082, Start
  opened the actual host port, Restart completed, logs rendered and JSONL downloaded. Actual database
  generation5 matchedobserved5/running38082. Screenshot reviewed, no fake status. Initial browser
  tool download selected a newer unavailable browser build; pinned existingPlaywright1.62.0 for QA.
  UI compatibility URL corrected from hardcoded8080 to actualpageorigin; direct listener explicitly
  usesHTTP (does not falsely claim TLS from the web page protocol). Tests updated accordingly.
- W05: Real SDK searches through bothcompatibility38882/mcp anddedicated38082/mcp returned indexed
  TEI/pgvector content. Supervisor restart honored persistedauto_start=false (stopped), manual Start
  recovered; stopping supervisor itself yieldedunknown after10s while knowledge/admin API stayed up.
  Boundaries7, route guards67, mypy136, frontend48 passed. Full backend968 passed,0 assertion failures/
  skips,2 deferred PDF/OCR collection errors,coverage85.93%,278.51s. Added a final crashed-task cleanup
  regression after that full run. Final-image browser/user deployment and cleanup remain.
- W06: Occupied a port inside the isolated managed container with a disposable75s socket helper.
  Configuring that port produced observederror/effective_portnull and compatibility503; restoring
  the previous free port recovered without restarting the container. Final built-image browser flow
  repeated on38082->38083, including logs/download. Stopped/restarted isolated PostgreSQL: supervisor
  reacquired its advisory lock and restored the persisted listener. Final focused59 tests passed,
  frontend48 passed, lint/format232 files clean, mypy136 and7boundaries/67route guards clean.
- W07: Backed up current user DB (537013bytes) to ignored local acceptance directory; verified1
  indexed document before upgrade. Applying additive0012 via one-shot migrate, then updating only
  API-control/data, web and new dedicatedmcp-service. No Docker socket, no Agent code edits, no
  knowledge/source-volume removal or key regeneration. Final full regression concurrently uses
  separate fixture DB, never the user's DB. User deployment verification follows.
- W08 FINAL: Additive0012 migration applied successfully; bootstrap was a no-op. User service now
  observedrunning8081/generation1=observed1. Actual SDK searched bothweb8080/mcp anddirect8081/mcp,
  returning5hits each from the existing KB. Oldapi-data embedded/mcp returned503 with a valid key;
  curatedlogs include successful tool calls and contain no key. Temporary scoped verification key
  revoked in finally. Existingdocumentcount1/indexed1 unchanged. Agent worktree remains untouched.
- W09: Final full backend969 passed,0 assertion failures/skips,2 unchanged deferredPDF/OCR collection
  errors,4 existing Alembic warnings,288.78s,coverage85.97% (80% gate unchanged). Finalfocused59passed.
  Frontend48 tests/typecheck/API-drift pass, Dockerbackend/webbuild pass, OpenAPI matches backend,
  Ruff232files/mypy136 clean,7boundarycontracts and67routeguard audit pass. No commit/push.
- W10 CLEANUP: Ownership labels checked before removing only3mcpui-20260917 integration containers
  and13cairn-mcpui-acceptance Compose containers/network. No volume deletion; test evidence/private
  DB backup retained locally. The user's12Cairn services, including new permanentmcp-service, remain
  running, with healthyAPI/web/stores/supervisor; oldM08 services untouched. No Agent repo diff,
  no active test/subagent process. Working tree changes remain uncommitted ondev.

## Final usage and limits

Administrator page: `http://127.0.0.1:8080/mcp-service/nav`, sidebar **MCP服务**. Port8081..8090 is
deployment-published; the selected listener is currently8081. Page starts/stops/restarts only MCP,
shows effective state/heartbeat/errors, saves configuration and shows filtered/followed/downloadable
logs. This is not Docker shell access. Unknown heartbeat is not a stopped-service claim. Protocol
client/key behavior remains document31; Agent integration is still deferred.

Logs are bounded curated metadata (2000persisted,1000queued,1000download cap), not unlimited raw
container stdout. Queue pressure can drop request logs. Listener running is distinct from model/KB
readiness. Direct ports useHTTP and defaultloopback publication; remoteTLS/proxy setup remains an
operator responsibility. The full test command is still nonzero solely for deferredPDF/OCR imports.
Private database backup and screenshots/reports stay under ignoreddata/acceptance-mcpui-20260917.

- W01: Read current SettingsView, platform settings/routes, MCP lifespan/ASGI adapter, Compose and
  operations documentation. No source/config/runtime changes made. Proposed management architecture
  distinguishes transport service control from whole-container lifecycle and published host ports.

## Approved implementation contract

Single deployment-scoped managed service, controlled only by the owning workspace's platform admin.
PostgreSQL is the authenticated private command channel: a versioned desired-state row, observed
heartbeat/state, and bounded structured logs. No new unauthenticated internal management HTTP API,
Docker socket or arbitrary command runner. A dedicated supervisor process holds an advisory leader
lock, reconciles changes and runs a single MCP-only listener on one allowed port8081..8090.
Internal stable gateway8099 forwards `/mcp` to the selected listener. nginx8080/mcp uses that gateway.
In Compose-managed mode, old API-data/all embedded MCP endpoints refuse requests, preventing bypass.

Admin API (all responses JSON except log download):
- GET `/v1/mcp-service` -> `{config, generation, observed_generation, desired_state, state,
  effective_port, started_at, heartbeat_at, last_error, allowed_ports, managed}`.
- PUT `/v1/mcp-service/config` body `{expected_generation, config:{port,auto_start,allowed_hosts,
  allowed_origins,request_timeout_s,max_request_bytes}}`; returns same snapshot. Config changes
  while running reconcile via bounded stop/start; failures show explicit error, not silent success.
- POST `/v1/mcp-service/actions` body `{expected_generation,action:start|stop|restart}` -> snapshot.
- GET `/v1/mcp-service/logs?after_id=0&limit=100&level=INFO|WARNING|ERROR&since=<ISO>` -> `{items}`.
  Log items `{id,at,level,event,detail,request_id,status,duration_ms}`; detail is a safe fixed message.
- GET `/v1/mcp-service/logs/download` -> capped JSONL attachment using same optional filters.
Admin requests have session/CSRF/permission guards and audit. Persist logs max2000 entries per service;
read max200/response; download max1000. No source/prompt/key/headers/raw exceptions in records.
Unreachable heartbeat older10s -> unknown, not stopped. State vocabulary stopped/starting/running/
stopping/error/unknown; pending generation disables conflicting UI operations. POST/PUT409 on stale
generation or pending operation. Defaultauto_start=true retains today's working MCP behavior.

## Execution tasks

- [x] Parent: core DTO/config, durable platform-owned state/log store, migration0012, guarded API,
  TDD concurrency/permission/state/log bounds. No user DB fixtures.
- [x] Sole sol/high worker: Vue service page, typed API, navigation, page tests using above contract.
  Existing ElementPlus/i18n style, admin-only; poll2s with abort cleanup; settings/save/action states,
  safe plain-text logs/filter/follow/download and connection snippets without secrets.
- [x] Parent: supervisor/socket binding/gateway, fresh actual health probe before running ack,
  reconcile/lease/shutdown/failure and log collection; real socket tests.
- [x] Parent: Compose managed container+range mapping, old-route disable; integration/browser actual
  start-stop-restart/port change/compat route/logs; final regression, local docs and safe deployment.
