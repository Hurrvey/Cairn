# Docling quality comparison — October9

Experiment only; no production parser switch or dependency change. Isolated environment
`data/pdf-docling-spike` uses docling2.135.0 (installed via PyPI), Python3.12, CPU2 threads.
Selected standard layout/TableFormer assets are prefetched under data/models/docling-2.135.0.
HF_HUB_OFFLINE and TRANSFORMERS_OFFLINE set during conversion; remote services and external
plugins disabled. This is offline configuration evidence, not yet an OS-level no-network test.

Downloaded revision metadata:
- layout heron8f39ad3c0b4c58e9c2d2c84a38465abf757272d8; model card declares Apache2.0.
- layout heron ONNX40bde044036bb181c130ddf6c51792187268748f; same model-card license.
- docling-models v2.3.0 resolves fc0f2d45e2218ea24bce5045f58a389aed16dc23; card declares
  CDLA-Permissive2.0. Review actual selected distribution notices before shipping; these are
  inspected declarations, not a blanket clearance of every transitive dependency/model.

## Native-text comparison (OCR disabled intentionally)

Both inputs are existing one-page excerpts, not complete originals. ConversionStatus.SUCCESS
does not itself establish correctness.

| Input | Baseline tables | Docling tables | Measured conversion time | Findings |
| --- | --- | --- | --- | --- |
| financial_table_heavy-01 |0|2|54.593s cold initialization included|Row amounts stay associated, but first table has5 columns instead of4, repeated label column/header defect|
| multicolumn-01 |1|0|2.422s warm|No false table; Markdown emits formula-not-decoded while JSON orig preserves five formulas|
| financial_table_heavy-05 |0|1|10.516s new-process first conversion|Four columns recovered; one number cell contains7548 and7549 although visual reference shows7549, so numeric fidelity is not accepted|
| financial_table_heavy-06 |0|6|11.453s warm|Six visible tables detected; inspected final income-tax row132,902/103,552/104,648 matches render, full-cell scoring remains open|
| cjk-06 complete24-page original |5|10|62.157s first conversion|All24 pages reported with SUCCESS; full text/table/citation gold remains unscored|

Outputs/logs: `data/acceptance-pdf-20261009/docling-*.json`, `.md`, `docling-spike.log`.
Visual source and independently transcribed value anchors are in the neighboring visual baseline.
Do not claim general table F1, OCR quality,100-page performance or peak memory from these4 excerpts
and one24-page complete original. Different table counts alone do not establish quality.

Changing do_cell_matching=False did not fix first-table structure (still11rows×5cols) and made
cell text worse: labels absorbed full rows of figures while numeric cells became empty. Reject
that setting for this comparison. Keep the default as the current candidate, not yet a default
production decision. Preserve JSON orig for undecoded formulas with explicit recognition status;
do not replace source text with an empty placeholder or call native formula text correct LaTeX.

Official references inspected: https://docling-project.github.io/docling/usage/advanced_options/
and https://pypi.org/pypi/docling/json. Installed API/model source was inspected directly.

Minimal runtime experiment: docling-slim[format-pdf,models-local]==2.135.0 alone missed rtree on
import; after adding rtree1.4.1, actual TableFormer construction missed cv2. Adding
opencv-python-headless4.14.0.94 produced a successful offline native financial05 conversion
(1 page,1 table). Environment data/pdf-docling-minimal; log docling-minimal-conversion.log.
This candidate avoids unrelated document-format packages but still needs full pinned dependency
and Linux CPU packaging verification. Do not substitute import-only checks for actual inference.

Optional TableFormerV2 check was bounded by reviewer instruction. The model repository metadata
at commit51559fad3946873e26a6f9b8e912f948e8745bef lists a209,735,468-byte weight file but no
README/LICENSE and no card license field (`https://huggingface.co/api/models/docling-project/TableFormerV2?blobs=true`).
Therefore no V2 download/integration in this increment; use the inspected standard candidate and
keep its structure defect visible. This is an unresolved artifact-licensing observation, not a
conclusion about the project's legal permissions or the code package's separate license.
