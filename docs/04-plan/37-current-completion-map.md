# Current completion map and fixed-team work queue

Updated October9. User priority is PDF parsing completion; quota and other new modules are deferred.
This map distinguishes implemented, verified increments from original
module/WBS completion. Old baseline checklists and conversation summaries are not proof that
every module is complete. Evidence/history: documents15 and36.

Team (user update October9): coordinator `/root`; unchanged sole reviewer `/root/design_advisor`;
sole implementer `/root/developer_61`, gpt-6.1-sol/medium. Old `/root/developer` is stopped and
superseded because its model could not be changed in place. Reuse the new fixed developer for
subsequent code work; no concurrent implementers.

Current delivery mode: developer implements the whole frozen PDF P1 stage autonomously;
root performs one full acceptance after READY and sends consolidated reviewer-approved rework.
See document38 and the2026-10-09-pdf-stage contract/plan/acceptance matrix. No frequent mid-stage
code review messages or unrelated additions. October10 P1 delivery completed but engineering gate
was not accepted after concentrated review/root reproduction. Approved REWORK-01 (R01–R09/Q01)
is now assigned as one batch; quality/performance gate remains OPEN.

October 12 decision: PDF parsing is Docling-only for the product. The pdfium layout no
longer re-sorts words by coordinates (reading order fixed, wrapped lines merged into
paragraphs); the Docling worker uses Docling's default backend, keeps its models loaded
across documents, de-duplicates overlapping table-cell text and keeps multipage text
instead of rejecting the document. Measured on the 36-document corpus with the warm
worker: 36/36 success, 6.85 s/page on 2 CPUs, 96 tables and 40 pictures. The quality gate
(independent human gold) remains OPEN.
Annotation kit for closing it: `scripts/prepare_pdf_annotations.py` generates an empty gold skeleton
and per-page templates; rules in `05-quality/05-pdf-annotation-guide.md`. Scans with an embedded
text layer now bypass Docling OCR (`pdf_routing.py`, rule `scan-text-layer/1`).
| Area | Current verified or implemented scope | Remaining work |
| --- | --- | --- |
| Core/identity/authz/tasks | Existing runnable product foundation, permissions, durable task queue, normal request limits | Complete quota-blocked task lifecycle and its fencing/dedup changes; later SSO remains stretch |
| Catalog/ingestion lifecycle | Upload/download, document/chunk editing, revision recovery, blue-green rebuild, deletion/retirement, runtime refresh | Archives and crawl source lifecycle, quota allocations; review other original endpoints individually |
| Object/vector storage | Local/S3 object drivers, sealed S3 credentials/rotation, Qdrant-only vectors, exact supported filters | Quantization/large-scale performance evidence, physical occupancy quota, source sync connectors |
| PDF/OCR | Runnable pypdfium2/Tesseract baseline with cited text and bounded subprocesses | Genuine30 complete annotated documents, table/reading-order/OCR gold quality and throughput/memory evidence |
| Retrieval | Hybrid/weighted/RRF/rerank/parent/MMR/filter/highlight/explain/batch/citations, overload reporting | Original scale/SLO commitments, registered reranker discovery integration, rewrite/compression hooks |
| MCP | Managed server/UI, scoped search/directory/indexed passages/resources, SDK/Compose acceptance | Wider real-client matrix and optional features; no claim OAuth/stateful-session coverage |
| Dify | Compatible HTTP adapter, bounded relevance score enrichment, actual HTTP/nginx acceptance | Live Dify application acceptance, nightly upstream compatibility |
| Model gateway | Session-authorized chat/rerank, native text provider contracts, streaming/cancel/retry/breaker, admin and ordinary-user console | Live commercial provider verification, model/provider update/discovery, fallback chains, embedding-wide accounting, KEK rotation workflow |
| Usage | Gateway attempts through bounded queue/Redis stream/SQL dedup, unknown counts/cost coverage, admin report | Embedding/retrieval attribution, self-scoped/broader period reports, strict quota ledger (separate), reporting retention ops |
| Client distribution | Accepted Python/TS core, optional LangChain/LlamaIndex adapters, local package/consumer and real Compose acceptance | Dedicated CI/nightly compatibility, registry publication if requested, streaming and broader client conveniences |
| Quotas | Reviewed design plus unintegrated account/allocation ORM draft; deferred for PDF | Migration/facade/capacity/API/UI, then vector/worker blocking and embedding-attempt reservation slices |
| Crawling/archives | Existing safe HTTP, HTML parser and generic task/ingestion foundations | Import/crawl models, frontier/robots/sitemap/schedules/reports/incremental/removal, secure browser rendering |
| Pipelines/functions | Fixed ingestion pipeline and trusted custom-executor seam | Versioned DAG/validation/runtime/editor, production sandbox/runner/library; independent security release gate |
| Evaluation | Retrieval service and versioned index foundation | Golden-set/import/review/metrics/run/compare/export/UI; temp-index identities/cleanup and generated/LLM-judge extras |
| Production ops | Compose/images/nginx/basic metrics and acceptance tooling | Helm/HPA/KEDA, Grafana/alerts, backup/restore drill, rolling-upgrade and1M/10M scale evidence, notification delivery |

## Latest acceptance checkpoints

1. Retrieval/MCP/Dify/S3 combined:1276 passed,84.71% coverage; frontend71 tests, real Compose
   workbench22 PASS plus S3 and external-client flows. Source/fixtures changed afterward.
2. Gateway/usage: main complete run1307 passed,14 S3-env skips,0 failed/errors,84.06%; real-MinIO
   affected-file supplement47 passed without skips covers all14. This is combined evidence,
   not a fabricated single no-skip run. Frontend72 tests and local-provider browser flow pass.
3. SDK: October8 coordinator independently passed46 Python tests,12 TypeScript tests/typecheck,
   and Python generation `--check`. The API-key/ordinary-user KB owner/grant defect is corrected;
   its focused matrix has67 passing tests plus static checks. Live harness evidence covers key-auth
   creation/upload/query/citations, same-state Python/TS ordered results, TS mutations and observed
   reembedding. Coordinator separately verified removal of temporary KBs, latest document/chunks,
   vector namespace and local objects in the actual product Compose stack. Final3 live rerun passes
   complete single/batch ordered comparisons, mutations and purge checks; TS generation check passes.
   Same reviewer accepts core and adapter source. Python62 tests and TS12 pass; final wheel/tarball
   clean consumers, actual LangChain1.6.6/LlamaIndex0.14.25 tests, final4 real adapter mapping and
   product purge all pass. Reviewed SDK increment is accepted; broader CI/publication remain open.
   Sync timeout remains explicitly soft aggregate plus per-I/O limits; async/TS require hard overall
   cancellation. No external registry publication or full SDK module completion is claimed.
4. 2026-10-13 real-document pilot (the owner's 64-page HR manual PDF and duty-roster XLSX)
   through the product stack: Docling parse 907 elements / 470 chunks, roster one table. The
   knowledge base was rebuilt from MiniLM to the bge-m3 sidecar (CUDA, RTX 3060, ~45 chunks/s,
   ~45 ms per query); 7/7 questions correct at rank 1 (MiniLM: 4/7 within the top three, 3
   missed, dense scores 0.44-0.50 for every Chinese chunk). Fixed on the way: Docling child
   address-space ceiling 12 GiB with honest failure codes; embedding deadline per batch with the
   worker's read timeout following it and compose knob `CAIRN_EMBEDDING_DOCUMENT_TIMEOUT_S`;
   sidecar warm-up before `/health` is ready; a terminal document failure names the blocked
   build on its index version and activation clears the note; the bge-reranker-v2-m3
   cross-encoder is bundled (TEI service with pinned, SHA-256-verified weights) and the pilot
   knowledge base reranks by default, sharpening the margins (annual leave: 0.99 vs 0.25).
   Open: the reindex estimate ignores CPU throughput; the PDF gold annotation is still pending.

## Ordered development queue

October9 supersedes the earlier ordering below: PDF correctness/quality/real pipeline acceptance
comes first under `superpowers/plans/2026-10-09-pdf-correctness.md`. No quota implementation while
that priority is active. The following items are retained as the later backlog.

1. SDK reviewed increment is complete; preserve its local acceptance evidence and pending broader scope.
2. Deferred: registered-resource quota foundation/admission/API/UI and workspace RPM, per reviewed design.
3. Vector reservation plus blocked/resume worker lifecycle, then embedding attempt-token budget.
4. Archive import and static crawl vertical slices, followed by whole-browser-egress-protected JS
   rendering. Source absence alone never authorizes deleting old documents.
5. Query-time golden-set evaluation/UI and gateway follow-ups; temp-index labels must use stable
   document/span identities or explicit lineage across rechunking.
6. Pure versioned DAG/editor using built-in nodes, then reviewed production sandbox integration.
7. Remaining production manifests/tools and actual restore/upgrade/performance evidence.

SDK work can continue while backend source is frozen for regression. Integration database tests
remain serial/exclusive. Only one full Cairn/company product stack is active; company sync waits
for Cairn acceptance. No external package publication, merge or push is implied by local builds.

External verification gaps are kept specific, not used to stop unrelated development: production
commercial credentials, target cluster/scale hardware, independently annotated PDF gold data,
and sandbox runtime/security sign-off may require project-owner input after code/test artifacts
are ready for concrete review.
