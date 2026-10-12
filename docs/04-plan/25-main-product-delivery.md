# Five-part main product delivery

Date: 2026-09-15. Status: ALL FIVE REQUESTED ITEMS ACCEPTED FOR SUPPORTED FORMATS.
The full original PDF/OCR-inclusive knowledge-base E2E gate remains OPEN.

## User-approved objective and scope

Complete (1) KB/document upload UI, (2) model/object-storage configuration UI, (3) Compose workers,
model configuration and persistent storage, (4) automatic Redis ACTIVE runtime refresh, and (5)
general deletion/purge plus retired-index removal. PDF/OCR and the real30-PDF evaluation are deferred
until these are complete; they must remain explicitly unsupported, not falsely implemented.

Use the existing Vue/ElementPlus UI, HTTP conventions, local ObjectStore, pgvector, TEI/Infinity
and durable task worker architecture. This is not a new platform redesign. Runtime/model cache
projections must never expose secrets; configuration writers keep tenant/platform permission checks.
One sol/high subagent at a time, coordinator independently reviews/tests and records evidence.

## Ordered packages and ownership

| Package | Required deliverable | Acceptance |
| --- | --- | --- |
| A | Periodic bounded runtime refresh, coherent publication/invalidation, document/KB purge and delayed index.drop handlers | Real PG/Redis/objects/vector tests for cache loss/idle, deletion races, retries and active/building drop fences |
| B | Model/provider management + usable model listing, storage management, real bounded upload/download path, nested KB/document authorization | Actual authorized HTTP setup/upload with parser capability rejection and no traversal/secret exposure |
| C | KB wizard/list/detail, document upload/list/progress/retry/delete, chunks/edit, retrieval test, model/storage settings pages | UI component tests/typecheck/build and browser journey; no fake backend or hidden unsupported options |
| D | Compose API/data/web + all workers, configured model/tokenizer, writable shared persistent objects, startup bootstrap/config documentation | Fresh isolated deployment, upload/index/query/restart/delete; actual TEI inference and persisted data |
| E | Final combined regression and honest supported-format release record | End-user browser + API key flow, permission denial, edits/rebuild/retry and cleanup; known deferred PDF test gate explicitly reported |

Do not mark the overall objective complete after only a backend package. Parent may prepare disjoint
acceptance/design while sole worker implements. Existing dirty recovery/retrieval changes are
preserved; no commits/pushes unless separately requested.

## Design decisions

- Retain current architecture rather than introduce a new broker, frontend framework or blob service.
- Persistent local object storage is the supported first deployment path; the configuration UI
  must not imply an unimplemented S3 driver exists. Network model calls retain SSRF/timeout guards.
- Control/maintenance process publishes and repairs Redis read models; retrieval does not hydrate
  catalog ORM on cache misses. Serialize publication with catalog version/deletion changes or use
  an equivalent proven monotonic mechanism; periodic refresh alone is not a race fix.
- Workers consume durable tasks and bootstrap periodic maintenance even after a fresh installation;
  a registered handler without a scheduler is not automatic refresh.
- Purge removes external data before discarding durable cleanup identity. Namespace retirement must
  check active/building pointers and retention while ownership is held; never drop a live namespace.
- Browser uploads are bounded, server-assigned scoped object keys; no arbitrary filesystem path
  provided by content contributors. Poll actual document/task state with cancellation on navigation.
- Model/runtime setup must work across control/data workers without copying dynamic provider IDs
  manually into multiple processes. No production claim based on hand-populated test fixtures.

## Durable checkpoints

P11 FINAL: Coordinator accepted all packagesA-E. Document29 is the final evidence record.
Final independent full backend893 passed,0 assertion failures/skips,2 deferred PDF/OCR collection
errors,coverage85.82%;43 focused acceptance tests and36 frontend tests passed. Mypy127,7 boundary
contracts,61-route authorization audit,213-file Ruff lint/format, backend OpenAPI equality and UI
API-drift/typecheck/build passed. Fresh final-image browser setup/upload/index/query passed in one
run; real lifecycle, retry, manual edit, cache repair, whole-stack persistence, document/KB purge and
accelerated old-index retirement were independently checked. Both sol/high workers are closed.
No commit/push, no PDF/OCR work, no unsupported S3/custom/MCP claims. LedgerL127 confirms owned
test containers/network removed, persistent demo volumes/model/image/evidence artifacts retained.

P10: Parent's standalone-worker metadata fix passed5/5 fresh-process tests. Rebuilt all workers;
the original failed document recovered through the normal retry task without re-upload, indexed
and activated version1. Real Chromium then displayed document chunks and the Saturn query result.
Real-ingress lifecycle script passed source download, duplicate reuse, incremental TXT indexing,
version1->2 rebuild, scoped bearer query through api-data, forbidden source access by query-only
key, key revocation and document deletion. Redis FLUSHDB on the isolated Compose Redis followed by
read-only verification recovered KB/provider projections automatically; no manual config publish.
Retired version1 namespace existed before its24h deadline. In this disposable DB only, parent moved
that version and its exact queued index.drop task deadline into the past: worker dropped version1,
kept active2, task done. This is accelerated retention acceptance, not a24h wall-clock wait.
Fresh user first-login/change-password flow now passed in rebuilt UI. A kb:read-only user's mobile
page displayed the document but no upload/delete/rebuild/model-admin affordances, with no horizontal
viewport overflow. Screenshots retained under ignored data/acceptance-product-20260915/browser.

P09: Browser now completed provider/model test/storage/KB setup and actual parse/chunk/TEI embed.
Index failed with sanitized INGESTION_STAGE_ERROR. Parent stopped only the disposable index worker,
requested normal HTTP retry and ran one isolated diagnostic worker to reveal the real exception:
NoReferencedTableError on knowledge_base.owner_user_id. Standalone worker never imported identity
ORM metadata, while integration fixture imports masked this. Five fresh-process queue bootstrap
tests all reproduced RED. Parent loads identity metadata at worker composition root (not across
domain modules) and reruns tests before rebuilding. Existing source/manifests/vectors retained to
verify recovery rather than erase/re-upload the failed document. Diagnostic run container removed.

P08: First real browser provider creation was rejected by CSRF. Live API logs showed Origin
http://127.0.0.1:38880 but upstream Host127.0.0.1: nginx `$host` had discarded the exposed port.
Parent changed proxy Host to `$http_host`, preserving the browser authority rather than weakening
the backend Origin/CSRF guard. This failure was invisible to direct httpx tests without Origin.
Rebuild and repeat real browser flow; no successful upload/query claim yet. Generated OpenAPI and
TypeScript now include new endpoints; UI DTOs derive from generated schemas, typecheck/api-drift pass.

P07: Parent verified all five worker processes running in the separate Compose project, and a
fresh pinned model download (not merely cached SHA checks) succeeded. Browser login and live DOM
inspection worked; first-run completion exposed a missing LoginView transition. RED routing test
then GREEN completion event/navigation, with dialog regression asserting the event. Added RED/GREEN
document polling regression for paginated lists and refresh cancellation, dashboard links with
honest deferred PDF/OCR notice, and scoped permission affordances (never treating admin role as
blanket KB access). Full frontend34 tests/typecheck passed before the final permission addition;
final run follows. New product_lifecycle_smoke.py will check real incremental upload, download,
deduplication, reindex, bearer retrieval, deletion and restart persistence; not yet executed because
packageB APIs are still under construction. Parent lowered ingress body limit to51MiB and requested
actual ASGI streaming limit, since handler-only file reads cannot bound pre-handler multipart spooling.

P06: Actual separate Compose startup exposed a packaging-only migration failure: installed CLI
resolved migrations under /opt/venv/lib/python3.12 instead of /app. Parent set immutable container
PYTHONPATH=/app/src:/app (source+scripts already copied) and is rebuilding/retesting. No application
DB affected. Separate product acceptance stores/model started successfully with persistent volumes;
integration test DB remains isolated and owned by packageB agent. Initial UI build+31 tests pass.

PackageB design approved (document28): strict TEI/Infinity admin registration and safe setup choices,
bounded multipart upload/authenticated download, nested KB scope, dynamic provider runtime projection
with periodic repair and data-plane-only consumption. Parent requested no reverse modelgw->catalog
ORM dependency, scoped provider projections and race-safe publication/cleanup. Existing UI and
deployment tests remain parent-owned. Worker Tesla is sole active agent.

PackageA bounded independent acceptance: parent14 tests passed in11.68s, including actual Redis
eviction repair, deleted content/object cleanup and one-billion sparse high-water namespace case.
Worker Hume explicitly closed. Final combined backend/deployment regression remains packageE;
items4/5 are implemented but whole five-part release is not yet accepted. Proceed packageB.

P01: Coordinator read current ledger, source, worker registration, local object deletion and frontend
dependencies. Baseline is dev45ab477 plus accepted dirty recovery/retrieval changes. No AGENTS.md
found. User explicitly approved all five deliverables; record final results here as packages land.

P02: Parent prepared packageB/C interface contract in document27 while sole agent owns packageA.
Started deployment changes in disjoint files: .dockerignore excludes secrets/weights/node_modules;
Dockerfile uses locked dependency sync with UV_PROJECT_ENVIRONMENT=/opt/venv instead of unlocked
fallback and includes README needed for wheel build. Initial real Docker backend build succeeded
as cairn-mainproduct:acceptance (Python3.12.14). Pinned observed Python/uv image digests after build.
Updated Compose to include all five workers and persistent object volume, shared tokenizer/model
read-only mount, optional pinned TEI local-model profile and loopback ingress. This is NOT yet
whole-stack acceptance; dynamic model setup and frontend remain pending. First Compose config read
correctly failed because no .env exists; added optional CAIRN_ENV_FILE for isolated validation.
Created ignored disposable compose.env with dedicated project DB/port38880, not application secrets.

P03: Compose syntax validation passed with the ignored isolated env file (no application .env
created). Web Docker build passed existing API-drift/typecheck/Vite build; existing large-bundle
warning remains. Pinned observed Node/nginx image digests. Added cross-platform init_env.py that
creates fresh secrets without printing them and refuses to overwrite an existing file; both paths
verified under ignored acceptance data. Model preparation uses existing httpx against fixed
revision URLs and verifies SHA256 before replacing assets. Script lint passes. Full deployment/
browser smoke follows new management and UI delivery, not merely these old-UI build checks.

P04: Parent implemented initial Vue KB list/create/detail, document upload/progress/retry/delete/
download, chunk browser/editor, retrieval console and model/storage configuration views using the
concrete packageB contract in document27. Existing visual tokens/ElementPlus patterns retained;
new UI renders untrusted content as text. Added multipart API-client RED (FormData was JSON-encoded
as `{}`), then fixed multipart request handling while retaining CSRF. New routes/sidebar links and
Chinese/English navigation added. Frontend typecheck passed. These pages are NOT accepted until
packageB endpoints and real browser flow pass; handwritten client contracts will be checked against
regenerated OpenAPI after backend delivery.

P05: Parent UI existing+multipart tests passed28; three new page rendering/empty-state tests passed,
and typecheck passed. Formatted only new/touched UI files. Deployment routing correction: base
api-control uses role=all for authenticated browser retrieval; nginx routes bearer retrieval to
api-data and cookie-session retrieval to api-control. Both paths still authenticate/authorize in
the backend; data-only middleware remains machine-key-only. This avoids requiring browser users to
paste an API key into the console and does not mark retrieval public. Whole-stack smoke still pending.
Added pinned model preparation script and verified all retained model asset SHA256 without downloads.
