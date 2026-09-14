# Ingestion execution ledger

Date: 2026-09-14. Execution: RESUMED, IN PROGRESS. Knowledge-base E2E NOT ACCEPTED.

This ledger is the durable restart point. Read it together with `13-ingestion-completion-coordination.md`
before dispatching work. Preserve the dirty worktree; do not infer completion from existing files.

## Latest checkpoint (read before historical steps)

September14: bounded slice3 ACCEPTED in L87. Euclid `01a09dfa-a006-7c02-b589-cdd3a1d2e563`
(gpt-5.6-sol/high) is explicitly CLOSED; no running subagents. Final independent evidence:
**101 focused passed; 761 full-backend passed,2 PDF/OCR collection errors,no assertion failures/
skips,coverage85.18%**. Full command exits1. Worker record: document20. L74 onward supersedes
earlier dispatch/resource status; next is slice4 durable failed-stage recovery. Overall knowledge-
base E2E remains OPEN. Owned container cleanup is recorded in L88; dirty changes are preserved.

September 10: bounded lifecycle slice2 ACCEPTED in L72. Hume
`01a089d3-4592-7be2-a698-5d50ce1cdb8e` (gpt-5.6-sol/high) is explicitly CLOSED; no running
subagents. Coordinator independent final results: **40 focused passed; 734 full-backend passed,
2 PDF/OCR collection errors, no assertion failures/skips, coverage85.05%**. Full command exits1.
Worker journal: document19. L54 onward supersedes prior dispatch/resource state. Slice1 remains
accepted (document17/L52). Next is slice3 manual chunk reembedding, not redoing fan-out. Overall
knowledge-base E2E remains OPEN. Owned service cleanup is recorded in L73.

- Running subagents: **none**. Both repair and advanced workers delivered and were explicitly closed.
- Pipeline bounded repair: accepted by coordinator; original severe regressions and real model/Office
  wiring passed (L25). Remote accepted writes are not claimed to be transactionally cancellable.
- Advanced semantic/custom package: coordinator independently passed **106 tests**; semantic is wired
  to the actual model service. Custom needs an explicitly injected scoped executor and otherwise fails
  closed; production M12 sandbox execution is NOT implemented or accepted.
- Full-KB lifecycle: OPEN. Snapshots, bounded fan-out/enrollment/deletion/activation barriers and
  manual reembedding are accepted. General stage recovery, purge/retirement and broader cache ordering
  remain open; terminal failed documents deliberately block nonempty rebuilds.
- PDF/OCR: OPEN, missing implementation modules still cause collection errors. Real 30-document PDF
  quality evaluation remains OPEN; there is no annotated benchmark or measured quality pass.
- Latest expanded backend (September14): **761 passed,2 collection errors**, no skips,
  **85.18% coverage**. Coverage meets unchanged80 threshold; command correctly exits nonzero.
  Earlier 690/703/725-pass counts remain historical evidence below, not current full-suite status.
- Owned PostgreSQL/Redis/TEI test containers were label-verified and removed. Pre-existing M08
  containers remain untouched. Local reports and pinned model assets are retained under ignored data.

## Operating contract

- Coordinator owns requirements, independent review, integration and acceptance.
- Implementation workers use `gpt-5.6-sol`, reasoning `high`.
- At most ONE running subagent. Close it before dispatching its successor.
- Record each dispatch, change, command result, review rejection and next action here or in the
  linked package document. Never replace failing evidence with an unverified success claim.
- The latest user instruction retains delegation and additionally requires durable step records.
- No commits, application-data cleanup, or removal of pre-existing changes without instruction.

## Verified steps from the preceding uninterrupted run

| Step | Action and evidence | Result |
| --- | --- | --- |
| L01 | Task lease mutations fenced by worker/attempt and wall-clock expiry; real queue/fencing tests | 27 passed; task completion is not proof of external vector fencing |
| L02 | Office worker implemented DOCX/PPTX/XLSX/CSV, OOXML safety, local language detection | Worker closed; coordinator separately reviewed |
| L03 | Coordinator composed four MIME-specific Office adapters and post-parse language resolution in registry; pinned dependencies and added typing stubs | `uv lock` and `uv sync --locked --dev` succeeded |
| L04 | `uv run python -m pytest -o addopts= tests/unit/ingestion/test_office.py tests/unit/ingestion/test_language.py tests/unit/ingestion/test_registry_composition.py tests/unit/ingestion/test_parsers.py -q` | 107 passed; focused mypy passed 5 source files |
| L05 | Fixed coordinator registry test punctuation flagged by Ruff, reran registry tests and targeted lint | 5 passed; targeted lint passed |
| L06 | Added `tests/integration/test_office_pipeline.py`: all four Office formats traverse real TaskWorker/catalog/local objects/pgvector | 4 passed in 6.33s; embedding provider is deterministic fake, NOT a real model |
| L07 | Added expired index lease acceptance test | FAILED: stale attempt wrote a vector before final heartbeat |
| L08 | Extended superseded-revision test to require initial index activation | FAILED: old revision ledger blocked activation |
| L09 | Added Windows backslash source-key traversal case | FAILED: source outside KB scope was parsed |
| L10 | Returned L07-L09 plus immutable artifacts, token truncation and runtime cache issues to pipeline worker | Interrupted during implementation; partial changes MUST be revalidated |
| L11 | Downloaded TEI CPU 1.9 image and began pinned MiniLM model acquisition under ignored `data/acceptance-20260908/tei-model` | Image present after interruption; model integrity/startup/live inference not yet verified |

Office limitations are explicit in `10-office-language.md`: ordinary external hyperlink relationships
are rejected, DOCX physical page positions are unknown, parser thread offload is not process isolation.

## Restart audit

L12: Coordinator inspected git status, current files, Docker services and tried to contact the former
pipeline agent `01a07f0c-c6d0-7a21-9b58-0baf0099a729`. Tool returned `agent ... not found`; no continued
execution can be assumed. Current code contains a bounded `index_mutation` context, attempt/UUID artifact
paths, source-key scope checks and token-size rejection, but package documentation still predates those
changes. Runtime cache fixes and acceptance results are not established.

Next: rerun the three regression cases; then assign ONE replacement worker to finish the bounded
pipeline repair package. Coordinator independently prepares live-model acceptance and updates this log.

L13: The focused restart command selected four cases (the source-key test has two parameters):
`uv run python -m pytest -o addopts= tests/integration/test_ingestion_pipeline.py -k
'expired_index_attempt or stale_revision_cannot or source_key_outside' -q` on `cairn_pipeline`.
Result: **2 passed, 2 failed**, 6.02s. Scope checks passed; remaining tests failed earlier at parse
retry (`INGESTION_STAGE_ERROR`), so neither lease fencing nor activation is accepted yet.

L14: Dispatched sole replacement worker **Zeno**, ID `01a0800b-9225-74d0-bf38-0d59f92993dc`,
`gpt-5.6-sol/high`. Scope is the pipeline repair modules and dedicated tests, with per-step logging
in `09-worker-pipeline.md`. No delegation by that worker and no other running worker permitted.

L15: Coordinator verified the model weight file (90,868,376 bytes), started owned
`cairn-completion-tei` container with the same continuation label, 2 CPU/2GiB/256 PID limits,
all capabilities dropped, no-new-privileges, read-only model bind and `HF_HUB_OFFLINE=1`.
Port: `127.0.0.1:38742`. TEI reports ready using **Candle CPU**, not ORT: ONNX is absent, then
the safetensors backend initializes successfully. Maximum tokens 256; batch requests capped at 4.
This is startup evidence only until the live test runs.

Pinned identities:
- Model revision: `sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41`.
- Image: `ghcr.io/huggingface/text-embeddings-inference@sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07`.
- Weights SHA256: `53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db`.
- Tokenizer SHA256: `be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037`.
- Artifacts stay under ignored `data/acceptance-20260908/tei-model`; no model was added to application dependencies.

L16: Coordinator added `tests/integration/test_live_model_pipeline.py`. This opt-in test uses actual
`PipelineRuntime`, TEI HTTP inference, exact local tokenizer, PostgreSQL/Redis/pgvector, query vectors
and replacement-source stale-point deletion. It explicitly skips unless both `CAIRN_TEST_TEI_URL`
and `CAIRN_TEST_TEI_TOKENIZER` are configured; it never substitutes a fake model. Execution pending.

L17: Live-model test first execution on `cairn_acceptance`: **1 failed in 4.01s**, same parse-stage
retry as L13. Test lint passed. A separate actual TEI `POST /embed` returned one 384-dimensional
vector (unnormalized norm about 7.1303), proving real inference works independently of ingestion.

L18: Coordinator sampled project-wide gates while the pipeline worker was editing. Ruff reported
10 findings across partial pipeline and advanced-chunking files; mypy reported 2 unreachable-type
checks in `semantic.py` (110 files checked). These are a mid-edit snapshot, not final acceptance.
The import audit passed **6 contracts**, and route authz audit passed **50 routes / 8 public**.

L19: Existing advanced/basic chunker tests passed **50 tests in 2.28s**, but inspection found
`validate_custom_chunks` never used source/config to check provenance and budgets. Added independent
`tests/unit/ingestion/test_custom_acceptance.py`: **1 passed, 8 failed** in 1.40s. It accepted fabricated
content, missing/out-of-range citations, oversized content, hidden standalone chunks, nonfinite
metadata, dangling parents and empty output. This is intentionally recorded red evidence for the
next advanced-chunking package; current pipeline worker is not assigned these files.

L20: Wrote `16-lifecycle-acceptance-plan.md` from existing M03/M07 requirements and inspected queue
producers/runtime publication. It defines the unimplemented fan-out/re-embed/retry/retirement slices
and per-version model coherence tests. No extra agent dispatched; Zeno remains the sole worker.

L21 (16:19 +08:00): After the worker changed the parse artifact path, coordinator reran
`uv run python -m pytest -o addopts= tests/integration/test_live_model_pipeline.py -q` against
`cairn_acceptance`, Redis DB0, `CAIRN_TEST_TEI_URL=http://127.0.0.1:38742` and
`CAIRN_TEST_TEI_TOKENIZER=D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json`.
**1 passed in 4.52s**, no skip; one existing Alembic deprecation warning. Actual production composition
performed real TEI inference, 384-dimensional vector indexing/query, source revision replacement and
old-point deletion. This supersedes L17's normal-path failure, NOT the pending fencing/lifecycle gates.
Coordinator notified the worker; worker remains responsible for finishing its repair package.

L22: Updated the WBS and historical continuation record with links to this ledger and explicit
current open gates. Historical success counts remain intact, but no longer imply the expanded
dirty worktree passes. Rejected custom-output cases and live-model normal-path evidence are both
visible from the main plan entrypoints.

L23: Worker documentation now records the confirmed parse root cause (Windows path length),
compact immutable artifact-key repair, independent-heartbeat guard, tokenizer overflow tests and
runtime lifecycle tests. Coordinator review found a new issue in the compact prefix: converting
workspace/KB IDs to unhyphenated hex violates `ObjectKeys.kb_prefix` and makes prefix-based cleanup
miss artifacts. Returned this to the same worker, requiring canonical first two key components and
a cleanup-prefix regression. Also requested safe handling of already-borrowed provider generations
during runtime reconfiguration. Package remains under review; no successor worker dispatched.

L24: Refined the independent custom over-budget test so its content exactly matches the source;
the sole invalid property is token budget. The focused case still fails (1 failed, 8 deselected,
1.26s); targeted lint passes. This prevents a future source-fidelity fix from accidentally masking
the separate missing budget check.

L25: Zeno delivered the repair package and was CLOSED before any successor dispatch. Coordinator
independently ran pipeline units, runtime units, pipeline integration, lease fencing, four Office
worker scenarios and the actual TEI scenario together on `cairn_acceptance`/Redis0:
**48 passed, no skips, 27.57s**, one existing Alembic warning. Machine-readable result:
`data/acceptance-20260908/pipeline-repair.xml` (ignored local verification artifact).
Targeted Ruff passed; focused strict mypy passed 7 source files; import audit kept all 6 contracts.

Acceptance: the bounded repair package passes its documented scope, with explicit limitations on
already-sent remote mutations, retired provider generations retained until shutdown, and unreferenced
crash artifacts retained until canonical-prefix cleanup. It does not close full-KB lifecycle or M07.

Next serial package: advanced semantic/custom chunker completion. Coordinator will own pipeline
composition and independent tests; implementation worker owns the chunker modules and its own tests.
This moves ahead of the larger lifecycle package because chunker interfaces must stabilize before
the final rebuild/ingestion acceptance. Lifecycle requirements remain unchanged in document 16.

L26: Dispatched sole advanced-chunking worker **Chandrasekhar**, ID
`01a08038-754f-7a00-afca-d59d3656bd51`, `gpt-5.6-sol/high`. It owns chunkers/semantic/custom, its
advanced tests and document 11; parent owns pipeline/runtime composition and independent acceptance.
It must fix all eight custom-output failures, semantic vector/source/token bounds and local static
findings without inventing an in-process untrusted Python sandbox.

L27: Added a semantic parameter to the actual TEI test. Initial RED: **1 failed**, chunk task ended
with `CHUNK_UNSUPPORTED_STRATEGY`. Parent then wired `EmbeddingServiceSemanticEmbedding` into the
chunk worker when strategy is semantic, using the current prepared model/service/binding identity.
Retest: **1 passed (markdown), 1 failed (semantic)**; semantic now reaches the real adapter but fails
`EMBED_CONFIG_INVALID`. Passed the exact source/config to the worker; suspected whitespace-only units
from overlapping punctuation separators require its root-cause test, not a silent source-dropping fix.

L28: Added optional `custom_executor_for(IngestionRun)` to `PipelineResources` and `PipelineRuntime`.
The explicit trusted resolver receives workspace/KB/document/revision context before resolving the
pinned function. The ordinary CLI has no executor and fails closed; task payloads cannot load code.
Added `tests/integration/test_custom_pipeline.py`: injected version-7 resolver completes the real
queue/index chain; absent resolver rejects custom processing before embedding. **2 passed in 3.98s**,
targeted Ruff passed. This proves wiring, not custom-output safety or the separate M12 sandbox gate.

L29: While the advanced worker owns its disjoint modules, coordinator converted two lifecycle source
findings into real database regressions in `tests/integration/test_pipeline_lifecycle_acceptance.py`.
Result: **2 failed in 4.99s** (targeted Ruff passes). A model-changing rebuild publishes the new 4D
model with the old 3D active namespace; starting another rebuild after version 2 fails attempts to
reuse version 2 and hits the version primary key. These are now runtime-proven failures for the next
lifecycle package, not merely code-reading concerns. The advanced worker is not assigned them.

L30: After the worker's semantic delimiter fix, coordinator reran live TEI `[markdown, semantic]`
and custom worker `[injected, absent]` together: **4 passed in 9.59s**. Saved JUnit report at
`data/acceptance-20260908/advanced-wiring.xml`. The live fixture now declares provider/model batch
limits of 16 to match the deployed server rather than relying on a larger default. A further review
request concerns headings larger than the semantic unit target (one-quarter chunk budget) but smaller
than the valid full chunk budget; worker must test this without dropping citation ranges.

L31: Full backend collection check, `uv run python -m pytest -o addopts= tests --collect-only -q`,
reported **680 tests collected and 2 collection errors** (4.46s). Both errors are the unfinished PDF/OCR
tests importing absent `cairn.ingestion.ocr`. This was collection, not execution: no full-suite pass or
coverage claim is made. Do not suppress/remove those pending tests to make the overall gate green.

L32: Ran the entire backend with `--continue-on-collection-errors --cov --cov-fail-under=80` so
missing modules stayed visible as errors while all collectable tests executed. Result:
**683 passed, 9 failed, 2 collection errors**, 146.97s; coverage **83.12%** clears the unchanged 80%
coverage threshold, but the command correctly fails. Report: `data/acceptance-20260908/backend-expanded.xml`.
Seven failures are legacy catalog fixtures using `raw/{name}`, now correctly rejected by the accepted
source-scope guard; two failures are L29 lifecycle regressions. Collection errors remain PDF/OCR.

L33: Fixed only the legacy `_upload` test fixture in `tests/integration/test_catalog.py` to use
`{workspace}/{kb}/originals/{hash}`. No production guard or assertion was weakened. Independent
catalog rerun: **32 passed in 21.97s**. The original nine-failure full-suite report is retained as
historical evidence; a complete rerun is still required before assigning a new full-suite count.

L34: Global static checks: Ruff lint passes, full strict mypy passes **110 source files**, all **6**
import contracts pass, route authorization audit passes **50 routes / 8 public**. Format check first
reported six files (catalog facade/service, pipeline/runtime tests and the two incomplete PDF/OCR
test files). Applied only formatter changes to those six; **163 files** now pass format checking.
Formatting the pending PDF/OCR tests does not implement their missing modules or remove their errors.

L35: Advanced worker delivered and was CLOSED. Coordinator reviewed the implementation and reran
all advanced/basic/custom acceptance tests independently: **106 passed in 2.74s**. JUnit:
`data/acceptance-20260908/advanced-independent.xml`. Global Ruff and full mypy (110 files) pass.
The legacy catalog fixture patch introduced mixed line endings; formatting that single file restored
the global **163-file** format gate. No behavior was changed by this formatting correction.

Accepted scope: real semantic-distance chunking and strict custom-output validation plus the explicit
worker composition seam. The acceptance does not include an M12 sandbox, PDF/OCR, reference-corpus
quality, full-KB lifecycle or a complete-project result.

L36: Final expanded backend rerun after fixture correction:
`uv run python -m pytest -o addopts= tests --continue-on-collection-errors --cov
--cov-report=xml:data/acceptance-20260908/coverage.xml --cov-fail-under=80
--junitxml=data/acceptance-20260908/backend-post-review.xml -q --tb=short`.
Result: **690 passed, 2 failed, 2 collection errors, no skips**, 125.05s, coverage **84.76%**.
The failures are exactly L29's active model/namespace mismatch and failed-version number reuse.
The collection errors are exactly the two pending PDF/OCR test files importing absent OCR code.
The real TEI markdown and semantic cases both executed; they were not skipped or replaced by fakes.
One pre-existing Alembic deprecation warning remains. All other collectable tests passed.

L37: Updated README and the docs map to expose this current ledger and all package records. Removed
stale statements that embedding had not started or that the four-stage worker was unconnected.
Historical 400-test results remain explicitly historical. Current open E2E/lifecycle/PDF/OCR/M12
gates remain visible at the repository entrypoint. No claim of whole-project completion was added.

L38: After all tests completed and both workers were closed, coordinator inspected each exact
container's `cairn.task` label and required `completion-20260908`, then removed only
`cairn-completion-tei`, `cairn-completion-redis` and `cairn-completion-pg`. Verified remaining running
containers are the pre-existing `cairn-m08-redis` and `cairn-m08-pg`. No application volume, model
asset, report, image, Git change or unrelated container was removed. No commit was created.

Observed database image identities for this run:
- `pgvector/pgvector:pg16`: `sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b`.
- `redis:7.4-alpine`: `sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf`.

## September 9 continuation

L39: Coordinator reread this ledger, package 16, current source and the two independent lifecycle
tests; inspected dirty Git status and exact Docker names. No AGENTS.md was found. No changes were
reverted. The only pre-existing running containers were cairn-m08-pg and cairn-m08-redis; both remain
untouched. Recreated cairn-completion-pg (39170, tmpfs) and cairn-completion-redis (38694) with label
`cairn.task=completion-20260909`, using the image tags and disposable credentials below. PostgreSQL
readiness passed; created separate cairn_pipeline and cairn_fencing databases. TEI is not running.
An initial read command used unsupported PowerShell brace expansion and failed before execution;
reran it with explicit file paths. No source or database mutation occurred in that failed command.

L40: Independent RED reproduced on fresh PostgreSQL/pgvector and Redis:
`uv run python -m pytest -o addopts= tests/integration/test_pipeline_lifecycle_acceptance.py -q
--tb=short --junitxml=data/acceptance-20260909/lifecycle-red.xml` with the external-service variables
below: **2 failed in 17.70s**, one existing Alembic warning. Active v1 is published with model B
instead of model A; failed v2 is allocated again and violates the version primary key. Root causes
confirmed in source: publication and ingestion read mutable KB model/config, and allocation uses
active+1 rather than durable allocation history. Next dispatch is ONLY version-owned configuration,
safe migration and monotonic allocation (package 16 slice 1). Fan-out/reembed/PDF/OCR remain separate.
Coordinator owns independent lifecycle acceptance tests and this ledger; worker owns bounded source,
migration, worker-specific tests and its package record. Only one sol/high agent may run.

L41: User requested durable status synchronization before pausing. Coordinator stopped before
dispatch: no new agent exists and no implementation was started. Verified each exact container's
`cairn.task` label equals `completion-20260909`, then removed only cairn-completion-redis and
cairn-completion-pg. Final `docker ps` shows only the pre-existing cairn-m08-redis (39716) and
cairn-m08-pg (38906) running; these were not modified. TEI was not started this round. Preserved
all dirty worktree changes, pinned model assets and the RED report. No commit was made. September 9
changes are documentation plus ignored test reports only. There is no test process left running.

Pause handoff:
- New result: exactly 2 lifecycle tests FAILED, matching the two known defects (L40).
- Last full backend result remains September 8: 690 passed, 2 failed, 2 collection errors,
  84.76% coverage; NOT a green suite and NOT a September 9 rerun.
- Next implementation: per-index-version frozen model/config, safe migration and monotonic version
  allocation. Read package 16, reproduce the two tests, dispatch one gpt-5.6-sol/high implementer,
  independently review and rerun acceptance; record actual dispatch ID only after dispatch happens.
- Later work: resumable fan-out, chunk reembed, stage recovery, activation/retirement; PDF/OCR and
  genuine annotated 30-PDF evaluation. Production M12 custom sandbox remains unimplemented.
- Recreate only dedicated disposable services using the command templates below, with a fresh
  ownership label matching the resumed run. Never use pre-existing M08/application databases:
  fixtures truncate their target database. No resource currently needs continuation cleanup.

L42: User resumed. Reread ledger/plan/current source, Docker names and Git status. Workspace is now
clean at user checkpoint `e9033ba`; previous changes were preserved in that commit outside this run.
No conflicting changes found. Created only cairn-completion-pg (39170, tmpfs) and
cairn-completion-redis (38694), label `cairn.task=completion-20260909-resume`. Dispatched sole worker
Pascal `01a0855f-d5ad-79d0-aa92-5c2a98a4b502`, sol/high, for package 16 slice 1. Explicit exclusions:
fan-out/reembed/retirement/PDF/OCR. Required full model snapshot validation, durable version high-water
mark, safe legacy migration and worker-specific TDD. Coordinator will add independent acceptance
without editing worker-owned source. DB test windows must be serialized because fixtures truncate.

L43: Fresh resume baseline reproduced **2 failed in 28.11s**, one existing Alembic warning,
report `data/acceptance-20260909/lifecycle-resume-red.xml`; no fix claimed. Created dedicated
cairn_pipeline/cairn_fencing databases after readiness. Released DB test window to Pascal.
Coordinator added three independent cases to the lifecycle acceptance file: allocation after failed
history purge, full active ModelRef preservation after registry drift, and active metric isolation.
These new cases await RED execution by worker before changes. Parent does not run truncating tests
concurrently. Current implementation scope excludes invisible remote model-weight mutation detection.

L44: While Pascal owns slice 1 and the DB test window, coordinator inspected production worker
registration, fan-out activation condition, parse manifest identity, manual edit payload, retry
state and both switch paths. Recorded concrete gaps and next acceptance contract in
`18-lifecycle-followup-review.md`. This is source investigation, NOT implemented functionality or
passing lifecycle evidence; no overlapping source edits or second agent was started.

L45: Coordinator added the sixth independent case (existing run retains version chunk settings
after desired config changes). Focused Ruff found only an extra import-group blank line; removed
it. Reviewed worker's initial document 17 design and REJECTED selective legacy snapshot backfill:
pointer alignment/no surviving failed row cannot prove unchanged model registry/chunk settings,
and old failed history may already have been purged. Requested conservative fail-closed legacy
handling or independently proven provenance, plus a populated migration regression. Also requested
explicit limits of reconstructing a pre-migration allocation maximum after historical purges.
This is a review correction, not acceptance. Pascal remains the sole agent with the DB test window.

L46: Coordinator verified retained TEI model/tokenizer SHA-256 against L15 (both match) and started
only cairn-completion-tei, label `cairn.task=completion-20260909-resume`, pinned CPU image and
read-only retained model mount using the existing command template below. `/health` succeeded;
logs show Candle CPU ready. This is readiness evidence, NOT yet a new live pipeline pass. Three
owned containers now require eventual label-verified cleanup. Added a dated historical-scope note
to parser checkpoint 05 so its September 7 unconnected-worker description is not mistaken for
current status. PDF/OCR quality remains OPEN; no new engine licence audit was performed.

L47: Worker reported initial five-case RED. Coordinator reviewed the metric case and found its
setup was invalid: ADR-0006 correctly rejects changing an active KB metric without opening a new
building pointer. Corrected ONLY the independent test setup to emulate the atomic desired-metric
and building-pointer update. Explicitly rejected removing the existing DB guard just to satisfy
the invalid setup. The corrected test must fail at the runtime metric assertion before acceptance.
This distinction preserves the existing database invariant rather than weakening production code.

L48: Interim review of slice-1 source: full version DTO, snapshot guard, conservative unavailable
legacy rows and full-ModelRef worker resolver are being implemented. Coordinator requested a
same-model-ID metadata-change rebuild regression: desired KB dimension must refresh even when no
replacement ID was supplied. Requested explicit tokenizer-asset limitation (snapshot stores ID,
not historical tokenizer bytes), narrowed DB exception assertions, and migration rollout guidance
for legacy Redis entries plus unavailable building versions. Parent's six-case file passes focused
Ruff and formatting checks. These source observations remain provisional until independent GREEN.

L49: Worker document 17 reports focused GREEN and explicitly releases the DB window. Coordinator
started independent verification of lifecycle6, worker snapshot/migration tests, runtime units,
catalog, original pipeline, Office, custom, live TEI and fencing against dedicated real services;
report target `data/acceptance-20260909/version-independent.xml`. Also started independent global
Ruff/format/mypy/import/authz checks. No acceptance is recorded until actual exit/results are read.
Interim migration review requested and received preservation of dangling building-pointer evidence,
guarded int4-range JSON evidence casts and a database guard against decreasing the high-water mark.

L50: Independent bounded verification completed: **86 passed, no skips, 64.69s**, four instances
of the existing Alembic path_separator warning (including downgrade/upgrade). This includes the
six parent lifecycle tests, snapshot/migration regressions, real TEI markdown+semantic, Office,
custom, original pipeline/catalog and task fencing. Report: `version-independent.xml` under the
September 9 acceptance directory. Global lint passed; mypy passed 110 source files; route authz
passed 50 routes/8 public. Initial format check rejected four worker-owned files; returned for
format-only correction. `python -m importlinter.cli` produced no contract evidence, so coordinator
called `from importlinter.cli import lint_imports; lint_imports()` explicitly: **6 kept, 0 broken**.
Started full backend with real TEI enabled, collection errors retained and unchanged coverage80
threshold. Its result is not yet known; PDF/OCR remain unimplemented.

L51: Pascal delivered format-only corrections and was explicitly closed. Coordinator reran global
format: **165 files already formatted**; `git diff --check` passed (only Git CRLF notices). No
business logic changed after the 86-test run. Full backend verification remains in progress.
Spec/quality review confirms slice-1 boundaries, retained ADR-0006 guard, immutable snapshots,
nondecreasing allocation guard, full-ModelRef resolver comparison before provider startup and
stage/vector writes, unchanged custom executor safety, and explicit conservative upgrade limits.
No second agent has been dispatched; later lifecycle slices remain unimplemented.

L52: Coordinator final full backend command completed against genuine PostgreSQL/pgvector, Redis
and pinned TEI, with both live-model environment variables enabled:

```powershell
uv run python -m pytest -o addopts= tests --continue-on-collection-errors --cov --cov-report=xml:data/acceptance-20260909/coverage.xml --cov-fail-under=80 --junitxml=data/acceptance-20260909/backend-post-snapshot.xml -q --tb=short
```

Result: **703 passed, 2 collection errors, no assertion failures, no skips, 137.18s; coverage84.83%**.
Four warnings are the existing Alembic path_separator warning from initial migration and migration
round-trip. Errors are exactly `test_ocr.py` and `test_pdf.py` importing missing `cairn.ingestion.ocr`.
No tests were removed/skipped and the coverage gate was not weakened. Command exits1, as required.
Coordinator ACCEPTS bounded lifecycle slice1 on spec and code quality after independent source,
migration/locking review, 86-test focused evidence, full-suite no-regression evidence and static
checks. This is not full lifecycle/E2E acceptance. Documentation maps and WBS are synchronized.

L53: With tests finished and Pascal closed, verified each exact container's `cairn.task` label
equals `completion-20260909-resume`, then removed only cairn-completion-tei,
cairn-completion-redis and cairn-completion-pg. Verified only pre-existing cairn-m08-redis (39716)
and cairn-m08-pg (38906) remain running. Preserved all workspace changes/reports/model assets;
no commit, application migration or application-data cleanup was performed. Current work is based
on user checkpoint e9033ba. An initial multi-document status patch failed its expected-text check
because worker document17's heading had changed; inspected current text and reapplied successfully.
No business code was changed during that documentation correction. Final diff whitespace check
passed, with only Git CRLF conversion notices. All commands/agents started by this round have ended.

L54 (September 10): Read ledger/document18/current source and inspected Git/Docker. Prior slice1
changes remain dirty; preserved all. No AGENTS.md found. Dispatched sole implementer Hume
`01a089d3-4592-7be2-a698-5d50ce1cdb8e`, sol/high, no descendants, for bounded fan-out and activation
enrollment barrier. Requires documented design, TDD, parse-artifact reuse, current-document coverage,
source-change/delete handling and production factory registration. Excludes manual reembed,
retirement handlers, PDF/OCR. Parent owns separate `test_reindex_fanout_acceptance.py`. Recreating
dedicated PG/Redis under label `completion-20260910`; M08 containers are not owned and remain untouched.
Agent must wait for DB readiness and exclusive test window. No new completion claims.

L55: Dedicated PG/Redis readiness passed; created cairn_pipeline/cairn_fencing databases.
Coordinator added three independent acceptance tests and ran
`uv run python -m pytest -o addopts= tests/integration/test_reindex_fanout_acceptance.py -q
--tb=short --junitxml=data/acceptance-20260910/fanout-red.xml` with external-service variables.
**3 failed in 27.47s**: no factory handler, premature v2 activation after only a new upload,
and delayed v2 failure clearing building v3. One existing Alembic warning. Parent fixed Ruff's
try/except/pass style warning using suppress(Conflict), preserving assertions. Released DB window
to Hume for TDD implementation; parent prepares additional acceptance without concurrent truncation.

L56: Parent added a fourth independent acceptance case: same-model rebuild through the production
worker factory must activate v2 without a second parser call; verify new parse envelope identity and
equal old/new namespace point counts. Focused lint and format pass; execution belongs to Hume's DB
window. Parent flagged deletion-after-first-batch indexing: excluding a deleted ledger is not enough
if its vectors already exist in the building namespace. Such points must be reconciled before
activation; absence of document.purge support must not be concealed by aggregate-count tests.

L57: Hume delivered pre-code document19 for coordinator review; no implementation yet at that
checkpoint. Approved cursor+generation+bounded reconciliation/direct enrollment design and trusted
constructor batch size1..1000. Required two corrections before continuing: external deletion guard
must not hold task FOR UPDATE while invoking separate-transaction heartbeat; mirror the accepted
index_mutation special lock order. Also handle a build becoming empty after start: explicitly fail
and preserve old active (or create/verify empty namespace), never publish a nonexistent namespace.
Sent continuation to the SAME agent; no second agent started. Restored owned TEI (same September10
label), verified retained model/tokenizer SHA256 and HTTP200 health; not yet new live pipeline evidence.

L58: Parent reserved and extended existing live-model acceptance for BOTH markdown and semantic:
after actual TEI initial indexing, query and source replacement, execute production worker-factory
rebuild and require active v2/384dim metadata, correct Pacific query result and version-scoped IDs.
All earlier assertions retained; not yet new GREEN. Worker notified of non-overlapping ownership.
The accepted batch constructor seam is `catalog.reindex.ReindexFanoutService(batch_size=...)`;
parent prepares bounded batch/replay acceptance against that seam.

L59: Parent added fifth acceptance case using trusted batch_size1: first batch is indexed but must
not activate; delete that document, complete rebuild, assert its remote target-version points are
gone and surviving-document points remain. Production worker factory supplies document handlers;
only the approved fan-out service instance is injected for batch sizing. Test deliberately defers
the out-of-scope `document.purge` task by one day rather than substituting a fake handler. This
tests build-only cleanup, NOT general deletion purge. Parent acceptance/live files pass lint/format;
worker owns RED/GREEN execution until it releases DB window.

L60: Parent corrected its deletion test operator from invalid `eq` to the vector filter contract's
`$eq`; the earlier KeyError was test setup error, not product evidence. Interim source review found
a concurrency risk: nonlocking version read followed by FOR UPDATE in the same SQLAlchemy session
can retain the identity-map's old generation. Requested forced refresh on locked reads plus copied
observed scalar values and a delayed-predecessor concurrency regression. Otherwise a stale task
could overwrite a newer generation despite acquiring the right locks. No acceptance yet.

L61: Interim review found a new completion-recovery gap: last fan-out (or build cleanup) can commit
activation then fail publishing Redis runtime; blindly skipping a replay because the build is now
active loses publication retry. Requested a bounded active-target publication retry with no repeated
enrollment/embedding, plus injected-failure regression. General cross-generation cache ordering
remains a later slice; this request only protects the new handler's own committed switch.

L62: Interim parse-reuse review caught regression of accepted source scoping: the draft moved
source-key validation after object-store resolution and skipped it when reuse succeeds. Returned
for correction: always validate source scope before resolver/read, and reject out-of-KB prior
artifact references before reading them. Requested scope regressions in addition to valid/corrupt
reuse checks. Same sole worker continues; no independent GREEN or final acceptance yet.

L63: Parent fixed its parse-envelope read to supply required `max_bytes`; previous TypeError was
test harness error. Cleanup review found a revision-chain gap: after replacement chunk commit,
old vectors can outlive their catalog chunk rows until index stage. Deleting only current chunk IDs
would miss them and clear the activation barrier. Requested target namespace/document-ID filtered
delete plus zero-count verification, and an indexed -> replaced/chunked -> deleted regression.
Also requested complete document workspace/KB checks on cleanup publication replay. This extends
the deletion correctness proof, not the scope to general purge or retirement.

L64: Parent parametrized its deletion acceptance with replacement-before-delete (parse+chunk only)
to independently prove cleanup removes old revision vectors too; parent file now six tests. Both
parent files pass lint/format. Further cleanup review requested namespace creation/verified absence
using frozen version configuration: a deleted chunked document may have no vector namespace yet,
and blindly deleting/counting a missing PostgreSQL table would strand the cleanup retry. Also
flagged unnecessary initial/no-chunk cleanup scheduling for existing one-stage worker tests.
All work is still under the sole Hume agent; no final acceptance or new full-suite result yet.

L65: Worker reported targeted/static GREEN and released DB window. Parent review REJECTED a scope
change made to preserve old control-plane fixtures: explicit activation had been exempted entirely
from the approved rebuild barrier. Added independent seventh acceptance case and ran it on real
services: **1 failed in 4.62s**, `DID NOT RAISE Conflict`; unfinished v2 was published immediately.
Report `data/acceptance-20260910/explicit-switch-red.xml`. Required restoring enrollment/coverage
guard for real rebuilds with document history while preserving only initial-v1/never-ingested
legacy control-plane seams. Returned focused DB window to the SAME Hume agent. No acceptance yet.
Read-only process/PG activity inspection found no stuck process; no process was killed.

L66: Hume corrected the explicit activation bypass and added empty-abandonment handling that
commits failed v2, preserves active/cached v1 and raises Conflict outside the transaction instead
of logging a false activation. One catalog test fixture now creates a building-v2 chunk directly
to test non-active chunk edit refusal without force-activating an unfinished real rebuild.
Worker reports focused6 passed in8.71s and releases DB window. Coordinator started independent
fanout7/owned regressions/live TEI/fencing/migration verification, report `fanout-independent.xml`.
Hume was explicitly CLOSED; no running subagents. Independent lint, mypy111, imports6 and route50/8
passed; one source formatting issue was returned and corrected, final format rerun pending.

L67: Independent targeted verification completed **31 passed, no skips, 32.60s**, including parent
seven-case acceptance, owned fanout/replay/failure/replacement tests, both actual TEI rebuild/query
strategies, task fencing and real migration round-trip. Report `fanout-independent.xml`. Four
warnings are existing Alembic path_separator notices. Final global lint and format passed (168
Python files); diff whitespace passed. Full backend with actual TEI enabled, collection errors
retained and unchanged coverage80 gate is now running; result pending. Hume remains closed.

L68: Final source/error-hierarchy review found prior-artifact fallback did not catch the object-store
contract's ObjectNotFound/ObjectTooLarge (CairnError subclasses, not OSError/ValueError). Missing or
oversized reusable artifacts could terminally fail instead of reparsing valid source as documented.
Resumed the SAME Hume ID (sole agent, sol/high retained) for narrow unit RED/GREEN and explicit
exception handling, without swallowing storage-unavailable errors. Parent full-suite baseline is
still running; worker is forbidden DB tests until released. Final acceptance remains pending.

L69: First independent full backend (before the final missing-artifact correction/tests) completed:
**725 passed, 2 known PDF/OCR collection errors, no assertion failures/skips, 85.01% coverage,
160.91s**, four existing Alembic warnings; report `backend-post-fanout.xml`. This is not final
acceptance because L68 remains open. Parent expanded parse-reuse integration to valid/missing/
corrupt/oversized actual local artifacts, preserving all envelope/index assertions; file now ten
cases and passes lint/format. Running missing/oversized RED independently; worker stays unit-only.

L70: Missing/oversized integration RED independently confirmed **2 failed in5.92s** (OBJECT_NOT_FOUND
and OBJECT_TOO_LARGE terminally blocked v2). Hume delivered explicit fallback exception handling
and six real-local-store unit regressions; unavailable-store errors still propagate and source
size limits still apply. Worker reports unit4fail/2pass ->6pass, then was explicitly CLOSED again.
No running subagents. Coordinator started final combined independent verification including all
ten parent cases and the six new units; report `fanout-final-independent.xml`. Final full backend
will follow this run; L69 remains pre-correction historical evidence.

L71: Final combined independent verification passed **40 tests, no skips, 37.64s**, including
actual missing/oversized/corrupt artifact recovery, all parent fan-out/deletion/explicit-switch
cases, unit storage-error boundaries, actual TEI markdown+semantic rebuild/query, fencing and
migration cycle. Four warnings are the existing Alembic deprecation. Report
`fanout-final-independent.xml`. Started final full backend with real TEI and unchanged coverage80
threshold, report `backend-final.xml`/`coverage-final.xml`; final static rerun also in progress.

L72: Final independent backend command completed after the last production fix, with real PG/Redis,
pgvector and pinned TEI plus both live-model environment variables enabled:

```powershell
uv run python -m pytest -o addopts= tests --continue-on-collection-errors --cov --cov-report=xml:data/acceptance-20260910/coverage-final.xml --cov-fail-under=80 --junitxml=data/acceptance-20260910/backend-final.xml -q --tb=short
```

**734 passed, 2 collection errors, no assertion failures/skips, 174.70s, coverage85.05%.** Errors
remain exactly `test_ocr.py` and `test_pdf.py` importing absent `cairn.ingestion.ocr`; four warnings
are existing Alembic path_separator notices. Command exits1; no test/coverage gate was weakened.
Final independent lint, format169, mypy111, import contracts6 and route authz50/8 passed. Coordinator
ACCEPTS bounded slice2 on spec and code quality after review corrections, exact regression evidence,
real-model rebuilding/query verification and no assertion regression in the complete backend.
No whole-E2E acceptance: initial/never-ingested administrative compatibility, failed-stage blocking,
general purge/retirement and cache-generation limitations remain explicit in document19. Source
changes and all pre-existing work remain uncommitted. README, WBS, package maps and next action updated.

L73: After every test process finished and Hume was closed, verified exact container labels equal
`completion-20260910`, then removed only cairn-completion-tei, cairn-completion-redis and
cairn-completion-pg. Final docker inventory contains the untouched pre-existing cairn-m08-redis
(39716) and cairn-m08-pg (38906). All reports and pinned TEI assets remain under ignored data;
all pre-existing and new workspace changes are preserved. No application database migration,
application-data cleanup, Git commit, branch or worktree was performed. No running subagent or
test process remains. Restart from package16 slice3 after recreating dedicated disposable services.

L74 (September14): Reread ledger/plan and current chunk edit/pipeline seams; inspected dirty Git
status, no AGENTS.md found. Existing changes from accepted slices1-2 remain uncommitted and were
not reverted. Only pre-existing M08 containers were running. Dispatched sole sol/high worker Euclid
`01a09dfa-a006-7c02-b589-cdd3a1d2e563` for bounded slice3, no descendants. Requires durable immutable
edit identity, current version/revision/hash fencing, exact token budget, parent non-vector policy,
verified external writes and production factory registration. Excludes general recovery/purge/
retirement/PDF/OCR. Coordinator owns `test_chunk_reembed_acceptance.py` and existing live-model tests.
Recreating dedicated PG/Redis under label `completion-20260914`; agent waits for DB window release.

L75: Dedicated PG/Redis readiness passed and test-only databases created. Parent added three
independent tests and ran `uv run python -m pytest -o addopts=
tests/integration/test_chunk_reembed_acceptance.py -q --tb=short
--junitxml=data/acceptance-20260914/reembed-red.xml`: **3 failed in28.25s**. Factory lacks handler,
edited catalog text leaves vector unchanged, task lacks expected hash/revision. One existing
Alembic warning. Parent file passes lint/format. Released exclusive DB test window to Euclid;
no second tester or agent will use truncating fixtures concurrently.

L76: Parent expanded independent acceptance to six cases (stale edit before embedding, parent
non-vector semantics, oversized edit refusal) and added actual TEI manual-edit/query assertions to
both markdown/semantic live scenarios after their existing v2 rebuild. Restored owned TEI under
September14 label; retained model/tokenizer SHA256 match prior pinned assets. Parent corrected
format/import issues found by Ruff; no production edits. Actual new TEI edit behavior not yet GREEN.
All three owned services require eventual cleanup; M08 services remain untouched.

L77: Reviewed doc20 design and approved bounded generation+snapshot/recheck approach. Requires
immutable edit_generation/hash/revision/version task identity, stable edited UUID, provider I/O
outside SQL locks, full payload verification and parent context-only edits. Edits during rebuild
or committed chunk/embed manifests are rejected; pre-manifest compatibility must carry edits safely.
Requested explicit migration handling for legacy identity-incomplete tasks, copied DTO metadata,
identity-map refresh and revision/version tests around physical chunk replacement. Corrected draft
evidence wording: parent ran only initial3 RED, not all expanded6. Same Euclid continues TDD; no
second agent. No implementation acceptance yet.

L78: Parent added seventh independent regression: a provider callback commits a newer edit during
the first embedding request; the old attempt must not hold SQL locks across model I/O, must not
write its obsolete vector, and the latest task must win. Timeout bounds deadlock detection at10s.
Parent file passes lint/format. Worker reports expanded baseline5failed/1passed (parent non-vector
state alone was already true despite the absent handler); no false baseline success inferred.

L79: Interim source review found task dedupe omitted source revision despite physical chunk rows
resetting edit generation after replacement. An unchanged deterministic chunk ID could reuse gen1
in revision2 while revision1/gen1 task remains ready, suppressing the new task. Requested revision
in dedupe and a replacement/old-task regression; also explicit deleting/archived KB and document
state checks. The same worker retains exclusive DB test window; no new acceptance yet.

L80: Interim handler/facade review confirmed optimistic model call followed by locked identity
recheck, but requested persisting measured token counts after successful reembedding (old counts
would mislead UI/rebuild estimates), consistent deleting/archived guards on load and mutation, and
stale-version handling before snapshot decoding. Asked worker to checkpoint exact current RED/GREEN
in document20 rather than deferring the durable record to final delivery. No acceptance yet.

L81: Worker checkpoint reports17 targeted tests passed, including all seven parent cases. Review
token-count regression exposed a KB-wide sum incorrectly updating one document (144 rather than44);
worker corrected to document/version-scoped aggregate and recorded RED/GREEN. Coordinator requested
remaining explicit shared-manifest/rebuild edit refusal, expired lease, stale scope/source/version
and post-write retry coverage before handoff. These are required guard proofs, not new scope.
Only Euclid remains active; parent has not independently accepted implementation yet.

L82: Final point-verification review required returned Hit.id to equal the edited chunk ID as well
as exact payload; worker added wrong-ID regression and correction. Worker journal reports focused/
static/live TEI GREEN and explicitly releases DB window. Coordinator starts independent new-feature,
catalog/original pipeline/fan-out/migration/fencing/real-model tests, report `reembed-independent.xml`,
and independent global static checks. Worker instructed not to run more DB commands. Results pending.

L83: Independent targeted verification passed **101 tests, no skips, 74.79s**, covering new manual
edit tests, original catalog/pipeline/fan-out, snapshot migration, fencing and actual TEI edits/query
for markdown and semantic. Four warnings are existing Alembic path_separator deprecations. Global
lint, format172, mypy111, all6 import contracts and route authorization50/8 passed. Started full
backend with live TEI enabled, retained PDF/OCR collection errors and unchanged coverage80 gate;
report `data/acceptance-20260914/backend-final.xml`. This full result is still pending.

L84: Euclid delivered final handoff and was explicitly closed. Coordinator reviewed scope against
slice3: revision-aware monotonic edit identity, active-version metadata, exact pre-provider budget,
parent no-vector policy, no provider network I/O under SQL locks, final lease/identity/payload checks,
post-write retry and document-scoped token aggregation. Remote write/read consistency and unchanged
provider/tokenizer asset provenance limits remain explicit, not a cross-system transaction claim.
Legacy identity-incomplete tasks require re-editing; no invented historical identity. No second
agent or additional lifecycle package was dispatched. Full backend still pending.

L85: Independent full backend found one missed compatibility assertion: old unit pipeline startup
test still expected only document.embed. Actual factory correctly registers chunk.reembed too.
Result before correction: **760 passed,1 failed,2 known PDF/OCR collection errors,85.18% coverage,
177.40s**, no skips. Coordinator updated only that expected handler set to include the implemented
kind, retaining exact-set assertion; no production behavior or test was removed. Final targeted/
full rerun required before acceptance. Original failed evidence remains in backend-final.xml.

L86: Corrected exact registration assertion passed with adjacent unit/entrypoint suites:
**27 passed in2.63s**; focused lint/format passed. Started fresh full backend with actual TEI
enabled and unchanged coverage80 gate; new reports backend-verified.xml / coverage-verified.xml
preserve the earlier failure report instead of overwriting it. No running agent; only this final
verification process is active.

L87: Fresh final full backend completed with actual PG/pgvector, Redis and pinned TEI enabled:

```powershell
uv run python -m pytest -o addopts= tests --continue-on-collection-errors --cov --cov-report=xml:data/acceptance-20260914/coverage-verified.xml --cov-fail-under=80 --junitxml=data/acceptance-20260914/backend-verified.xml -q --tb=short
```

**761 passed,2 collection errors,no assertion failures/skips,165.78s,coverage85.18%**. Exactly the
known missing OCR module causes test_ocr.py/test_pdf.py collection errors; four existing Alembic
warnings remain. Command correctly exits1. Coordinator ACCEPTS bounded slice3 on spec/code quality
after independent101-test verification, full backend regression correction/rerun, global static
gates and review of edit identity, locks, exact budget/payload, retry and parent semantics. No test
was removed and coverage80 gate remains unchanged. Full E2E/PDF quality is NOT accepted. README,
WBS and package map updated with current evidence and remaining lifecycle/legacy upgrade limits.

L88: With Euclid closed and all test processes finished, verified each exact container's
`cairn.task` label equals `completion-20260914`, then removed only cairn-completion-tei,
cairn-completion-redis and cairn-completion-pg. Final Docker inventory shows untouched pre-existing
M08 Redis39716 and PG38906. Preserved all code/docs, historical reports, final reports and pinned
model assets. No application DB migration, unrelated cleanup, Git commit/branch/worktree was made.
No running subagent or test process remains. Resume at package16 slice4 after recreating dedicated
disposable services, never with the application or pre-existing M08 databases.

L89 (user-requested repository checkpoint): User explicitly authorized committing and pushing to
the current online branch. Coordinator verified current branch `dev`, upstream `origin/dev`, and
origin `https://github.com/Hurrvey/Cairn`. After `git fetch origin`, HEAD/upstream divergence was
0/0. Reviewed all39 changed/new source, migration, test and documentation files; model assets and
ignored acceptance reports remain excluded. `git diff --check` passed with only CRLF notices.
This checkpoint includes accepted bounded slices1-3 and retains the known two PDF/OCR collection
errors/E2E-open status. No business code or new test run was needed for this Git-only operation.
Commit message: `feat(ingestion): add versioned rebuild and manual reembedding lifecycle`.
The containing commit identifies this checkpoint; final push outcome is verified against remote
refs rather than assumed here. No new branch, force-push, history rewrite or resource startup is
authorized or required. Earlier no-commit statements describe their historical development steps.

## Concrete next action

1. Read this latest checkpoint, then document 16. There is no running worker to wait for or assume
   is fixing anything. Dispatch at most one new `gpt-5.6-sol/high` worker.
2. Recreate disposable services before any database test; never point truncating fixtures at the
   pre-existing M08 or application databases. Use dedicated database names as in the inventory below.
3. Bounded slices1-3 are accepted; do not redispatch snapshot/fanout/manual-reembed work. Next is
   package16 slice4: persist failed stage and resume validated committed artifacts without gratuitous
   reparse/reembedding, preserving task/revision/version fencing and manual edits. Then general
   purge/retirement and broader runtime cache-generation ordering. Read documents17/19/20 limits,
   especially legacy unavailable snapshots and identity-incomplete edit-task remediation.
4. Then finish PDF/OCR adapters and the genuine annotated 30-document quality evaluation. Existing
   `test_pdf.py`/`test_ocr.py` are unfinished tests, not implemented parsers; keep the quality gate open.
5. Rerun the complete backend without collection errors/failing cases before claiming the knowledge-
   base E2E acceptance. The M12 production custom-code sandbox remains a separately explicit gate.

### Recreate this local verification stack

The credentials below belong only to disposable test containers. These commands create fresh test
services; check that the exact names/ports are free first. Wait for `pg_isready` before `createdb`.

```powershell
docker run -d --name cairn-completion-pg --label cairn.task=completion-20260908 --tmpfs /var/lib/postgresql/data -e POSTGRES_USER=cairn -e POSTGRES_PASSWORD=cairn-disposable-only -e POSTGRES_DB=cairn_acceptance -p 127.0.0.1:39170:5432 pgvector/pgvector:pg16
docker exec cairn-completion-pg pg_isready -U cairn -d cairn_acceptance
docker exec cairn-completion-pg createdb -U cairn cairn_pipeline
docker exec cairn-completion-pg createdb -U cairn cairn_fencing
docker run -d --name cairn-completion-redis --label cairn.task=completion-20260908 -p 127.0.0.1:38694:6379 redis:7.4-alpine
$env:CAIRN_TEST_USE_EXTERNAL_SERVICES='1'
$env:CAIRN_DATABASE_URL='postgresql+asyncpg://cairn:cairn-disposable-only@127.0.0.1:39170/cairn_acceptance'
$env:CAIRN_REDIS_URL='redis://127.0.0.1:38694/0'
```

For the opt-in actual-model cases, retained weights/tokenizer must match L15. Recreate TEI with the
pinned image digest and model bind; the fixed host port below is the port observed during this run.

```powershell
docker run -d --name cairn-completion-tei --label cairn.task=completion-20260908 --cpus 2 --memory 2g --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges --mount 'type=bind,source=D:\code\Cairn\data\acceptance-20260908\tei-model,target=/model,readonly' -e HF_HUB_OFFLINE=1 -p 127.0.0.1:38742:80 ghcr.io/huggingface/text-embeddings-inference@sha256:ad950d30878eceb72aaf32024d26fa2b1d04a75304fa0b4776b49aa1941fea07 --model-id /model --pooling mean --max-client-batch-size 16 --max-batch-tokens 4096 --max-concurrent-requests 16
$env:CAIRN_TEST_TEI_URL='http://127.0.0.1:38742'
$env:CAIRN_TEST_TEI_TOKENIZER='D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json'
```

After startup, verify TEI readiness and rerun L36. Without the two explicit TEI environment variables,
the two live-model cases intentionally skip; such a run is not equivalent to the recorded evidence.

## Package acceptance status at pause

1. Pipeline bounded repair: ACCEPTED September 8 (L25), with documented remote-write cancellation,
   retired provider lifetime and orphan artifact limitations; not full lifecycle acceptance.
2. Pipeline lifecycle: `kb.reindex_fanout`, `chunk.reembed`, recovery/incremental/version retirement.
3. Advanced chunking: bounded semantic/custom modules and worker composition ACCEPTED September 8.
   Custom requires an explicitly injected trusted executor; M12 production sandbox is still OPEN.
4. PDF/OCR: finish actual adapters and registry/worker integration; follow the planned OCR engine or
   document and justify any deviation. Partial tests do not establish implemented parsers.
5. Genuine 30-document corpus and independent annotations/evaluation per `05-parser-spike.md`.
6. Full backend/static/boundary/authz acceptance plus real model integration, reproducible docs and
   cleanup of only continuation-owned disposable resources.

## Disposable service inventory

Historical inventory only: the three continuation-owned containers were removed in L38.

- `cairn-completion-pg`, label `cairn.task=completion-20260908`, loopback port 39170, pgvector PostgreSQL16,
  tmpfs data. Dedicated databases: `cairn_pipeline`, `cairn_fencing`, `cairn_acceptance`.
- `cairn-completion-redis`, same label, loopback port 38694. DB1 pipeline, DB2 fencing, DB0 acceptance.
- Set `CAIRN_TEST_USE_EXTERNAL_SERVICES=1` and the correct dedicated connection URLs for EACH command.
  Integration fixtures truncate their test DB. Do not use an application database.
- Pre-existing `cairn-m08-pg` and `cairn-m08-redis` are NOT owned by this continuation; leave untouched.
- No live TEI container started as of L12. Downloaded image/model assets are not inference evidence.
- Use `uv run python -m pytest/mypy/ruff` to avoid stale Windows executable-launcher paths.
