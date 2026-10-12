# Model-bounded chunking repair

Date: 2026-09-16. Status: FIXED, DEPLOYED AND USER DOCUMENT RECOVERED.

## Evidence and cause

Read-only inspection of the user's running local Compose database found one failed document at
embed with `EMBED_INPUT_TOO_LARGE`. Its immutable version uses default parent-child configuration:
child512, overlap64, parent2048, but the registered MiniLM input limit is256. There are101 searchable
chunks, the largest511 tokens. Retrying resumes the committed chunk manifest and therefore repeats
the deterministic error. Previous acceptance used short text and did not expose this mismatch.
No source text or document title is copied into this log.

## Repair and ownership

1. Sole sol/high worker Kierkegaard owns model-bound DocumentChunker and dedicated unit tests.
   Parent owns pipeline composition, safe recovery, integration acceptance, deployment and this log.
2. Enforce the actual model budget while chunking, including special tokens/normalized text and
   prefixed headings. Preserve all source content/citations; never raise model capacity or truncate
   embeddings. Nonembedded parent context may exceed the embedding limit. Oversized protected tables
   must be split into bounded searchable pieces; custom output remains validated, not reinterpreted.
3. For previously committed oversize chunks, explicit retry must return to the preserved parsed
   artifact under the existing recovery-generation fence, not reuse the bad chunk manifest. Manual
   edits must not be silently reassigned after boundary changes.
4. Reproduce RED, verify focused and real-model tests against separate disposable services only.
   Never run truncating integration fixtures against the user's current `cairn` database.
5. Deploy the fix preserving source/DB volumes, retry the affected document through the normal
   service and verify it indexes. Record what was changed, tests and remaining limitations here.

## Journal

- R01: Read current source and actual database configuration/state using read-only SQL. Confirmed
 512-versus256 root cause and failed embed stage. User Compose is running; old M08 containers are
 unrelated and remain untouched. No user data changed. Dispatched one bounded worker; no second agent.
- R02: Created dedicated `cairn-budget-pg`/`cairn-budget-redis` with label
  `cairn.task=chunk-budget-20260916`, ports39170/38694. Three new integration tests reproduced RED:
  default long content fails embedding; retry resumes bad chunks; edited boundaries have no fence.
  Parent added pipeline model-limit wiring and exact oversize recovery to parsed stage, with manual
  edit/custom-output safeguards. Added a real MiniLM parent-child/default-config long-document case
  to the existing live-model pipeline matrix. No test fixture uses the user database.
- R03: Parent independently passed36 integration/pipeline tests, including actual MiniLM default
  parent-child long input, retry/rebuild/manual reembedding, existing generation fences and a new
  post-retry manual-edit guard at chunk commit. A late edit is retained and repair fails closed.
  New/existing basic chunker tests62 passed. An initial unit command misspelled advanced_chunkers
  and collected zero tests; corrected command evidence is separate. Full regression now running.
- R04: Created a217177-byte PostgreSQL custom-format backup of the user's current database at
  ignored `data/acceptance-budget-20260916/cairn-before-chunk-budget.dump`. User document remains
  revision1, source35761 bytes, no manual edits, intact parsed artifact. Building corrected backend;
  no schema or model-limit changes, no data-volume removal. Actual user recovery follows deployment.
- R05: Corrected image built successfully. Compared SHA256 of all four changed production files
  between image and working tree; exact matches. Recreated only API/control-data and five worker
  containers with `up --no-deps --no-build`; DB, Redis, model and data volumes remained running.
  Reloaded nginx upstream addresses after API recreation. Backends and ingress are healthy.
- R06: Resolved the affected document owner through normal authz principal service, checked
  `kb:write`, then invoked the normal `retry_document` service for the exact failed document only.
  It resumed at parsed state, ran chunk/embed/index, and reached indexed100% with revision1 unchanged.
  One transient EMBED_TIMEOUT retried automatically while CPU inference was busy; recovery completed
  without intervention. Existing historical failed tasks remain as history, not active failures.
- R07: Actual user document now has225 total chunks (100 unembedded parents,125 searchable children).
  Max searchable token count is255, model capacity remains256; pgvector contains125 vectors. Verified
  original source SHA256 and35761-byte size against catalog. Actual TEI-backed retrieval under the
  owner's permissions returned5 hits for this document. No source text printed into tool logs/docs.
- R08: Parent full backend915 passed,0 assertion failures/skips,2 unchanged deferred PDF/OCR collection
  errors,4 existing Alembic warnings,288.25s,coverage85.84% above unchanged80%. Focused36 integration
  tests and62 basic/new chunker tests passed independently. Worker reports114 basic/advanced/bounded
  unit tests passed. Ruff lint/format215 files, mypy127,7 import contracts and61-route auth audit passed.
  Sole worker closed after review. No commit/push; no frontend/schema/migration/model-capacity change.
- R09: Verified exact chunk-budget-20260916 ownership labels, removed only the three new disposable
  test containers. Confirmed all11 user project services remain running (API/web/stores healthy) and
  both old M08 services untouched. `git diff --check` passes. Backup/reports remain ignored locally.

## User action and scope

The running local project already contains this fix and the affected document is already indexed.
Refresh its document page and use retrieval; no delete/re-upload/reinitialize operation is required.
Future long documents are split by their immutable model snapshot budget. Protected table text is
split rather than truncated when necessary, with source offsets preserved. Custom outputs and
manual edit boundary changes fail closed instead of silently discarding user work. The old short-
document acceptance remains historical; this regression adds explicit long-input/model-bound coverage.

Temporary test containers have the chunk-budget-20260916 label and are removed separately. The user's
11 running project containers, persistent volumes and old M08 services must remain running/untouched.
The ignored database backup contains user data: keep it private and do not commit it.
