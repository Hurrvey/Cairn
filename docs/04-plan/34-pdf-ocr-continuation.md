# PDF/OCR, corpus quality, and retrieval continuation

Date: 2026-09-21. User authorized the sequence PDF/OCR -> public 30-document
quality evaluation/full regression -> advanced retrieval. Public samples approved.

## Scope and acceptance

- Preserve all existing dirty work and running company databases. No truncating
  tests against application services. No commits or live upgrades as a side effect
  of unit tests. Baseline copies are under ignored data/acceptance-pdf-20260921.
- Implement in Cairn first; synchronize only verified runtime changes into the
  company fork, retaining its namespace, credentials, company SVGs and no-docs/no-tests policy.
- Existing draft interfaces in test_pdf.py/test_ocr.py are the starting contract,
  not evidence that a complete parser or a qualified engine already exists.
- PDFium runs in a killable subprocess, never concurrently in application threads.
  Bound source bytes, pages, per-page pixels/chars, emitted bytes and wall time.
  Cancellation must reap children and remove temporary data.
- Blocks preserve page, PDF-point bottom-left bounding boxes, source URL and order.
  Image OCR uses pixel top-left bounding boxes. Encrypted, corrupt, oversized,
  unavailable-engine and timed-out inputs have stable sanitized errors.
- OCR uses a replaceable protocol. Tesseract is the initial bounded CPU adapter
  matching the draft tests, with English/simplified/traditional Chinese runtime
  packages. This is a documented baseline deviation from proposed RapidOCR;
  it does not claim better CJK quality or replace the quality gate.
- Text PDFs work without OCR. OCR dependency/configuration is explicit; no silent
  empty output or whole-document fallback when only one page needs OCR.
- Public corpus: six documents each in academic, financial/table-heavy, scans,
  CJK and multi-column categories; source, redistribution terms and SHA-256.
  Independent reference annotations cannot be copied from parser output. Human
  review remains explicit when only machine/reference-text checks are available.
  Report table F1 against 0.85 and per-category reading order/citation/OCR results;
  never label synthetic regression tests as the genuine 30-document gate.

## Implementation sequence

- [x] Reproduce existing RED: both PDF/OCR test files fail collection because
  cairn.ingestion.ocr is absent. Existing application services inventoried read-only.
- [ ] OCR subprocess adapter: src/cairn/ingestion/ocr.py and process helper;
  run tests/unit/ingestion/test_ocr.py plus cancellation, output bounds, malformed
  image/TSV and language-selection cases. Preserve OcrWord/OcrResult/OcrLimits,
  TesseractOcrEngine.recognize and ImageOcrParser.parse draft interfaces.
- [ ] PDF process protocol and layout: pdf.py, pdf_worker.py, pdf_layout.py.
  PdfParser(ocr=None, limits=PdfLimits()).parse(bytes, ParseContext) returns
  ParsedDocument. Test real PDF extraction, multi-page order, tables/columns,
  page OCR geometry, invalid/encrypted input, resource bounds and cancellation.
- [ ] End-to-end wiring: registry/runtime settings, validated upload formats and
  magic, frontend input hints, pinned dependencies and Docker OCR runtime.
  Verify public multipart -> task -> parse -> chunk -> embed -> pgvector -> query
  using separate disposable services and actual model inference.
- [ ] Public corpus and reproducible evaluation: pinned manifest/downloader,
  reference annotation format, bounded runner and category report. Keep any unmet
  threshold or unavailable independent annotations visibly open.
- [ ] Full backend/frontend/static/authorization regression. Resolve existing
  PDF/OCR collection failures by implementation, not skips. Review bounded-process
  code and integration changes before synchronizing the company runtime fork.
- [ ] Advanced retrieval: inspect current frozen contracts and implement weighted
  fusion/parent expansion/rerank in independently verified increments after the
  PDF and evaluation checkpoint. Do not advertise unsupported capabilities.

## Validation commands

```powershell
uv run python -m pytest tests/unit/ingestion/test_pdf.py tests/unit/ingestion/test_ocr.py -q
uv run python -m pytest tests/unit/ingestion -q
uv run python -m ruff check src apps tests scripts
uv run python -m mypy src/cairn apps
```

Full service-backed commands will record dedicated container labels and explicit
connection URLs without exposing passwords. A stopped deployment or absent
annotation is a limitation, not a passing result.

## Decision record

PDF baseline: pypdfium2 plus deterministic layout; isolates native PDF operations.
Docling remains the quality-comparison candidate if baseline tables fail the gate.
OCR baseline: Tesseract subprocess rather than an implicit native library in the
API process; portable timeout/cancellation and available CPU language packages.
Neither choice closes T-M07-01 until measured on the public corpus.

Primary API references inspected on September 21:
- https://pypdfium2.readthedocs.io/en/stable/python_api.html
- https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html

## Progress

P01: User approved public redistributable sample corpus. Draft tests fail at the
expected missing-module imports. No application database modified.

P02: PDF/OCR baseline implemented. OCR/process17 tests and PDF33 tests passed,
real English/Chinese PNG and image-only PDF OCR passed in no-network CPU container.
Public multipart upload -> task -> parse/chunk/embed/index -> pgvector retained
page and bbox. Actual TEI PDF lifecycle test passed. Frontend48 tests/build passed.
Initial full backend1023:1021 pass,2 failures,0 collection errors/skips. Failures
were obsolete supported-format expectation and concurrent blank-page patch; both
addressed pending final full rerun. Independent review fixed missing-language
silent fallback, TIFF directory enumeration and unkillable parent-process layout.

P03: Public corpus30 artifacts all parsed;23 are DocLayNet source-page excerpts,
7 are complete original documents. Metadata/terms/SHA256 under ignored evidence.
These do not meet30 complete annotated originals. No gold table-cell or reading
order labels, so table F1/read-order remain unmeasured and quality gate OPEN.
Official scan PDFs contain existing text layers (corrected weak earlier byte probe);
separate image-only page derivatives exercise actual OCR, not counted as originals.
Advanced retrieval proceeds independently after this measured checkpoint, not on
a claim that PDF quality acceptance is complete.

P04 (September21-22): Advanced retrieval implemented in Cairn: weighted min-max
fusion with target weights (`retrieval/fusion.py`), workspace-scoped TEI/Infinity
rerank adapter with SSRF-safe bounded HTTP (`retrieval/rerank.py`, operator
`CAIRN_RETRIEVAL__RERANK_ENDPOINTS`), and parent expansion from immutable
version/revision-scoped parent snapshots written into child payloads during the
chunk stage. Manual chunk edits mark the active KB `parent_snapshots_safe=false`
(catalog projection) so siblings cannot expand obsolete parent text; publication
is invalidated before commit and republished after. Capability discovery reports
`weighted_fusion`, `parent_expansion` and `rerank` (true only when endpoints are
configured). The previous run ended at "overall verification" because of a Codex
usage limit; P05 records the verification completed afterwards.

P05 (September22 closure): One retrieval unit helper predated the producer's
parent-snapshot `metadata` field and left five unit failures; corrected the test
fixture only (no production change). Ruff formatter applied to four files.
Independent gates on the dirty worktree: Ruff lint clean, format clean (248
files), mypy strict 142 source files, import contracts 7 kept/0 broken, route
authz 67 routes/8 public. Frontend: 48 tests, typecheck, production build and
API drift check pass. Full backend against the retained disposable
`pdf-20260921` PostgreSQL/pgvector, Redis and pinned TEI with both live-model
variables set: **1066 passed, 0 failed, 0 collection errors, 0 skips,
coverage 84.62%** (80% gate unchanged), exit 0. Reports:
`data/acceptance-final-20260922/backend-full.xml` and `coverage.xml`. This is
the first complete backend command that exits zero; PDF/OCR draft tests are
now implemented tests. The genuine annotated 30-document quality gate remains
OPEN (P03). Company fork synchronized: retrieval service/fusion/rerank,
pipeline parent snapshots, catalog edited-chunk guard, runtime DTO, config and
capability discovery ported with the fork's namespace/brand strings preserved;
fork image rebuilt, stack restarted, `/readyz` ok, `/v1/meta` reports the new
capabilities, no worker tracebacks. Sync helpers stay under ignored
`data/fork_compare.py` and `data/fork_sync.py` (never copied into the fork).
