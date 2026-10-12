# Five-part supported-product acceptance

Date: 2026-09-15. Coordinator record. Status: FIVE-PART SUPPORTED PRODUCT ACCEPTED.

This record covers only the five deliverables in document25. It does not close PDF/OCR,
the genuine annotated30-PDF quality gate, MCP, custom-function sandbox or advanced reranking.

## Environment and ownership

- Branch `dev`, user checkpoint45ab477, existing dirty work preserved. No new commit or push.
- One subagent at a time: packageA Hume and packageB Tesla are now closed. Both used `gpt-5.6-sol`
  with `high` reasoning. Coordinator owns the final UI, Compose and combined acceptance.
- Dedicated integration PostgreSQL39170/Redis38694 and actual TEI38742 have label
  `cairn.task=mainproduct-20260915`; never use application databases for truncating tests.
- Separate persistent Compose project `cairn-product-acceptance`, HTTP38880, database
  `cairn_product_acceptance`; its env lives in ignored `data/acceptance-product-20260915/compose.env`.
  Application `.env` and pre-existing M08 containers were not touched.

## Observed product journey

| Check | Actual evidence |
| --- | --- |
| Fresh model preparation | All pinned MiniLM files downloaded into a new directory and SHA256 verified |
| Deployment | Real image builds, actual migrations/bootstrap, two API roles, web, TEI and all five workers |
| User setup | Real Chromium login and mandatory first-credential change; redirect to application verified |
| Configuration | Browser-created TEI provider/model, real384-dimensional probe, local object and pgvector bindings |
| Upload | Browser multipart Markdown upload; actual parse, parent-child chunking, TEI embedding and pgvector indexing |
| Recovery | Initial independent-worker ORM bootstrap defect reproduced; after fix, existing failed document recovered via HTTP retry with no re-upload |
| Retrieval | Real browser hybrid query returned Saturn content; API-key query used data-only process |
| Incremental changes | Additional TXT document indexed without whole-KB rebuild; duplicate bytes reused existing document |
| Rebuild | Confirmed estimate followed by actual version1-to2 rebuild and activation |
| Manual editing | Browser changed a searchable chunk; `chunk.reembed` completed and subsequent query returned Titan text |
| Document deletion | Normal API deletion; document.purge completed, source route refused access and remaining document remained searchable |
| Retired index | Old namespace retained before deadline; exact disposable version/task deadlines advanced, index.drop removed only old namespace and preserved active2 |
| Cache repair | FLUSHDB on the isolated Redis; maintain worker republished both KB and provider configuration without manual publication |
| Persistence | Stopped and restarted the entire Compose stack including PG/Redis/TEI; same version and exact source remained usable |
| Permissions | Query-only key could retrieve but not download; kb:read user had no upload/delete/rebuild/admin-model UI actions |
| Small viewport | Read-only390px Chromium page had no horizontal viewport overflow |
| KB-wide purge | Browser deletion completed kb.purge; zero KB/chunk rows, zero vector namespaces and zero object files; last-KB-scoped key revoked |

Screenshots are retained locally under `data/acceptance-product-20260915/browser`, including
`retrieval.png`, `edited-retrieval.png` and `readonly-mobile.png`. They are not source-controlled.

## Deployment-specific defects caught and fixed

1. Wheel-installed CLI resolved migrations relative to site-packages. Container source PYTHONPATH
   now points at `/app/src:/app`; migration and bootstrap succeeded in the built image.
2. Nginx `$host` dropped the non-default ingress port, causing legitimate browser Origin/CSRF
   mismatch. Preserve `$http_host`; do not bypass CSRF. Direct HTTP clients without Origin had
   not exposed this defect.
3. Mandatory credential completion closed its dialog without leaving login. Completion event and
   route transition have a regression test; real new-user browser flow now passes.
4. A document list with `next_cursor` stopped polling. Loaded pages now refresh and navigation
   cancels outstanding polling. Regression test reproduced the old behavior before the fix.
5. Standalone workers did not load identity ORM metadata, so KB foreign-key resolution failed at
   index commit. All five independent-process bootstrap tests reproduced RED, then passed after
   loading identity metadata at the worker composition root. API/test fixture imports no longer
   mask this boot requirement.
6. Unhealthy storage probes were displayed with a success alert. RED/GREEN UI regression now
   requires an explicit error state.

## Reproduction commands

Use only a disposable deployment: these scripts create and mutate configuration and content.
Supply `CAIRN_SMOKE_PASSWORD` privately; first login also needs `CAIRN_SMOKE_NEW_PASSWORD`.

```powershell
uv run --no-project --with playwright python scripts/browser_product_smoke.py `
  --output data/acceptance-product-20260915/browser
uv run python scripts/product_lifecycle_smoke.py `
  --output data/acceptance-product-20260915/lifecycle.json
docker compose --env-file data/acceptance-product-20260915/compose.env `
  -p cairn-product-acceptance --profile local-model stop
docker compose --env-file data/acceptance-product-20260915/compose.env `
  -p cairn-product-acceptance --profile local-model up -d --no-build
uv run python scripts/product_lifecycle_smoke.py --phase verify `
  --output data/acceptance-product-20260915/lifecycle.json
```

The main browser script expects an empty configuration for its setup phase. `--existing-kb ID`
verifies an existing document/query journey without creating duplicate setup records. Browser
tooling is isolated from the project's locked production dependencies.

## Verification checkpoint

The first completed expanded backend run found890 passing tests plus3 regressions and the2 known
PDF/OCR collection errors (coverage84.49%). Two live-model fixtures still configured only legacy
environment providers despite newly registered dynamic models; coordinator now runs the real
maintenance projection refresher and queries with an empty environment provider mapping. The third
assertion still expected the old maintain-handler set; it now explicitly requires purge/drop handlers.
These are test-composition updates, not relaxation of dynamic-provider fail-closed behavior. Focused
and full reruns follow; the earlier unsuccessful run is retained as a checkpoint, not release evidence.

- Frontend: **36 tests passed**, typecheck and generated API-drift check passed.
- Five standalone worker processes: **5 tests passed** after the metadata fix.
- Module boundaries: **7 contracts kept**, no new exception for model-service domain logic.
- Route audit: **61 routes**,8 intentionally public, every other route declares authorization.
- Mypy: **127 source files** passed at this checkpoint.
- Full Ruff lint and format pass across213 Python files after coordinator fixed two ordering
  findings and normalized12 changed files. Agent's focused lint was not mistaken for whole-tree lint.
- Final updated image browser setup/upload/query passed in one uninterrupted run against an empty
  configuration, followed by a second successful lifecycle run. New KB id is retained in the ignored
  `lifecycle-final.json`; old KB purge was verified before new setup, not hidden by dropping a database.
- Actual backend OpenAPI exactly matches the exported spec; default repository `git diff --check`
  passes. An experimental `core.autocrlf=false` check produced artificial CRLF warnings; no repository
  EOL setting was changed and those warnings were not treated as source defects.
- Final independent full backend: **893 passed,0 assertion failures,0 skips,2 known PDF/OCR
  collection errors**,4 existing Alembic warnings,235.61 seconds,**85.82%** combined coverage.
  The unchanged80% coverage gate passes. The full command still exits1 because the deferred draft
  tests import unimplemented `cairn.ingestion.ocr`; they were not hidden, removed or marked skipped.
- Final browser and actual-ingress lifecycle passed on rebuilt images, and a second Redis FLUSHDB
  recovery verification passed against the final dynamic-provider implementation.

## Acceptance decision

All five requested deliverables are accepted for the documented supported-format local product:
KB/document UI, model/storage UI, full worker/model/persistence Compose wiring, automatic ACTIVE
runtime repair, and general document/KB/retired-index cleanup. Tests include real PostgreSQL/pgvector,
Redis, TEI and Chromium, not only mocks or hand-populated vector fixtures.

The supported configuration surface is TEI/Infinity embeddings, a deployment-owned local object
volume and pgvector. It does not claim S3 or arbitrary credential-backed/chat providers. This remains
a loopback HTTP local preset, not production HTTPS/network/backup hardening. The broader original
knowledge-base E2E gate stays OPEN because PDF/OCR and annotated30 evaluation are explicitly deferred.
No work on user items6/7 was undertaken in this delivery, and no Git commit/push was made.

Final test artifacts: `backend-final.xml`, `coverage-final.xml`, `final-focused.xml` and screenshots
under ignored `data/acceptance-product-20260915`.

## Resource disposition

L127 confirms label-verified removal of the3 completion PG/Redis/TEI containers and project-verified
removal of the12 Compose acceptance containers/network. No test/subagent process remains. Compose
was stopped with `down` without `-v`: its3 named volumes, downloaded model assets, built images and
local reports/screenshots are intentionally retained. M08 PG/Redis remain running with8 days uptime;
no application `.env`, application database or unrelated container was modified. Branchdev remains
at45ab477; all requested implementation changes remain in the working tree, not committed or pushed.
