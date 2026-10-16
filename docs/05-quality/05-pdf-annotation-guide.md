# PDF annotation guide (independent human gold)

**Document:** `05-quality/05-pdf-annotation-guide.md` · **Date:** 2026-10-12 · **Status:** Normative

The PDF quality gate (table structure F1 >= 0.85 and the companion OCR, reading-order
and geometry metrics) stays OPEN until the 30-document corpus has **independent human**
annotations. Machine output, agent transcriptions and publisher XML are reference
material, never gold. This guide fixes the file format and the rules so that whoever
annotates produces something the frozen scorer (`scripts/pdf_gold_metrics.py`,
protocol `region-iou-greedy/1`) can consume unchanged.

## 1. Start from the kit

```powershell
uv run python scripts/prepare_pdf_annotations.py `
  --corpus-manifest data/acceptance-pdf-rework-01/corpus-final.json `
  --corpus-root data/acceptance-pdf-20260921/corpus `
  --output data/annotations
```

This writes two files under `data/annotations/` (local, not committed):

- `annotations.skeleton.json` — a valid annotation file that covers nothing. Every
  document is listed with `method: "pending"` and no pages, so the evaluator accepts the
  file and counts zero gold.
- `page-templates.json` — every page of every document with its size in PDF points and
  empty `tables`, `order` and `geometry` lists. Copy a page template into the document's
  `pages` array when you annotate that page. Each entry also carries `resolved_path`, the
  absolute path of the source PDF as found on this machine (the manifest references some
  documents by a corpus-relative `local_pdf` and others by an absolute `path`).

A local helper covers the mechanical parts (it never annotates for you):

```powershell
uv run python scripts/pdf_annotation_helper.py render cjk-06 --pages 1,3-5
uv run python scripts/pdf_annotation_helper.py render financial_table_heavy-01 --pages 2 --crop 60 300 560 620
uv run python scripts/pdf_annotation_helper.py scaffold cjk-06 --pages 1-3
uv run python scripts/pdf_annotation_helper.py status
```

`render` writes PNGs into `data/annotations/renders/` with a grid every 50 PDF points
(labels every 100) in the required bottom-left PDF-point space, so boxes can be read off
the image; `--crop x0 y0 x1 y1` zooms into a region, in PDF points, and the crop goes to a
distinct file name. `scaffold` copies the selected pages' templates into the working file
(never overwriting pages already there) and marks the document `independent_human`; run it
only for pages you are about to examine. `status` validates the working file and prints
coverage per document.

## 2. File format

```json
{
  "schema_version": 1,
  "documents": [
    {
      "source_sha256": "<sha256 of the exact PDF bytes>",
      "annotation_version": "2026-10-20-r1",
      "method": "independent_human",
      "covered_pages": [1, 2],
      "pages": [
        {
          "page": 1,
          "width": 612.0,
          "height": 792.0,
          "tables": [
            {
              "id": "t1",
              "bbox": [72.0, 400.5, 540.0, 700.0],
              "rows": 3,
              "cols": 4,
              "cells": [
                {"row_start": 0, "row_end": 1, "col_start": 0, "col_end": 1, "text": "Metric"},
                {"row_start": 0, "row_end": 1, "col_start": 1, "col_end": 4, "text": "Fiscal year"}
              ]
            }
          ],
          "ocr_text": "exact transcription of the page",
          "order": [{"id": "o1", "bbox": [72, 650, 540, 700]}, {"id": "o2", "bbox": [72, 500, 300, 640]}],
          "geometry": [{"id": "g1", "bbox": [72, 650, 540, 700]}],
          "region": [0, 0, 612, 792]
        }
      ]
    }
  ]
}
```

Rules the validator enforces:

- `source_sha256` must match a document in the corpus manifest; one entry per document.
- `annotation_version` and `method` are required. Only `method: "independent_human"`
  counts toward the gate. Keep `"pending"` for documents you have not touched.
- `covered_pages` lists exactly the pages present in `pages`, each between 1 and the
  document's page count. **A page you examined and found empty is covered with empty
  lists; a page you did not examine is absent.** The scorer treats these differently.
- Every page needs positive `width` and `height` (take them from the templates).
- Every reference needs a unique `id` per page and a finite `bbox` with `x0 < x1` and
  `y0 < y1`.
- Table cells use 0-based, end-exclusive spans inside `rows` x `cols`.
- `ocr_text`, `order`, `geometry` and `region` are optional per page; include only the
  modalities you actually annotated.

## 3. Coordinates

All boxes are in **PDF points with the origin at the bottom-left corner of the page**,
the same `pdf_points_bottom_left` space the parser stores in element locations. Use
the page's own crop box (the template width/height), before any viewer rotation. For a
page whose `rotation` in the template is 90, 180 or 270, still annotate in the
unrotated PDF coordinate space of the stored page, not in what a viewer shows.

## 4. What each modality means

| Modality | Reference | How it is scored |
| --- | --- | --- |
| `tables` | Each table with its box, grid size and every cell's span and text | Tables are matched to predictions by IoU >= 0.5 (greedy, highest overlap first). Structure F1 counts exact cell spans; cell text accuracy is separate. Unmatched reference tables count as misses, unmatched predicted tables as false positives. |
| `ocr_text` | The exact transcription of the page in reading order | Character error rate (Levenshtein) against the engine's page transcription; `\r\n` is normalised to `\n`. Do not strip punctuation or CJK characters to flatter the score. |
| `order` | Regions in the correct reading order | Pairwise order agreement between matched regions. |
| `geometry` | Regions whose boxes must be reproduced | Mean IoU and match rate of citation geometry. |
| `region` | Optional sub-area of the page | Limits scoring to predictions overlapping that area. |

Reference `id` values are yours; they must not be copied from a parser run, because
the point of independent gold is that it does not depend on this round's output.

## 5. Running the scorer

```powershell
uv run python scripts/evaluate_pdf_stage.py --engine docling `
  --python /path/to/docling/python --assets data/models/docling-2.135.0 `
  --manifest data/models/docling-manifest.json `
  --corpus-manifest data/acceptance-pdf-rework-01/corpus-final.json `
  --annotations data/annotations/annotations.gold.json `
  --output data/acceptance-pdf-gold-<date>
```

The report keeps failed parses and un-annotated documents in the denominators and
states coverage per modality. The gate closes only when `independent_human`
coverage reaches the full corpus and the structure F1 threshold is met across
categories, not on aggregate alone.

## 6. Known engine limitations to annotate honestly

- Docling occasionally merges two distinct values into one cell (for example
  `7,549 7,522`); annotate the true cells and let the scorer count the miss.
- Some source PDFs draw a value twice at the same spot; the parser de-duplicates
  repeated tokens inside a cell, so annotate the value once.
- Display mathematics is split across lines by every engine; annotate reading order
  at the block level, not symbol by symbol.
