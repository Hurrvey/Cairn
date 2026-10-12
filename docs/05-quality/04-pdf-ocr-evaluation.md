# PDF/OCR baseline evaluation, September 21

Status: runnable baseline verified; original quality gate remains OPEN.

## Frozen baseline

- Python PDF library: pypdfium2 5.13.0, pinned in uv.lock.
- OCR: Debian Bookworm Tesseract 5.3.0, English/simplified/traditional Chinese
  language packs; Pillow 12.3.0. CPU execution, no network, container 2 CPU / 2 GiB.
- All native PDF extraction, rendering and layout execute in killable subprocesses.
  PDF child address-space ceiling is 1 GiB on Linux; request timeout 120 seconds.
- Original inputs, manifest, license/source metadata and checksums are retained at
  `data/acceptance-pdf-20260921/corpus/manifest.json` (ignored local evidence).
- Reproducible runner: `scripts/evaluate_pdf_corpus.py MANIFEST --output DIRECTORY`.

## Observed results

The 30 public PDF artifacts contain 149 pages and all parsed in the first bounded
offline run. All returned the manifest page count and nonempty source/page/box
provenance. Sequential wall time was 25.97 seconds including process startup.
This is a robustness run, not the 100-page-document/core throughput benchmark.

| Category | Artifacts | Parsed | Caveat |
|---|---:|---:|---|
| Academic | 6 | 6 | DocLayNet original-page excerpts |
| CJK | 6 | 6 | Five excerpts and one 24-page government document |
| Financial/table-heavy | 6 | 6 | Table regions are annotated; cell structure is not |
| Multiple columns | 6 | 6 | Source layout-region labels do not establish reading order |
| Scanned originals | 6 | 6 | GovInfo original scans already contain machine text layers |

To exercise actual OCR on historical scan content, the first page of each of the
six GovInfo originals was rendered at 144 DPI and saved as an image-only PDF.
All six derivatives routed to OCR and emitted cited text, taking 2.30–2.78 seconds
each. These are transformations of real source pages, explicitly not six extra
original documents. Their machine text is not an independent gold transcription.
Separate English and Chinese synthetic image/PDF regressions also passed; those
are not counted in the public corpus.

## Unclosed quality requirements

1. The corpus has 7 complete original documents and 23 real single-page excerpts;
   it is not 30 complete original documents in five balanced categories.
2. Twenty-three samples have independent human layout-region labels, but no
   independent table-cell structure, reading-order sequence or gold OCR text.
   Table-structure F1 and reading-order/OCR accuracy must remain unreported.
3. The baseline detects some aligned simple tables, but several financial pages
   containing tables yielded no table blocks. Successful text extraction does
   not meet the required table F1 >= 0.85. Merged cells, irregular layouts and
   scanned table reconstruction require a stronger layout/table engine comparison.
4. No peak-memory benchmark or 120 typical 100-page documents/hour/core acceptance
   has been completed. The subprocess ceiling is protection, not a measurement.
5. Tesseract is the portable initial adapter, a documented deviation from the
   original RapidOCR/PaddleOCR proposal. CJK/table quality has not established
   equivalence. The replaceable OCR protocol preserves a future comparison seam.

The next quality work is to complete independently annotated full-document data,
compare Docling or another reviewed layout engine, and rerun actual quality metrics.
Do not close FR-F-02 or the original full M07/E2E gate based on the parser smoke run.

## Primary sources

- PDFium process/thread guidance: https://pypdfium2.readthedocs.io/en/stable/python_api.html
- OCR protocol: https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html
- DocLayNet release and data terms: https://github.com/DS4SD/DocLayNet
- DocLayNet license: https://github.com/DS4SD/DocLayNet/blob/main/LICENSE
- Government CJK source: https://data.gov.tw/dataset/178095
- Historical scan source: https://www.govinfo.gov/content/pkg/USREPORTS-89/pdf/USREPORTS-89-395.pdf
