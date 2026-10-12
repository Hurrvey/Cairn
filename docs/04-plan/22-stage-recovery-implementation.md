# Failed-stage recovery implementation

Date: 2026-09-14. Status: bounded lifecycle slice4 independently ACCEPTED by coordinator.

Parent independently passed45 tests (38.74s), including actual TEI recovery/query, prior lifecycle,
scoped HTTP retry and migration round-trip. Final backend780 passed,2 known PDF/OCR collection
errors,no assertion failures/skips,coverage85.96% (188.00s); full command correctly exits1.
Lint,mypy111,imports6,route50/8 and normal format scope plus migration0011 passed. Worker is closed;
ledger L98-L99 records acceptance. Historical design/RED/GREEN below is retained. No overall E2E
acceptance: public retrieval/UI/deployment and essential cleanup/retirement remain open.

Deployment requires matching API/workers with migration0011 and draining old binaries: pre-upgrade
workers do not enforce recovery generation. Missing task generation means0 only; later retries
must never be processed by old workers. Legacy failures without a proven resumepoint remain
fail-closed. This verification touched disposable databases only, not application data.

## Objective and boundaries

Implement contributor-initiated retry of the exact durable ingestion run that failed. A retry keeps
the document's source revision and index version, starts at the failed stage, reuses only committed
prerequisites, and fences all tasks from earlier recovery generations. The supported stages are
`parse`, `chunk`, `embed`, and `index`, including the special case where indexing committed and
activated successfully but runtime publication failed afterward.

This slice does not implement PDF/OCR, general artifact cleanup, delayed index retirement, public
retrieval, UI work, cache-generation redesign, or arbitrary recovery of failed/retired rebuild
versions. It preserves manual chunk edits and the existing parent/non-vector semantics by resuming
the same run through the existing stage handlers and commit paths.

## Current-state findings

- `CatalogService.retry_document` increments `document.revision`, selects whichever index is current,
  creates a new run, and always enqueues parse. That destroys the failed run identity and repeats
  completed work.
- `CatalogIngestionFacade.record_failure` changes both the document and run to `failed`, but does not
  persist the failed stage or the run's last committed state.
- Stage task payloads already carry `revision` and `index_version`; task lease ownership fences a
  particular task attempt. They do not fence a later retry successor from an older still-running
  stage task for the same run.
- Parse, chunk, and embedding artifacts are checksummed envelopes whose manifests contain document,
  source revision, index version, and source-content hash. Stage handlers already decode and verify
  these identities outside catalog transactions.
- The index handler may commit `run.state = indexed`, activate the version, and then fail while
  publishing runtime state. Recovery must retain that committed `indexed` state and only republish.

## Considered approaches

### A. Retry the exhausted task row

Resetting the failed task with `TaskService.retry` is small, but cannot safely distinguish the last
committed run state after `record_failure` overwrote it. It also does not give the run a new immutable
recovery identity, so an old attempt and a user retry are not fenced from one another. Rejected.

### B. Create a new source revision and reuse prior artifacts

This resembles the current retry path and could opportunistically reuse parse/embedding artifacts.
It nevertheless changes source identity without a source change, risks selecting a different current
index/version snapshot, and makes artifact and edit ownership harder to prove. Rejected.

### C. Restore the same run and enqueue a generation-fenced stage successor

Persist the failed stage, previous committed state, and monotonically increasing recovery generation
on `document_ingestion`. In one catalog transaction, lock and validate the exact failed run, increment
its generation, restore its committed state, clear public failure fields, and enqueue one successor
for the failed stage carrying the unchanged revision/index and new generation. All stage loads and
commits require the task generation to equal the run generation. Recommended.

## Durable protocol

Migration `0011_stage_recovery` adds these columns to `document_ingestion`:

- `failed_stage`, nullable and constrained to `parse`, `chunk`, `embed`, or `index`.
- `previous_committed_state`, nullable and constrained to `registered`, `parsed`, `chunked`,
  `embedded`, or `indexed`.
- `recovery_generation`, non-null integer with server default `0` and a nonnegative check.

The fields describe the same immutable run key `(document_id, revision, index_version)`. They do not
create a second run or alter version snapshots. Legacy rows receive generation zero and null recovery
metadata; a legacy `failed` row therefore cannot be presented as resumable.

Every ordinary document stage task carries `recovery_generation`, starting at zero. For compatibility with
already-enqueued pre-migration tasks, a missing payload field is interpreted as zero. `load_run`,
`_lock_current`, and every stage mutation compare the payload generation to the locked run generation.
After a retry increments the run generation, an older task can neither load prerequisites nor commit,
enqueue a successor, update counters, or publish resumed work.

Manual `chunk.reembed` tasks remain deliberately independent of ingestion recovery generation. Their
immutable ownership is revision, index version, chunk content hash, and edit generation. This allows a
valid edit accepted after index commit but before a publication failure to converge after publication
recovery; tying it to the run generation would silently strand that user edit.

`record_failure` derives the previous committed state from the locked run before changing it. The
state is authoritative even when the visible failing operation was an earlier stage: a crash after a
stage commit but before task acknowledgement or queue notification may already have advanced the run
to `parsed`, `chunked`, or `embedded`. Recovery therefore dispatches the next uncommitted stage from
the durable state; replaying the same stage is also safe only where its handler's idempotent branch
durably guarantees the successor already exists.

| Durable previous state at terminal failure | Recovery queue/kind |
| --- | --- | --- |
| `registered` | `parse` / `document.parse` |
| `parsed` | `chunk` / `document.chunk` |
| `chunked` | `embed` / `document.embed` |
| `embedded` | `index` / `document.index` |
| `indexed` after runtime publication failure | `index` / `document.index` |

`failed_stage` retains the operation that reported failure for diagnosis, while
`previous_committed_state` determines dispatch. Only a terminal task failure changes the run to
`failed` and writes resumable metadata. Transient attempt failures may update sanitized document
diagnostics but do not claim that a contributor retry is available yet. Failure recording remains
under the owned task lease and requires the task payload generation to equal the run generation, so
an old-generation failure cannot overwrite a resumed state or diagnostics.

## Transactional retry entrypoint

`CatalogService.retry_document` continues to rely on the existing router authorization, but performs
its own tenant and lifecycle checks while holding catalog row locks. It selects failed ingestion rows
matching the document's current revision and content hash, then accepts exactly one whose version is
still the KB's active or building version. Zero candidates fail closed; multiple candidates are an
explicit ambiguity conflict. It never chooses the latest row or retargets merely because an index is
currently set. Persisting a separate document failure-version pointer is unnecessary for this bounded
slice because exact candidate cardinality provides the same safety without another mutable identity.

The retry is allowed only when all of the following remain true:

- the document exists, is not deleted/deleting, belongs to the actor's workspace, and is `failed`;
- the selected run matches the document ID, workspace/KB, revision, and content hash;
- the run has a known failed stage and a recognized previous committed state; dispatch follows the
  committed state and does not require a strict one-to-one match with the reporting stage;
- the exact run index version still exists, retains its frozen snapshot, is `active` or `building`,
  and is still the KB's corresponding active/building version;
- the KB is not deleting or archived.

Inside the same transaction, retry increments `recovery_generation`, restores run/document state to
`previous_committed_state`, clears failure diagnostics, and enqueues exactly one failed-stage task.
The task payload contains `revision`, `index_version`, and `recovery_generation`; its dedupe key also
contains all three identities plus the stage kind. The transaction is the sole state transition, so
a double click cannot produce parallel recoveries: the first request moves the document out of
`failed`, while a racing request sees the locked new state and returns conflict without enqueueing.
Queue notification remains best-effort after commit.

The endpoint description changes from “re-runs from the start” to “resumes the failed stage.” The
service, not the URL's `kb_id` text, remains responsible for resource identity; existing authorization
continues to establish contributor permission for that KB.

## Artifact validation and fallback

Retry performs no object-storage or network I/O while catalog locks are held. It restores only the
catalog's last committed state and dispatches that state's next stage. The resumed handler reads its
own prerequisites after the transaction and applies the existing checksummed envelope plus document,
revision, index-version, source-hash, artifact-version, embedding-binding, dimension, normalization,
and chunk/vector payload checks appropriate to that stage.

A missing, corrupt, mismatched, or unsupported prerequisite fails safely using the existing typed
pipeline/storage error mapping. Decode and identity failures remain `INGESTION_ARTIFACT_INVALID`;
`ObjectNotFound`, `ObjectTooLarge`, and other `CairnError` subclasses retain their own sanitized codes
unless implementation tests justify an explicit mapping. Recovery does not copy a different pointer,
infer a later state, or forge a manifest. Automatic fallback is intentionally excluded because it
would require another durable transition and could repeat provider work unexpectedly. The existing embedding content-hash
cache may avoid a second provider call when embedding completed but `commit_embedded` failed; this is
normal deterministic cache reuse, not trust in the orphaned uncommitted manifest or pointer.

Legacy failed runs whose `failed_stage` or `previous_committed_state` is unknown return conflict with
an explicit remediation message: replace/re-upload the source to create a normal new source revision,
or rebuild through the separately authorized lifecycle path. This slice does not silently pretend the
legacy row is resumable and does not add an automatic full-parse fallback. Missing generation remains
compatible as generation zero only before the first retry; once the run generation increments, every
missing/zero-generation task is stale. Legacy versions without a valid frozen snapshot retain the
existing snapshot restriction and cannot be made retryable by this protocol.

## Publication-only recovery

When `record_failure` observes `run.state == indexed` after runtime publication failed, it records
`previous_committed_state = indexed`. Retry restores `indexed` and enqueues `document.index`. The
existing index handler's already-indexed branch checks that this exact version is active, republishes
runtime state, and confirms publication. It does not resolve the embedding provider, read embedding
artifacts, upsert vectors, alter point counters, or reactivate another version. Existing activation
and version-generation fences remain authoritative.

## Test-first implementation sequence

After parent approval and exclusive disposable database access:

1. Observe the parent-owned four-stage acceptance RED without editing it.
2. Add RED migration/model/repository tests for recovery metadata, constraints, exact-run selection,
   generation increments, and legacy null metadata.
3. Add RED unit tests for generation propagation and stale-generation rejection at load, commit,
   failure-recording, and manual-edit interaction seams.
4. Add RED integration tests in `tests/integration/test_stage_recovery.py` for terminal failures at
   parse, chunk, embed, index mutation, and runtime publication; assert unchanged revision/version,
   direct stage queue selection, no duplicate parser/provider calls, stable counters/vectors, and
   eventual indexing/publication.
5. Add concurrency tests proving double-click serialization and proving an old task cannot commit or
   enqueue after a recovery generation is issued.
6. Add safety tests for missing/corrupt/wrong-identity artifacts, source replacement, manual edits,
   deleted documents, retired/failed versions, and unknown legacy failure metadata.
7. Implement the minimal migration, models, repository/facade/service changes, then pipeline identity
   propagation needed to turn each RED case green.
8. Run only the agreed targeted suites, then Ruff and mypy through Python. Preserve the known global
   baseline of 761 passes plus two PDF/OCR collection errors; do not run a duplicate full suite.

Planned production file ownership is limited to `migrations/versions/0011_stage_recovery.py`,
`src/cairn/catalog/models.py`, `src/cairn/catalog/repository.py`, `src/cairn/catalog/service.py`,
`src/cairn/catalog/ingestion.py`, `src/cairn/catalog/router.py`, and
`src/cairn/ingestion/pipeline.py`. `src/cairn/tasks/service.py` changes only if the existing enqueue
and owned-task seams prove insufficient. Planned owned tests are
`tests/integration/test_stage_recovery.py` and focused unit/migration tests adjacent to existing
catalog and ingestion coverage.

Parent-owned files `tests/integration/test_stage_recovery_acceptance.py`,
`tests/integration/test_live_model_pipeline.py`, and `docs/04-plan/15-execution-ledger.md` remain
untouched. No commit, revert, clean, service teardown, or PDF/OCR change is part of this work.

## Pre-code journal

- Confirmed baseline `45ab477` on `dev` and preserved the existing dirty documentation files.
- Read documents 21 and 16; both identify durable failed-stage recovery as the next bounded slice.
- Traced document/run state, stage commits, task lease ownership, task dedupe, manifest checksums,
  rebuild version ownership, manual edit fencing, and activation/publication retry behavior.
- Incorporated the parent's four-stage RED contract: same source revision and index version, direct
  failed-stage queue, no repeated parser/provider invocation, and eventual indexed state.
- Parent approved the design direction and granted the exclusive database window. Parent RED is four
  expected failures in 5.95 seconds with failure injection at parser, `commit_chunked`,
  `commit_embedded`, and vector upsert; its source revision advances from initial 1 to failed run 2.
- Clarified that recovery generation fences document-stage loading, commits, and failure recording.
  Manual re-embed remains fenced by its independent edit identity so a publication-only recovery does
  not strand an accepted edit. Embed recovery may use the existing content-hash cache after a
  pre-commit crash.
- Parent approved approach C without another review pause. Added fail-closed candidate cardinality
  instead of latest-run guessing; durable committed state now controls the recovery queue even when a
  stage committed before acknowledgement/notification; old-generation failure recording is fenced.
- Corrected artifact error documentation: typed object-store errors retain their existing codes, while
  malformed or wrong-identity manifests use `INGESTION_ARTIFACT_INVALID`.
- Selected approach C because it adds one run-scoped generation and durable resumepoint without a
  new recovery orchestrator, storage I/O under catalog locks, or changes outside slice 4.
- Parent review received; entering TDD with the parent-owned acceptance file read-only.
- TDD RED: parent-owned four-stage acceptance reproduced as 4 failed in 6.76 seconds; each failure
  showed the old retry incrementing revision 1 to 2. Owned recovery tests reproduced 4 failed in
  7.73 seconds from missing durable columns/generation and absent publication-stage dispatch.
- Implemented migration 0011, run recovery metadata, exact failed-run cardinality, transactional
  same-run successor enqueue, generation-aware stage/manual-edit ownership, and publication resume.
- First GREEN: owned `tests/integration/test_stage_recovery.py` passed 4 tests in 5.33 seconds against
  the dedicated PostgreSQL/Redis services; only the existing Alembic path-separator warning appeared.
- Parent added a repeated terminal-failure RED. It reproduced as 1 failed and 4 passed in 8.00 seconds:
  the second record attempted invalid `previous_committed_state = failed`. `record_failure` now keeps
  the first terminal resumepoint when the run is already failed.
- Bound retry to the URL knowledge base via `expected_kb_id`; added a real HTTP regression using an
  API key scoped only to the path KB. Broader same-pattern catalog endpoint review remains a follow-up,
  not part of slice 4.
- Reordered retry locks to document -> failed run rows -> KB -> index version, matching stage commit
  order. Retry cancels only ready ingestion-stage tasks for the exact document/revision/index/current
  generation; running tasks are never cancelled under catalog locks and remain generation-fenced.
- Combined owned and parent recovery suites passed 12 tests in 10.82 seconds. Adjacent manual chunk
  reembedding, acceptance, unit identity, and parse-reuse suites passed 33 tests in 18.25 seconds.
- Broader affected ingestion, lifecycle, catalog, task queue/fencing, and pipeline unit suites passed
  98 tests in 42.97 seconds.
- Added and passed bounded safety coverage for missing, corrupt, and wrong-identity committed
  prerequisites without provider/downstream index work; legacy unknown resumepoints; failed/retired
  versions; post-commit stale ready successors; stale-generation commit/failure recording; true
  concurrent double-click retry; and same-workspace cross-KB HTTP path confusion. Combined owned and
  parent recovery suites now pass 18 tests in 14.44 seconds.
- Parent-owned real TEI markdown and semantic recovery tests passed 2 tests in 8.74 seconds against
  `http://127.0.0.1:38742` and the pinned local tokenizer, including terminal index failure, same-run
  contributor retry, final indexing, and retrievable Saturn content.
- Parent added a publication-callback manual-edit regression. RED reproduced as 1 failed and 5 passed
  in 8.21 seconds: the valid pending edit was skipped solely because run recovery generation advanced.
  Removed only that coupling; revision/index/content-hash/edit-generation ownership remains intact.
- Final focused recovery/manual-edit batch passed 52 tests in 33.59 seconds, including the parent
  publication-callback edit case. Final Ruff passed for every touched Python file; mypy reported no
  issues in the seven touched source modules.
- Migration compatibility test passed 1 test in 3.53 seconds. A direct Alembic downgrade/upgrade
  command was attempted but configuration validation rejected it before execution because the shell
  lacked `CAIRN_REDIS_URL` and `CAIRN_MASTER_KEY`; no schema change occurred from that attempt. Normal
  integration fixtures repeatedly upgraded migration 0011 successfully during all focused runs.
- Released the dedicated PostgreSQL/Redis test window to the parent after final focused validation;
  this contributor will run no further database or service commands.
