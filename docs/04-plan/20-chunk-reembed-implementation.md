# Lifecycle slice 3: manual chunk re-embedding

**Date:** 2026-09-14. **Status:** bounded slice3 independently ACCEPTED by coordinator.

Coordinator independently passed101 focused tests (74.79s), including actual TEI markdown/semantic
manual-edit retrieval, previous lifecycle regressions and migration round-trip. Global lint,
format172, mypy111, import contracts6 and authorization50/8 passed. Final full backend: **761 passed,
2 known PDF/OCR collection errors,no assertion failures/skips,85.18% coverage,165.78s**. Command
correctly exits1; see ledger L83-L87. An old startup expected-handler-set assertion was corrected
without weakening exact matching; adjacent27 units and full backend rerun passed. Euclid is closed.
No overall E2E acceptance: general recovery/purge/retirement/cache ordering and PDF/OCR remain open.

## Scope and ownership

Rollout boundary: drain old API/worker processes before migration0010 and start the matching new
binaries together. Mixed-version writers do not share edit-generation/dedupe semantics and are not
accepted by this slice. Legacy pending edits without complete task identity need explicit re-editing
after upgrade. This test run migrates disposable databases only, not an application database.

This slice makes the production `embed` worker consume `chunk.reembed`. A successful searchable
chunk edit updates exactly one stable vector point in the active namespace. The point uses the
current catalog text, hash, citations, metadata, parent ID and document revision. Parent/non-searchable
edits remain catalog-only and never enqueue or write a vector. General purge, retirement, stage
recovery, PDF/OCR and M12 are excluded.

The implementation may change the bounded catalog DTO/model/repository/service/facade surface,
ingestion handler/runtime composition, production worker factory, migration `0010`, focused unit
tests, `tests/integration/test_chunk_reembed.py`, and this journal. The parent owns
`tests/integration/test_chunk_reembed_acceptance.py`, `tests/integration/test_live_model_pipeline.py`
and document 15; none will be edited. Existing snapshot/fan-out and user changes remain untouched.
There will be no commit, revert, clean, worktree, or descendant agent.

## Initial evidence

Documents 16, 19, M03, M07, the chunking rules and the worker-pipeline lock/artifact design were
read before implementation. Source inspection established:

- `CatalogService.edit_chunk` currently changes catalog text/hash and enqueues `chunk.reembed`, but
  the payload has only `chunk_id` and `index_version`; hash-only dedupe would permit ABA and no
  production handler is registered.
- ordinary index writes use immutable chunk/embedding manifests. An edit after a manifest is
  committed but before `document.index` can otherwise be overwritten by that manifest.
- `metadata.embed=false`, not `parent_id`, is the authoritative non-searchable marker. Parents are
  catalog context and children are searchable points.
- `EmbeddingService` normalizes document input with NFKC plus whitespace folding and otherwise
  silently truncates oversized input. The re-embed handler must therefore perform the identical
  normalized tokenizer count first and reject before calling the service.
- Hugging Face tokenizer `count` includes configured special tokens; the exact limit check can use
  the prepared tokenizer directly. Provider APIs consume a full version-owned `ModelRef`.
- the existing external index mutation guard deliberately does not lock the task row. Heartbeats
  use separate transactions, while document/run/KB/version locks fence remote writes.

The parent independently ran the first three acceptance cases against fresh PostgreSQL/pgvector and
Redis and reported **3 failed in 28.25s**: no production handler, edited vector left unchanged, and
task payload missing `content_hash`; report `data/acceptance-20260914/reembed-red.xml`. Three further
cases are prepared but have not yet run: stale first edit versus latest edit, parent context edits,
and exact 129-over-128 token rejection. Parent lint/format checks were green. The exclusive
disposable DB window is released to this worker for targeted TDD only.

## Considered designs

### Hash-only identity

Keep the current content hash as the edit identity. This is rejected: edit A, edit B, then edit A
again returns to the same hash, so an old A task cannot be distinguished from the latest A edit.

### Rewrite mutable document manifests

Patch the shared chunk and embedding manifests on each manual edit. This is rejected because it
turns immutable stage evidence into shared mutable state, expands object-store failure handling,
and still leaves a catalog/object/vector multi-system transaction that cannot be atomic.

### Monotonic row generation with snapshot/recheck (selected)

Each physical chunk row receives monotonic `edit_generation` and
`reembed_applied_generation` counters. The transactional edit increments the generation and embeds
that generation, revision, hash and version in the task. The worker captures a credential-free
catalog DTO, performs no-lock network work, then rechecks the complete identity under an external
mutation guard before upsert. This directly fences ABA, source replacement, deletion, version
switches and later edits while preserving the existing module boundary.

## Durable identity and edit policy

Migration `0010` adds non-negative integer columns to every chunk partition through the partitioned
parent:

- `edit_generation`, default `0`, increments for every accepted manual edit including identical
  text and ABA text;
- `reembed_applied_generation`, default `0`, records the latest manual generation whose exact point
  payload was verified remotely.

Generation is scoped to one physical `(kb_id, chunk_id)` row. Replacement/rechunking may create a
new physical row whose generation starts at zero. Old tasks still cannot cross that reset because
the independent document revision, index version, chunk UUID and current hash fences must all match;
the worker never interprets generation alone as global ordering. A zero applied generation is merely
the absence of a completed manual vector write for that row. It does not mark context-only edits as
pending and cannot block fan-out, ordinary indexing, activation or unrelated rebuild work.

For searchable edits, the task payload is immutable and explicit:

```json
{
  "chunk_id": "<uuid>",
  "index_version": 2,
  "revision": 4,
  "content_hash": "<lowercase sha256>",
  "edit_generation": 3
}
```

The dedupe key includes chunk ID, version and generation. Hash remains in the payload as content
integrity, not ordering. Credentials, endpoint secrets and vectors never enter task payloads or
durable public artifacts.

`edit_chunk` validates tenancy and locks in ordinary ingestion order: document, current ingestion
row, KB, active version, then the chunk row. It rejects a deleted document/KB, a retired or building
chunk, an existing rebuild at edit time, and the unsafe shared-manifest interval where the current
active-version ingestion is `chunked` or `embedded`. An `indexed` searchable revision enqueues
`chunk.reembed`. The pre-manifest `registered`/`parsed` states may accept the catalog edit but enqueue
no manual re-embed task: the existing preserved-edit read plus `commit_chunked` recheck serializes
the race and puts the manual text into the ordinary immutable chunk manifest, whose normal embed and
index stages own the eventual vector. This preserves the accepted control-plane fixture without
weakening active, revision, or build guards or creating two writers for one pre-indexed point.

The catalog service has no tokenizer dependency. For indexed searchable edits, the worker writes its
exact normalized tokenizer measurement to `chunk.token_count` in the same guarded SQL commit as the
applied generation, then recomputes the document's active-version token aggregate. Context-only
parent edits and accepted pre-manifest compatibility edits are not independently measured by the
catalog; parents remain non-searchable, while the ordinary chunk stage measures pre-manifest edits
before committing its manifest and rows.

If a rebuild begins only after the edit commits, the active-version task may still update the active
namespace while the rebuild remains building; fan-out carries the edit by ordinal. If activation
wins first, the old task becomes stale and performs no write. An edit requested while a rebuild is
already present is rejected before changing catalog text. Source replacement after enqueue changes
the document revision and makes the task stale. A source replacement already in `chunked` or
`embedded` state blocks editing until ordinary indexing completes.

Every accepted edit updates the existing chunk row in place. Its UUID is intentionally stable even
though content/hash changes: manual editing overrides the chunker's content-derived identity within
that physical index version so vector upsert replaces the existing point instead of creating an
orphan. A later rebuild still receives its version-specific chunker-generated UUID; preserved text
is carried by ordinal under the existing FR-F-09 policy.

## Parent/non-searchable effect

`chunk_metadata.embed is False` is decisive. Editing such a parent updates only the parent's exact
catalog text/hash/generation and audit record. It enqueues no task, calls no provider, writes no
parent point, and does not mutate or re-embed any child. Children retain their original searchable
text, citations and vectors. This is intentionally a context-only correction; there is no silent
parent-to-child propagation or hidden document reindex.

## Catalog facade and worker flow

The catalog facade exposes a frozen `ChunkReembedTarget` DTO containing only the worker's required
data: workspace/KB/document/chunk identities, revision, active index version, generation, exact
content/hash, token count, parent ID, copied metadata, version-owned `ModelRef`/metric, and vector
binding. Ingestion imports no catalog ORM class.

The handler proceeds in four phases:

1. Parse the complete task identity and call `load_chunk_reembed`. Missing/malformed, wrong-workspace,
   deleted, retired, source-replaced, non-searchable, non-indexed, superseded-generation, or
   later-generation tasks are skipped before provider resolution. Only the exact current active
   indexed row is returned. A replay whose generation is still current remains eligible even when
   SQL says it was applied, because remote payload verification is required before task success.
2. Resolve the prepared embedding with the full version-owned `ModelRef`. Normalize exactly as the
   embedding service (`" ".join(unicodedata.normalize("NFKC", content).split())`), count with the
   prepared tokenizer including special tokens, and raise terminal `EMBED_INPUT_TOO_LARGE` when the
   count exceeds `max_input_tokens`. No truncation method and no provider call occurs on rejection.
3. After resolving and validating that the prepared embedding still equals the frozen version-owned
   `ModelRef`, support post-write retry without a second provider call: inspect the existing point. A point is
   reusable only when every expected payload field, including exact unnormalized catalog text,
   content hash, revision, parent ID, metadata and `manual_edit_generation`, matches. Recheck that
   candidate under the mutation guard before marking the generation applied. Otherwise call
   `embed_documents(target.model, [target.content])` for exactly one chunk outside SQL locks.
4. Enter `chunk_reembed_mutation`, recheck the full task identity, upsert the same chunk UUID into
   the active namespace, fetch it, compare the complete expected payload, and only then set
   `reembed_applied_generation`. A crash after remote upsert but before SQL commit leaves a point
   carrying `manual_edit_generation`; retry verifies it and durably advances the marker.

The point payload begins with an exact copy of catalog metadata and overlays canonical keys:
`chunk_id`, `content`, `content_hash`, `document_id`, `index_version`, `kb_id`, `parent_id`,
`revision`, and `manual_edit_generation`. The original user string, including spacing, Unicode,
citations and unknown metadata fields, is never replaced by the normalized provider input.

## Locks, leases and remote consistency

No SQL lock is held during embedding provider I/O. The initial DTO is an optimistic snapshot.
Immediately before any vector mutation, the facade heartbeats in the worker's independent short
transaction, then opens one bounded transaction and locks document, current ingestion, KB, active
version and chunk in that order. It reads task ownership/lease with the existing non-locking lease
query; it never takes task `FOR UPDATE`, so heartbeat remains independent.

The guard revalidates workspace, KB/document relationship, non-deletion, document revision/source
identity, active version/state, indexed run state, chunk ID/hash/generation,
`is_edited`, and `metadata.embed`. It rechecks lease budget before fetch/upsert, verification and SQL
commit. A later edit or source/version transition waits for these catalog locks; if it won before
the guard, the stale task writes nothing. This order extends the existing document -> run -> KB ->
version discipline and places chunk last, avoiding an edit/upsert lock inversion.

Vector writes remain non-transactional with PostgreSQL and depend on each driver's read-after-write
behavior. The verified generation marker and exact point payload make retries convergent; they do
not prove dense-vector equality independently of the write that attached that payload and do not
claim cross-system atomicity.

## Error and retry semantics

Identity loss is a successful stale skip and never mutates document failure state. Provider outages,
timeouts and circuit-open errors retain their existing retryable codes. Invalid model/tokenizer,
oversized input, invalid vectors and dimension mismatch are terminal. Manual re-embed failures are
returned on the task itself and do not move an otherwise indexed document back to `failed` or alter
its ordinary ingestion ledger. Error detail remains bounded and never contains text, hashes,
credentials, endpoint configuration or vector values.

Legacy queued `chunk.reembed` payloads that lack revision, hash or generation fail closed as stale
skips. The worker never guesses missing identity from the current chunk. Operators remediate by
re-editing the chunk, which creates a complete generation-scoped task, or by an explicit future
recovery workflow outside this slice.

## TDD sequence and acceptance

Implementation begins only after parent approval of this design.

1. Add focused RED tests for generation/task payload and searchable versus parent enqueue behavior;
   migrate/model/repository/service minimally to green.
2. Add unit RED tests for exact normalized token counting, no truncation/provider call on overflow,
   exact point construction, full payload verification and production handler registration; add the
   handler/factory wiring minimally to green.
3. Add `tests/integration/test_chunk_reembed.py` RED/GREEN coverage for one-point exact updates,
   stable UUID, unchanged sibling, stale/later edit fences, source/deletion/version/workspace fences,
   race after embedding, post-upsert retry, parent behavior and shared-manifest edit rejection.
4. Run only the owned targeted unit/integration set in the exclusive DB window, then the parent six
   acceptance cases and the parent-owned live TEI manual-edit case when authorized. Use the supplied
   external PostgreSQL/Redis variables and, for live TEI, `CAIRN_TEST_TEI_URL=http://127.0.0.1:38742`
   with `CAIRN_TEST_TEI_TOKENIZER=D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json`.
5. Run focused Ruff format/check, mypy via `uv run python -m ...`, and `git diff --check`; append every
   meaningful RED/GREEN command and result here. Release the DB immediately after targeted GREEN.
   The parent owns the full suite and final acceptance.

## Evidence log

### 2026-09-14 approved six-case baseline RED

After parent approval and before any production edit, the complete parent acceptance file ran on
the exclusive PostgreSQL/pgvector and Redis services:

```powershell
uv run python -m pytest -o addopts= tests/integration/test_chunk_reembed_acceptance.py -q --tb=short
```

Result: exit 1, **5 failed, 1 passed, 1 warning in 8.06s**. Factory registration, exact one-point
update, immutable hash/revision payload, stale-first/latest-second ordering and oversize failure code
failed for the expected absent-handler/identity behavior. The parent context-only case already
passed because the existing edit path enqueued no runnable parent point in that scenario; later owned
tests will make the no-task/no-vector policy explicit. The single warning is the existing Alembic
`path_separator` deprecation.

### 2026-09-14 durable identity and catalog checkpoint

An owned ABA regression was added before schema/service implementation and run alone:

```powershell
uv run python -m pytest -o addopts= tests/integration/test_chunk_reembed.py -q --tb=short
```

Result: exit 1, **1 failed, 1 warning in 5.41s**. PostgreSQL reported missing
`chunk.edit_generation`; logs also showed the third A edit was deduplicated by the first A hash.
Migration `0010_chunk_reembed`, physical-row generation fields, lock-ordered edit validation,
generation-scoped task payloads and searchable/indexed-only enqueue were then implemented. The
owned ABA case plus parent payload and parent-context cases passed: **3 passed, 1 warning in 5.09s**.

Review then identified that physical generations reset after deterministic-ID replacement, so a
dedupe key without revision could merge revision-2 generation 1 into a still-ready revision-1
generation 1 task. Owned cross-revision and deleting-document tests were added first and failed as
expected: **2 failed, 1 warning in 5.37s**. The dedupe key now includes document revision, and edit
state guards reject deleting documents and deleting/archived KBs before mutation.

### 2026-09-14 handler and external-mutation checkpoint

Six isolated handler tests were added for exact normalized/special-token counting, terminal overflow,
legacy incomplete payload fail-closed behavior, prepared-model drift before remote reuse, canonical
exact payload construction and nested metadata copying. Initial collection failed because
`ChunkReembedTarget` did not exist. After the DTO, handler, full-model resolver check, exact payload
builder, catalog load/recheck guard, generation commit and production registration were implemented,
the first unit rerun exposed two test-double defects (an incompletely constructed `Vector` and a fake
catalog bypassing identity parsing). Correcting only those test fixtures produced **6 passed in
2.36s**.

The owned integration set and all seven parent acceptance cases then ran together. Result: **9
passed, 1 failed, 1 warning in 10.74s**; every parent case passed, including the in-flight later-edit
race, and the sole owned failure expected `ChunkNotEditable` where the service intentionally hid a
deleting document as `NotFound`. After aligning that assertion and tightening stale load/guard
checks, unit + owned integration + parent acceptance passed **16 tests, 1 warning in 9.98s**.

### 2026-09-14 measured token-count checkpoint

Review found successful re-embedding left `chunk.token_count` and the document aggregate stale. A
unit assertion requiring the exact normalized count at guarded commit failed as expected. The count
was threaded into the commit, but a deliberately unrelated 100-token chunk exposed misuse of the
KB-wide aggregate: the focused integration test failed with document count **144 instead of 44**.
A document/version-scoped repository sum replaced that call. The current complete targeted command:

```powershell
uv run python -m pytest -o addopts= tests/unit/ingestion/test_chunk_reembed.py \
  tests/integration/test_chunk_reembed.py \
  tests/integration/test_chunk_reembed_acceptance.py -q --tb=short
```

Result: exit 0, **17 passed, 1 warning in 11.52s**. The warning remains the existing Alembic
`path_separator` deprecation.

Current implementation includes migration/model generations, revision-aware enqueue identity,
state-aware edit policy, frozen/deep-copied worker DTO, full active-version `ModelRef` resolution,
exact pre-provider budget rejection, one-point upsert with canonical payload, post-embedding guarded
generation recheck, payload verification before success, applied-generation persistence, and exact
chunk/document token-count refresh. Ingestion imports no catalog ORM.

Current blockers: none. Remaining bounded work is owned post-upsert retry and stale scope/version
coverage, focused Ruff/mypy/diff checks, the parent-owned live TEI target, final targeted rerun, DB
release and handoff. No full suite will be duplicated; the parent owns it.

### 2026-09-14 durability, guard and legacy regression checkpoint

Owned post-write and stale-state coverage was added for a crash immediately after vector upsert,
source replacement, document deletion, retired version and wrong workspace. The first focused run
passed four cases and failed only the wrong-workspace setup because mutating the persisted task's
workspace made it unclaimable before handler dispatch. The test was corrected to invoke the handler
with a wrong-workspace `TaskContext`; no product code changed for that setup issue. The rerun passed
**5 tests, 1 warning in 6.78s**. The crash case proves retry observes the exact persisted payload,
makes no second provider call, and advances `reembed_applied_generation` only after verification.

Four explicit policy cases for an existing rebuild, `chunked` and `embedded` shared-manifest states,
and an expired claimed task lease passed **4 tests, 1 warning in 6.00s**. The expired task completes
without changing the point.

Focused static checks initially reported only import/format layout, one intentionally full-width test
character, and one long test line. Ruff formatted seven files; the normalization fixture now uses an
escape so its semantics remain visible without an ambiguous-glyph lint exception. The production
entrypoint unit expectation now includes both embed handlers. Fresh static results:

```powershell
uv run python -m ruff check <slice files>
# All checks passed!

uv run python -m ruff format --check <slice files>
# 11 files already formatted

uv run python -m mypy
# Success: no issues found in 111 source files
```

The legacy regression target combined the accepted catalog edit-carry case, production entrypoint
unit tests, the original ingestion pipeline integration file, all owned slice tests and all seven
parent acceptance cases. Result: exit 0, **60 passed, 1 warning in 38.15s**. This confirms the
registered control-plane edit remains preserved without a manual vector task and ordinary pipeline
guards remain intact.

The parent-owned live model file then ran with the supplied TEI endpoint and tokenizer. Result: exit
0, **2 passed, 1 warning in 9.13s** for markdown and semantic variants, including the appended manual
edited-point upsert and TEI query after a v2 rebuild. Every warning in these runs is the existing
Alembic `path_separator` deprecation.

Remaining work: one fresh slice-only verification after all test additions, `git diff --check`,
worktree ownership review, DB release, and handoff to the parent for the full suite. No product
failure or blocker is currently known.

### 2026-09-14 final identity verification and handoff evidence

Final review observed that exact payload equality alone could accept a malformed driver response
whose `Hit.id` differed from the requested chunk. A focused unit regression supplied a wrong point ID
with an otherwise exact payload. It failed as expected because the provider was not called. Both the
remote-reuse and final-success checks now share one predicate requiring exactly one hit, the target
chunk UUID, and complete payload equality. The regression then passed **1 test in 2.32s**.

Fresh slice-only verification after that correction:

```powershell
uv run python -m pytest -o addopts= tests/unit/ingestion/test_chunk_reembed.py \
  tests/integration/test_chunk_reembed.py \
  tests/integration/test_chunk_reembed_acceptance.py -q --tb=short
```

Result: exit 0, **27 passed, 1 warning in 18.06s**. This includes all seven parent acceptance cases,
ABA and cross-revision generation/dedupe, stable edited UUID, exact text/citations/metadata, parent
non-vector behavior, normalized special-token budget rejection, later-edit embedding race,
post-upsert retry, source/deletion/retirement/workspace stale skips, rebuild and shared-manifest edit
refusals, token aggregates, expired lease, prepared-model drift, legacy payload fail-closed behavior,
deep-copied metadata, production registration and wrong-hit identity.

Fresh final static checks on the exact final code:

```powershell
uv run python -m ruff check <slice files>
# All checks passed!

uv run python -m ruff format --check <slice files>
# 11 files already formatted

uv run python -m mypy
# Success: no issues found in 111 source files

git diff --check -- <slice files and this journal>
# exit 0; only existing LF-to-CRLF worktree notices
```

The parent-owned live TEI target was rerun after the final point-ID correction with the supplied
endpoint/tokenizer. Result: exit 0, **2 passed, 1 warning in 7.67s** for markdown and semantic. The
warning remains the existing Alembic `path_separator` deprecation.

Status at handoff: targeted GREEN, static GREEN, live TEI GREEN, no known slice-3 failure or blocker.
The exclusive PostgreSQL/Redis window was released immediately after this journal update. No full
suite was run; parent integration and full-suite review remain authoritative. No commit, revert,
clean, worktree, descendant agent, general purge, retirement, recovery, PDF/OCR or M12 work occurred.

## Design self-review

The design has no placeholder behavior. All slice-3 requirements map to an identity field, guard,
payload assertion or test. It intentionally does not modify immutable shared manifests, broaden
artifact fallback exceptions, expose credentials, regenerate edited UUIDs, index parents, reindex
children, or add lifecycle work from slices 4-5.
