# T-M07-04 / T-M07-06 — Office parsing and language resolution

**Date:** 2026-09-08 · **Status:** implemented; owned tests and strict static checks verified

## Scope and integration contract

This package adds parser adapters without changing the existing parser protocol or registry:

- `DocxParser()` handles the OOXML Word MIME type.
- `PptxParser()` handles the OOXML PowerPoint MIME type.
- `XlsxParser()` and `CsvParser()` are strict MIME-bound spreadsheet adapters.
- `SheetParser()` remains available for explicit direct-call auto-detection; do not register it
  for both MIME types because `ParseContext` carries no MIME and corrupt XLSX could look like CSV.
- `get_language_detector()` builds a cached offline detector comparing all Lingua languages.
- `await resolve_language(text, explicit="und", detector=None)` returns
  `LanguageResult(language: str, confidence: float)`, preserves an explicit caller language,
  and performs bounded CPU-offloaded detection only for `und`.
- `tokenizer_for_embedding(model, registry)` delegates to
  `TokenizerRegistry.for_model(model)`. Language detection never guesses, replaces, or selects a
  tokenizer independently of the configured embedding model.

Parent composition now registers DOCX, PPTX, XLSX and CSV instances in `get_parser_registry()`.
The parent-owned integration is present: `ParserRegistry.parse()` resolves language after successful parsing only when
`ctx.language == "und"`, then assigns `result.language` to the parsed document. Workers consume
that composed registry; they must not detect again or override explicit caller language.
Registry and worker edits remain parent-owned and outside this package's write scope.

## Dependencies for the parent change

The parent has pinned and installed the application dependencies and updated its lock:

- `python-docx==1.2.0` for WordprocessingML package semantics;
- `python-pptx==1.0.2` for PresentationML package semantics;
- `openpyxl==3.1.5` for SpreadsheetML values, formulas, dates, and worksheet visibility;
- `defusedxml==0.7.1` for DTD/entity-rejecting XML parsing;
- `lingua-language-detector==2.2.0` for local statistical language detection.

The parent also installed `types-openpyxl` and `types-defusedxml` as development dependencies.
`office_compat.py` uses normal imports and narrowly typed upstream boundary wrappers, not dynamic
imports, missing-dependency fallbacks, or broad mypy suppressions. Parent owns all declarations/locks.

python-docx, python-pptx, and openpyxl publish MIT licenses; defusedxml publishes PSF licensing;
lingua-py publishes Apache-2.0 licensing. These are engineering observations, not release-image
legal clearance. Bundled model, wheel, dependency and notice review remains a release responsibility.

## Parser behavior

Every adapter executes package parsing in an AnyIO worker thread. The parser registry remains the
outer upload-size boundary; direct Office adapter calls also receive a package-level safety bound.

DOCX emits headings, paragraphs/lists, and GFM tables in document order. Heading levels and paths
are retained. Merged table cells emit text once, with empty continuation cells, and omitted grid
columns are preserved. WordprocessingML does not contain reliable rendered page boundaries, so DOCX blocks
have `page=None` and `pages=1`; inventing physical page numbers would create false citations.

PPTX emits one atomic block per non-empty slide. A slide block includes its title first, then text
and GFM tables in shape z-order, including nested groups. It uses the one-based slide number as
`page`. The title is also its heading path. Empty slides still count in `pages` and slide metadata.
Slides containing a table use the `table` block kind so chunking keeps the complete slide table
together. Slide names and numbers are retained in metadata.

XLSX emits one atomic GFM table block per non-empty worksheet. The first row is preserved verbatim
as the table header, sheet names form heading paths, and the one-based sheet position is the logical
page. Hidden-state provenance is retained in metadata. Workbooks load read-only with
`data_only=False` and `keep_links=False`: scalar and array formulas are indexed as formula source
and never evaluated or replaced by potentially stale cached values. Excel data-table formulas
without an indexable source expression are rejected explicitly. Worksheet dimensions are reset so
incorrect producer-provided dimensions cannot silently discard cells; XML cell coordinates are
bounded before OpenPyXL can expand a sparse rectangle. Blank leading rows remain the header if
present; trailing empty rows are removed. Empty sheets count in page/sheet metadata but emit no block.

CSV uses the caller's explicit encoding (UTF-8 by default), rejects decoding failures and NUL
bytes, and parses with strict CSV semantics. Its first record is the GFM header. Ragged records are
padded to a stable rectangular table and formula-looking strings remain inert source text.
Comma, semicolon and tab detection uses at most 64 KiB, falls back to the header line for ragged
input, and defaults to comma for single-column input. Quoting always uses strict standard CSV
double-quote semantics; inferred quoting rules cannot relax validation. Blank records are skipped.
Cells preserve whitespace and escape literal HTML and pipe characters for GFM rendering.

## OOXML safety envelope

Before a package library sees input, `inspect_ooxml_package()` validates ZIP metadata, reads every
member to verify CRC/expanded size, and inspects XML by both suffix and manifest content type.
It rejects:

- non-ZIP data and OLE encrypted-package containers;
- encrypted ZIP entries;
- absolute paths, drive paths, backslash paths, NULs, and `..` traversal segments;
- more than 10,000 members;
- input larger than 64 MiB;
- a member larger than 64 MiB uncompressed;
- more than 256 MiB total uncompressed data;
- compression ratios above 100:1 for members of at least 1 KiB;
- duplicate member names, symbolic links, and compression other than stored/deflate;
- XML parts above 16 MiB, 500,000 nodes, or 128 levels deep;
- XML DTD or entity declarations, including UTF-16 encoded declarations;
- malformed relationship XML, missing targets, root-escaping relative targets, and all external targets;
- worksheet extents above 100,000 rows, 1,024 columns or 1,000,000 grid cells;
- Word grid spans above 1,024 columns or cumulative span expansion above 1,000,000 cells;
- individual rendered tables above approximately 8 Mi characters and documents above 16 Mi characters.

The package is inspected in memory and never extracted to disk. Rejecting external relationships
is intentionally stricter than merely ignoring them: it guarantees parsing cannot follow network,
UNC, or local-file targets and makes linked-package behavior explicit to contributors.

## Language behavior

The cached detector compares all bundled Lingua languages, then permits output only for Chinese,
Japanese, Korean, English, French, German, Spanish, Portuguese, Italian, Dutch, Polish, and Czech.
An unsupported winning language maps to `und`; restricting model candidates would otherwise force
unsupported text into a falsely confident supported tag. Supported results map deterministically
to `zh`, `ja`, `ko`, `en`, `fr`, `de`, `es`, `pt`, `it`, `nl`, `pl`, and `cs` respectively.
Detection samples at most 20,000 characters from the beginning, middle, and end of the
document. Inputs with fewer than 20 letters, a top confidence below 0.75, or a top-two confidence
margin below 0.15 resolve to `LanguageResult("und", 0.0)`. Lingua scores are relative confidence
within its candidate set, not calibrated correctness probabilities. Mixed-language documents may
remain undetermined; these tests do not establish a measured corpus accuracy threshold. Model
initialization and inference run off the event loop; no runtime download or network client is used.

Explicit language values are returned byte-for-byte without loading or calling the detector. This
keeps caller authority above inference and avoids changing already classified documents.

Tokenizer selection is independent of this result: composition loads each model's exact tokenizer
artifact under its configured ID and registers it with `TokenizerRegistry.register()`. Call
`tokenizer_for_embedding(model, registry)` and pass that same returned tokenizer to chunking and
embedding. A missing/unregistered tokenizer raises `TokenizerUnavailable`; non-embedding model
references raise `ValueError`. There is no word-count, language-specific, random, or generic fallback.

## Deliberate limits

No OCR, image/chart extraction, slide notes, rendered DOCX pagination, nested Word table traversal,
or guaranteed visual reading-order reconstruction is claimed. Entire sheet/slide tables are atomic;
the existing chunker must retain table blocks intact to keep headers with data. This package does
not add row-based chunk splitting or duplicate headers across chunks. Native/parser work is
offloaded to threads, not isolated processes: hostile-document CPU/memory limits still require
deployment-level resource isolation. Rejected external relationships include ordinary hyperlinks.
The hard safety limits deliberately reject some large but legitimate documents instead of truncating.

## Test and validation matrix

Tests create real Office packages in memory with python-docx, python-pptx, and openpyxl. They cover
document-order headings and tables, slide and sheet provenance, hidden sheets, formulas, strict CSV
decoding and rectangularization, corrupt and encrypted packages, traversal entries, compression
bombs, DTD/entity declarations, external relationships, event-loop offload, deterministic CJK and
Latin detection, low-confidence `und`, explicit-language preservation, and model-bound tokenizer
lookup.

Targeted validation commands:

```text
.venv/Scripts/python.exe -m pytest tests/unit/ingestion/test_office.py tests/unit/ingestion/test_language.py -q
.venv/Scripts/python.exe -m ruff check src/cairn/ingestion/office.py src/cairn/ingestion/office_package.py src/cairn/ingestion/office_compat.py src/cairn/ingestion/language.py tests/unit/ingestion/test_office.py tests/unit/ingestion/test_language.py
.venv/Scripts/python.exe -m mypy --strict --follow-imports=silent src/cairn/ingestion/office.py src/cairn/ingestion/office_package.py src/cairn/ingestion/office_compat.py src/cairn/ingestion/language.py
```

### Executed verification, 2026-09-08

- TDD RED: initial collection failed because the two production modules did not exist. Additional
  fixture-backed RED cases exposed UTF-16 DTDs, extensionless XML, root-escaping relationships,
  bad worksheet dimensions, grouped shapes, merged cells, array formulas and output expansion.
- Final command: `uv run python -m pytest -o addopts= tests/unit/ingestion/test_office.py tests/unit/ingestion/test_language.py -q`.
  Result: **60 passed in 3.29s**.
- `uv run python -m mypy --strict --follow-imports=silent` over `office.py`, `office_package.py`,
  `office_compat.py`, `language.py`, `test_office.py`, and `test_language.py`:
  **Success: no issues found in 6 source files**. Imports remain normal, stubs are installed, and
  imported parent-owned modules are not re-diagnosed by this scoped check.
- `uv run python -m ruff check` over the same six paths: **All checks passed!**
- `uv run python -m ruff format --check` over the same six paths: **6 files already formatted**.
- `git diff --check --` over the seven owned paths exited **0**. These new files are untracked in
  the shared workspace, so this Git check alone is not a substitute for the executed formatter.
- Tests used actual in-memory python-docx, python-pptx and openpyxl packages and real offline Lingua
  detection; no database, OCR, network detector, registry edits, commits or child agents were used.
- Full application/E2E validation and registry-composition checks belong to the coordinator; the
  figures here report only this package's independently executed checks.
