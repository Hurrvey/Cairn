# T-M07-01 — PDF parser evaluation checkpoint

**Date:** 2026-09-07 · **Status:** licence reconnaissance complete; quality gate OPEN.

This resumes Claude session `9d9ddebd-3ba8-4607-8976-b90680a04ffb`, whose last
completed increment was the Phase 2b catalog. Git initialization and the directory
rename to `D:\code\Cairn` have already happened. No repository, package name, or
domain has been registered by this continuation.

## 1. Version-specific licence findings

The earlier specifications classified MinerU as AGPL without pinning a version.
That is no longer an accurate description of its current upstream branch.
These are engineering findings, not legal clearance for distribution.

| Candidate | Upstream snapshot inspected | Finding | Current disposition |
| --- | --- | --- | --- |
| Docling | `025b27ca2c2f040b5b9628dccffd2611246990ca` | Top-level MIT; optional PDF engines, model weights and transitive dependencies still need separate review | Preferred evaluation candidate, NOT a validated production parser |
| pypdfium2 | `7d6fd423eba0a363d6d49286d0ca8edabaf417aa` | Library Apache-2.0/BSD-3-Clause; PDFium binaries carry additional third-party notices | Baseline evaluation candidate; check the actual wheel's bundled licences |
| MinerU | `4fe4bde114a23ee5dd637eae99b767f4669bf58c` | Apache-2.0 **with additional commercial-threshold and attribution terms**, not plain Apache-2.0 | Excluded from the application image under the existing commercial-restricted policy; requires explicit review |
| PyMuPDF | No new release audited | Existing AGPL/commercial concern remains unresolved | Do not introduce into the application image |

Primary sources retrieved on 2026-09-07:

- [Docling licence](https://github.com/docling-project/docling/blob/025b27ca2c2f040b5b9628dccffd2611246990ca/LICENSE)
- [Docling dependency declarations](https://github.com/docling-project/docling/blob/025b27ca2c2f040b5b9628dccffd2611246990ca/pyproject.toml)
- [pypdfium2 licensing and threading notes](https://github.com/pypdfium2-team/pypdfium2/blob/7d6fd423eba0a363d6d49286d0ca8edabaf417aa/README.md)
- [MinerU licence and additional terms](https://github.com/opendatalab/MinerU/blob/4fe4bde114a23ee5dd637eae99b767f4669bf58c/LICENSE.md)

MinerU's inspected terms require a separate commercial licence if consolidated
MAU exceeds 100 million or consolidated monthly revenue exceeds USD 20 million,
and require attribution for online services. An HTTP sidecar is an architectural
boundary, **not** an automatic exemption from any licence obligation.

PDFium is explicitly not thread-safe. A future PDF adapter must use isolated
processes or serialize all PDFium access; it must not copy the text parsers'
thread-offloading strategy blindly.

## 2. Quality gate — not yet run

No 30-document annotated PDF corpus exists in this checkout. Consequently there
are **no measured table F1, reading-order, OCR or throughput results**, and no
claim that any candidate meets FR-F-02. Synthetic text tests are not a substitute.

Before closing T-M07-01:

1. Freeze 30 redistributable PDFs: six each of academic, financial/table-heavy,
   scanned, CJK and multi-column documents. Record source URL, SHA-256 and licence.
2. Annotate expected table cell structure, reading order and page citations;
   keep annotations separate from any candidate's output.
3. Pin each candidate, model weights, CPU/GPU configuration and dependency tree.
   Review all bundled licences before installation into a distributable image.
4. Run candidates in isolated, resource-limited processes with network disabled
   after explicitly preparing model assets. Record cold/warm time, peak memory,
   pages/second, extraction failures, table F1 and reading-order correctness.
5. Apply the existing table F1 threshold of 0.85 and report failures by document
   category, not just aggregate averages. Record a version-pinned decision.

## 3. Work delivered without prematurely choosing a PDF engine

The engine-independent part of T-M07-02 and T-M07-05 now exists:

- `cairn.ingestion.base`: `Parser`, `ParseContext`, `ParsedDocument`, typed `Block`
  and `Asset`; JSON-ready layout including ordinal, heading path and source URL.
- MIME normalization and explicit registry; duplicate registrations fail atomically;
  unknown MIME types fail rather than silently parsing binary data as text.
- TXT, CommonMark/GFM-table Markdown, JSON and trafilatura HTML parsers.
  Tables, code fences and nested lists remain atomic; heading context is retained.
- JSON extraction supports an explicit ordered tuple of top-level field names.
  Missing selected content does not fall back to indexing the entire object.
- Strict decoding (UTF-8 by default, explicit encoding override), empty/corrupt
  content errors, a configurable registry byte cap and cancellation propagation.

Scope is deliberately limited: the parsers are library components, **not yet wired
to upload tasks or the worker pipeline**. Language is `und` unless supplied by the
caller; automatic detection remains in the later language/OCR work. Unpaginated
formats report one logical page while block page/bbox remain unknown (`null`).
The default registry limit is 50 MiB; workers must apply deployment-specific limits
and process-level time/memory isolation when they are connected. Direct adapter
calls bypass the registry byte cap. Raw Markdown/HTML-derived content is untrusted
data and must be sanitized at any future UI rendering boundary.

PDF, Office formats, OCR, chunkers, embedding, indexing and the reference corpus
remain open. T-M07-02 is delivered ahead of the PDF-specific dependency because
its existing published interface is independent of engine selection; this does
not close the T-M07-01 quality gate or claim all of Phase 2c is complete.
