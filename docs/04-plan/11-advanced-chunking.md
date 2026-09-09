# M07 advanced chunking increment

Coordinator acceptance (2026-09-08): **106 independently rerun unit cases passed** (L35 in the
execution ledger); actual TEI semantic processing and scoped custom worker wiring were also verified
separately. The bounded package is accepted. Production M12 sandbox execution and the overall M07/E2E
gate remain open. No subagent remains running after this delivery.

**Date:** 2026-09-08 · **Status:** worker package delivered; parent acceptance pending. M12 remains separate.

## Ordered execution record

1. **2026-09-08 — package accepted.** Write scope is limited to
   `src/cairn/ingestion/{chunkers,semantic,custom}.py`,
   `tests/unit/ingestion/test_advanced_chunkers.py`, optional worker-owned advanced tests, and this
   document. No commit, rollback, subagent, worker/runtime/registry work, untrusted Python execution,
   or M12 sandbox expansion is permitted.
2. **Inherited RED baseline (not yet rerun by this worker).** Coordinator command against
   `test_custom_acceptance.py` reported **1 passed, 8 failed**: fabricated content, missing and
   out-of-range citations, a source-faithful 40-token chunk over a 32-token budget, hidden standalone
   output, non-finite metadata, a dangling parent, and empty output were accepted. The earlier
   advanced/basic suite reported **50 passed**, while Ruff reported advanced-test style findings and
   strict mypy reported two unreachable checks in `semantic.py`. These are starting evidence, not a
   completion claim.
3. **Parent composition contract confirmed.** Pipeline will inject
   `EmbeddingServiceSemanticEmbedding(service, model, binding_identity)` for semantic runs and may
   resolve a trusted `CustomChunkExecutor` from `custom_executor_for(IngestionRun)` with workspace/KB
   context. This package keeps both public seams unchanged and does not touch pipeline/runtime. The
   service adapter must reject any semantic unit that exceeds the selected model's real tokenizer
   limit before `EmbeddingService` can silently truncate it; absence of a matching tokenizer is an
   explicit failure, not a fixed-chunk fallback or disguised semantic result.
4. **Worker baseline reproduced (RED).**
   `uv run python -m pytest -o addopts= tests/unit/ingestion/test_advanced_chunkers.py
   tests/unit/ingestion/test_chunkers.py tests/unit/ingestion/test_custom_acceptance.py -q`
   completed with **51 passed, 8 failed in 2.88s**. All eight failures are the intended independent
   custom-boundary cases from step 2; no semantic/basic regression failed. Root cause inspection
   confirms `validate_custom_chunks` only checks list/type/scope/ordinal/ID/hash/token-count and coarse
   serialization sizes: it never reads `doc` or `cfg`, never reconstructs citations, never validates
   role links/provenance/table rules, and permits JSON NaN because `allow_nan` defaults to true.
5. **Live semantic integration RED received from parent.** Pipeline composition now reaches the
   injected semantic adapter, but the real TEI case fails during chunking with
   `EMBED_CONFIG_INVALID`; markdown remains green. For the Astronomy source, configured separators
   include both `". "` and `"."`. Current `_semantic_units` records both ends independently, creating
   the source-preserving but embedding-invalid one-character whitespace interval between coincident
   sentence delimiters. The regression will use the real `HuggingFaceTokenizer` wrapper (including
   special tokens), require no blank embedding request, and require continuous citation coverage;
   the fix must coalesce delimiter boundaries without dropping source whitespace.
6. **Custom input-snapshot review added.** `_chunk_custom` currently exposes the same mutable
   `ParsedDocument` and copied `ChunkConfig` objects to the executor and then validates against those
   potentially modified objects. The executor will instead receive independent deep copies while
   validation retains an unexposed pre-execution baseline. A regression must reject output made
   self-consistent by replacing `doc.blocks` and increasing `cfg.child_tokens`, and must prove the
   caller's document remains unchanged. Malformed field types, invalid Unicode, and non-finite JSON
   remain custom-output errors; cancellation and executor failures are not rewritten as validation
   failures.
7. **Expanded worker tests verified RED.**
   `uv run python -m pytest -o addopts= tests/unit/ingestion/test_advanced_chunkers.py -q`
   completed with **21 passed, 19 failed in 1.85s**. The failures independently expose model-limit
   truncation, the overlapping-delimiter blank unit, omitted source ranges, ten citation/top-level
   provenance mutations, child ranges outside their parent, split protected tables,
   heading-and-special-token budget overflow, unstable raw exceptions for wrong/invalid-Unicode
   content, and executor mutation of the validation baseline. Existing peak/identity/batching,
   CJK/table isolation, malformed-vector, local/provider batch-cap and cancellation tests passed;
   the latter passing cases document behavior already present rather than claiming the package green.
8. **Focused implementation GREEN.** After coalescing overlapping semantic delimiter ends, enforcing
   pre-service normalized token limits, validating runtime vector shapes, snapshotting custom inputs,
   and adding source/provenance/link/table/budget/finite-JSON validation, the same focused advanced
   command completed with **40 passed in 1.27s**. This establishes the worker-owned cases only; the
   independent custom acceptance and existing basic chunkers are checked next.
9. **Independent/basic regression GREEN.** The required combined command
   `uv run python -m pytest -o addopts= tests/unit/ingestion/test_advanced_chunkers.py
   tests/unit/ingestion/test_chunkers.py tests/unit/ingestion/test_custom_acceptance.py -q`
   completed with **94 passed in 2.43s**. In particular, the coordinator-owned acceptance changed
   from 1/8 to **9/9 passing** without modification. This does not include PDF/OCR modules or parent-
   owned pipeline/runtime/registry tests.
10. **Targeted Ruff RED.**
    `uv run python -m ruff check src/cairn/ingestion/chunkers.py
    src/cairn/ingestion/semantic.py src/cairn/ingestion/custom.py
    tests/unit/ingestion/test_advanced_chunkers.py` reported **26 findings**: import ordering,
    postponed-annotation `cast` quoting, and five overlong lines. These are static-only findings in
    the bounded files; they are corrected manually before rerunning tests and strict mypy.
11. **Targeted Ruff GREEN.** The identical four-file `ruff check` command now reports
    **All checks passed**. No rule, strictness setting, or exclusion was changed.
12. **Parent-owned real integration evidence received.** Parent independently ran real
    `test_live_model_pipeline` for `[markdown, semantic]` plus custom-pipeline `[injected, absent]`:
    **4 passed in 9.59s**, recorded at the ignored local artifact
    `data/acceptance-20260908/advanced-wiring.xml`. This covers actual TEI semantic inference,
    revision-2 replacement and stale-point deletion, trusted scoped custom resolution, and absent-
    executor fail-closed behavior. The parent owns those tests and wiring; this worker made no
    pipeline/runtime/registry changes.
13. **Focused strict mypy GREEN.**
    `uv run python -m mypy src/cairn/ingestion/chunkers.py
    src/cairn/ingestion/semantic.py src/cairn/ingestion/custom.py` reports
    **Success: no issues found in 3 source files**. The prior unreachable checks were removed by
    validating provider returns through explicit `object`/runtime-sequence boundaries; strict mode
    and `warn_unreachable` remain enabled.
14. **Semantic long-heading budget review accepted.** The internal
    `child_tokens // 4` unit target is incorrectly passed to heading-inclusive `_fit`; therefore a
    heading larger than that sampling target but smaller than the real child budget can reject an
    otherwise legal semantic chunk. A real HF tokenizer/special-token regression will establish RED.
    Semantic unit splitting must use the complete child budget as its hard fit bound, continue
    coalescing whitespace-only delimiter intervals, and retain complete citation coverage.
15. **Long-heading regression verified RED.** The single-case command for
    `test_semantic_unit_target_does_not_turn_legal_long_heading_into_hard_limit` failed with
    `ChunkBudgetExceeded` in `_fit(..., budget=16)` even though the real HF tokenizer counts the
    heading-prefixed source below the configured 64-token child limit. This confirms the internal
    quarter-target, not the configured/model budget, is the root cause.
16. **Long-heading regression GREEN.** Semantic sentence units now use `child_tokens` as their hard
    heading-inclusive fit bound; the single-case command passes **1 test in 1.18s**. The change does
    not filter whitespace: overlapping delimiter ends are still coalesced so all source positions
    remain represented by unit/window citations.
17. **Expanded advanced suite GREEN.** After adding explicit over-dimension vector, all four local
    custom-output limit, and retained scope/ordinal/hash/token/ID guard cases, the worker-owned command
    completed with **52 passed in 1.19s**. These additions are bounded characterization/edge tests;
    they do not introduce a sandbox, lifecycle, or reindex feature.
18. **Post-edge static checks.** Targeted `ruff check` remained GREEN (`All checks passed`). Targeted
    `ruff format --check` then reported **3 files would be reformatted** (`chunkers.py`, `custom.py`,
    `semantic.py`); most displayed churn is mixed Windows line endings in these new dirty-worktree
    files, plus ordinary formatter compaction. The formatter is applied only to the bounded files,
    followed by fresh tests/lint/type checks.
19. **Formatting and required unit acceptance GREEN.** Targeted `ruff format` reformatted exactly
    three bounded source files and left the advanced test unchanged. The required combined pytest
    command then completed with **106 passed in 2.78s**: 52 expanded advanced cases, 45 existing basic
    chunker cases, and all 9 coordinator-owned custom acceptance cases. Counts include parametrized
    cases. No PDF/OCR tests were run, per the known missing-module limitation.
20. **Fresh static verification and project mypy RED.** Targeted format check reported **4 files
    already formatted**; targeted Ruff and project-wide `ruff check .` both reported **All checks
    passed**; focused strict mypy again reported no issues in the three source files. Project-wide
    `uv run python -m mypy` checked 110 files and found one parent-wiring type error at
    `pipeline.py:170`: the semantic Protocol described writable identity/batch attributes while
    `EmbeddingServiceSemanticEmbedding.max_batch_size` is intentionally a read-only property. The
    runtime API is unchanged; the Protocol is corrected to express these members as read-only before
    rerunning the project gate.
21. **Project strict mypy GREEN.** After expressing `SemanticEmbedding.identity` and
    `max_batch_size` as read-only Protocol properties, `uv run python -m mypy` reports
    **Success: no issues found in 110 source files**. `DocumentChunker(..., semantic=..., custom=...)`
    and `chunk(doc, cfg)` remain unchanged; this was a structural-typing correction required by the
    parent-owned pipeline composition.
22. **Final worker verification GREEN.** Fresh project-wide `uv run python -m ruff check .` reports
    **All checks passed**; focused `ruff format --check` reports **4 files already formatted**; and the
    required combined pytest command reports **106 passed in 3.12s**. Project-wide strict mypy's
    immediately preceding fresh result is **110 source files clean**. Delivered files are exactly:
    `src/cairn/ingestion/chunkers.py`, `src/cairn/ingestion/semantic.py`,
    `src/cairn/ingestion/custom.py`, `tests/unit/ingestion/test_advanced_chunkers.py`, and this
    document. No commit or rollback was made. Known boundaries remain: executor trust resolution and
    worker composition are parent-owned; arbitrary code isolation is M12; lifecycle/reindex and
    PDF/OCR are not part of this package. Parent acceptance, not this worker record, determines M07
    closure.
23. **Final review reopened one bounded semantic edge.** Step 22 remains valid evidence for that
    revision, but it is not the final claim: delimiter coalescing prevents one-character blank units,
    while `_fit` can still split a long source whitespace run into all-blank units under a byte-level
    child budget. A new actual-tokenizer regression must fail first. The correction will merge blank
    source spans into adjacent semantic units without deleting them; final `_windows` processing must
    still enforce output budgets and complete citation coverage.
24. **Budget-split whitespace regression verified RED.** The focused overlapping-delimiter test,
    extended with a byte-level tokenizer and 100 source spaces under a 32-token budget, failed with
    `SemanticChunkingError`. Observed provider input candidates were `"first. "`, three 32-space
    strings, and `"   second."`. This confirms blank units arise after `_fit`, beyond the earlier
    delimiter-end coalescing stage.
25. **Budget-split whitespace regression GREEN and parent backend evidence received.** Blank fitted
    spans are now attached to adjacent non-blank semantic units without deleting source positions;
    the focused case passes **1 test in 1.10s**, retains exact citation coverage, and final output
    stays within 32 tokens. Parent's broader backend run reported **683 passed, 9 failed plus the 2
    known missing PDF/OCR collection modules** at 83.12% coverage; seven failures were accepted-scope
    stale catalog fixtures and passed as **32 catalog tests** after parent canonicalized those fixtures,
    while two are assigned to the future lifecycle package. No new failure was attributed to this
    package. Per parent instruction, no further tests or functionality will be added.
26. **Final delivery verification.** The required three-file pytest command passes **106 tests in
    2.75s** after the final whitespace fix and formatting. Project-wide Ruff reports **All checks
    passed**. The first post-patch format check found only the newly patched `chunkers.py` line-ending
    region; formatting that one bounded file and rerunning reports **4 files already formatted**.
    Project-wide strict mypy reports **110 source files clean**. Final changed-file list remains the
    five paths recorded in step 22; no parent-owned acceptance, pipeline/runtime/registry, M12,
    lifecycle, PDF/OCR, or catalog file was modified. Status is delivery to parent for independent
    acceptance, not project or M07 completion.

## Goal

Complete the `semantic` and `custom` `ChunkConfig` strategies without changing the
published `Chunker.chunk(doc, cfg)` call shape. Semantic chunking must place real
boundaries at adjacent embedding-distance peaks. Custom chunking must invoke only an
explicitly injected, version-pinned executor and must distrust and validate every returned
`ChunkSpec`.

## Composition contract

`DocumentChunker` retains its required `document_id`, `tokenizer`, and `index_version`
constructor arguments and adds optional keyword-only `semantic` and `custom` dependencies.
The worker factory selects and prepares these dependencies; the chunker does not resolve
models, function versions, credentials, or services itself.

- A semantic dependency has a stable model/configuration identity, an explicit maximum batch
  size, and an asynchronous document-embedding operation. Cairn supplies adapters for an
  `EmbeddingService` paired with a concrete `ModelRef` and for a locally composed asynchronous
  callback.
- A custom dependency receives a positive `FunctionVersionRef`, `ParsedDocument`,
  `ChunkConfig`, deterministic document/index/tokenizer scope, and immutable execution limits.
  Its adapter is responsible for calling the published M12 chunk slot
  `run(doc, cfg) -> list[Chunk]` and materializing `ChunkSpec` values.
- If the selected optional dependency is absent, `semantic` or `custom` raises
  `CHUNK_UNSUPPORTED_STRATEGY`. There is no fallback to fixed or recursive chunking.

This local executor seam is not a Python evaluator and is not a security boundary. Production
user code still requires the separate versioned M12 sandbox runner, gVisor/Firecracker boundary,
resource controls, egress policy, and security review described in M12.

## Semantic design

1. Preserve heading changes and protected tables as hard section boundaries.
2. Divide prose into source-preserving sentence/paragraph units, splitting long units at exact
   tokenizer boundaries so later chunks can always satisfy the configured budget including the
   injected heading.
3. Embed units in batches no larger than both Cairn's limit and the injected provider's limit.
   Reject missing, extra, non-finite, zero-length, zero-norm, or dimension-mismatched vectors.
4. Compute cosine distance between adjacent vectors. A semantic boundary is a positive local
   maximum at or above the deterministic 80th-percentile distance threshold.
5. Aggregate units at semantic peaks while enforcing `child_tokens` exactly with the embedding
   model tokenizer. Budget-forced boundaries take precedence; sub-minimum peaks merge forward.
6. Generate the same citations, table flags, content hashes, and UUIDv5 identity shape as other
   built-in strategies. The semantic provider identity participates in the configuration key.

## Custom validation

Custom output is rejected unless it is a concrete list of `ChunkSpec` values within all local
limits. Validation covers contiguous ordinals, unique canonical UUIDv5 IDs, exact document scope,
UTF-8 content hashes, exact tokenizer counts, heading-inclusive budgets, role/embed consistency,
parent existence and citation containment, citation fields and source ranges, reconstructed source
content, top-level provenance, JSON-serializable metadata, and protected-table integrity.

The local defense-in-depth limits are fixed and passed to the executor: 10,000 chunks, 8 MiB total
serialized output, 1 MiB UTF-8 content per chunk, and 4,096 citations per chunk. These bounds do not
replace M12's process CPU, memory, PID, filesystem, network, wall-clock, concurrency, or lifecycle
controls.

## TDD sequence

- Add a failing semantic peak test using the real byte-level tiktoken fixture, then implement
  source-preserving units, bounded embedding calls, cosine peaks, and exact-budget assembly.
- Add failing semantic identity, CJK/table, malformed-vector, batching, failure, and cancellation
  tests, implementing only the validation and propagation each requires.
- Add failing custom version/executor tests, then introduce the executor request/scope/limits
  contract while retaining unsupported behavior when no executor is injected.
- Add failing custom output-validation tests for scope, IDs, hashes, tokens, provenance, links,
  tables, and resource limits, then implement strict validation.
- Run focused advanced tests after every red/green cycle, then all existing ingestion tests,
  formatting, lint, strict mypy, and the complete backend suite if the shared workspace remains
  runnable. Do not alter unrelated failures or user changes.

## Out of scope

Worker factories, stage handlers, registry changes, catalog/core configuration changes, function
authoring and publishing, sandbox transport/runtime implementation, model downloads, and arbitrary
untrusted Python execution are not part of this increment.
