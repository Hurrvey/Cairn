# PDF parsing completion — priority reset

User October9 explicitly prioritizes PDF completion over SDK/quota work. Root plans and verifies;
the original design_advisor reviews; the original developer (gpt-5.6-sol/high) alone writes code
and tests. Existing quota ORM draft is preserved without further quota development. Existing
September21 PDF scope and public-corpus authorization remain applicable; reviewer approved A below.

## Verified problem and decision

The parser processes all pages within its configured bounds. Its quality is not complete:
native text count>=8 suppresses OCR even when a scanned body is accompanied by a short native
header; OCR words become one page paragraph; whole-page x clustering misorders spanning
footers before the right column; table detection refuses cell spans wider than40% of a page.
Coordinator independently reproduced the last two cases. Visual inspection of existing public
financial_table_heavy-01 shows two full-width financial tables while baseline emits0 tables and
separates numeric columns from labels. Public multicolumn-01 includes formulas/figure/text,
so treating every aligned multi-column region as a table is also wrong.

Three options considered: only raise OCR threshold/remove table-width guard (insufficient and
regressive); fix bounded baseline defects then measure a model-backed quality parser (chosen);
replace the entire engine immediately (insufficient evidence and avoidable deployment risk).

## A — bounded correctness fixes, approved for implementation

- Use bounded raster geometry/coverage and native text distribution to identify image-dominated
  pages. Do not trigger solely on any image or a larger character threshold. Preserve normal
  native-text pages containing small decorative images and complete existing OCR text layers.
- Scan-page OCR word boxes enter a bounded, killable layout worker in rendered visual-frame
  points (swap page width/height for90/270 rotation), bottom-left origin. Only final block boxes
  convert through rotation/crop back to original PDF-point citation coordinates. Never reorder
  those blocks again by original-PDF x/y: rotated pages would scramble visual reading order.
  Preserve per-block page/source/geometry/order,
  avoid concatenating native and OCR copies of the same text, and retain existing timeout/cancel
  cleanup. No expensive layout loop in the parent event loop.
- Partition native reading order by spanning horizontal bands before ordering local columns.
  Headings/footers/spanning paragraphs belong at their vertical position, not in a global left
  column that precedes all right-column content.
- Accept wide tables only with independent reliable row/column/rule evidence. Removing the
 40% rule alone is rejected; ordinary two-column prose and formulas are negative fixtures.
- Verify resource counts/bytes/pages and geometry validation on the expanded worker protocol.

## B — real quality-engine comparison, part of this PDF task

Evaluate isolated, version-pinned Docling on real financial/CJK/multi-column/scanned samples.
Use local prefetched model assets, no implicit remote inference or runtime model download;
preserve source normalization and citation geometry, bounded execution and safe failures.
Only choose a default/optional quality path after inspecting actual output and resources.
Missing models/unsupported configuration must be explicit, never a hidden lower-quality fallback.
Check selected package/model/dependency notices before including them in a deployable artifact.

Official references rechecked October9: https://pypdfium2.readthedocs.io/en/stable/python_api.html
and https://docling-project.github.io/docling/usage/advanced_options/. Verify actual pinned APIs
locally; current website examples alone do not prove installed-version compatibility.

## Completion gates beyond A

Original page OCR cache/queue/heartbeat/retry behavior and corpus quality gate remain PDF work,
not unrelated follow-ups. Retained corpus30 is7 originals+23 excerpts, no independent cell/order
gold; earlier parsed30 means robustness only. Prepare genuine complete-document inputs and
independent rendered-page annotations, reporting exact provenance and coverage. Parser output
cannot be copied as expected truth. Preserve original table structure F1>=0.85 goal; report
quality/throughput/memory findings explicitly instead of replacing gates with nonempty text.

End-to-end acceptance includes upload→parse/layout→chunk→index→retrieval citation in the current
Qdrant product, not obsolete pgvector instructions. Only one product Compose stack; destructive
integration fixtures run only against the designated disposable services. No commits/publication
or company deployment implicit in this task. A passing A patch alone is not full PDF completion.
