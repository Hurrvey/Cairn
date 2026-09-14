# Lifecycle slice 1: version-owned configuration

Updated: 2026-09-09. Status: bounded slice 1 independently ACCEPTED by coordinator;
no whole-pipeline acceptance claim.

Coordinator acceptance: 86 focused tests passed (64.69s), including real TEI/Office/custom/fencing
and migration round-trip. Full backend subsequently completed with 703 passed, 2 known PDF/OCR
collection errors, no assertion failures or skips, coverage84.83% (137.18s). Global Ruff lint,
format165, mypy110, import contracts6 and route authz50/8 passed. See ledger L49-L52 and ignored
reports under `data/acceptance-20260909/`. Agent Pascal was explicitly closed. Remaining lifecycle
work and conservative migration/asset-provenance limitations below are not waived by acceptance.

## Scope and ownership

This increment implements only lifecycle slice 1 from
`16-lifecycle-acceptance-plan.md`: immutable per-index-version configuration, active-runtime
publication from that snapshot, worker drift fencing, and monotonic version allocation. Fan-out,
manual chunk re-embedding, recovery entrypoints, activation verification, and retirement are
deliberately excluded and remain open.

The implementation agent owns bounded catalog model/repository/service/ingestion changes,
ingestion runtime/pipeline changes required to enforce the snapshot, migration `0008`, focused
tests, and this file. The coordinator owns
`tests/integration/test_pipeline_lifecycle_acceptance.py` and
`docs/04-plan/15-execution-ledger.md`; neither is edited here. Existing user changes are preserved.

## Initial design record (before production edits)

### Durable version snapshot

Each `kb_index_version` will own a credential-free snapshot containing the complete `ModelRef`
shape (`id`, provider family, model key, capability, dimension, maximum input tokens, normalize,
query prefix, optimal batch size, and tokenizer ID), plus metric and the complete validated chunk
configuration. A catalog DTO will expose this as one immutable value. `KnowledgeBase` remains the
desired configuration edited by `start_reindex`; preserving that mutation is intentional because
the control plane must continue showing the requested target while the old active version serves.

Every newly allocated initial or rebuild version captures the snapshot exactly once. Subsequent KB
or model-registry mutation does not rewrite a version snapshot. Runtime publication resolves the
namespace and embedding/metric from the ACTIVE version row, while retrieval settings and storage
binding remain KB-owned mutable runtime configuration.

### Worker enforcement and trust boundary

`CatalogIngestionFacade.load_run` will return the target version snapshot instead of copying model,
metric, or chunk settings from the mutable KB row. Ingestion will continue using only the catalog
facade and public DTOs; it will not import catalog ORM models.

Production `PipelineRuntime` must still load provider endpoint/configuration and credentials through
the model gateway at execution time. Its embedding resolver will accept the frozen `ModelRef`, load
the currently registered runtime by snapshot ID, and compare the complete registered `ModelRef`
with the snapshot before returning a tokenizer/provider/service. Any identity, dimension, token
limit, tokenizer ID, normalization, prefix, batch-size, capability, provider-family, or model-key drift
fails closed. Parse, chunk, embed, and index paths validate the snapshot before writing their stage
artifact or vector mutation, with index validating before namespace creation/upsert.

This boundary cannot detect a provider silently changing the weights served behind an otherwise
unchanged endpoint and model identity. Provider binding revisions participate in the existing
runtime fingerprint/cache lifecycle, but externally stable metadata is not cryptographic model
provenance. Likewise, the snapshot freezes the tokenizer ID in `ModelRef`, not tokenizer asset bytes
or their fingerprint; replacing bytes behind a stable tokenizer ID is not detected against the
version snapshot. That limitation remains documented rather than being overclaimed as solved.

### Monotonic allocation

`knowledge_base.index_version_high_water` is the durable allocation source. It is initialized from
the greatest historical version/pointer during migration and incremented only while the KB row is
locked. New versions therefore never reuse numbers after failed/retired rows or after those rows are
purged. Version rows remain independently deletable without lowering the high-water mark.

### Safe migration and legacy provenance

Migration `0008` will add the high-water mark and nullable snapshot columns. **No legacy version is
automatically assigned a snapshot.** Pointer alignment and row state cannot prove provenance:
registered model fields may already have changed; failed rows may already have been purged; mutable
KB chunk settings may have changed without a switch; and a building version may already contain
artifacts/vectors generated before metadata drift. All pre-`0008` versions therefore retain their
pointers and history but have an explicitly unavailable snapshot.

Missing snapshots are a deliberate fail-closed state. Catalog runtime publication and ingestion
loading report that a rebuild is required instead of combining an unproven legacy namespace with
current KB/model metadata. New writes require complete snapshots. This favors visible upgrade
remediation over silently querying or embedding in a mixed vector space. A future explicit recovery
flow may establish provenance from independently signed/verified artifacts, but that is outside
slice 1.

The migration initializes `index_version_high_water` from the greatest retained evidence it can
find: `kb_index_version.version`, active/building pointers, retained `chunk.index_version`, task
payload `index_version`, and audit detail `index_version`, where the latter schemas permit safe
numeric extraction. Untrusted JSON values are considered only when they are JSON numbers with a
bounded positive-integer textual form before conversion. For every legacy KB the migration also
reserves at least `active_index_version + 1`, or version 1 when no active pointer exists. This is a
bounded consequence of the supported pre-`0008` allocator: initial builds used 1, failed rebuilds
used active+1, and public activation never decreased active. It can introduce a harmless version
gap when no failed version was purged. This survives the normal case where a version row was purged but its durable
operational evidence remains. It cannot reconstruct an index number whose row, vectors/chunks,
tasks, audit evidence, and pointers were all physically deleted before `0008`; no migration can
recover information that no longer exists, especially histories produced by unsupported manual SQL
rollback/manipulation. That pre-migration limitation is explicitly excluded. After `0008`,
the KB-owned high-water mark is never decremented or deleted with historical versions and is the
sole allocator under the KB row lock.

### Rollout and operator recovery

SQL migration alone cannot revoke legacy Redis `kb:runtime:{id}` entries, and an old API process
could republish the unsafe KB-row-derived payload for up to the 300-second TTL. Rollout therefore
requires stopping/draining old API and worker publishers, applying `0008`, invalidating affected KB
runtime keys (or waiting the full TTL while all old publishers remain stopped), and only then
starting the new binaries and serving retrieval. When `publish_runtime` encounters an ACTIVE version
without a snapshot, it deletes the stale runtime key before raising rebuild-required.

A legacy missing-snapshot building pointer also blocks `start_reindex` through the existing
single-build invariant. Slice 1 does not add automatic recovery, fan-out, or operator endpoints. The
bounded operator action is: stop publishers, explicitly abandon/fail the unproven building version
through the existing catalog failure seam (or an equivalent reviewed administrative data fix), then
start a fresh snapshot-owned rebuild. An unproven ACTIVE version remains unavailable until that
fresh rebuild is completed and activated. Do not claim automatic upgrade recovery.

### TDD and verification plan

1. Run the coordinator-owned five-case lifecycle acceptance file unchanged and record RED evidence.
2. Add focused migration/config/allocation/runtime/worker regressions in separately owned test files;
   run each RED before production edits.
3. Add migration/model/DTO/repository/service behavior minimally until catalog tests are GREEN.
4. Change facade and runtime/pipeline resolution to consume and validate full snapshots; make worker
   regressions GREEN.
5. Run focused suites, then `uv run python -m pytest`, `uv run python -m mypy`, and
   `uv run python -m ruff check .`. Record exact commands, results, changed files, and limitations.

## Evidence log

### 2026-09-09 initial inspection

* Baseline commit reported by coordinator: `e9033ba`.
* Initial `git status --short` showed only
  `M docs/04-plan/15-execution-ledger.md`, owned by the coordinator and left untouched.
* Existing failure mechanisms confirmed in source: `publish_runtime` joins ACTIVE namespace to the
  mutable KB model/metric; `start_reindex` allocates `(active_index_version or 0) + 1`; ingestion
  `load_run` and production `embedding_for(UUID)` resolve mutable model metadata.
* PostgreSQL/Redis exclusive test window was transferred by the coordinator before any DB command.

Further RED/GREEN command evidence is appended below as work proceeds.

### 2026-09-09 coordinator acceptance RED (before production edits)

Command (exclusive disposable PostgreSQL/Redis window):

```powershell
$env:CAIRN_TEST_USE_EXTERNAL_SERVICES='1'
$env:CAIRN_DATABASE_URL='postgresql+asyncpg://cairn:cairn-disposable-only@127.0.0.1:39170/cairn_acceptance'
$env:CAIRN_REDIS_URL='redis://127.0.0.1:38694/0'
uv run python -m pytest tests/integration/test_pipeline_lifecycle_acceptance.py -q
```

Result: exit 1, **5 failed** in approximately 12 seconds (13.0 seconds wall time).

* `test_rebuild_keeps_active_model_bound_to_active_vectors`: ACTIVE namespace v1 was published
  with the replacement four-dimensional model selected for building v2.
* `test_failed_rebuild_does_not_reuse_its_index_version`: the second rebuild attempted to insert
  v2 again and raised the `kb_index_version` primary-key violation.
* `test_purged_failed_version_does_not_erase_allocation_history`: allocation history disappeared
  with the purged failed row.
* `test_published_model_metadata_is_not_reconstructed_from_mutable_registry`: publication rebuilt
  ACTIVE model identity from changed registry metadata instead of frozen version metadata.
* `test_active_metric_does_not_follow_mutated_desired_config`: the initial test setup attempted a
  direct desired-row metric mutation without opening a building pointer and was correctly rejected
  by `guard_kb_embedding_immutable`. The coordinator is correcting the setup to model the sanctioned
  rebuild writer. This RED is not evidence against ADR-0006, and `0008` must preserve the existing
  guard while adding immutable version-snapshot protection.

### 2026-09-09 focused RED

* `uv run python -m pytest tests/unit/ingestion/test_runtime.py::test_registered_model_drift_is_rejected_before_provider_startup -q`
  exited 1: drift reached tokenizer lookup (`TokenizerUnavailable`) instead of a snapshot mismatch,
  proving `embedding_for(UUID)` had no frozen identity to validate.
* `uv run python -m pytest tests/integration/test_version_snapshots.py::test_initial_version_freezes_complete_configuration tests/integration/test_version_snapshots.py::test_captured_version_configuration_cannot_be_mutated -q`
  exited 1 with 2 failures: `IndexVersionView` had no config and PostgreSQL had no
  `config_snapshot` column/immutability guard.
* `uv run python -m pytest tests/integration/test_version_snapshot_migration.py -q` exited 1: after
  constructing populated legacy v1 data, mutating registry/chunk metadata, purging simulated failed
  v2, and running the then-current head, PostgreSQL correctly reported that the new snapshot columns
  did not exist. The test restored migration head in `finally`.

### 2026-09-09 implementation checkpoint

Status: active, no implementation blocker, database window retained for remaining catalog,
ingestion, full-suite, and live-TEI verification.

Implemented so far:

* `0008_version_snapshots` adds a KB-owned high-water mark, atomic JSONB config snapshot,
  explicit legacy-unavailable marker/coherence check, conservative retained-evidence migration,
  and an immutable snapshot trigger while preserving ADR-0006's existing KB trigger.
* Initial upload and confirmed rebuild allocation capture the complete credential-free `ModelRef`,
  metric, and validated chunk config. Allocation increments only the locked KB high-water mark.
* ACTIVE runtime publication reads the active version snapshot and invalidates Redis before raising
  rebuild-required for unavailable legacy provenance.
* `CatalogIngestionFacade.load_run` returns target-version configuration, not desired KB model,
  metric, or chunk settings.
* Production ingestion resolution accepts a full `ModelRef`, compares every dataclass field against
  current registered metadata before tokenizer/provider startup, and the pipeline independently
  rejects a resolver that returns a different prepared model. Parse/chunk/embed/index call this
  check before their first stage artifact/vector write.

GREEN evidence at this checkpoint:

* `uv run python -m pytest tests/integration/test_version_snapshots.py -q`: **2 passed**.
* `uv run python -m pytest tests/integration/test_version_snapshot_migration.py -q`: **1 passed**;
  actual `0008 -> 0007 -> 0008` cycle, populated legacy model/KB drift, purged failed v2,
  unavailable snapshot, reserved high-water=2, publication rejection, and Redis invalidation.
* `uv run python -m pytest tests/unit/ingestion/test_runtime.py -q` with required DB/Redis settings:
  **5 passed**.
* Coordinator-owned `tests/integration/test_pipeline_lifecycle_acceptance.py`: **6 passed** in
  12.8 seconds, including desired chunk settings isolation.

One newly added worker-drift regression was not yet GREEN at this checkpoint because the test
pipeline's fake resolver asserted before returning its deliberately mismatched model; that generic
`AssertionError` followed the task retry path and left the document registered. The fake will be
made neutral so the production pipeline's own snapshot mismatch branch is exercised. This is test
harness correction, not a reason to weaken production validation.

Remaining verification: worker-drift regression, catalog and ingestion integration suites, updated
custom/live-model callers, real TEI pipeline, migration/static review, then complete pytest/mypy/ruff
commands. Fan-out/re-embed/recovery/retirement remain excluded.

Follow-up RED from review:

* `uv run python -m pytest tests/integration/test_version_snapshots.py::test_same_model_id_registry_drift_is_captured_by_fresh_rebuild -q`
  exited 1 because a same-ID registry change was captured in v2 but left desired
  `knowledge_base.embedding_dim` at 3. Rebuild allocation now synchronizes desired model ID and
  dimension on every rebuild, not only when `ReindexSpec.embedding_model_id` is explicit; the new
  building pointer remains part of the same ORM flush for ADR-0006.

### 2026-09-09 final implementation evidence

The database window was released to the coordinator after the commands in this section completed.
No further database commands are planned by this implementation agent.

Focused and real-service GREEN commands:

```powershell
uv run python -m pytest tests/unit/ingestion/test_runtime.py -q
# 5 passed

uv run python -m pytest tests/integration/test_version_snapshots.py `
  tests/integration/test_version_snapshot_migration.py -q
# 5 passed; includes actual 0008 -> 0007 -> 0008 migration cycle

uv run python -m pytest tests/integration/test_pipeline_lifecycle_acceptance.py -q
# 6 passed in 12.8 seconds

uv run python -m pytest tests/integration/test_catalog.py -q
# 32 passed in 27.9 seconds

uv run python -m pytest tests/integration/test_ingestion_pipeline.py `
  tests/integration/test_custom_pipeline.py -q
# 25 passed in approximately 30 seconds

$env:CAIRN_TEST_TEI_URL='http://127.0.0.1:38742'
$env:CAIRN_TEST_TEI_TOKENIZER='D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json'
uv run python -m pytest tests/integration/test_live_model_pipeline.py -q
# 2 passed in 15.9 seconds against real TEI

uv run python -m pytest tests/integration/test_pipeline_lifecycle_acceptance.py `
  tests/integration/test_version_snapshots.py -q
# 11 passed in 15.7 seconds after all review fixes
```

All PostgreSQL commands above used the coordinator-provided external-service settings. The
migration regression additionally proves:

* populated legacy active v1 remains `snapshot_unavailable=true` after registry and desired chunk
  settings drift;
* a purged legacy failed v2 reserves high-water 2 under the supported pre-0008 history policy;
* malformed string, 10-digit int4-overflow, and much larger numeric task payloads are ignored
  without an unsafe cast;
* stale Redis runtime is deleted before missing-provenance publication raises rebuild-required;
* raw snapshot mutation and high-water decrement both fail with SQLAlchemy `IntegrityError`.

Full command gate:

```powershell
uv run python -m pytest -q
```

Result: exit 1 during collection, before tests ran, because
`tests/unit/ingestion/test_ocr.py` and `tests/unit/ingestion/test_pdf.py` import the currently absent
`cairn.ingestion.ocr`. Those tests and the concurrently investigated OCR/PDF work are outside this
slice and were not altered. To distinguish that independent collection blocker from this patch, the
remaining runnable suite was executed:

```powershell
uv run python -m pytest -q `
  --ignore=tests/unit/ingestion/test_ocr.py `
  --ignore=tests/unit/ingestion/test_pdf.py
```

Result: exit 0, progress reached 100% in approximately 123 seconds. The only emitted warnings were
the existing Alembic `path_separator` deprecation warnings.

Static gates:

```powershell
uv run python -m ruff check .
# All checks passed!

uv run python -m mypy
# Success: no issues found in 110 source files

git diff --check
# exit 0; only Git's existing LF-to-CRLF worktree notices
```

Coordinator static review subsequently found four files that passed lint but did not pass Ruff's
formatter because of mixed CRLF and expression layout. No behavior changed. The bounded formatting
repair was:

```powershell
uv run python -m ruff format `
  src/cairn/catalog/dto.py `
  src/cairn/catalog/service.py `
  tests/integration/test_version_snapshot_migration.py `
  tests/integration/test_version_snapshots.py
# 4 files reformatted

uv run python -m ruff format --check src apps tests
# 165 files already formatted

uv run python -m ruff check `
  src/cairn/catalog/dto.py `
  src/cairn/catalog/service.py `
  tests/integration/test_version_snapshot_migration.py `
  tests/integration/test_version_snapshots.py
# All checks passed!

git diff --check -- `
  src/cairn/catalog/dto.py `
  src/cairn/catalog/service.py `
  tests/integration/test_version_snapshot_migration.py `
  tests/integration/test_version_snapshots.py
# exit 0
```

These were static-only commands. No PostgreSQL, Redis, pgvector, Alembic migration, or TEI process
was started while the coordinator's independent database acceptance was running.

## Changed files owned by slice 1

Production and migration:

* `migrations/versions/0008_version_snapshots.py`
* `src/cairn/catalog/dto.py`
* `src/cairn/catalog/ingestion.py`
* `src/cairn/catalog/models.py`
* `src/cairn/catalog/service.py`
* `src/cairn/ingestion/pipeline.py`
* `src/cairn/ingestion/runtime.py`

Focused tests and compatibility updates required by the resolver contract:

* `tests/integration/test_version_snapshots.py`
* `tests/integration/test_version_snapshot_migration.py`
* `tests/unit/ingestion/test_runtime.py`
* `tests/integration/test_ingestion_pipeline.py`
* `tests/integration/test_custom_pipeline.py`
* `tests/integration/test_live_model_pipeline.py`

Documentation:

* `docs/04-plan/17-version-snapshot-implementation.md`

The coordinator-owned `tests/integration/test_pipeline_lifecycle_acceptance.py` and
`docs/04-plan/15-execution-ledger.md` were read and executed but not edited by this agent. Other
concurrent OCR/PDF documentation changes visible in the worktree were likewise left untouched.

## Final limitations and open work

* Slice 1 freezes credential-free registered model metadata, tokenizer ID, metric, and chunk config;
  it does not freeze provider weights or tokenizer bytes/fingerprint behind stable identifiers.
* Provider endpoint/config/credential resolution remains live by design. Existing binding-revision
  fingerprinting handles declared runtime changes, not invisible upstream replacement.
* Every legacy pre-0008 version is intentionally unavailable. Rollout must drain old publishers and
  invalidate runtime keys/wait the TTL before serving. Recovery is the explicit operator abandon/fail
  seam followed by a new rebuild; there is no automatic legacy recovery in this slice.
* Legacy high-water migration is conservative for supported API histories and retained evidence. It
  cannot reconstruct unsupported manual SQL histories after every row, chunk, task, audit record,
  and pointer was deleted before migration.
* Full-KB fan-out, manual re-embedding, retry/recovery entrypoints, activation verification,
  cache-generation fencing, and namespace retirement remain open lifecycle slices. No handler was
  added for those task kinds and no knowledge-base E2E completion is claimed.
* Remote vector writes remain non-transactional relative to catalog commits; this slice only ensures
  snapshot validation happens before namespace/vector mutation.
