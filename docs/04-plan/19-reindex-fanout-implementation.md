# Lifecycle slice 2: bounded resumable reindex fan-out

Updated: 2026-09-10. Status: bounded slice2 independently ACCEPTED by coordinator.

Final parent evidence: 40 focused tests passed (37.64s); full backend 734 passed, 2 known PDF/OCR
collection errors, no assertion failures/skips, coverage85.05% (174.70s). Full command correctly
exits1. Global lint, format169, mypy111, import contracts6 and route authz50/8 passed. Reports live
under ignored `data/acceptance-20260910/`; see ledger L71-L72. Hume was closed after the final
fallback correction. Historical intermediate failures/rejections below remain as audit evidence;
they are not the current result. Whole lifecycle/E2E and the explicit limitations remain OPEN.
This file is the durable implementation journal for lifecycle slice 2. It records every meaningful
design, RED/GREEN, migration, review, and verification step before the next production step begins.
No whole-lifecycle acceptance claim is made here.

## Scope and ownership

Deployment boundary: migration0009 and the new workers must be rolled out together with old
publishers/workers drained. Pre-0009 binaries do not enforce enrollment state; this slice does not
claim safe mixed-version rolling operation. The legacy snapshot/cache rollout restrictions in
document17 also remain applicable. No application database was migrated by this verification run.

This increment implements only slice 2 from `16-lifecycle-acceptance-plan.md`: production maintain
consumption of `kb.reindex_fanout`, bounded and resumable enrollment, coherent concurrent document
changes, validated parse-artifact reuse, stale build/task fencing, and an activation enrollment and
coverage barrier. It may repair `fail_index_version` because an obsolete failure must not clear a
newer build.

The implementation owns catalog DTO/model/repository/service/facade changes, ingestion artifact and
pipeline changes needed for parse reuse, production worker registration, migration `0009`, focused
tests under `tests/integration/test_reindex_fanout.py` and relevant unit tests, and this journal.
The parent owns `docs/04-plan/15-execution-ledger.md` and
`tests/integration/test_reindex_fanout_acceptance.py`; neither will be edited. Existing slice-1 and
user changes remain in place. There will be no commit, revert, clean, branch, worktree, or descendant
agent.

Explicit exclusions remain `chunk.reembed`, index-retirement handlers, cache-generation redesign,
recovery entrypoints, PDF, and OCR. Existing retirement task scheduling may be preserved at an index
switch, but this slice will not add a consumer for it. A narrowly build-scoped deleted-document
vector cleanup is included because a deletion committed before activation must not leave an orphan
in the namespace being activated. Provider and storage credentials remain in their gateways and
never enter task payloads or ingestion artifacts.

## Initial evidence, before production edits

The worktree starts at commit `e9033ba` with accepted slice-1 changes and unrelated user/parent
changes already present. Required package documents 16, 17, and 18 were read in full. Source
inspection established:

* `start_reindex` durably creates a building version and one `kb.reindex_fanout` task, but the
  production maintain worker does not register that kind.
* `register_upload` and `start_revision` already choose the building version before active, giving
  this bounded slice a coherent building-only visibility policy rather than dual-write behavior.
* `_commit_indexed` activates when currently enrolled nonfailed ledgers reach zero. It has neither
  an enrollment-complete barrier nor proof that every current live document has an indexed target
  ledger. Terminal failed ledgers are excluded and can therefore produce false success.
* Stage commits lock task, document, ingestion run, and KB in that order. Fan-out must not hold the
  KB lock while acquiring document locks.
* `ParseManifest` includes document, revision, source hash, and index version. A prior envelope can
  be validated and its parsed value reused, but the old envelope cannot become the new run's
  artifact because its index identity differs.
* Task dedupe applies only while a task is ready or running. Durable fan-out generation must
  therefore fence delayed/replayed predecessor tasks even after a successor has completed.
* `fail_index_version` currently clears the building pointer without checking that the requested
  version still owns it.

Parent-owned independent RED evidence was produced on fresh PostgreSQL/pgvector and Redis before
implementation:

```powershell
$env:CAIRN_TEST_USE_EXTERNAL_SERVICES='1'
$env:CAIRN_DATABASE_URL='postgresql+asyncpg://cairn:cairn-disposable-only@127.0.0.1:39170/cairn_acceptance'
$env:CAIRN_REDIS_URL='redis://127.0.0.1:38694/0'
uv run python -m pytest -o addopts= tests/integration/test_reindex_fanout_acceptance.py -q
```

Result reported by the parent: exit 1, **3 failed in 27.47s**, exactly covering missing production
handler registration, activation of v2 by a new upload while old documents remain undispatched,
and delayed failure of v2 clearing building v3. Durable report:
`data/acceptance-20260910/fanout-red.xml`.

The parent then transferred the exclusive disposable database window to this implementation.

## Considered enrollment designs

### Cursor only

A single UUID cursor is small and naturally resumable, but it cannot discover a document inserted
or revised with an ID behind the cursor unless every writer is permanently perfect. It also gives
activation no independent proof that the cursor covered the current live set. This option is
rejected.

### Frozen roster

Materializing every document identity into a separate build-roster table gives an explicit snapshot,
but creates an unbounded start transaction or a second fan-out problem, and concurrent replacement
and deletion still require reconciliation against current revisions. The extra durable entity does
not remove the coverage query. This option is rejected for the bounded slice.

### Cursor, direct enrollment, and bounded reconciliation

The chosen design combines three mechanisms:

1. A version-owned cursor performs the ordinary ordered scan in bounded batches.
2. Upload and replacement writers atomically enroll their current revision into the building
   version while that build owns the KB pointer. This closes changes behind the cursor and defines
   visibility as building-only until activation.
3. After the ordered scan is exhausted, bounded reconciliation repeatedly selects current live
   documents missing a target-version ledger. Completion is then recorded under the KB build guard.
   Activation separately proves complete current live-document coverage, so cursor or ledger drift
   cannot become false success.

This approach is bounded, does not rely on UUID order as a creation-time fence, preserves the
existing writer policy, and adds no snapshot-roster table.

## Durable version enrollment state

Migration `0009` will add these fields to `kb_index_version`:

* `enrollment_state`: `scanning`, `reconciling`, or `complete`.
* `enrollment_cursor`: nullable UUID containing the greatest primary-scan document ID committed.
* `enrollment_generation`: nonnegative integer identifying the one fan-out task allowed to advance
  the current state.

Fresh rebuilds with an active version start at `scanning`, null cursor, generation zero. Initial
upload-created versions remain `complete`; this preserves the existing four-stage first-upload path,
which already enrolls each document transactionally. Historical control-plane creation of an empty
initial v1 also remains explicitly activatable.

For migration, an existing building version that is the KB's building pointer and has an old active
version becomes `scanning`; other existing rows become `complete`. Existing pre-`0009` fan-out task
payloads without a generation are interpreted as generation zero only. New task payloads always
carry both positive `index_version` and nonnegative `enrollment_generation`.

The production batch size is the trusted constructor argument
`cairn.catalog.reindex.ReindexFanoutService(batch_size=...)`, implemented in
`src/cairn/catalog/reindex.py`, with a bounded validation range, defaulting to 100 and capped at
1000. Tests may construct the service with batch size 1 or 2. Task payloads cannot choose or enlarge
it.

## Fan-out transaction and lock order

Each maintain execution performs at most one bounded selection and uses the database state—not task
cursor input—as the source of truth:

1. Lock and validate the owned task attempt.
2. Read the version's phase/cursor only for bounded candidate selection.
3. Select at most `batch_size` candidate document IDs. In `scanning`, select live IDs greater than
   the durable cursor. In `reconciling`, select live current revisions with no target ledger.
4. Lock only those selected document rows, in ascending UUID order.
5. Lock the KB row and revalidate workspace, target building pointer, positive target version, and
   nonnegative expected generation.
6. Lock/revalidate the target version row and require `building` plus the expected enrollment phase
   and generation.
7. Revalidate each locked document's liveness/current revision and idempotently insert its
   revision/version ledger plus first `document.parse` task in the same transaction.
8. Advance cursor/phase and generation, and transactionally enqueue exactly one generation-fenced
   successor if more scan or reconciliation work remains.
9. Perform a final nonlocking wall-clock ownership check for the current task attempt, commit, then
   best-effort notify `parse` and `maintain` queues.

The coherent lock order is therefore **task → selected documents (sorted) → KB → index version**.
No transaction holds the KB row while acquiring document locks, and no locked query scans an
unbounded document set. Ordinary stage commits retain **task → document → run → KB → version**.
Writers that replace a source retain **document → KB**. Upload registration locks the KB but creates
a new document rather than acquiring an existing document lock, so it does not form the reverse
edge needed for a fan-out deadlock.

A transaction crash loses cursor advancement, ledger inserts, parse tasks, and successor together;
retry repeats the same bounded selection safely. A crash after commit leaves the successor durable.
A delayed predecessor whose payload generation no longer equals the version generation is skipped,
even if its old task was later reclaimed after the live-task dedupe window ended.

## Cursor reconciliation and concurrent changes

The primary scan stores the greatest selected UUID after every committed batch. When no greater live
ID exists, it changes to `reconciling` and schedules the next generation. Reconciliation starts from
the whole current live set each time but returns only the first bounded set lacking target ledgers;
successful enrollment removes those rows from the next result. No unbounded rows are locked or
materialized.

Concurrent behavior linearizes at document/KB locks:

* New uploads while a build owns the pointer get a target ledger and parse task in the upload
  transaction. They are not written to the old active namespace. If activation linearizes first,
  the upload targets the newly active version instead.
* Replacements lock document then KB, increment revision, and enroll that revision in the building
  version. An old target task fails the existing revision/source-hash ownership fence. If replacement
  commits before activation coverage is checked, the new revision blocks activation until indexed;
  if it commits after the switch, it targets the new active version.
* Deletion makes the document nonlive and cancels its document tasks. If the document has any chunk
  rows in the building version, the deletion transaction also creates one version-fenced
  `document.reindex_delete` task. A task that reaches an ordinary stage afterward fails the existing
  live-document fence. If deletion linearizes after activation, normal deletion cleanup owns removal
  from the active namespace; this slice does not add the general purge or retirement handler family.

Reconciliation is defense in depth for behind-cursor rows, migrated in-flight builds, and any
bounded writer race. It does not replace direct writer enrollment.

## Ledger and first-task idempotency

The target ledger identity remains `(document_id, revision, index_version)`. Catalog repository
insertion will use PostgreSQL conflict-ignore semantics and report whether it inserted. Only the
transaction that inserts the ledger creates its first parse task. The task identity and dedupe key
include document, revision, and target index version. Replay therefore neither creates a second
ledger nor a second first-stage task.

For ordinary upload/replacement paths, existing row locks and unique ledger identity continue to
make the ledger and first parse task atomic. Their parse dedupe keys will be normalized to include
index version so a current revision can never alias work for another target version.

## Parse-artifact reuse

When fan-out enrolls an unchanged current revision, it stores the best prior parse-envelope key on
the new ledger. The key is a credential-free artifact reference, not a provider or storage secret.
The parse worker still validates the target version's full `ModelRef` snapshot before any artifact
write, preserving slice-1 drift fencing.

If a prior key exists, the parse worker loads and checksum-decodes that envelope, verifies document,
revision, and source-content hash, and accepts the old index version only as provenance for reuse.
A valid artifact bypasses source loading and parser invocation. Its parsed value is encoded into a
new immutable `ParseManifest` carrying the target index version and written under the target run's
fresh attempt key. The normal `commit_parsed` transition then records only the new envelope.

If the prior artifact is absent, unreadable, corrupt, or has mismatched document/revision/hash, the
worker falls back to the ordinary source-integrity and parser path. It never points the new ledger at
the old envelope. Later chunk/embed/index stages retain exact manifest identity checks.

## Build-scoped deletion cleanup

Excluding a deleted ledger from pending counts is insufficient: a first fan-out batch may already
have indexed target points before the source is deleted. Activating that namespace would publish
orphan points because the general `document.purge` handler is not implemented yet.

For a deletion while a version is building, catalog creates a target-version
`document.reindex_delete` task after cancelling the document's ordinary tasks. Its payload contains
only document and index identity. The production index worker registers its handler alongside
`document.index`, using the injected production `PipelineRuntime` storage resolver just like normal
indexing.

The cleanup external guard mirrors the existing `index_mutation` lease design. It first performs a
short owned-task heartbeat transaction, then opens the document/KB/version guard transaction without
holding the task row `FOR UPDATE`. It confirms the document is deleted/deleting and the target still
owns the building pointer, then returns a catalog DTO with the credential-free vector binding and the
target document identity. Before each remote filtered delete/count and before catalog commit it uses
a nonlocking `lease_until > clock_timestamp()` ownership query to derive a timeout budget. It does
**not** call `context.heartbeat()` while document/KB/version locks are held, because heartbeat's
separate transaction would block behind task ownership and can deadlock with a reclaimed attempt.

The handler deletes by the vector contract's exact `document_id` filter and verifies the same filter
counts zero. This removes every indexed revision, including an old revision whose catalog chunks were
replaced before its successor reached vector indexing. Only then does catalog commit remove the
building-version chunk rows and set deleted runs' point counts to zero. Crash before catalog commit
leaves rows that cause an idempotent retry; crash before external deletion leaves both rows and
vectors. The unsafe ordering—catalog row removal before remote deletion—is never used. The effective
cleanup order is therefore a short task heartbeat followed by **deleted document → KB → target
version**, with nonlocking task-clock checks around external work.

The deletion commit reevaluates the same activation barrier as fan-out and ordinary index commit.
If cleanup finishes before enrollment completion it cannot switch. If enrollment finishes first,
catalog-tracked orphan points keep the build blocked until cleanup commits. Remote vector drivers
remain subject to the already documented consistency limits, but the handler performs the strongest
available read-after-delete verification before catalog success.

## Activation barrier and failure semantics

Automatic worker activation and explicit administrative activation of a real rebuild require all of
the following under the locked KB and target-version guard:

* the KB building pointer still equals the target version;
* the target version state is `building`;
* enrollment state is `complete`;
* an exact anti-join finds zero current live documents lacking a target ledger for their exact
  current revision and source hash in `indexed` state;
* an exact catalog query finds zero target-version chunk points belonging to deleted/deleting
  documents.

The anti-join starts from `document`, not `document_ingestion`, so activation proves expected current
coverage rather than merely completion of the ledgers that happen to exist. A terminal failed run
remains missing coverage and leaves the build blocked; this slice does not falsely activate or add
the recovery workflow planned for the next slice. Deleted/deleting rows are not expected live
coverage, but their target chunk rows are a separate activation blocker until verified remote
cleanup commits.

Index commit flushes the just-indexed run before evaluating coverage. First-batch completion cannot
activate while enrollment is `scanning` or `reconciling`. A new upload already enrolled directly
also cannot activate the rebuild while preexisting documents remain undispatched.

True empty **rebuilds**—a KB with an active version but zero current live documents—are rejected in
the confirmed `start_reindex` transaction before allocating a version or creating a fan-out task.
Documents may all be deleted after enrollment starts and before any target namespace is created, so
finalization also counts current live documents under the locked KB/version guard. When that count is
zero, it explicitly fails/abandons the still-owned building version, records a bounded error, clears
only its matching building pointer, preserves and republishes nothing over the old active version,
and keeps the old active runtime coherent. It does not activate or strand a nonexistent namespace.
This bounded policy applies even if a target namespace happened to be created before the final
deletion; its eventual cleanup remains covered by the existing failed-version drop scheduling seam.

A never-ingested control-plane KB remains outside both rebuild rejection paths for compatibility
with accepted historical administrative tests; its versions require the existing explicit activation
call rather than fan-out autoactivation. The initial-v1 explicit activation seam also remains
compatible while no active version exists. Once both an active version and document history exist,
explicit activation is a real rebuild switch and must pass the same enrollment, exact current-live
coverage, and deleted-point barrier as automatic activation. Rebuilding with zero live documents is
rejected or, if deletion races an admitted build, failed during finalization.

Activation keeps the old active version/model coherent until the atomic pointer switch. Automatic
switches preserve the existing retirement timestamp and enqueue semantics but do not implement an
`index.drop` consumer. Runtime publication remains after the catalog commit. Remote vector writes
remain nontransactional relative to PostgreSQL, as already documented.

## Stale failure fence

`fail_index_version(kb_id, version, error)` will lock the KB and require its building pointer to equal
`version`. A delayed failure for an already failed/retired/active version or while a newer version is
building raises `Conflict` without changing the KB or any version row. A valid current failure marks
only that building row failed, clears only its matching pointer, preserves the active version, and
keeps the existing drop-task scheduling behavior.

## TDD implementation sequence

1. Add owned integration RED cases for migration state, two-or-more fan-out batches, cursor replay,
   generation fencing, exact coverage, direct upload/replacement enrollment, deletion convergence,
   terminal failure blocking, empty rebuild rejection, now-empty finalization failure, parser bypass
   with a new envelope identity, build-scoped deleted-vector cleanup, and initial-upload regression.
2. Add focused unit RED cases for trusted batch-size validation, parse reuse validation/fallback,
   and production maintain registration where useful without duplicating parent ownership.
3. Run each focused case with `uv run python -m pytest -o addopts= ...` and append exact expected RED
   output before production edits for that behavior.
4. Add migration/model/repository primitives minimally, then rerun their tests GREEN.
5. Implement the generation-fenced fan-out and production registration, then rerun multi-batch,
   replay, and concurrency-boundary tests GREEN.
6. Implement parse republishing and fallback, then rerun parser-call and envelope-identity tests
   GREEN.
7. Add the shared coverage barrier, build-scoped deleted-vector cleanup, and stale failure fence,
   then rerun failure, deletion, no-early-activation, and parent acceptance files GREEN.
8. Run targeted catalog/ingestion regressions, `ruff` and `mypy` through `uv run python -m`, and
   `git diff --check`. Record exact evidence and fake-versus-actual-model scope here.
9. Release the exclusive PostgreSQL/Redis window to the parent after targeted GREEN. The parent owns
   independent review and the full suite, so this implementation will not duplicate that full-suite
   run.

Coordinator review approved this design direction on 2026-09-10 with the cleanup lease-order and
now-empty finalization corrections incorporated above. TDD implementation may proceed without
another approval pause.

## Evidence log

### 2026-09-10 expanded coordinator acceptance RED

After the coordinator transferred the exclusive disposable PostgreSQL/Redis window, the updated
four-case parent-owned acceptance file was run unchanged before production edits:

```powershell
$env:CAIRN_TEST_USE_EXTERNAL_SERVICES='1'
$env:CAIRN_DATABASE_URL='postgresql+asyncpg://cairn:cairn-disposable-only@127.0.0.1:39170/cairn_acceptance'
$env:CAIRN_REDIS_URL='redis://127.0.0.1:38694/0'
uv run python -m pytest -o addopts= tests/integration/test_reindex_fanout_acceptance.py -q
```

Result: exit 1, **4 failed, 1 warning in 6.99s**. The failures were exactly:

* production maintain worker had no `kb.reindex_fanout` handler;
* a new upload's v2 index completion activated before the existing v1 document was enrolled;
* delayed `fail_index_version(v2)` cleared the newer building-v3 pointer;
* the production worker-factory rebuild could not execute fan-out, so v2 stayed building and parse
  artifact reuse was unreachable.

The only warning was the existing Alembic `path_separator` deprecation. No production file had been
edited when this RED command ran.

### 2026-09-10 owned schema and constructor RED

The first owned tests were added in `tests/integration/test_reindex_fanout.py`, then run against the
exclusive disposable services:

```powershell
uv run python -m pytest -o addopts= tests/integration/test_reindex_fanout.py -q
```

Result: exit 1, **3 failed, 1 warning in 4.98s**. Both constructor-bound cases failed because
`cairn.catalog.reindex` did not exist. The initial-upload state case failed because
`kb_index_version.enrollment_state`, `enrollment_cursor`, and `enrollment_generation` did not exist.
These are the expected missing-feature failures; the only warning was Alembic's existing
`path_separator` deprecation.

### 2026-09-10 owned schema and constructor GREEN

After adding migration `0009`, ORM enrollment fields, and only the bounded constructor skeleton:

```powershell
uv run python -m pytest -o addopts= tests/integration/test_reindex_fanout.py -q
```

Result: exit 0, **3 passed, 1 warning in 3.62s**. The warning remained the existing Alembic
deprecation. No fan-out handler behavior existed at this checkpoint.

### 2026-09-10 bounded multi-batch fan-out RED

An owned two-document integration test was added with trusted `batch_size=1`. It requires one
ledger/parse task after the first generation, indexing that first batch without activation, a stale
generation-zero replay that cannot advance state, bounded completion of the second scan batch and
reconciliation, exactly two v2 ledgers/tasks, and final activation.

```powershell
uv run python -m pytest -o addopts= \
  tests/integration/test_reindex_fanout.py::test_one_document_batches_resume_and_generation_replay_is_safe \
  -q
```

Result: exit 1, **1 failed, 1 warning in 4.68s**. Setup reached an active two-document v1 and opened
v2; the test then failed because `ReindexFanoutService.handle` did not exist. This is the expected
missing-behavior RED.

The first GREEN attempt exposed one transactionally contained implementation defect: PostgreSQL
rejected the conflict-safe ledger insert because it passed the transient ORM object's unset Python
default (`None`) as the nonnullable state. The fan-out task retried and its cursor/generation/ledger
changes all rolled back. Comparing the working ORM-add path confirmed the root cause; the repository
upsert now writes the explicit domain initial state `registered`.

Rerunning the exact focused command after that one-line correction resulted in exit 0,
**1 passed, 1 warning in 4.81s**. This proves the bounded scan/reconcile and generation-fence cycle;
parse reuse and build-deletion cleanup were not yet implemented at this checkpoint.

### 2026-09-10 expanded acceptance checkpoint after bounded fan-out

The parent-owned five-case acceptance file was rerun unchanged after bounded fan-out and the
activation enrollment barrier:

```powershell
uv run python -m pytest -o addopts= tests/integration/test_reindex_fanout_acceptance.py -q
```

Result: exit 1, **3 failed, 2 passed, 1 warning in 8.83s**. Production factory registration and a
new upload not activating before old-document enrollment passed. Expected remaining product REDs
were the stale v2 failure clearing v3 and the rebuild invoking the parser a second time. The deletion
case stopped before deleting the document because the parent-owned test supplied `Compare(op='eq')`
while the vector filter contract uses `'$eq'`; it raised `KeyError('eq')`. That file remains untouched.
An owned deletion test will use the published operator and exercise the intended product behavior.

### 2026-09-10 implementation checkpoint: fan-out, reuse, concurrency, publication

Status: active, not blocked. The implementation now includes migration/model enrollment state,
trusted batch construction, generation-fenced bounded scan and reconciliation, atomic conflict-safe
ledger plus first-task creation, exact live-document activation coverage, production maintain
registration, stale `fail_index_version` ownership fencing, validated parse reuse with a newly
published target envelope, and committed-switch publication retry.

The `0009` migration was initially exercised before the parse-reuse pointer was added. The first
attempt to recycle that disposable schema was rejected before connecting because the standalone
Alembic shell lacked required Redis/master-key configuration. With complete test configuration, the
next downgrade correctly reported that the old local `0009` shape did not yet contain the newly
declared column; PostgreSQL rolled the transaction back. The local development downgrade was made
tolerant with `DROP COLUMN IF EXISTS`, after which this exact bounded cycle exited 0:

```powershell
uv run python -m alembic downgrade 0008_version_snapshots
uv run python -m alembic upgrade head
```

Alembic reported `0009_reindex_fanout -> 0008_version_snapshots` followed by
`0008_version_snapshots -> 0009_reindex_fanout`. Fresh upgrades receive all declared columns in one
transaction.

Coordinator review identified a SQLAlchemy identity-map race in the unlocked-observation then
locked-version reread. An owned deterministic regression paused generation 0 after its unlocked
selection, advanced generations 0 and 1 in another worker, then released the stale predecessor.
Before the fix it failed in **4.90s**: the stale handler returned success, rewound generation 2 to 1,
and deduplicated against an obsolete successor. The locked repository read now uses
`populate_existing=True`, while phase, cursor, and generation are copied to immutable scalars before
refresh. The exact test then exited 0 with **1 passed, 1 warning in 4.29s**; generation 2, cursor,
single ledger, and single parse task remained intact.

Focused parent-owned stale-failure and parser-reuse cases were then run. Result: **1 passed, 1
failed, 1 warning in 5.86s**. The stale failure fence passed. Parse reuse satisfied all reached
product assertions: v2 activated, parser call count stayed one, vector counts matched v1, and the v2
ledger was indexed with a nonnull new parsed key. The parent test's final envelope read omitted the
required `LocalObjectStore.get_bytes(max_bytes=...)` argument and raised `TypeError`; the parent file
remains untouched, and owned verification will read the envelope through the published API.

A committed-switch publication regression was added. RED exited 1 in **4.29s** because
`ReindexFanoutService` had no publisher seam. The implementation now accepts an injected publisher
for testing and recognizes only the exact active, complete target at `generation == payload + 1` as
a publication-only retry. GREEN exited 0 with **1 passed, 1 warning in 4.24s**: first publication
failure left v2 committed while Redis still held v1; retry published v2 and retained one v2 ledger.

### 2026-09-10 deletion and empty-policy RED

The corrected parent deletion case and two owned empty-policy cases were run together before their
implementation:

```powershell
uv run python -m pytest -o addopts= \
  tests/integration/test_reindex_fanout.py::test_now_empty_rebuild_fails_without_replacing_active_version \
  tests/integration/test_reindex_fanout.py::test_empty_rebuild_is_rejected_before_version_allocation \
  tests/integration/test_reindex_fanout_acceptance.py::test_deleting_an_indexed_first_batch_removes_its_points_before_switch \
  -q
```

Result: exit 1, **3 failed, 1 warning in 7.21s**. A now-empty v2 incorrectly activated over v1; an
already-empty active KB allocated v2 instead of rejecting; and indexed deleted v2 points kept
activation blocked because no build-scoped cleanup task existed. These are the expected product
REDs. The implementation for all three is now present but has not yet been rerun at this checkpoint.

Remaining product verification at this checkpoint:

* make the three deletion/empty cases GREEN and validate cleanup's lease/lock behavior;
* add owned exact parse-envelope identity/fallback coverage;
* run the parent five-case file after its harness fixes, the owned fan-out file, initial-upload and
  targeted catalog/ingestion regressions, and the parent-owned real-TEI rebuild file;
* run Ruff, mypy, and `git diff --check`, record exact evidence, then release the exclusive database
  window to the parent without duplicating the parent-owned full suite.

### 2026-09-10 final targeted implementation evidence

Status: targeted implementation GREEN; no blocker and no hung command. The exclusive disposable
PostgreSQL/Redis window is released to the parent after the commands recorded in this section. This
implementation does not run the parent-owned full suite.

Deletion, empty-build, source-scope, and revision-chain evidence:

* The three-case deletion/empty RED above became **3 passed, 1 warning in 6.07s** after adding
  build-scoped cleanup, empty-start rejection, and now-empty finalization failure.
* Moving parse reuse initially regressed the accepted source-key guard. The existing wrong-KB and
  traversal test failed twice because object resolution occurred first. Restoring source scope
  validation before embedding/storage resolution made the exact test **2 passed, 1 warning in
  4.05s**. An owned outside-prior-pointer test additionally proves that an invalid prior artifact
  reference is never read and falls back only to the already validated source path.
* Parent review added a replacement-before-delete variant. Before filtered cleanup, ordinary
  deletion passed but replacement deletion failed with one old-revision v2 vector remaining:
  **1 failed, 1 passed in 7.12s**. Cleanup now deletes by exact namespace `document_id` filter and
  verifies the same filter counts zero; both variants then passed: **2 passed in 6.48s**.
* An owned before-any-v2-index deletion test initially failed because filtered delete reached a
  nonexistent namespace and retried. Cleanup now calls the driver-neutral `ensure_namespace` with
  dimension and metric from the immutable version snapshot under the lease guard. The exact test
  then passed: **1 passed in 5.34s**.
* Adding cleanup to every building version initially broke the original first-upload deletion
  regression: a no-op cleanup claimed its sole index slot and v1 did not activate. After limiting
  initial-build cleanup to documents with target chunks, while retaining unconditional rebuild
  cleanup, the original test passed: **1 passed in 3.69s**.
* Terminal parser failure and replacement behind the durable cursor passed together: **2 passed in
  5.11s**. Terminal failure leaves enrollment complete but v2 building/blocked; replacement creates
  the exact revision-2 ledger directly, stale revision 1 cannot advance, and activation waits for
  revision 2 to index.

Activation-publication recovery evidence:

* Fan-out-triggered committed activation publication retry passed: **1 passed in 4.24s**. First
  publication failure left v2 committed and v1 cached; retry performed publication only and repaired
  Redis to v2 without changing ledger count.
* Cleanup-triggered committed activation publication retry passed: **1 passed in 5.32s**. The retry
  validated task/document/workspace/KB/active-version ownership and republished without repeating
  deletion, enrollment, parsing, embedding, or indexing.

Primary fan-out gates:

```powershell
uv run python -m pytest -o addopts= \
  tests/integration/test_reindex_fanout.py \
  tests/integration/test_reindex_fanout_acceptance.py -q
```

Result after filtered cleanup: **19 passed, 1 warning in 21.87s**. A final fresh run after namespace
creation, formatting, and type narrowing also included the original first-upload deletion control:

```powershell
uv run python -m pytest -o addopts= \
  tests/integration/test_reindex_fanout.py \
  tests/integration/test_reindex_fanout_acceptance.py \
  tests/integration/test_ingestion_pipeline.py::test_deleted_current_document_does_not_leave_build_aggregation_pending \
  -q
```

Result: exit 0, **20 passed, 1 warning in 25.37s**.

Worker/unit compatibility:

```powershell
uv run python -m pytest -o addopts= \
  tests/unit/ingestion/test_pipeline.py \
  tests/unit/ingestion/test_runtime.py \
  tests/unit/test_entrypoints.py -q
```

The first run had 22 passing and three exact-handler-set failures caused by the new production
handlers. Expectations were updated without weakening other queues: maintain includes
`kb.reindex_fanout`, and index includes `document.index` plus `document.reindex_delete`. Rerun:
**25 passed in 3.50s**.

Focused real-service compatibility:

```powershell
uv run python -m pytest -o addopts= \
  tests/integration/test_catalog.py \
  tests/integration/test_ingestion_pipeline.py \
  tests/integration/test_pipeline_lifecycle_acceptance.py \
  tests/integration/test_version_snapshots.py \
  tests/integration/test_version_snapshot_migration.py \
  tests/integration/test_task_fencing.py -q
```

The first run exposed five accepted historical control-plane assumptions: explicit administrative
activation was not intended to be converted into the automatic enrollment barrier, and never-
ingested KB fixtures must retain their explicit version-management seam. The implementation was
narrowed accordingly while automatic worker activation remained gated. The five controls plus the
owned document-history empty rejection passed together (**6 passed in 9.17s**), then the complete
focused group passed: **73 passed, 4 warnings in 50.19s**. The warnings were only Alembic's existing
`path_separator` deprecation.

Real-model evidence, using the parent-owned live file unchanged:

```powershell
$env:CAIRN_TEST_TEI_URL='http://127.0.0.1:38742'
$env:CAIRN_TEST_TEI_TOKENIZER='D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json'
uv run python -m pytest -o addopts= tests/integration/test_live_model_pipeline.py -q
```

Result: exit 0, **2 passed, 1 warning in 9.34s**. This uses the real local TEI model/runtime for the
existing markdown and semantic scenarios and the parent-appended full v2 rebuild/query assertions.
The focused fan-out tests use the deterministic fake embedding provider to make task ordering,
parser-call counts, vector identity, crash/replay, and deletion behavior exact. Fake-model evidence
is not represented as provider/model authenticity evidence; the TEI command is the actual-model
evidence.

Static evidence:

```powershell
uv run python -m ruff format <slice-owned files>
# 7 files reformatted, 4 files left unchanged

uv run python -m ruff check .
# All checks passed!

uv run python -m mypy
# Success: no issues found in 111 source files
```

The first global `ruff format --check src apps tests migrations` invocation also listed pre-existing
format differences in migrations `0001`, `0002`, `0003`, `0005`, `0006`, `0008`, and
`migrations/env.py`; those unrelated files were not reformatted. Slice-owned files were formatted
directly and global Ruff lint passed. Final touched-file format and `git diff --check` evidence is
recorded after the final worktree review below.

No targeted failure remains at this checkpoint. Parent-owned full-suite execution and independent
review remain the acceptance authority.

### 2026-09-10 final local static and worktree review

After the final behavioral command and after releasing the database window, only local checks were
run:

```powershell
uv run python -m ruff format --check \
  migrations/versions/0009_reindex_fanout.py \
  apps/worker/main.py \
  src/cairn/catalog/models.py \
  src/cairn/catalog/repository.py \
  src/cairn/catalog/service.py \
  src/cairn/catalog/ingestion.py \
  src/cairn/catalog/reindex.py \
  src/cairn/ingestion/pipeline.py \
  tests/integration/test_reindex_fanout.py \
  tests/unit/ingestion/test_pipeline.py \
  tests/unit/test_entrypoints.py
# 11 files already formatted

uv run python -m ruff check .
# All checks passed!

git diff --check
# exit 0; only existing Git LF-to-CRLF worktree notices
```

Mypy had already passed after the final source edit: **Success: no issues found in 111 source
files**. No PostgreSQL, Redis, pgvector, Alembic, or TEI command was run after the database window
was released.

## Slice-owned changed files

Production and migration:

* `migrations/versions/0009_reindex_fanout.py`
* `apps/worker/main.py`
* `src/cairn/catalog/models.py`
* `src/cairn/catalog/repository.py`
* `src/cairn/catalog/service.py`
* `src/cairn/catalog/ingestion.py`
* `src/cairn/catalog/reindex.py`
* `src/cairn/ingestion/pipeline.py`

Focused tests and exact production-handler expectation updates:

* `tests/integration/test_reindex_fanout.py`
* `tests/integration/test_catalog.py` (one invalid unfinished-rebuild activation fixture corrected)
* `tests/unit/ingestion/test_pipeline.py`
* `tests/unit/test_entrypoints.py`

Documentation:

* `docs/04-plan/19-reindex-fanout-implementation.md`

The parent-owned `docs/04-plan/15-execution-ledger.md`,
`tests/integration/test_reindex_fanout_acceptance.py`, and
`tests/integration/test_live_model_pipeline.py` were read/executed but not edited by this
implementation. Accepted slice-1 and unrelated dirty-worktree files were preserved. No commit,
revert, clean, branch, worktree, or descendant agent was used.

## Final bounded limitations

* The enrollment/coverage/orphan barrier governs every automatic worker switch and every explicit
  switch of a real rebuild (`active_index_version` is present and document history exists). Explicit
  activation bypass remains only for never-ingested control-plane versions and the initial-v1 seam;
  it must not switch an unfinished real rebuild.
* A never-ingested control-plane KB may still create and explicitly activate versions. Once document
  history exists, an empty rebuild is rejected before allocation; a build that becomes empty is
  failed and clears only its owned building pointer while preserving the old active version.
* Terminal document failure deliberately leaves a nonempty build blocked. Recovery/resume of that
  failed stage belongs to the next lifecycle slice; this slice prevents false activation.
* `document.reindex_delete` is intentionally limited to building-version cleanup before activation.
  The general `document.purge` handler remains unimplemented, so deletion after the atomic switch is
  still follow-up work and was explicitly deferred in acceptance tests.
* Filtered cleanup ensures the target namespace from the immutable version snapshot, deletes every
  revision for the document, verifies filter count zero, then removes catalog target chunks. Remote
  vector writes remain nontransactional relative to PostgreSQL and subject to each driver's
  read-after-write consistency.
* Runtime publication retry is bounded to the exact fan-out or cleanup task whose committed switch
  it follows. This does not implement the broader cache-generation ordering overhaul planned for a
  later slice.
* Parse reuse verifies artifact checksum plus document/revision/source hash and republishes a fresh
  target-version envelope. Invalid, missing, corrupt, or out-of-scope prior artifacts fall back to
  the validated source/parser path.
* Full credential-free `ModelRef`, metric, chunk settings, and high-water behavior from migration
  `0008` remain intact. Provider weights/tokenizer bytes behind stable registered identifiers retain
  the slice-1 provenance limitations.
* `chunk.reembed`, retirement/drop handlers, general recovery entrypoints, cache-generation redesign,
  PDF, and OCR remain outside this slice.

Status at handoff: implementation targeted-GREEN, static-GREEN, database window released, no known
remaining targeted product failure, and no process blocked or hung. Parent full-suite execution and
independent review determine final acceptance.

## Post-handoff review rejection and correction

The coordinator rejected the first handoff because the compatibility repair after the 73-test run
unilaterally narrowed the approved explicit activation barrier. Removing the owned explicit-barrier
test and restoring unconditional `activate_index_version` behavior allowed a real indexed KB's
unfinished v2 rebuild to bypass enrollment and coverage. That was a production-policy regression,
not an acceptable fixture compatibility adjustment.

The corrected policy is the design stated above: never-ingested control-plane KBs and initial v1 may
retain explicit activation compatibility, but any KB with an existing active version and document
history is a real rebuild. Its explicit activation must call the same locked
`activate_build_if_ready` predicate as worker completion and raise `Conflict` while enrollment,
current-live coverage, or deleted-point cleanup is incomplete. Tests whose sole setup depended on
explicitly switching an unfinished real rebuild must be updated to establish their intended state
without weakening production.

The parent added an independent seventh acceptance case for this regression and began the RED run
after the implementation released its database window. This implementation will not touch the
database until the parent reports that RED command has finished and transfers the window again.

### 2026-09-10 explicit activation review resolution

The parent-owned explicit-switch acceptance RED completed before the database window was returned:
**1 failed in 4.62s**, because `activate_index_version(v2)` did not raise and published v2 before any
fan-out. Durable parent report: `data/acceptance-20260910/explicit-switch-red.xml`.

The owned explicit real-rebuild regression was restored and independently reproduced the same
failure: **1 failed, 1 warning in 4.40s**. Production now computes the real-rebuild boundary while
holding the KB/version locks: an existing active version plus any document history routes explicit
activation through `activate_build_if_ready`; initial v1 and never-ingested control-plane versions
retain their historical direct seam.

The parent and owned explicit-switch cases were then run together with never-ingested v2 controls
and initial-v1 snapshot/migration controls. Result: exit 0, **6 passed, 4 warnings in 7.59s**.

One existing catalog test had explicitly activated an unfinished real v2 only to manufacture a
non-active chunk. Its setup was invalid under the approved policy. The test now writes/lists a chunk
in building v2 and directly asserts that editing that building chunk raises `ChunkNotEditable`; it
no longer bypasses activation. The full post-review focused gate covered all parent acceptance cases,
all owned fan-out tests, this corrected catalog test, never-ingested controls, and initial-upload
deletion: exit 0, **25 passed, 1 warning in 26.32s**.

A final branch review noted that explicit activation of an enrollment-complete build which had become
empty correctly committed failed-v2/cleared-building state but returned normally. Source inspection
confirmed publish/log were already guarded by `if activated`; the remaining defect was silent API
success. An owned regression forced that state and failed as expected: **1 failed, 1 warning in
4.65s**, `DID NOT RAISE Conflict`.

The explicit activation method now distinguishes committed abandonment from pending rejection. A
pending/unfinished rebuild raises inside the transaction and rolls back; a now-empty complete rebuild
commits `failed` v2, clears only its owned building pointer, retains active/cached v1, then raises an
`abandoned` `Conflict` after commit. No v2 runtime publication or activation-success log occurs.
The final focused command covered committed abandonment, parent and owned unfinished-switch barriers,
never-ingested controls, and initial four-stage upload activation: exit 0, **6 passed, 1 warning in
8.71s**.

The database window was released to the parent immediately after that command. Final local-only
checks after formatting the review-touched files:

```powershell
uv run python -m ruff check .
# All checks passed!

uv run python -m mypy
# Success: no issues found in 111 source files

git diff --check
# exit 0; only existing Git LF-to-CRLF notices
```

Final post-review status: explicit and automatic real-rebuild activation both enforce enrollment,
exact current-live coverage, and deleted-point cleanup; compatibility bypass is limited to initial v1
and never-ingested control-plane versions. No implementation process holds the disposable database,
and the parent owns the pending full-suite acceptance.

Parent independent static review reported lint, mypy 111, import contracts 6, and route authorization
50/8 passing, with a possible mixed-line-ending format issue in `src/cairn/catalog/service.py`.
The requested local formatter verification was run after the final focused test and after DB release:
`uv run python -m ruff format src/cairn/catalog/service.py` reported **1 file left unchanged**;
`uv run python -m ruff format --check src/cairn/catalog/service.py` reported **1 file already
formatted**; focused `git diff --check` for service plus this journal exited 0.

### Final parse-reuse domain-error correction

Review found that prior-artifact fallback caught `OSError` and `ValueError`, while actual
`LocalObjectStore.get_bytes` raises `ObjectNotFound` for missing objects and `ObjectTooLarge` for
oversized objects. Both are domain errors, so these candidates incorrectly terminally failed parsing.
The correction is limited to explicitly catching those two candidate errors; storage unavailability
must continue to propagate, and the independent source size cap must still apply after fallback.

Unit RED used actual `LocalObjectStore` objects under pytest temporary directories, with mocked
catalog/model seams to avoid external services:

```powershell
uv run python -m pytest -o addopts= tests/unit/ingestion/test_parse_reuse.py -q --tb=short
```

Result: **4 failed, 2 passed in 4.03s**. Missing and oversized candidates raised their domain errors,
and both source-cap tests confirmed fallback never reached the source read. Corrupt-candidate
fallback and `ObjectStoreUnavailable` propagation already passed. The parent independently reported
actual missing/oversized integration RED: **2 failed in 5.92s**, with terminal `OBJECT_NOT_FOUND` /
`OBJECT_TOO_LARGE` and no v2 activation; report `data/acceptance-20260910/reuse-fallback-red.xml`.
No database, Redis, TEI, or integration command was run by this implementation during this correction.

After adding only the explicit `ObjectNotFound` / `ObjectTooLarge` import and candidate exception
catch in `pipeline.py`, the identical unit command passed: **6 passed in 3.05s**, exit 0. Actual local
missing/oversized/corrupt objects now yield `None` for source fallback; an unavailable store still
raises its original `ObjectStoreUnavailable`; oversized source reads still fail `OBJECT_TOO_LARGE`
under `max_source_bytes` without invoking the parser. No broad `CairnError` catch was added.

Focused Ruff lint passed; format check reported **2 files already formatted**; mypy reported
**Success: no issues found in 111 source files**. This correction touches only
`src/cairn/ingestion/pipeline.py`, new `tests/unit/ingestion/test_parse_reuse.py`, and this journal.
Parent owns the expanded actual-storage integration tests and subsequent integration/full-suite
verification. The parent-reported earlier full baseline (725 passed, two known PDF errors, 85.01%
coverage) predates this correction and is not evidence for the new fallback behavior.
