# Worker ingestion pipeline

September 10 continuation: later lifecycle work and current validation are tracked in
`19-reindex-fanout-implementation.md` and ledger15 L54 onward. The September8 implementation and
pending-acceptance statements below are historical. Do not use this older package's scope to infer
that current advanced chunking or reindex fan-out is absent; equally, it does not accept PDF/OCR,
manual chunk reembedding, general purge, retirement or the whole knowledge-base E2E gate.

Coordinator acceptance (2026-09-08): the repair package was independently rerun together with
Office worker scenarios and actual TEI/MiniLM inference: **48 passed, no skips**. Targeted Ruff,
focused mypy (7 source files) and all 6 import contracts passed. The worker was closed after delivery.
See execution ledger L25 for command scope and report location. The broader lifecycle/PDF/OCR/
advanced-chunking gates remain open; this accepts only the bounded repair described below.

**Status:** worker implementation delivered with focused verification passed on 2026-09-08;
independent coordinator acceptance is pending. Coordinator ledger L21 separately records a passing
actual TEI/PipelineRuntime normal path. This package does not claim project completion, full-KB
reindex fan-out, chunk re-embedding, PDF/OCR, advanced custom/semantic chunking, or the complete M07
lifecycle.

## 2026-09-08 bounded repair continuation log

### R01 - Restart audit and operating constraints

- Read `docs/04-plan/15-execution-ledger.md`, this package document, the applicable
  debugging/TDD/verification workflows, and `git status --short` before changing code.
- Confirmed the worktree is intentionally dirty and the prior pipeline agent is no longer
  available. This continuation uses no subagents, commits, rollbacks, container cleanup, or
  edits outside the user-approved pipeline/catalog/test/documentation paths.
- The ledger says the coordinator's latest focused run produced two passing source-scope cases
  and two failures that entered retry at parse with `INGESTION_STAGE_ERROR`. The previously
  suggested Windows path-length explanation remains an untested hypothesis, not a diagnosis.
- Next command: reproduce those exact focused cases with the dedicated PostgreSQL/Redis URLs,
  retain full tracebacks, and trace the first failing boundary before making a production fix.

### R02 - Coordinator regression reproduction

- Command (with the required dedicated service environment):
  `uv run python -m pytest -o addopts= tests/integration/test_ingestion_pipeline.py::test_stale_revision_cannot_commit_or_enqueue_next_stage tests/integration/test_ingestion_pipeline.py::test_parse_rejects_source_key_outside_task_kb_scope tests/integration/test_ingestion_pipeline.py::test_expired_index_attempt_cannot_mutate_vector_store -vv --tb=long`.
- Result: **2 failed, 2 passed** in 5.66s. Both source-scope parameters passed. The stale-revision
  case and expired-index case failed before their intended assertions because the current parse
  attempt returned retryable `INGESTION_STAGE_ERROR`; no chunk task was immediately claimable.
- This reproduces the coordinator's observation and disproves neither the new stage-key design nor
  the Windows path-length hypothesis: the handler deliberately sanitizes unexpected exceptions, so
  the traceback only shows the downstream empty queue. Next step is to inspect the persisted failure
  and trace parse boundaries (load, resolve, read, parse, heartbeat, encode, put, commit) to identify
  the hidden exception before changing implementation behavior.

### R03 - Parse retry root cause and bounded artifact-key design

- Minimal boundary reproduction used `LocalObjectStore.put()` with the current
  `{artifact_prefix}/t{task_id}/a{attempt}/{uuid}/parsed.json` shape under a pytest-style Windows
  temporary root. The key was 181 characters and the absolute path was 272 characters.
- Result: deterministic `FileNotFoundError: [WinError 3]` while `path.parent.mkdir(...)` created
  the nested directory. This is the hidden unexpected exception sanitized by the pipeline as
  retryable `INGESTION_STAGE_ERROR`; it also explains the coordinator-owned live-model pipeline's
  parse retry. No TEI/provider operation is reached at this failure point.
- Design: keep the existing revision/version artifact prefix as the durable ownership boundary,
  but replace verbose per-attempt directory components and filename with a compact, fixed-length
  stage key derived from task ID, attempt and fresh randomness. Different attempts must never share
  a key, while retries and crashes may leave unreferenced immutable objects. Catalog commit remains
  the sole pointer publication step. The test will assert both attempt isolation and a bounded key
  length representative of Windows local storage.
- Parent-owned `tests/integration/test_live_model_pipeline.py`, TEI endpoint `127.0.0.1:38742`, and
  `data/acceptance-20260908/tei-model` are explicitly outside this package and will not be modified or
  redeployed here. The coordinator will independently rerun that acceptance after this repair.

### R04 - Repair boundary and acceptance matrix

- Coordinator update: the live TEI/MiniLM deployment is ready and its parent-owned
  `tests/integration/test_live_model_pipeline.py` reproduced the same parse retry. This worker will
  not edit or run that file, the TEI deployment, or the separately developed custom-module safety
  package.
- This package remains limited to: compact immutable stage artifacts; source-key validation before
  reads and at both registration/replacement boundaries; current-revision/non-deleted aggregation;
  external index mutation fencing without blocking the heartbeat connection; rejection rather than
  silent document truncation using the actual tokenizer; and runtime binding/resource lifecycle.
- Verification will use focused pipeline unit tests, dedicated runtime/guard tests, pipeline
  integration tests and relevant task fencing tests. It will not run all of `tests/unit/ingestion`
  because the independently incomplete PDF package is outside this scope.

### R05 - Immutable artifact RED

- Updated the existing crash-after-artifact integration case to require two distinct stage objects
  after retry, require the catalog ledger to reference one of those immutable objects, and require
  each local Windows path to remain below 260 characters.
- RED command:
  `uv run python -m pytest -o addopts= tests/integration/test_ingestion_pipeline.py::test_artifact_write_crash_retries_from_uncommitted_stage[parse-parsed.json-parsed_object_key-registered] -q --tb=short`.
- Result: **1 failed** at `assert failing_store.failed`. The stage wrapper was never reached because
  the overlong current key failed inside `LocalObjectStore.put()` first. This is the expected RED
  for the diagnosed parse root cause; next change is limited to compact immutable stage keys.

### R06 - Compact immutable artifact GREEN

- Changed `IngestionRun.artifact_prefix` to use hyphenless UUIDs and compact `i/revision-version`
  components. Stage writers now use `{task-id-hex}-{attempt-hex}-{random-64-bit}/{stage}.json`.
  The task/attempt component prevents a stale attempt from sharing the retry's key; fresh randomness
  also separates duplicate execution within one attempt. Only the key committed to
  `document_ingestion` is published to downstream stages.
- Re-ran the focused parse crash/retry case with the dedicated service environment: **1 passed** in
  3.43s. The first write now reaches the simulated post-write crash, the retry writes a second key,
  the catalog references a valid key, and both absolute local paths satisfy the Windows bound.

### R07 - Original severe regressions unmasked

- Re-ran the original stale-revision case, both source-scope parameters, and expired-index-attempt
  case after the artifact repair.
- Result: **4 passed** in 5.16s. The superseded ledger no longer blocks activation because aggregation
  joins only the document's current revision and excludes deleted/deleting documents; the stale parse
  cannot enqueue a revision-1 chunk task; both wrong-KB and Windows backslash traversal keys are
  rejected before object-store resolution/read; and a lease expired before vector resolution causes
  zero upsert calls.
- These cases establish the originally reported outcomes, but do not yet cover heartbeat progress
  during a guarded external request, expiry/version replacement during the mutation sequence, ordinary
  chunk truncation, or runtime resolver lifecycle. Those remain the next TDD slices.

### R08 - Coordinator-owned live-model confirmation

- Coordinator reported an independent 16:19 run of parent-owned
  `tests/integration/test_live_model_pipeline.py` against `cairn_acceptance`, actual
  `PipelineRuntime`, TEI MiniLM (384 dimensions) and pgvector: **1 passed** in 4.52s.
- That run verifies the compact artifact repair on the normal live path, including initial
  index/query, revision-2 replacement and old-point removal. It does not establish lease expiry,
  version replacement, persisted-manifest rejection or runtime failure/concurrency lifecycle; this
  worker continues to own and verify those boundaries without modifying the parent test/deployment.

### R09 - Heartbeat lock RED and guard redesign

- Added an integration case that claims a real index task, enters the catalog index-mutation guard,
  then runs `TaskContext.heartbeat()` through its normal independent transaction with a 0.5s bound.
- RED result: **1 failed** with `TimeoutError`; the heartbeat's `UPDATE task` blocked behind the
  guard transaction's `SELECT ... FOR UPDATE` on the same task row.
- Fix design: heartbeat immediately before opening the catalog guard and before every remote mutating
  call; inspect task ownership/lease without locking the task row; keep only document/run/KB/version
  rows locked to serialize revision/version replacement; bound the whole sequence and each remote
  request by the live lease; then recheck ownership before catalog commit/publication. This permits
  the independent heartbeat connection to progress while retaining catalog version serialization.

### R10 - Heartbeat-safe external mutation guard GREEN

- `index_mutation` now heartbeats before opening the guard and before each external mutating call.
  It checks ownership and wall-clock lease budget without locking the task row, while retaining
  document/run/KB/index-version locks and a 30s whole-sequence timeout. `ensure_namespace`, `upsert`
  and stale-point `delete` all execute through the guard; catalog commit heartbeats and fences again.
- Focused result: heartbeat-progress plus expired-before-vector-resolution cases: **2 passed** in
  4.18s. The independent heartbeat no longer waits on the guard transaction and an already-expired
  attempt still performs zero vector writes.
- External-store limitation: cancellation or network timeout cannot revoke a request already accepted
  by a remote vector server. Such an operation may complete after the SQL guard rolls back. Cairn
  therefore guarantees that an expired/superseded attempt cannot publish catalog state, not that an
  arbitrary remote server can roll back an accepted request. Namespaced idempotent upsert plus exact
  fetch/count verification on retry is the repair path; revision/version locks minimize, but cannot
  eliminate, this non-transactional uncertainty window.

### R11 - No silent document truncation RED/GREEN

- Added a real Hugging Face `WordLevel` tokenizer with `[CLS]`/`[SEP]` template processing. The test
  supplies a misleading manifest token count of 3 while the actual embedding tokenizer counts 5
  tokens against a model limit of 4.
- RED: **1 failed**, `EmbeddingInputTooLarge` was not raised; `EmbeddingService` logged
  `embedding.truncated` and called the provider with shortened ordinary content.
- Fix: normalize each searchable document chunk exactly as `EmbeddingService` does, then call the
  service's model-bound tokenizer count before provider/cache execution. Either declared or actual
  count above the model limit now fails terminally; no ordinary or protected-table chunk relies on
  the service's truncation fallback.
- GREEN: special-token ordinary case plus protected-table case: **2 passed** in 2.51s; provider call
  lists remained empty.

### R12 - Runtime lifecycle RED

- Added dedicated unit cases for tokenizer-before-open ordering, cleanup after provider startup
  failure, concurrent resolution of one model, and invalidation when full model/provider configuration
  changes under the same model UUID.
- RED result: **4 failed**. Current runtime opened a provider before discovering a missing tokenizer,
  leaked a provider whose `__aenter__` failed, opened two providers concurrently, and permanently
  returned the UUID-cached binding after model/config changes.
- The minimal implementation will serialize resolution/close, fetch current runtime metadata before
  consulting the cache, fingerprint every `ModelRef` field plus provider ID/family/base URL/full config,
  credentials state and actual tokenizer fingerprint, validate the tokenizer before constructing or
  opening a provider, and close provisional/replaced providers on every failure/change path.

### R13 - Runtime GREEN and priority regression rerun

- Runtime now serializes resolution and shutdown, reloads current model/provider metadata on each
  lookup, validates the configured tokenizer before provider construction, closes a provisional
  provider on startup/cancellation failure, and replaces/closes stale bindings. Cache identity is a
  SHA-256 fingerprint over all current model fields, provider identity/family/base URL/full config,
  credential state and the actual tokenizer fingerprint.
- Dedicated runtime result: **4 passed** in 2.25s.
- Priority rerun after the heartbeat guard and compact-key change: original four severe regressions
  plus independent-heartbeat case: **5 passed** in 5.17s.
- Artifact keys now retain the full compact UUID4 hex nonce. Task ID and attempt partition retries;
  the full nonce separates duplicate writes within one attempt. The implementation never reuses a
  generated key and only publishes a catalog pointer, but does not claim mathematically impossible
  nonce collision.

### R14 - Mid-sequence lease, concurrent revision, scope and aggregation guards

- Added a real sequence test that expires the task lease immediately after guarded
  `ensure_namespace`. The next heartbeat fence rejects ownership, so `upsert`, `delete` and catalog
  commit/publication are not reached. Added a concurrency test proving `start_revision` waits while
  the index guard owns document/run/KB/version locks, then advances only after guard release.
- This changes the index-specific lock protocol from the earlier documented task-first row lock:
  ordinary catalog stage commits still lock `task -> document -> run -> KB`; the external index guard
  first heartbeats in its independent short transaction, then locks `document -> run -> KB -> index
  version` and performs nonlocking task lease reads. Before every external mutation and the final
  commit it heartbeats/rechecks again. This is deliberate so heartbeat cannot deadlock on the guard's
  task lock while revision/version replacement remains serialized.
- Strengthened source scope to assert object-store resolution is never called for forged keys and
  added one test proving upload registration and revision replacement reject the same Windows
  traversal without mutating the document. Added deleted-current-document aggregation coverage.
- First run: **5 passed, 1 test error** because `DocumentView` intentionally does not expose
  `object_key`; this was a test-observation error, not a product failure. The assertion was corrected
  to read the persisted catalog key. Rerun: **6 passed** in 6.60s.

### R15 - Prefix-purge and runtime generation review RED

- Review found the compact artifact prefix incorrectly changed the canonical workspace/KB UUID
  segments, so `ObjectKeys.kb_prefix()` could not find or purge new artifacts. It also found that
  immediate close of a replaced runtime provider could invalidate a `PreparedEmbedding` already in
  use by another task.
- Added two regressions. Artifact crash/retry now requires every generated key to start with the
  canonical KB prefix and verifies real `LocalObjectStore.delete_prefix(kb_prefix)` removes them.
  Runtime reconfiguration requires the old generation to stay open until runtime shutdown, when all
  generations must close.
- RED results: artifact prefix case **1 failed** at canonical-prefix containment; runtime generation
  case **1 failed** because the old provider was closed immediately.
- Static/task evidence before this correction: task fencing **6 passed**; Ruff reported three local
  import/ClassVar issues; focused mypy reported one stale ignore and one metric narrowing error. These
  are recorded failures to fix, not accepted results.

### R16 - Prefix-purge/runtime generation GREEN and static repair

- Restored canonical `str(workspace_id)/str(kb_id)/` as the first two artifact key segments. Windows
  length is reduced only below that ownership prefix: `i/{base64url-document-id}/{revision-version}`
  plus task/attempt and a full 128-bit UUID encoded as compact base64url. The real prefix purge test
  now passes while preserving the local path bound.
- Replaced runtime's immediate stale-generation close with retirement until `PipelineRuntime.close()`.
  New calls cannot retrieve the old fingerprint, concurrent in-flight users keep a live provider, and
  shutdown closes active plus retired generations. Intentional generation retention is bounded by the
  worker process lifecycle; repeated live configuration churn should be followed by a worker restart
  if immediate connection reclamation is operationally required.
- Focused GREEN: canonical prefix plus actual purge **1 passed**; runtime old-generation lifecycle
  **1 passed**. Targeted Ruff: **all checks passed**. Focused mypy: **no issues in 7 source files**.

### R17 - Worker delivery evidence

- Final bounded package command, with dedicated PostgreSQL/Redis environment:
  `uv run python -m pytest -o addopts= tests/unit/ingestion/test_pipeline.py tests/unit/ingestion/test_runtime.py tests/integration/test_ingestion_pipeline.py tests/integration/test_task_fencing.py -q --tb=short`.
  Result: **43 passed**, one pre-existing Alembic `path_separator` deprecation warning, in 21.56s.
- Additional task queue command:
  `uv run python -m pytest -o addopts= tests/integration/test_task_queue.py -q --tb=short`.
  Result: **21 passed**, the same warning, in 3.12s.
- Static commands: targeted `uv run python -m ruff check ...` returned **All checks passed**;
  targeted `uv run python -m mypy ...` returned **Success: no issues found in 7 source files**.
- No commit, rollback, container cleanup, parent live-model test edit, Office/PDF/custom/semantic
  implementation, reindex fan-out or chunk re-embedding was performed. Coordinator will now rerun
  the focused package and live TEI/Office acceptance independently.

## Scope

This increment closes the orchestration gap between upload registration and a published vector
index. It adds TaskWorker handlers for `document.parse`, `document.chunk`, `document.embed` and
`document.index`, revision-scoped durable artifacts, catalog-owned stage transitions, persisted
content-hash reuse and verified index publication.

The implementation covers `T-M07-10..13`, `FR-G-06`, `NFR-R-03`, `NFR-R-05` and `NFR-R-06`.
Office/PDF/OCR parser additions, language routing and advanced semantic/custom chunkers are
separate increments and remain behind the existing parser and chunker registries.

## Module boundaries

The ingestion module does not import catalog ORM models or repositories. Its only persistence
boundary is `CatalogIngestionFacade` in `cairn.catalog.ingestion`:

- `load_run(context)` validates workspace, KB, document, revision, index version and storage
  bindings before returning a credential-free run description.
- `commit_parsed`, `commit_chunked`, `commit_embedded` and `commit_indexed` advance one durable
  stage and create the next task in the same PostgreSQL transaction.
- `existing_point_ids(context)` exposes only the catalog information needed to calculate stale
  vectors.
- `preserved_edits(context)` returns active-version manual edits as public DTO data so chunk
  artifacts, embeddings, vectors and catalog rows all use the same effective text.
- `record_failure(context, ...)` stores sanitized contributor-facing failures under the same
  task/revision fence.
- `start_revision(document_id, ...)` updates source provenance, allocates the next document
  revision and enqueues parsing atomically.

Ordinary stage commits acquire locks in this order:

1. task row through `TaskService.lock_owned_task(session, context)`;
2. document row;
3. document-ingestion ledger row;
4. knowledge-base/index rows when required.

The external index guard intentionally uses a different order. It first heartbeats through the
worker's independent short transaction, then locks document, ingestion ledger, KB and index-version
rows while reading task ownership/lease without a task-row lock. Before each external mutation and
the final commit it heartbeats and rechecks the wall-clock lease. This lets heartbeat progress while
revision/version replacement waits for the guard. The final index commit cannot activate a stale
revision even if an external vector operation completed immediately before fencing.

## Durable artifacts

Every stage output is immutable at this object-store prefix:

```text
{workspace_id}/{kb_id}/i/{base64url_document_id}/{revision_hex}-{index_version_hex}/
  {task_id_hex}-{attempt_hex}-{base64url_uuid}/{stage}.json
```

`parsed.json`, `chunks.json` and `embeddings.json` use a versioned JSON envelope with a SHA-256
checksum. Every manifest repeats document ID, revision, index version and source content hash;
the consumer rejects a checksum or identity mismatch. Canonical workspace/KB UUID segments keep
every artifact inside `ObjectKeys.kb_prefix`, so normal KB prefix purge removes originals and stage
artifacts together. Compact lower segments keep Windows local-store paths bounded. Every execution
uses a new full UUID nonce, so a stale attempt writes a different object and cannot overwrite the
retry's artifact; only the catalog-committed pointer is consumed. A crash may leave an unreferenced
immutable object for later prefix cleanup.

The `document_ingestion` table is the durable stage ledger. Its primary key is
`(document_id, revision, index_version)`, with cascading foreign keys to the document and KB index
version. It records committed artifact keys, expected point count and stale point IDs. No new
FK-less table was introduced, so integration fixture cleanup needs no additional table name.

## Stage behavior

### Parse

The parse worker validates source scope before object-store resolution or reads, then loads bounded
source bytes and recomputes the actual SHA-256 before parsing. It accepts only keys whose first two
path components exactly match the task's workspace and KB, and rejects backslashes, drive markers,
NULs, empty/dot/parent components and Windows trailing-dot/space aliases. Upload registration and
revision replacement call the same predicate. Integrity failures use stable code
`SOURCE_INTEGRITY_MISMATCH`; hashes, keys and engine details are not returned to task/document error
detail.

### Chunk

The chunk worker verifies the parse manifest and uses `DocumentChunker` with the tokenizer bound
to the configured embedding model. There is no tokenizer or model-name guessing. Chunk metadata,
heading/page provenance and parent-child links are preserved in both the manifest and catalog.
Parent chunks with `metadata.embed == false` remain catalog-visible but are excluded from expected
vector IDs.

### Embed

The embedding worker verifies the chunk manifest and resolves an explicit `PreparedEmbedding`
containing the model reference, tokenizer, service and binding fingerprint. It loads the most
recent valid persisted embedding manifest for the same document. Vectors are reused only when the
content hash, binding fingerprint, dimension and normalization policy all match. Redis remains an
optional cache, not proof that a chunk was previously embedded.

Only missing unique content hashes are sent to `EmbeddingService`. Parent chunks are never sent.
Every searchable chunk is normalized exactly as the document embedding path will normalize it and
counted with the actual model-bound tokenizer, including Hugging Face special tokens. If either the
manifest count or actual count exceeds `model.max_input_tokens`, ordinary chunks and protected tables
both fail with `EMBED_INPUT_TOO_LARGE` before the service can silently truncate provider input.

### Index

The index worker writes only to `Namespace(kb_id, index_version)`. A building namespace is never
confused with the active namespace. It creates the namespace with the catalog dimension/metric,
upserts every expected searchable point, fetches the exact ID set back and verifies content hashes.
Only after successful upsert verification does it delete stale IDs; it then verifies their absence
and checks the exact document-filtered point count. Any partial write or verification discrepancy
keeps the document at `embedded` and returns a retryable sanitized failure.

`ensure_namespace`, `upsert` and `delete` each pass through the heartbeat/lease guard. Lease loss
between calls stops all later mutations and catalog publication. A concurrent revision/version change
waits on catalog locks. A remote server may nevertheless complete a request that it accepted before
client timeout/cancellation; PostgreSQL cannot revoke that external side effect. The guarantee is
therefore no stale catalog publication, followed by idempotent retry and exact remote verification,
not cross-system rollback of an already-sent request.

Publication is two-step: the fenced catalog transaction marks the ledger/document indexed and,
only when no registered/parsed/chunked/embedded run remains for that building version, atomically
switches `active_index_version`, clears `building_index_version`, bumps `config_version`, updates
version progress and retires the previous version. Runtime cache invalidation is published only
after that transaction commits. If publication fails after activation, the retry recognizes the
already-indexed active run, republishes idempotently and clears the transient error under the task
lease fence.

## Idempotency and recovery

- Upload registration creates the document, ingestion ledger and parse task in one transaction.
- Every next-stage task has a revision/version-specific dedupe key and is inserted in the same
  transaction as its preceding stage transition.
- A second commit of an already advanced stage is a no-op and cannot enqueue another task.
- A crash after parse/chunk/embed artifact write resumes from the last committed stage and writes a
  new attempt-scoped immutable key; the stale uncommitted key is never published.
- A crash after vector upsert remains visible as an uncommitted embedded document. Retry upserts
  idempotently, verifies the stored points and only then publishes.
- Parse, chunk and embed renew their lease after expensive work and before commit. Index additionally
  renews before entering its guard, before every external mutation and before final catalog commit.
- An expired lease or stale document revision cannot mutate catalog state or enqueue a successor.
- Re-ingesting unchanged content after Redis eviction makes zero provider calls because the valid
  persisted embedding manifest is the reuse source. Editing one paragraph calls the provider only
  for the affected content hash and removes its stale point.

## Production composition

`apps.worker.main.build_worker` registers the matching handler for parse, chunk, embed and index
queues. The process closes embedding providers plus object/vector registries during shutdown.

`PipelineRuntime` validates the actual configured tokenizer before opening a provider, serializes
concurrent model resolution, and fingerprints complete current model/provider configuration rather
than caching permanently by model UUID. Replaced generations are removed from lookup immediately but
remain open for already-issued `PreparedEmbedding` users; shutdown closes active and retired
generations. Failed/cancelled provider startup is closed immediately.

No `core/config.py` change is required. The worker-only settings live in
`cairn.ingestion.runtime_config`:

| Environment setting | Default | Meaning |
| --- | ---: | --- |
| `CAIRN_INGESTION__TOKENIZERS_JSON` | `{}` | JSON map from model `tokenizer_id` to `tiktoken:<encoding>` or `hf:<tokenizer-json-path>` |
| `CAIRN_INGESTION__MAX_SOURCE_BYTES` | `52428800` | Maximum raw source object size |
| `CAIRN_INGESTION__MAX_ARTIFACT_BYTES` | `268435456` | Maximum durable stage artifact size |

Production embedding models must carry an explicit positive `dimension`, `max_input_tokens` and
`tokenizer_id`. TEI and Infinity provider records must provide `base_url` and a nonempty
`config.binding_revision`; optional `config.allow_private` and `config.max_batch_size` configure
their adapters. Credential-backed hosted providers remain a model-gateway invoker dependency and
are rejected rather than bypassing credential isolation.

## Verification

The targeted unit suite covers checksummed manifest round trips and tamper rejection, persisted
content-hash reuse, parent exclusion, real-tokenizer ordinary/protected overflow, startup registration,
runtime startup failure cleanup, concurrent resolution and binding-generation changes.

The targeted integration suite uses the actual TaskWorker claim/execute path, PostgreSQL task and
catalog transactions, Redis-backed `EmbeddingService`, local object storage and the pgvector
driver. It covers:

- atomic upload/ledger/task creation and initial building version allocation;
- fenced, duplicate stage commit and expired-lease rejection;
- upload through all four workers to verified publication;
- no premature activation while another registered document remains;
- no historical-revision or deleted-document aggregation hang;
- unchanged reprocessing after Redis eviction with zero provider calls;
- one-paragraph edit with one provider call and stale-vector deletion;
- preserved manual-edit text shared by the artifact, catalog row and vector payload;
- stale-revision rejection;
- actual-byte hash validation, pre-read source-scope rejection, and matching registration/replacement
  validation;
- crash/recovery after parse, chunk and embed artifact writes, distinct attempt keys, Windows path
  bounds, canonical KB-prefix containment and actual prefix purge;
- expired-before-write and lost-between-write index leases, heartbeat progress, and concurrent
  revision serialization;
- visible vector-upsert partial failure followed by verification and publication on retry;
- post-activation runtime-publication failure followed by fenced idempotent republish.

The focused package command completed **43 passed** in 21.56s. The deterministic integration provider
returns three-dimensional vectors. Coordinator ledger L21 separately records **1 passed** against the
actual 384-dimensional TEI MiniLM runtime and pgvector normal path; that does not replace the fault
tests above.

## Parent integration notes

- Keep `CAIRN_INGESTION__TOKENIZERS_JSON` synchronized with registered models; do not add fallback
  tokenizer inference.
- Integrate the enhanced office/language/PDF/parser and semantic/custom chunker factories through
  their public registries when those suspended increments resume.
- Update the pre-existing entrypoint assertion that expected an empty parse worker; parse now
  intentionally registers `document.parse`.
- Review and verify the separately owned TaskWorker fencing patch. Pipeline tests exercise
  `lock_owned_task`, but this increment does not own `cairn.tasks`.
- Full-KB reindex fan-out, failed-build namespace cleanup and credential-backed gateway invocation
  are not implemented by this increment. They must be completed before claiming the entire M07
  acceptance journey or production blue/green rebuild lifecycle.
- `kb.reindex_fanout` and `chunk.reembed` are intentionally deferred to the next bounded lifecycle
  package; this repair does not register placeholder handlers or silently emulate either operation.
