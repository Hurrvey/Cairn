# Ingestion execution ledger

Date: 2026-09-09. Execution: PAUSED AT USER REQUEST. Knowledge-base E2E NOT ACCEPTED.

This ledger is the durable restart point. Read it together with `13-ingestion-completion-coordination.md`
before dispatching work. Preserve the dirty worktree; do not infer completion from existing files.

## Latest checkpoint (read before historical steps)

September 9 pause checkpoint (L39-L41): user requested a pause after the independent RED run.
**No subagent was dispatched; none is running. No business code was changed this round.**
Lifecycle slice 1 is still PLANNED, NOT IMPLEMENTED. Both newly created September 9 test containers
were ownership-verified and removed. Reports remain in `data/acceptance-20260909/`.
Resume at package 16 slice 1 only after recreating disposable services. Do not infer fixes from
the successful reproduction of failures. The following September 8 acceptance results remain
historical; there was no new full-suite run on September 9.

- Running subagents: **none**. Both repair and advanced workers delivered and were explicitly closed.
- Pipeline bounded repair: accepted by coordinator; original severe regressions and real model/Office
  wiring passed (L25). Remote accepted writes are not claimed to be transactionally cancellable.
- Advanced semantic/custom package: coordinator independently passed **106 tests**; semantic is wired
  to the actual model service. Custom needs an explicitly injected scoped executor and otherwise fails
  closed; production M12 sandbox execution is NOT implemented or accepted.
- Full-KB lifecycle: OPEN, two real regressions in `test_pipeline_lifecycle_acceptance.py` (L29).
- PDF/OCR: OPEN, missing implementation modules still cause collection errors. Real 30-document PDF
  quality evaluation remains OPEN; there is no annotated benchmark or measured quality pass.
- Final expanded backend: **690 passed, 2 failed, 2 collection errors**, no skips, **84.76% coverage**.
  Coverage meets the unchanged 80% threshold; the test command still correctly exits nonzero.
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

## Concrete next action

1. Read this latest checkpoint, then document 16. There is no running worker to wait for or assume
   is fixing anything. Dispatch at most one new `gpt-5.6-sol/high` worker.
2. Recreate disposable services before any database test; never point truncating fixtures at the
   pre-existing M08 or application databases. Use dedicated database names as in the inventory below.
3. Implement/accept the lifecycle package, starting with the two existing failing tests. Stabilize
   version-owned model configuration and monotonically allocated versions before fan-out/re-embed.
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
