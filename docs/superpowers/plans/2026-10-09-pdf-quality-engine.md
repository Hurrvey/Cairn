# PDF quality engine and parse reuse plan

Same fixed team. Status: reviewed direction; implement after A correctness fixes. This is still
the user's PDF task, not a return to SDK/quota work. Source of decisions: October9 original advisor.

## B1 — configurable isolated standard Docling adapter

Candidate spike artifacts: `docs/05-quality/2026-10-09-docling-spike.md` and ignored data outputs.
Docling2.135.0 and model revisions are recorded there; root prepared an isolated Windows env for
comparison. Do not silently add the entire candidate dependency tree to the API environment.

- [ ] Implement ingestion/docling_pdf.py and a private worker entrypoint using the existing
  bounded child-process contract. Runtime command/path are operator-owned, never document input.
- [ ] Select engine pdfium|docling via ingestion runtime settings; retain baseline explicitly.
  Missing selected engine/assets fails clearly. No fallback or runtime weight downloads.
- [ ] Standard layout/table pipeline only; remote services and plugins disabled; configured
  local prefetched assets, actual offline execution test. No VLM/formula-enrichment dependency
  in this increment. Give model worker measured memory/time budgets separate from pdfium.
- [ ] Normalize ordered Docling items to existing ParsedDocument/Block, page bottom-left PDF
  coordinates. Preserve source URLs and correct page-local provenance for multi-page items.
  Validate nonfinite/inverted/out-of-page geometry, source/output bounds and missing provenance.
- [ ] Emit table GFM anchored once per spanning cell; keep structured source rows/columns/spans
  as metadata/artifact. Do not collapse the observed erroneous5-column schema into a fabricated
  correct4-column table. Renderer dedup is not structure accuracy.
- [ ] Formula text empty with nonempty orig retains that native text and explicit unrecognized
  formula status; never replace source content with formula-not-decoded alone or claim LaTeX.
- [ ] Tests: missing package/model, bounds, cancellation/reap, geometry/page ordering, tables
  and original-formula preservation; real financial/multicolumn samples and scan/CJK tests.

Default production engine decision remains gated by broader measured quality and resource
results. One successful sample does not establish tableF1>=0.85 or the30-original quality gate.
do_cell_matching=False was worse and is rejected for this spike; no parameter-search loop.

## B2 — parser identity makes repaired output take effect

Files: registry.py, artifacts.py, pipeline.py, runtime_config.py and tests. Add a small immutable
registry descriptor API. Canonical hash includes schema/parser/implementation/layout identity,
engine package/model manifest digest, OCR engine+traineddata/languages/PSM and output-affecting
render/routing/table options. Exclude PID/installation path/timing/concurrency. Context hash
includes language/encoding/json_fields/source URL. Descriptor is verified deployment data;
no control-plane torch import or online metadata lookup.

Compatibility resolution: retain Parser protocol parse/supported_mimes. Registration accepts
optional descriptor metadata; describe(mime) returns descriptor or None. Undescribed custom
parsers remain usable but never reuse parse artifacts. Never treat None==None as a cache hit or
class/module name as a version. Builtins register explicit owned implementation versions; PDF
adds its full engine/model/layout/OCR descriptor. No deployment manifest for selected Docling
means explicit unavailable. The prepare operation verifies assets and runtime reads that record.

- [ ] ParseManifest adds optional parser/context fingerprints; old manifests decode but miss
  reuse. New attempts capture descriptor once and require matching actual worker identity.
- [ ] _reuse_parsed checks source identity plus both fingerprints plus current bounds/structure.
  Missing/mismatched fingerprint reparses; normal reindex must actually use the repaired parser.
- [ ] Test same-parser hit, legacy miss, changed layout/model/OCR/context misses, lower runtime
  limits not bypassed by cached output. Preserve source revision and pipeline fencing.
- [ ] One rebuild uses one parser deployment; no claim of identical parse configuration across
  mixed-version workers. Do not create a new global index configuration framework for this.

## C — page OCR reuse and recovery (following concrete lifecycle review)

Store scoped OcrResult pixel text/words/geometry rather than final PDF blocks. Cache key scopes
workspace/KB/document/revision/imageSHA/OCRfingerprint, excluding downstream layout version.
Bounded checksum envelope includes identity/schema/engine params and validates counts/geometry.
Corruption/old format/read failure is a miss; never cache failures/cancellation/empty fake success.

Inject a narrow get/put cache protocol bound to current ingestion run; parser receives no arbitrary
object-key access. Existing lease/revision/recovery mutation guard precedes writes so stale workers
cannot recreate objects after purge. Cleanup must include this scoped prefix. Cache has no mutable
latest pointer and does not replace parse-stage commit authority. Prove interrupted long scans
reuse completed pages, corrupt cache reruns, changed OCR params miss, new layout reuses OCR but
rebuilds blocks, rotation/crop citations remain correct. Queue/progress/heartbeat work requires
review of existing task facade; do not declare page cache alone a complete OCR queue implementation.

Do not deep-fork Docling to intercept unstable internal OCR callbacks. Decide its page-recovery
capability separately and document it; never run both engines' OCR redundantly on the same page.
