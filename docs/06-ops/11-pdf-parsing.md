# PDF parsing and immutable structure inspection

The product parses PDFs with Docling 2.135.0 (`CAIRN_INGESTION__PDF_ENGINE=docling`,
set by the Compose `worker-parse` service). The `pdfium` engine with local Tesseract
OCR remains the library default and the baseline for environments without prepared
model assets; it extracts text and simple tables but no pictures. Selecting Docling
never falls back to pdfium, downloads models at runtime, enables remote inference, or
enables external plugins. PostgreSQL, Qdrant, ObjectStore and the existing task
workers remain in use.

## Preparing the model worker (required for the product default)

Build the standalone CPU model image from the repository root:

```powershell
docker build -f deploy/pdf-worker/Dockerfile -t cairn-pdf-worker:2.135.0 .
docker run --rm --network none --entrypoint python cairn-pdf-worker:2.135.0 -c "import importlib.metadata as m,json;print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()},sort_keys=True))" > runtime-packages.json
docker run --rm --network none --entrypoint python cairn-pdf-worker:2.135.0 /opt/ocr_environment.py '["eng","chi_sim","chi_tra"]' > ocr-environment.json
uv run python scripts/prepare_pdf_worker.py --assets D:/models/docling --inventory approved-model-inventory.json --runtime-packages runtime-packages.json --ocr-environment ocr-environment.json --output D:/models/docling-manifest.json
```

The inventory must be an operator-approved record containing `docling=2.135.0`
and the relative path, byte count and SHA256 of every required asset. The retained
evaluation inventory is `data/acceptance-pdf-20261009/docling-assets-inspection.json`;
it is not a generic trust source for arbitrary future downloads. The prepare command
checks path containment and every file hash and records pinned model revisions and
the exact runtime package set in a canonical identity manifest. OCR-enabled workers
require the actual worker Tesseract version, binary SHA256 and selected traineddata
SHA256 values from `ocr-environment.json`. The adapter probes the selected isolated
Python environment at startup; the worker verifies the same resources at conversion.
`CAIRN_INGESTION__OCR_LANGUAGES` and `CAIRN_INGESTION__OCR_PSM` are effective
worker settings. Context language uses the existing Tesseract language aliases,
including English with Chinese. Unsupported languages fail explicitly. OCR disabled
workers do not require traineddata or constrain native text context language. Keep the resulting
manifest and assets read-only. To fetch those reviewed revisions during an explicit
preparation step, use `uv run --with huggingface-hub python scripts/prepare_pdf_worker.py
... --download`. Omit `--download` when reusing already downloaded assets.

The model image uses the standard Heron/TableFormer pipeline with cell matching on.
The pinned Linux dependencies are in `deploy/pdf-worker/requirements.lock` and
`requirements-ocr.txt`. scipy is required by the standard OCR region deduplication
path; import-only or native-only testing does not establish scan support. Package
and model license declarations and bundled notices are recorded in the acceptance
directory; redistribution review remains a separate operator responsibility.

The product Compose file builds the parse worker from `deploy/pdf-worker/Dockerfile.parse`
(the regular product image plus the pinned model environment) and starts it with
`CAIRN_INGESTION__PDF_ENGINE=docling`. Build the regular product image and the model
base image first, then set `CAIRN_PDF_MODELS_DIR` and `CAIRN_PDF_MANIFEST` in `.env` to
the prepared paths; `docker compose up` fails fast with a clear message when either is
unset. The service selects `/usr/local/bin/python` for the model child; the online
process continues using `/opt/venv`. Only `worker-parse` carries the model image;
retrieval and control services do not. `docker-compose.pdf.yml` is superseded and
now empty.

For a separate prepared Python environment, configure:

```text
CAIRN_INGESTION__PDF_ENGINE=docling
CAIRN_INGESTION__DOCLING_PYTHON=<operator-owned Python executable>
CAIRN_INGESTION__DOCLING_ASSETS_PATH=<read-only model directory>
CAIRN_INGESTION__DOCLING_MANIFEST_PATH=<prepared identity manifest>
CAIRN_INGESTION__DOCLING_TIMEOUT_S=600
CAIRN_INGESTION__DOCLING_MEMORY_BYTES=4294967296
```

The shipped CPU profile is one concurrent document, 2 CPUs and 4 GiB hard container memory. Set
`CAIRN_TASKS__CONCURRENCY=1` for a separately configured model parse worker. The model worker
also applies Linux address-space limits. Windows environments were used for
comparison; they are not evidence of Linux inference performance or Windows hard
memory isolation in older evidence. The current Windows gate assigns itself to a
kill-on-close Job before launching the worker; all worker descendants inherit it.
Ownership failure fails startup. Its optional Job memory cap applies to the owned
tree. This mechanism is covered by Windows process lifecycle tests, separately from
Linux inference performance. Conversion must process the entire source and return SUCCESS;
partial success and skipped pages never produce an indexed document.

Actual no-network inference can be reproduced with:

```powershell
uv run python scripts/verify_pdf_worker_offline.py --assets D:/models/docling --manifest D:/models/docling-manifest.json --output data/offline-pdf --ocr complete-source.pdf
```

That command uses a non-root container, `--network none`, 2 CPUs, 4 GiB, a PID
limit, dropped capabilities and read-only models. It retains exit codes, output,
source hash and elapsed time. This is stronger evidence than offline environment
variables alone.

## Persistent model worker

With `CAIRN_INGESTION__DOCLING_PERSISTENT=true` (the default) each parse-worker process
starts one Docling child, which verifies the manifest, loads the models once and then
serves documents over length-prefixed stdin/stdout frames. Measured on the corpus, the
load costs about 5 seconds once; a warm conversion then takes 1 to 5 seconds for a text
page and about 6.5 seconds per page for scans that need OCR. A document that exceeds
`CAIRN_INGESTION__DOCLING_TIMEOUT_S`, returns more than the output budget or crashes the
child is reported as `PDF_TIMED_OUT`, `PDF_TOO_LARGE` or `PDF_ENGINE_UNAVAILABLE`; the
child is killed with its process tree and the next document starts a fresh one. The
child exits when the worker closes its stdin, including on runtime shutdown. OCR runs
with the full configured language set because the converter is built once; set the
variable to `false` to return to one short-lived child per document.

## Scanned PDFs with an embedded text layer

Docling runs OCR on every bitmap page, even when the producer already embedded an
OCR text layer (archive scans, scanner output). With
`CAIRN_INGESTION__DOCLING_SCAN_TEXT_LAYER=true` (the default) the parse worker first
inspects the document with the pdfium baseline, which costs well under a second and
performs no OCR. If at least 90 percent of the content pages are full-page scans
(largest raster covering 80 percent or more of the page) and every one of those pages
carries at least 200 characters of text that is mostly alphanumeric, the baseline parse
is used; such documents take about a second instead of about 6.5 seconds per page. Any
other document, including scans without a text layer and ordinary native PDFs, goes to
Docling. If the baseline still decides a page needs OCR, the document falls through to
Docling as well. The decision and page counts are recorded in
`metadata.pdf_routing` (`rule`, `engine`, `reason`), and the routing rule
`docling+scan-text-layer/1` is part of the parser descriptor, so changing the policy
reparses on the next reindex. Set the variable to `false` to send every PDF to Docling.

## Word and PowerPoint through Docling

With `CAIRN_INGESTION__DOCLING_OFFICE=true` (the default when the engine is Docling) DOCX
and PPTX uploads go through the same warm Docling child as PDFs. Docling's Office backends
are XML parsers, not models, so a document converts in well under a second and the worker
extracts embedded pictures as PNG assets, headings, lists and full table grids. Word
elements carry no page geometry (Word has no pages); PowerPoint elements carry the slide
number and a box converted from EMU to points in the stored bottom-left space. The OOXML
safety preflight that protects the native parsers (zip bombs, external relationships,
encryption) runs first. If Docling fails on a document, the native python-docx or
python-pptx parser produces the result and `metadata.docling_fallback` plus a warning
record the reason; text and tables are kept, pictures are not. XLSX stays on openpyxl so
formula source text is preserved. The worker image needs the `format-docx` and
`format-pptx` extras, so its manifest changes when the image is rebuilt; regenerate it.
Office documents have no page preview.

## Reading order and paragraphs (pdfium baseline)

The pdfium baseline keeps pypdfium2's own reading order. The worker records the
newline markers pdfium emits at line ends, and the layout step never re-sorts words
by coordinates: the earlier heuristic grouped a single column's body text, indented
equations and equation numbers into separate x-bands and scrambled the page. Lines
are kept whole; consecutive lines with the same left margin and normal line spacing
merge into one paragraph, and CJK lines join without inserted spaces. Tables are
still detected from consecutive aligned multi-cell lines and rendered as GFM. OCR
words from Tesseract carry no line markers, so that path groups words into lines by
position and recovers columns from vertical whitespace gutters instead.

The layout version is part of the parser descriptor (`cairn-pdf-layout/4`), so an
ordinary reindex reparses documents indexed with the old layout. Display-math symbols
that pdfium places on their own lines remain separate blocks; formula recognition is
outside the baseline.

## Reindexing and manual edits

Parser descriptors include owned implementation/layout/routing versions, engine
package/model identity, rendering choices, and Tesseract version/language-data
identity. ParseContext language, encoding, JSON fields and source URL have a separate
fingerprint. Undescribed custom parsers continue to work but cannot reuse saved
parse results. Legacy artifacts without IR/fingerprints are readable and miss reuse.

Use the existing knowledge-base rebuild action after changing parser deployment.
Ordinary reindex reparses when those identities change; embedding-only rebuilds can
reuse a valid unchanged IR. The immutable `parse_version` identifies owner-scoped
source evidence and output content, independently of `index_version`. A building
index rejects mixed parser deployments for the same MIME type with
`PARSER_DEPLOYMENT_MISMATCH`.

`REPARSE_MANUAL_EDITS_CONFLICT` means existing chunk edits could not be proven to
match the new element identities, ranges and chunk configuration. The old active
index and edits remain. Reconcile or remove those edits deliberately before retrying;
there is no automatic semantic merge or ordinal-based migration across parses.

## Recovery and safe failures

The baseline caches successful pixel-space OCR text, words and geometry under the
current workspace/KB/document/revision. Image SHA and OCR identity form the key;
layout version is excluded so repaired layout can reuse OCR and rebuild citations.
Invalid checksum/schema/identity/geometry or lowered limits cause a miss. Failed,
empty and cancelled OCR are not cached. Mutation writes retain lease/revision/recovery
and deletion locks, and document/KB purge includes the scoped cache and assets.

Progress records real completed/total pages for pdfium. A heartbeat runs while parsing
waits. Docling reports `converting` with unknown page progress during its long call,
then actual completed pages; its retry unit is the whole document. It does not promise
Docling internal page resume. Both engines run in killable subprocesses; timeout and
cancellation reap the owned child tree even if its original leader has already
exited while descendants keep pipes open. Linux uses a dedicated process group;
Windows uses the Job gate described above. Execution has its configured deadline;
cleanup has a separate fixed 0.5-second upper bound. Repeated cancellation does not
reset either bound. Transport closure can finish draining a dead process tree.
Process supervision failure is `PDF_ENGINE_UNAVAILABLE`.

`PDF_ENGINE_UNAVAILABLE` covers unavailable selected packages/assets/runtime identity.
`PDF_TOO_LARGE` and `PDF_TIMED_OUT` identify resource failures. When returned page
provenance cannot safely partition an element's content (a multipage table without
per-cell page provenance, or a multipage image), indexing and reading keep that
element's full text but attach no page or bounding box to it, so the document still
indexes and the citation for that element reports no page. The strict projection
used by tests and diagnostics still raises `PDF_UNSAFE_PROJECTION` for such elements.
The Docling adapter follows the same policy. It no longer derives character spans
from Docling's page-local charspans, so merged multi-page text is kept once with no
page claim instead of being rejected. `PDF_UNSAFE_PROJECTION` from the adapter now
only means the worker payload itself could not be normalized (missing provenance or
a malformed grid); a bounded run-scoped diagnostic is retained for investigation.

The Docling worker uses Docling's default PDF backend (`docling_parse`). The earlier
explicit `pypdfium2` backend doubled overlapping text runs inside table cells. Table
cell text is additionally de-duplicated token-wise when a cell repeats the same token,
which happens when a source PDF draws a value more than once at the same spot. Cells
whose tokens are all distinct are left untouched, including cells where Docling merged
two different values.

Formula native text is preserved when recognition is absent. Table grids/spans are
stored as emitted, including wrong column counts; GFM is a derived anchored rendering,
not an independently corrected table. Machine extraction is not a quality guarantee.

## Inspecting a parse

DocumentDetail provides active/candidate selection, immutable version and engine,
warnings, ordered elements, table spans, formula native/status, actual page preview
and citation overlay. Text is rendered with Vue escaping. Assets use authorized
IDs and checksum validation, never caller-provided URLs or object paths.

The overview is `GET /v1/documents/{id}/parse?view=active|candidate`. Elements,
element details, assets and page preview requests require both `view` and
`parse_version`. Cursors bind the view/version/page. `kb:read` is checked before
version lookup; query-only keys cannot inspect evidence. `PARSE_VERSION_STALE`
returns 404 for a version that no longer resolves. ACTIVE comes from the current
revision's indexed run for the active index; candidate comes from its building run.
Source replacement never combines a new revision's parse with old active citations.

Preview defaults are 4,000,000 pixels/page, 8 MiB/image, 64 MiB/document, 100 pages,
30 seconds, concurrency 1, and 8 MiB/HTTP response. The corresponding settings are
`preview_max_page_pixels`, `preview_max_image_bytes`, `preview_max_document_bytes`,
`preview_max_pages`, `preview_timeout_s`, `preview_max_concurrency`, and
`preview_response_max_bytes` under `CAIRN_INGESTION__`. The total preview deadline includes SQL lookup, admission queue, object resolution,
source read, source hash verification and rendering. Lightweight authorization and
SQL source identity precede admission; large source reads occur only after a slot
is acquired. Preview uses the verified SQL run/source locator without decoding all
IR artifacts. Cancellation and failure release the slot. Above-budget previews are
explicitly unavailable; parse completeness is not reduced to fit preview budgets.

## Evaluation status

Use `scripts/evaluate_pdf_stage.py` for complete-source inventory, parser outcomes,
latency and sampled parent-plus-child RSS. Missing gold scores remain null. Source
PDF hashes, permission/completeness statements and version differences are recorded
separately. Cell structure scoring counts exact row/column spans with multiplicity;
cell text fidelity, detection coverage, reading order, OCR and geometry need separate
reference annotations. Agent-reviewed visual anchors are not expert human gold.

The original gate remains 30 complete documents (six in each of five categories),
independent annotations, table structure F1 >= 0.85, and the original 100-page/OCR/RSS
performance requirements. Existing files and successful conversions do not close
that gate. Current stage evidence and exact outstanding gaps are reported in
`data/acceptance-pdf-rework-01/DELIVERY.md`.


## Identity migration and safe projection

New IRs retain envelope v2 and carry `identity_version=2`. The digest includes
producer/schema, source owner, page geometry, provenance, warnings, quality status,
structured content, references and semantic metadata. Only explicit runtime noise
(PID, elapsed time, temporary path and cache hit/miss counts) is excluded. Element
IDs derive from parse version plus unique local ID and are verified during loading.
Writing validates a single detached snapshot, so nested metadata mutation cannot
bypass verification.

An existing v2 IR without `identity_version` is routed only to legacy algorithm 1.
It must pass that algorithm and SQL owner/source checks; inspection marks it legacy.
It is readable but never reused for a new rebuild. No failed version-2 validation
falls back to legacy. Reindex reparses into a new immutable artifact/version while
retaining the old ACTIVE version until successful activation. Old artifacts and
SQL parse-version rows are never rewritten in place. Existing v1 text formats
remain readable. Manual edits require unique matching source citations and role;
legacy or changed identities cause `REPARSE_MANUAL_EDITS_CONFLICT`, and a concurrent
edit changing after proof prevents commit of a stale snapshot.

Indexing and legacy `/content` now use one core projection. `/content` reads the
current revision's committed parse, even before index activation, and verifies its
SQL run/source/key identity. The newer `/parse` interface still requires its exact
ACTIVE or candidate run and never falls back. Multipage text requires exact spans
against its declared orig/text source. A multipage table requires single-page cell
provenance and contiguous row fragments; its complete IR remains intact while each
anchored fragment appears once. Multipage images and ambiguous table evidence are
kept as unplaced text (no page or bounding box) rather than rejected; only the strict
projection raises `PDF_UNSAFE_PROJECTION`. Legacy GFM tables are promoted only when an
escape-aware parse roundtrips exactly; otherwise the original text remains intact.

Finite, ordered source boxes may extend beyond the page. Stored source boxes remain
unchanged; the UI clips only the displayed overlay to the crop/page intersection.
An entirely outside box has no overlay. This preserves source evidence without
invalid display rectangles. Fresh output and reusable IR both validate applicable
page characters/words/objects/rasters, rendering pixels/bytes, page dimensions,
elements/table grid, assets and output bytes. Missing resource facts cause a cache
miss. Native pages with no raster processing record zero rendering pixels, so a
rendering budget is not incorrectly applied to their geometric page area.

## Independent annotation input and scoring

Run `uv run --with psutil==7.2.2 python scripts/evaluate_pdf_stage.py --engine pdfium --output <dir>
--corpus-manifest <manifest.json> --annotations <gold.json>`; select `docling` with
`--python`, `--assets` and `--manifest` for its isolated worker. The annotation input
is schema 1 and binds every document to its SHA256, annotation version, source,
method and covered pages. Each page records PDF-point bottom-left dimensions and
optional `tables`, `ocr_text`, `order`, and `geometry` fields. Missing fields mean
unannotated; explicit empty lists mean a reviewed empty scope. See the retained
synthetic CLI fixture in `data/acceptance-pdf-rework-01/gold-cli` for exact JSON.
The RSS sampler uses psutil; keep evaluation tooling isolated from the model
package set, or use the exact worker version (7.2.2 for the retained Linux manifest),
since the child inherits PYTHONPATH and verifies every prepared runtime package.
Synthetic and agent annotations are diagnostics, not independent human gold.

Matching is frozen as `region-iou-greedy/1`, same-page IoU >= 0.5, descending IoU,
then stable reference ID/prediction order, with one-to-one use. Table detection,
exact cell-position/span multiset F1 and exact cell text accuracy are separate.
CER uses Unicode Levenshtein with only CRLF normalization; empty references report
insertions with undefined CER. Reference order pairs include missing regions as
failures. Main geometry mean counts missing regions as IoU zero; conditional
matched mean is separate. Failed documents retain their annotated denominators.
Counts-based micro, document macro, declared-gate and independent-human coverage
are reported. Docling currently lacks authoritative raw page OCR transcription;
it explicitly records unsupported evidence instead of joining cells into invented
line breaks. Baseline Tesseract retains its raw page transcription. Gold-dependent
unsupported/failed OCR produces coverage failure and an empty-prediction penalty.
