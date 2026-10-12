# September 29 history reconciliation and continuation

## Sources read

| Session | Role in the project history |
| --- | --- |
| Codex `01a07b03-6111-7560-8022-cb2f1e3cd585` | Original continuation: parser/worker lifecycle, complete management/upload/configuration UI, runtime refresh and cleanup, generic MCP and managed MCP UI. Later messages concern the separate Agent integration/deployment. |
| Codex `01a0bdb3-48bb-73d0-b65f-bf8ed8d243c3` | Company fork creation and branding, then PDF/OCR baseline and weighted retrieval/parent expansion/rerank continuation; interrupted before final verification. |
| Claude `d8c12a1f-bca6-4eeb-a347-987e40eec18d` | Verified the interrupted PDF/retrieval increment and rebuilt the modern workbench. Its historical 1066-backend/60-web counts are checkpoints, not current counts. |
| Claude `295150be-3d69-4b97-903a-d9b70bd238ef` | Qdrant-only vector cutover, BM25, Compose migration/browser follow-up, learned sparse bge-m3 sidecar and hosted embedding providers. |
| Claude `a78248a1-a318-48b2-9b61-d11a15b85b02` | One-stack constraint and optional bge-m3 operation; implemented much of retrieval contract closure, metadata, batching/load shedding; drafted S3/MCP/Dify designs, interrupted during final verification. |

Local logs were parsed into ignored temporary text for inspection, without copying conversation
secrets into project docs. Actual source, current diffs and new tests govern acceptance.

## Starting state

HEAD `bccb491` (September28) records the Qdrant migration. The working tree contained an
uncommitted schema-2 retrieval increment, migration0015, metadata UI, explain UI and benchmarks.
README still claimed pgvector was current and several implemented retrieval options were absent.
Those claims were stale; existing changes were preserved and completed, not reverted.

User constraints retained: Cairn development first; one complete product stack at a time;
company synchronization after Cairn acceptance; bge-m3 optional; one persistent guidance agent.
The current guidance agent is `/root/design_advisor`, reused throughout this continuation.

## Implemented this continuation

1. Retrieval validation and invariants: malformed/empty logical filters, scalar type semantics,
   schema-2 facts/ordinal on both initial and manual reembedding paths, metadata container depth,
   ACTIVE/live citation reads, batch validation before search, shared-encoding cancellation
   isolation, complete explain timing.
2. MCP: authorized directory and descriptions, metadata filters on search, `get_document` as
   ordered indexed passages, resources/templates, fixed ACTIVE paging, byte/point/time bounds,
   permission checks before storage, no stale-parent or guessed overlap reconstruction.
3. Dify: `/retrieval`, registration probe/authentication, typed errors/rate limit handling,
   supported metadata operators, separate relevance scoring without changing native ranking,
   bounded missing-dense-score enrichment on the same namespace; real HTTP acceptance.
4. S3: bounded multipart/get/list/delete/presign, real MinIO contracts, sealed write-only
   credentials, rotation with row locks and versioned client refresh, in-flight stream leases,
   per-connection DNS/IP/origin checks, tenant-scoped probes, UI and optional MinIO build/profile.

Design corrections were discussed with the same advisor before modification. Details are in
the September28 specs and their September29 review resolutions.

## Verification record

- Initial baseline: Ruff and strict mypy152 files; frontend70 tests; all backend units green.
- Scoped real integration:17 tests covering retrieval/MCP SDK/Dify/catalog maintenance passed.
- S3: real MinIO driver contracts and units passed; encrypted rotation, foreign workspace probe,
  real parse/chunk/embed/index/content/delete integration passed.
- Static current: Ruff, format, strict mypy159 files,7 import contracts,73-route auth audit.
- Frontend:71 tests,669 i18n keys, typecheck, API drift and production build passed.
- Images: Cairn backend, web, and optional MinIO built successfully.
- Real Compose browser:22 PASS assertions in88 seconds,33 screenshots; separate S3
  create/credential replacement/KB upload/index script passed with3 screenshots.
- Dify through nginx:170 concurrent invalid-auth probes produced109 structured403 responses
  and61 structured429 responses with Retry-After; no HTML error pages.

Full-suite attempts are recorded honestly: first stopped after Qdrant tmpfs exhaustion. The
integration fixture was leaking namespaces across tests; explicit exclusive-test cleanup fixed
that. Second run completed1184 passed/92 failed, coverage82.87%; every failure came from object
registry lifecycle leaking between tests after a production shutdown test. Per-test registry
disposal/reset fixed it; the direct10-test live-model/management reproduction passed. A third
clean complete suite is running, reports under `data/continuation-20260929-backend-final.*`.
Final clean result: **1276 passed, 0 failed, 0 skipped, 4 warnings, 84.71% combined coverage,
687.07 seconds**. Test Qdrant returned to zero collections/8 KiB usage. This closes the combined
regression gate for the four-increment implementation, not the whole-project plan.

## Still unfinished after this increment

Actual source audit confirms general model invocation/streaming, usage and strict quotas,
crawler/archive import, standalone SDKs, configurable DAG pipelines, production function sandbox,
evaluation, Helm/autoscaling/monitoring/restore drills and scale evidence remain unfinished.
The annotated30 PDF gate lacks independent full-document labels; sandbox release requires a
real gVisor/Firecracker environment and independent security sign-off. These are not closed by
passing ordinary unit tests or shipping configuration files.

Next reviewed slice: `2026-09-29-model-invocation-and-usage-design.md`. Complete current combined
acceptance first, then implement session-authorized chat/rerank invocation and honest asynchronous
usage reporting, before strict quota/crawler/evaluation work.
