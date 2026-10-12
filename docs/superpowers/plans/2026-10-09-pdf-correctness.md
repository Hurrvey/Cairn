# PDF correctness implementation plan

Fixed ownership: root designs/accepts, original developer implements using the existing
executing-plans/TDD workflow, original design_advisor reviews. Reuse agents; no replacements.
Design: ../specs/2026-10-09-pdf-completion-design.md. No quota or SDK expansion.

## A1 — image-dominated mixed-page routing

Files: ingestion/pdf_worker.py, pdf.py; tests/unit/ingestion/test_pdf*.py.

- [ ] Run current PDF/OCR/process baseline and record exact failures/counts.
- [ ] Add a real PDF with a large scan image and >8 native header characters; demonstrate lost
  body and absent OCR before the fix. Negative cases: ordinary text+logo, full OCR text layer,
  blank pages, rotation/crop and overlapping raster rectangles (union, not naive area sum).
- [ ] Extend bounded worker extraction with image geometry and native coverage facts. Design
  explicit classification and thresholds; no blanket OCR for every image. Validate protocol.
- [ ] Fix routing, preserving source content without duplicate native/OCR copies. Test missing
  OCR and OCR failure visibly, not silently dropping required page content.
- [ ] Run focused tests and send the same reviewer a bounded diff/evidence.

Reviewer A1 blocker reproduced on actual PDFium: a unit image inside a Form drawn at page scale
has local child bounds0..1 although its visible page area is large. Accumulate ancestor Form
matrices before clipping image geometry to the page crop; do not double-apply the image's own
transform already included in local bounds. Real nested-Form/crop/rotation regression required.

## A2 — OCR block geometry and reading order

Files: ingestion/pdf.py, pdf_worker.py, pdf_layout.py; corresponding unit tests.

- [ ] Red tests for scan two-column output and simple OCR table, precise page/rotation/crop boxes,
  and spanned header/footer ordering. Preserve words and do not forge table structure.
- [ ] Route positioned OCR words through a bounded child layout action, preserving limits,
  cancellation and sanitized errors. Avoid parent-process CPU loops for layout.
  Reviewer corrected the original transform order: layout in rendered visual-frame points with
  bottom-left origin first; after ordering map final block boxes through existing inverse render
  rotation/crop function to original PDF points. For90/270 swap display width/height. Do not sort
  mapped blocks again. Test multiple words/lines on rotated pages, not just one union bbox.
- [ ] Segment full-width bands before per-band column ordering. Test narrow/short headings,
  ordinary indents and tables among prose so local fixes do not scramble other layouts.
- [ ] Run PDF/layout/OCR/process regression and inspect actual rendered examples.

OCR text remains authoritative. Use word layout only when nonempty words, concatenated in engine
order with whitespace removed, exactly equal text with whitespace removed. Do not normalize case,
Unicode compatibility forms, sort words or compare sets. Valid text with incomplete/empty words
retains whole text in a paragraph and explicit ocr_layout_unavailable_pages/reason; validated
word union bbox or page bbox remains. Invalid/nonfinite/out-of-range boxes still fail. Positive
fixtures must have internally consistent OCR words; preserve an inconsistent fixture proving
fallback retains all text rather than weakening existing content assertions.

A2 reviewer reproduced a resource regression: max_page_chars5 with matched10-character OCR text/
words succeeds on the new layout path. Common result validation must run before fingerprint-like
text joining or either branch: nonblank text/char limit, strict positive integer dimensions and
pixel bound, bounded word count/cumulative text, finite ordered in-image boxes. Worker return
blocks need bounded count/text and ordered display-page-contained boxes before citation mapping.
Small floating tolerance is acceptable; genuine inversion/out-of-page coordinates are not.

## A3 — wide table evidence and quality handoff

- [ ] Red tests for wide financial-style aligned tables with explicit supported evidence and
  negative two-column prose/formula cases. Do not simply delete width guard.
- [ ] Implement minimal evidence-driven support and document limits for ambiguous/merged cells.
- [ ] Re-evaluate retained real samples; compare text loss, order, table output and page geometry.
  Keep source PDFs and annotated expectations separate from parser output.
- [ ] Reviewer gate, then real OCR/container and current-Qdrant ingestion acceptance.

## Following PDF increments

After A, same developer implements reviewed Docling comparison/adapter and durable page OCR
checkpoint/retry work in separate concrete plans. Coordinator and reviewer resolve exact seams
from measured results; do not prematurely declare the30-complete-document quality gate passed.
