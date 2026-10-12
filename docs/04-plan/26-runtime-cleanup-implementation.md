# Runtime refresh and cleanup implementation

Date: 2026-09-15. Status: DESIGN APPROVED; TDD IN PROGRESS.

## Scope and ownership

This package implements package A from document 25 only:

- automatic bounded repair of the Redis `kb:runtime:{uuid}` ACTIVE projection from the maintain
  worker, including repair after Redis restart and before the five-minute runtime TTL expires;
- coherent runtime publication/invalidation that cannot write an older ACTIVE snapshot after a
  concurrent switch, archive, or delete;
- production `document.purge`, `kb.purge`, and delayed/failed `index.drop` handlers;
- focused integration/unit tests owned by this worker and this implementation journal.

The coordinator owns documents 15 and 25 and
`tests/integration/test_product_maintenance_acceptance.py`; they will not be edited. Package B,
frontend/model-gateway work, Dockerfiles, Compose, `.dockerignore`, and UI work are excluded. Existing
dirty accepted recovery/retrieval code and user documentation are preserved. There will be no commit,
revert, clean, branch, worktree, descendant agent, Docker deletion, or direct filesystem deletion.

## Initial evidence

- Required documents 15, 19, 24, and 25 and the current catalog/task/object/vector implementations
  were read before production edits.
- Retrieval is intentionally Redis-only. This package repairs its projection; it does not add ORM or
  catalog fallback to the retrieval process.
- `CatalogService.publish_runtime` currently reads an unlocked snapshot and writes Redis afterward,
  so a delayed publisher can overwrite a newer invalidation/switch. It also permits a deleting KB
  with an ACTIVE version to be written as a runtime.
- The maintain worker registers existing platform and reindex handlers but has no purge/drop handlers
  and no periodic scheduler.
- The coordinator's initial package-A RED completed in 5.41 seconds: handler registration and actual
  indexed document deletion/vector cleanup failed, while republishing an already deleted KB did not
  recreate the cache in that test.
- The exclusive disposable PostgreSQL `127.0.0.1:39170/cairn_acceptance` and Redis
  `127.0.0.1:38694/0` window was transferred to this worker after that RED. It remains held until
  package-A targeted tests are GREEN and is then released explicitly.

## Chosen structure

`src/cairn/catalog/maintenance.py` will own immutable cleanup targets, catalog guards, the three task
handlers, bounded runtime refresh, and handler registration. Narrow repository/service/facade methods
will expose catalog-owned reads and finalization; storage drivers are resolved through existing
registries and all deletion uses `ObjectStore`/`VectorStore` contracts. The task and authorization
modules retain ownership of task draining and KB-access cleanup through narrow public facades; catalog
maintenance will not import their ORM models.

Two alternatives are rejected:

1. TTL-only refresh or unlocked compare-then-write remains racy: a publisher can observe v1, a delete
   can invalidate it, and the delayed publisher can recreate v1.
2. Redis CAS/tombstones would require a second durable generation protocol and tombstone retention.
   PostgreSQL already contains the authoritative generation and pointer, so serializing on the KB row
   is smaller and gives one linearization order to every current writer.

## Runtime publication and invalidation

Every `publish_runtime(kb_id)` call will open a transaction, read the KB with `FOR UPDATE` including
soft-deleted rows, construct the runtime from the exact locked ACTIVE version/binding, and perform the
Redis set/delete before releasing that row lock. It deletes rather than publishes when the row is
absent, deleted, `deleting`, `archived`, lacks an ACTIVE pointer, or lacks a valid ACTIVE snapshot.
Runtime JSON remains credential-free and retains the existing TTL.

Catalog writers already serialize status/config/pointer changes on the same KB row. Consequently:

- publisher first: it writes while owning the row, then switch/delete proceeds and publishes/deletes;
- switch/delete first: the later publisher observes only the new ACTIVE state or unusable state;
- a stale caller never carries an earlier detached ORM snapshot into Redis.

A committed SQL transition may still outlive a transient Redis failure, but periodic repair converges
it. No retrieval-side hydration is introduced.

## Periodic maintain-worker bootstrap

`TaskSettings` adds exactly these settings:

- `CAIRN_TASKS__RUNTIME_REFRESH_INTERVAL_S`, default `60`, positive and bounded;
- `CAIRN_TASKS__RUNTIME_REFRESH_BATCH_SIZE`, default `100`, range `1..1000`.

`MaintenanceScheduler` is an injectable constructor seam with refresh callable, interval, batch size,
and leader-lock acquisition. `apps.worker.main._run` constructs and starts it automatically beside
`TaskWorker.run()` only for the `maintain` queue; tests and deployments do not manually enqueue a
refresh task. Shutdown cancels/awaits the scheduler before shared cache/database teardown.

One maintain replica holds one bounded dedicated PostgreSQL connection and a session advisory lock
while leading refresh cycles. Other replicas retry leadership on their next interval and do not run
duplicate cycles. The leader connection is not borrowed from the ordinary publication path; the
documented minimum pool budget is one scheduler connection plus worker/publication connections. A
leader walks KB IDs in stable UUID order in bounded batches, immediately continuing until one complete
pass, then waits until the next monotonic deadline. Each KB publishes independently under its row lock
and a bounded cache-operation timeout, so Redis outage cannot retain the KB lock indefinitely. One
invalid snapshot is invalidated/logged without starving later KBs. Scheduler/refresh exceptions and
leader connection loss are caught and reconnected on a later cycle; they neither hang/terminate the
queue worker nor prevent failover. Connection loss releases the advisory lock server-side.

This yields startup repair on a fresh installation, full repair after Redis restart, and periodic TTL
renewal without an unbounded query or duplicate multi-worker sweep.

## Shared external-operation guard

Purge/drop handlers follow the accepted lease pattern from document 19:

1. heartbeat in a short transaction;
2. cancel relevant ready work and make an unlocked observation of matching running work;
3. open the catalog guard transaction, acquire domain rows in the orders below, and recheck the task
   drain while those domain locks are held so a claim between observation and lock cannot write late;
4. do not hold the task row while calling storage; derive a timeout from the nonlocking
   `lease_until > clock_timestamp()` identity check before every remote mutation/verification and
   before SQL finalization;
5. retain all catalog cleanup identity until every external delete is verified absent;
6. commit SQL removal last. A crash before commit leaves sufficient identity for an idempotent replay.

Attempt, worker, lease, workspace, KB, document, deleted state, current revision, version state, and
pointer predicates are rechecked as applicable. Absent external objects/namespaces and absent SQL rows
are successful no-ops. Purge never calls `ensure_namespace`.

The lock orders are:

- ordinary ingestion remains **task -> document -> run -> KB -> version**;
- document purge uses a short task drain, then **document -> runs (revision/version sorted) -> KB ->
  versions (sorted)**;
- KB purge uses a short KB-wide task drain, then **KB -> versions (sorted)** because no document/run
  row is needed after the drain;
- index drop uses a short task heartbeat, then **KB -> target version**.

No transaction holding KB/version locks performs a separate heartbeat that could wait behind a
reclaimed attempt.

## `document.purge`

Deletion already marks the document `deleting`, records `deleted_at`, decrements live document/byte
counters, cancels ready document work, and enqueues one deduplicated purge. The handler first drains
all other running document tasks so a stage cannot write an artifact after object cleanup. Under the
guard it requires the same workspace/KB/document identity and still-current deleted revision.

It resolves every KB-owned index version, deletes the exact `document_id` filter from each existing
namespace, and verifies the filter count is zero. This covers ACTIVE, building, retired, and failed
versions and all old-revision vectors, including points whose chunk/run rows no longer describe the
remote orphan. Missing namespaces are skipped without creation.

Object cleanup uses these exact scoped identities through `ObjectStore`:

- every explicit current/historical source key derived from document/run source hashes, but only when
  no other live document in the KB references that source key/hash;
- canonical UUID prefixes `{workspace}/{kb}/parsed/{document_uuid}/` and
  `{workspace}/{kb}/assets/{document_uuid}/`;
- compact artifact-document prefix `{workspace}/{kb}/i/{base64url_uuid_without_padding}/`, which
  covers every revision, version, attempt, and uncommitted stage artifact;
- any validated explicit document/run object key not already covered by those prefixes.

All keys must pass workspace/KB scope validation before deletion. Prefix deletion is followed by an
empty bounded list check; exact keys are followed by `head is None`. Shared source keys are retained.
Only then are all document chunks, ingestion rows, and the document row deleted, and KB counters are
recomputed from live SQL state (including ACTIVE chunk count) rather than decremented a second time.

## `kb.purge`

The handler cancels every ready task for the KB except its owned purge attempt and retries while any
other KB task is running. This drains parse/chunk/embed/index/reindex/document-purge/index-drop work
before external cleanup and prevents a late artifact/vector write.

Under the deleted-KB guard it drops every existing namespace named by every owned index-version row,
including ACTIVE, building, retired, and failed versions, and verifies each absent. It then deletes and
verifies the canonical `{workspace}/{kb}/` object prefix, which includes sources, icons, parsed/assets,
and all compact pipeline artifacts without crossing another KB UUID prefix. Runtime invalidation occurs
while the KB row lock is held.

After external verification, the authorization owner facade removes/revokes KB resource grants and KB
allowlist references while applying its permission-cache invalidation policy; catalog maintenance does
not access authorization ORM. Finally catalog explicitly removes partitioned chunks and deletes the KB
row, allowing catalog foreign-key cascades to remove documents, runs, and versions. Replaying after
either external deletion or final SQL deletion is harmless.

## `index.drop`

The handler accepts only:

- `retired` with non-null `retire_after <= clock_timestamp()`; or
- `failed` while the version no longer owns either ACTIVE or building pointer.

It never drops an ACTIVE/building/pointer-owned namespace, even if a delayed payload says otherwise.
With KB/version locks held, it rechecks state and pointers, resolves absence through the driver-neutral
`VectorStore.namespace_exists` capability, drops through the existing idempotent
`VectorStore.drop_namespace` contract, and verifies absence. It leaves object artifacts in place:
another building/current run may retain an old version's parsed artifact as a validated prior pointer,
so only document/KB purge has sufficient ownership context to remove them safely. It then removes
target-version chunks, runs, and the version row. A missing namespace or row is success; an early
retired task is retryable at its retention deadline; no path creates a namespace.

## TDD and validation order

After coordinator approval:

1. Add unit REDs for unusable-runtime invalidation, row-lock publication ordering, bounded cursor
   refresh, leader/scheduler continuation after failure, multi-worker singleton behavior, key-prefix
   derivation, purge identity validation, and ACTIVE/building/retention drop fences.
2. Run those exact tests and record expected failures before production edits.
3. Implement the minimum runtime/scheduler behavior, rerun GREEN, then implement purge/drop one RED
   group at a time.
4. Run owned `tests/integration/test_catalog_maintenance.py`, relevant catalog/maintenance/runtime
   regressions, and the coordinator-owned product-maintenance acceptance file against the dedicated
   external services.
5. Run focused Ruff format/check, mypy for Python source, and `git diff --check`; do not duplicate the
   coordinator's full suite.
6. Record exact commands/results here and explicitly release the PostgreSQL/Redis window.

## Evidence log

### 2026-09-15 design checkpoint

Repository/docs inspection and the coordinator RED are recorded above. No package-A production or
test file has been edited yet. Design is ready for coordinator review before TDD begins.

### 2026-09-15 coordinator approval and implementation plan

The coordinator approved the design without another implementation pause and added four corrections,
all incorporated above: drain recheck under domain locks, deleting-KB fences at registration/run/stage
commit boundaries, no version-scoped artifact deletion, and bounded scheduler/cache connections and
timeouts.

Implementation proceeds in five TDD verticals:

1. **Registration and runtime coherence:** add REDs for maintain handler registration, deleting/archive
   invalidation, stale publish ordering, and bounded Redis timeout; implement locked runtime snapshots.
2. **Automatic refresh:** add REDs for immediate worker-start repair, bounded batch calls, singleton
   leadership, failure/reconnect continuation, and shutdown; implement the dedicated-connection
   scheduler and the two approved settings.
3. **Document purge:** add REDs for all-version vectors, multiple revision/attempt prefixes, shared
   source retention, drain race, replay, and counters; implement the document/run/KB/version guard and
   external-before-SQL finalization.
4. **KB purge and write fences:** add REDs for late registration/stage commits, every namespace and
   canonical object prefix, grant-owner facade cleanup, running-work retry, and replay; implement the
   fences and KB finalizer.
5. **Index retirement:** add REDs for retention, ACTIVE/building pointer changes, failed versions,
   absent namespace/row replay, and retained prior artifacts; implement idempotent drop/finalization.

Each vertical runs its exact RED before production edits and exact GREEN afterward. Final validation is
the owned integration file, coordinator acceptance file, focused adjacent regressions, Ruff, mypy, and
`git diff --check`; the coordinator retains the full-suite run.

The first registration RED ran against the dedicated services and failed as expected in 1.59 seconds:
the maintain worker exposed only the four platform handlers and `kb.reindex_fanout`, with all three
catalog-maintenance kinds absent. The minimal catalog-maintenance registration seam is now present;
behavior remains intentionally unimplemented until its own REDs.

The registration RED became GREEN in 1.08 seconds. The runtime race RED then failed in 4.48 seconds
with the paused publisher restoring ACTIVE JSON after `delete_kb` had committed and invalidated it,
proving the test exercises stale resurrection rather than a cache miss. Implementation now moves the
snapshot/cache operation under the authoritative KB row lock with a two-second cache timeout.

The runtime race and parent deleted-KB check became GREEN together: 2 passed in 5.16 seconds. The
document purge RED first exposed and corrected a missing test import, then failed at the intended
product assertion with one real pgvector point remaining and the placeholder handler skipping. After
the guarded vector/object/SQL implementation, owned plus parent document purge passed: 2 passed in
4.78 seconds. Scheduler tests are now RED at the missing scheduler type/bootstrap seam.

The two scheduler REDs covered immediate retry after a failed cycle and automatic maintain-worker
startup/shutdown; both became GREEN in 2.40 seconds. The KB purge RED then failed at the intended
still-existing real namespace before the deletion barrier and handler were implemented.

The first KB implementation rerun exposed asyncpg's inability to infer a nullable exclusion parameter
in the cancellation SQL; two explicit query shapes corrected it. The KB vertical then passed in 4.50
seconds. The index-drop RED preserved ACTIVE v1 but failed with the unpointed failed-v2 namespace still
present, proving both sides of the intended fence before implementation.

After the complete owned/parent package command passed 11 tests in 9.06 seconds, coordinator review
required two concurrency corrections: commit advisory-lock/keepalive transactions while retaining the
session lock, and make ready-task cancellation use `FOR UPDATE SKIP LOCKED` before the under-domain-lock
live-work recheck. API-key allowlist cleanup also revokes a key whose final explicit KB is removed,
because changing `[deleted_kb]` to the empty-list “all owner KBs” meaning would broaden authority.

Focused Ruff passed and mypy reported no issues in 116 source files. The first post-review combined
run reached 12 passing tests and one KB-purge failure: SQLAlchemy's base `ARRAY.contains()` type-checks
but raises `NotImplementedError` at execution. Namespace/object deletion completed, SQL identity rolled
back, and the task rescheduled, exercising the intended external-before-SQL replay path. The authz
owner query now uses PostgreSQL `ANY` membership.

After `ANY`, focused static checks and the package command passed: 13 tests in 10.99 seconds, then
seven adjacent catalog/reindex/recovery/authz regressions passed in 9.22 seconds. Two final edge REDs
then proved that document purge must enumerate namespaces through the durable high-water even when a
version row is missing, and that shared-source safety is key-or-hash rather than key-and-hash. The
finalizer's ACTIVE chunk recount is also narrowed to chunks belonging to live documents.

Final review rejected iterating `1..index_version_high_water`: migrated allocator evidence may be
sparse and near the integer ceiling, making purge proportional to reserved history rather than actual
storage. The vector-store contract now exposes scoped `list_namespaces(kb_id)`; pgvector queries only
the exact physical KB prefix and the fake mirrors it. The rowless-orphan test sets high-water to one
billion while creating only v1/v2, so cleanup work remains proportional to the two actual namespaces.

### 2026-09-15 final targeted evidence and DB release

Post-review scheduler coverage in the coordinator-owned acceptance file evicts the real Redis runtime
twice while the actual PostgreSQL-advisory-led scheduler is running; both losses are repaired without a
manually enqueued task. The final owned integration file also covers bounded batch size 1, stale
publisher/delete ordering, rowless high-water namespace cleanup, shared-source retention, canonical
UUID and compact artifact prefixes, KB grant/object/namespace cleanup, ACTIVE/failed drop fencing, and
retired retention before/after its deadline.

The final dedicated-service package command was:

```powershell
uv run python -m pytest -o addopts= `
  tests/integration/test_catalog_maintenance.py `
  tests/integration/test_product_maintenance_acceptance.py `
  tests/unit/catalog/test_maintenance_scheduler.py -q
```

Result after the first high-water/shared-source corrections: exit 0, **14 passed, 1 existing Alembic
warning in 10.57 seconds**. Immediately before those two corrections the same command passed 13 tests
in 10.99 seconds; its one earlier `ARRAY.contains()` failure retained SQL identity and rescheduled after
external deletion, then became GREEN with PostgreSQL `ANY`. Coordinator review then replaced the
bounded-but-still-history-proportional integer range with actual driver namespace listing and changed
the test high-water to one billion. Because the DB window had already been explicitly released, that
final pgvector case is assigned to coordinator independent verification and is not falsely reported as
part of the earlier 14-test result.

Adjacent regression command covered catalog runtime activation, upload rejection after KB deletion,
reindex activation publication retry, build-deletion cleanup, stage recovery dedupe, and grant union:
exit 0, **7 passed, 1 existing Alembic warning in 9.22 seconds**.

Final static evidence after the last production edit:

- focused `uv run python -m ruff check ...`: **All checks passed**;
- focused `uv run python -m ruff format --check ...`: **12 files already formatted**;
- `uv run python -m mypy src apps/worker`: **Success: no issues found in 116 source files**;
- scoped `git diff --check`: exit 0 with only existing LF-to-CRLF worktree notices.

The exclusive PostgreSQL/Redis window was released to the coordinator immediately after the final
14-test command. No DB, Redis, Docker, or full-suite command was run after release. Package A is ready
for coordinator independent review; overall five-part delivery remains active and is not claimed
complete.

### 2026-09-15 namespace-listing correction verification

At the coordinator's explicit request, the DB window was briefly returned only for the final namespace
enumeration correction. The exact one-billion-high-water pgvector test was:

```powershell
uv run python -m pytest -o addopts= `
  tests/integration/test_catalog_maintenance.py::test_document_purge_removes_vectors_objects_and_catalog_identity `
  -q
```

Result: exit 0, **1 passed, 1 existing Alembic warning in 3.88 seconds**. The test creates only actual
v1/v2 namespaces, removes the v2 version row, sets `index_version_high_water=1000000000`, and confirms
document purge removes vectors from both actual namespaces without iterating the reserved integer
range. PostgreSQL/Redis ownership was immediately and explicitly released again; no service command
will be run by this worker after this checkpoint.

Current package-A status: no known failing targeted test and no implementation blocker. Coordinator
independent maintenance acceptance is running. The exact scheduler unit file is
`tests/unit/catalog/test_maintenance_scheduler.py`; the actual Redis/advisory-lock repair case is in
the coordinator-owned `tests/integration/test_product_maintenance_acceptance.py`. Package B API,
package C browser acceptance, and package D deployment acceptance remain serial follow-up work and
are not part of this package-A claim.
