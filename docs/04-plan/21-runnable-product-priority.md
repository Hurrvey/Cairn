# Runnable product checkpoint and priority

Date: 2026-09-14. Status: user-directed priority; bounded failed-stage recovery now accepted.

September15 update: first public retrieval is independently accepted in document24/ledger L113
(138 focused,856 backend passing with2 PDF/OCR collection errors,85.20% coverage). It is now an
authenticated HTTP path, not only direct-driver testing. Next close runtime-cache refresh and
permission/deletion release gaps while wiring model/storage setup, upload/UI and deployment.
The original source-inspection inventory below is historical and superseded where noted.

Update: document22/ledger L99 accepts recovery with780 passing backend tests,2 known PDF/OCR
collection errors and85.96% coverage. This removes the next bounded ingestion blocker. Proceed to
public retrieval (document23 preflight); keep essential cleanup/security/deployment gaps as release
gates. The project-wide inspection below is historical source evidence, not a claim those other
gaps have already been implemented.

## User direction

Prioritize a complete runnable main product flow using supported formats. PDF/OCR and advanced
features may follow later. Keep one implementation subagent at a time (gpt-5.6-sol/high), coordinator
acceptance and durable step records. Do not equate this reduced first-release scope with completion
of the original PDF quality requirements or the full project.

## Verified current position

The project remains in Phase2 (knowledge core), not an accepted end-user product release. The
original release objective in document01 is deploy -> create KB -> upload -> index -> retrieve via
authenticated API. Recent work accepted backend lifecycle slices1-3, not that whole journey.

- Foundation, identity/authz, tasks, catalog, local objects/pgvector, text/Office parsing, language,
  chunking and embedding have implemented code and service-backed tests.
- Version snapshots, bounded reindex fan-out, build cleanup and manual chunk reembedding have
  coordinator acceptance. Real TEI tests exercise indexing and vector-driver queries directly.
- No `cairn.retrieval` package exists. `apps/api/main.py` mounts no retrieval data-plane router.
  Direct vector-driver queries in tests are not a public, authorized retrieval service.
- Web routes currently cover login/dashboard/users/API keys/audit/settings. No knowledge-base
  wizard, document upload/list, chunk browser or retrieval console is wired in the frontend.
- The document API registers bytes already stored elsewhere. It is not a complete browser upload
  entrypoint; model/provider management also needs user-facing setup rather than test fixtures.
- Compose files/Dockerfiles/install script exist, but the current Compose file has no ingestion
  worker services or model-server profile implementation. Recent dedicated service tests do not
  verify a fresh full Compose deployment, writable shared object storage or tokenizer provisioning.
- M10 has model/provider registry plus bounded TEI/Infinity embedding adapters; this is not the
  complete credential-backed model gateway. M11 pipelines, M12 sandbox, M13 MCP and M14 evaluation
  are later modules, not implemented by the ingestion work.
- Latest full backend evidence remains L87:761 passed,2 PDF/OCR collection errors,85.18% coverage,
  no assertion failures/skips. Full command is nonzero. No tests were run in this status-only update.
- Frontend27-test/typecheck/build evidence is the historical September7 checkpoint, not a new run.

## Main-flow priorities

1. Complete bounded failed-stage recovery and essential deletion/retirement/cache consistency so
   ordinary failure/retry/delete does not strand user data or show stale active content. Keep this
   bounded; it must not indefinitely postpone retrieval and UI integration.
2. Implement the Phase2 retrieval API through public interfaces: authentication/KB authorization,
   active-version/model coherence, retrieval results with source citations and stable error behavior.
   Start with the supported local/pgvector+TEI configuration; do not claim unimplemented advanced
   retrieval features as working.
3. Wire the management-to-retrieval user journey: model/storage setup, KB creation, real file upload,
   document progress/errors/retry, chunk browsing/editing and retrieval testing in the web UI.
4. Complete an actually runnable deployment preset with migrations, API/web, all needed queues,
   model/tokenizer settings and shared persistent object storage. Exercise clean installation and
   restart against disposable resources; do not use production data for destructive tests.
5. Accept the reduced-format release via browser and external API journeys, including permission
   denial, worker restart, replacement/rebuild and deletion. Publish exact supported/unsupported
   capability boundaries and reproducible startup instructions.

PDF/OCR, the genuine30-document PDF evaluation, crawling/connectors, MCP, DAG editing, production
custom-code sandbox and platform-level evaluation remain explicitly deferred, not marked complete.
The existing PDF/OCR draft test collection errors require an explicit test-suite organization decision
before release: keep full-suite failure visible; never silently skip them merely to announce green CI.

## Acceptance definition

A new operator can deploy without hand-populating ORM rows or using test helper code; log in and
configure the supported model/storage; upload a supported document; see indexing complete; retrieve
cited content in the UI and through an authorized API key. Edits/replacements/retries/deletes converge,
and restarting workers preserves durable work. Unsupported PDF/OCR must be clearly rejected or
disabled in the user-facing capability list. No release claim before these journeys actually pass.
