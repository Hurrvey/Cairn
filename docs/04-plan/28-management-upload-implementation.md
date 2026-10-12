# Management and upload backend implementation

Date: 2026-09-15. Status: IMPLEMENTED AND PACKAGE-B VERIFIED.

## Scope and ownership

This package owns backend API/source only: model gateway router/schema/service/catalog changes,
safe knowledge-base setup options, bounded multipart upload and authenticated download, nested
knowledge-base/document scoping, dynamic provider runtime projection and retrieval consumption,
the narrow package-A runtime refresh extension, and focused unit/integration tests. It does not edit
`apps/web`, deployment/Compose/scripts, documents 15/25/27, or
`tests/integration/test_product_maintenance_acceptance.py`. Existing dirty work is preserved and no
Git commit, revert, reset, or clean operation is performed.

## Design

### API and authorization

- Add admin-only `/v1/model-providers` and `/v1/models` management routes guarded by
  `platform:models`. Accept only TEI and Infinity providers for this product path. Provider input is
  strict: `name`, `family`, `base_url`, and `config` containing only `binding_revision`,
  `allow_private`, and `max_batch_size`; credential-like or unknown fields are rejected.
- Add `GET /v1/knowledge-base-options`, guarded by `kb:create`, returning enabled
  embedding models, safe storage choices (`id`, `name`, `kind`, `driver` only), configured tokenizer
  IDs, the exact supported extension list, and the 50 MiB upload bound. It exposes neither provider
  runtime configuration nor storage paths.
- Protect model and provider deletion when a knowledge base or immutable index-version snapshot
  references the model. Keep the dependency direction `catalog -> modelgw`: model-gateway code does
  not import catalog ORM. Reference checks use an injected catalog-owned usage facade at API
  composition (and restrictive schema constraints where available). Provider deletion first
  requires no models and never cascades model/data loss.
- Model testing resolves the registered provider and configured tokenizer, validates embedding
  capability/dimension/token limit, then performs one actual bounded embedding probe through the
  existing SSRF-guarded TEI/Infinity adapter. It does not accept an arbitrary test URL.

### Storage, upload, and download

- `config: {}` for local storage resolves to the deployment object root. An explicit local path is
  accepted only when its resolved path remains under that root; existing direct registry fixtures
  using temporary paths remain valid because the restriction belongs at the administrative API/
  service boundary, not the low-level local driver.
- Multipart upload reads `UploadFile` in bounded chunks into a temporary spool while calculating
  server-side SHA256. It rejects empty, over-50-MiB, PDF/OCR, unsupported extension, and mismatched
  content-type/extension inputs before durable registration.
- The source key is always canonical (`workspace/kb/originals/<sha256>`), never derived from the
  supplied filename. The object is written atomically, then the existing transactional registration
  path creates the document/task. Duplicate live content reuses the valid object. If registration
  fails after this request created a previously absent write, cleanup deletes only an unreferenced
  canonical source while holding catalog checks that prevent racing delete/upload from removing a
  newly referenced object. A pre-existing object is never deleted on registration failure. Writes
  and reference changes are serialized by the KB row lock. Upload holds that lock across object
  write and document/task registration, matching package-A purge's lock while it removes the KB
  prefix; therefore upload cannot write after purge cleanup. Failure cleanup reacquires catalog
  scope and deletes only when no live reference exists. If safety cannot be proven it leaves a
  bounded orphan for maintenance rather than deleting concurrent valid data.
- Download verifies `kb:read`, loads the document with the path knowledge-base ID, validates its
  canonical scope and object binding, then streams bytes with a safe filename and stored media type.
  Get/delete/retry/list-chunks all pass the decoded path KB into service/repository ownership checks.

### Dynamic runtime projection

- Define a credential-free provider runtime DTO in `cairn.core`, containing provider identity,
  workspace identity, TEI/Infinity dialect, endpoint, trusted-private flag, binding revision, and
  bounded batch size.
  The data plane imports this shared DTO and never imports model gateway ORM types.
- Control-plane model/provider writes publish or invalidate `model:provider-runtime:<provider-id>` in
  Redis with provider-row locking and post-commit ordering that cannot resurrect a deleted
  projection. The package-A `RuntimeRefresher` also republishes provider projections in bounded
  pages so Redis loss is repaired without manual environment provider UUIDs.
- Query embedding keeps environment endpoints as a compatibility fallback only when no dynamic
  projection lifecycle applies; invalidated/deleted registered providers cannot be revived by the
  fallback. For a registered model, each request loads the Redis projection, validates the provider
  and runtime-KB workspace relationship, and compares a
  deterministic fingerprint. A changed projection opens a provisional provider, swaps it under a
  lock only after successful startup, and retires the old instance. Provisional and active/retired
  providers are closed safely, including shutdown. Tokenizers continue to come only from immutable
  configured files (`CAIRN_RETRIEVAL__TOKENIZER_FILES`; ingestion keeps
  `CAIRN_INGESTION__TOKENIZERS_JSON`).

## Rejected alternatives

1. Environment-only provider UUID mapping cannot satisfy dynamic registration or Redis-loss repair.
2. Loading model-gateway ORM rows in retrieval violates the control/data-plane boundary and couples
   query availability to PostgreSQL.
3. Putting provider credentials/config or local paths in ordinary setup options violates the public
   read-model contract.
4. Reading multipart bodies into one in-memory `bytes` value is simpler but does not provide the
   required bounded streaming behavior.

## TDD sequence

1. RED/GREEN model/provider schemas, guards, deletion fences, actual provider probe, and safe setup
   options.
2. RED/GREEN local-binding root confinement while preserving low-level temporary-path fixtures.
3. RED/GREEN multipart validation, canonical write/registration/cleanup, authenticated streaming
   download, and every nested ownership check.
4. RED/GREEN shared provider projection publication/invalidation and bounded maintenance repair.
5. RED/GREEN retrieval lazy projection consumption, environment fallback, fingerprint replacement,
   concurrency, failure retention, and shutdown cleanup.
6. Run focused unit tests first, then `tests/integration/test_management_upload.py` against the
   dedicated PostgreSQL/Redis ports, then focused regressions, Ruff, and mypy. Clear only acceptance
   database rows through the existing acceptance fixture before releasing the database window.

## Journal

- P01: Read documents 25 and 27, the current Vue API/settings views read-only, current catalog/model
  gateway/object-store/ingestion/retrieval/runtime-refresh seams, dirty status, and recent history.
  No `AGENTS.md` applies. Confirmed parent-owned files and existing dirty recovery/retrieval work.
- P02: Recorded the approved design and implementation sequence before source changes. The design
  keeps existing APIs and runtime environment fallback while adding the smallest safe dynamic path.
- P03: Parent approved the design with corrections: workspace-bound projections, catalog-owned
  model-usage checks without a reverse ORM dependency, row-lock publication ordering, tombstone-safe
  environment fallback, router-bound local-path confinement, and never cleaning up a pre-existing
  duplicate source object. Incorporated all corrections before endpoint implementation.
- P04: Parent added the required upload-versus-KB-purge race. The implementation will share the KB
  row-lock serialization already used by purge and perform conservative, reference-checked cleanup.
- P05: Ran the parent-owned management upload acceptance against PostgreSQL `39170` and Redis
  `38694` before endpoint code: both tests failed only with expected `405 Method Not Allowed`.
  Implemented bounded multipart upload, canonical KB-locked object registration, duplicate reuse,
  authenticated exact-byte download, and nested KB ownership threading. The same parent acceptance
  then passed 2/2. Added owned management/options/local-root/nested-scope integration tests; the
  management route and root-escape cases are RED while all four nested cross-KB handlers are GREEN.
- P06: Implemented strict admin provider/model management and safe KB setup options. Provider bodies
  accept TEI/Infinity plus the allowlisted runtime config only; credential and unknown fields fail
  request validation. Full `/storage-bindings` listing is now `platform:storage` only, while setup
  options expose only binding id/name/kind/driver. Explicit local paths are accepted by the API only
  below the configured deployment root; `config: {}` resolves through the existing deployment root.
  Fixed cookie sessions to use the established permission resolver, restoring admin platform
  capabilities and `kb:create`. Owned management/options/storage/nested-scope tests passed 3/3.
- P07: Added the shared `ProviderRuntimeProjection` with provider and workspace identity and no
  credentials. New model snapshots mark dynamic providers explicitly; historical snapshots retain
  environment endpoint fallback, while dynamic models fail closed if Redis is missing/flushed or a
  tombstone exists. Query runtime loads the projection per request, validates KB workspace, replaces
  changed providers under a lock by fingerprint, keeps in-flight retired providers alive, and closes
  provisional/current/retired providers. Unit runtime/service compatibility passed 36/36.
- P08: Provider management publishes under a provider row lock with a two-second Redis bound;
  deletion writes the tombstone while holding the same lock, preventing refresher resurrection.
  Package-A `RuntimeRefresher` now pages provider IDs and repairs missing projections before KB
  projections. Real Redis eviction/repair passed. Model deletion checks catalog-owned current and
  snapshot references without a modelgw-to-catalog import; the restrictive KB model FK resolves
  create/reindex deletion races, and FK violations map to conflict.
- P09: Added `/v1/models/{id}/test` through an embedding-layer probe injected at API composition.
  It resolves the stored provider, loads the configured tokenizer file, uses the existing SSRF-safe
  TEI/Infinity adapter, performs one bounded embedding, validates dimensions/token budget, updates
  model health, and closes the provider. With the parent-provided fresh TEI at `127.0.0.1:38742`, the
  live probe returned 384 dimensions; a second live call embedded a retrieval query through the
  Redis projection with empty environment provider mappings.
- P10: Added a pure ASGI upload-body limiter before FastAPI multipart parsing. It applies only to the
  actual upload route, rejects declared or actually streamed request bodies above 51 MiB with 413,
  and leaves the exact file limit at 50 MiB. Unit tests cover oversized Content-Length, chunked/no-
  length bodies, and unrelated routes (3/3). Authentication remains before this limiter.
- P11: Added and passed the upload-versus-KB-delete race: delete waits for the KB-locked canonical
  object write and registration before marking the KB, so purge cannot finish prefix cleanup and
  then receive a late upload. Model/provider in-use deletion also returns conflict. The combined
  package-B integration suite plus parent read-only acceptance passed 8/8. Focused Ruff passed.
- P12: Broader real-service regression passed 143/143 in 56.74 seconds: modelgw/retrieval/upload body
  units plus identity/session, catalog/version behavior, package-A maintenance, retrieval acceptance,
  package-B management/upload, and parent upload acceptance. The only output warning is Alembic's
  existing `path_separator` deprecation.
- P13: Compatibility review found that the new `ModelRef.dynamic_provider` provenance marker was
  serialized into old snapshots and initially participated in dataclass equality and ingestion's
  binding fingerprint. Added RED recovery tests, then made the marker non-comparable and removed it
  from `_binding_fingerprint`. The full ingestion runtime plus upload-limit review set passed 12/12:
  old snapshots with no marker resolve against current registered models, embedded artifact identity
  remains unchanged for embedded-to-index recovery, and real model/tokenizer drift still fails.
- P14: Replaced the receive-wrapper exception approach after a real FastAPI `UploadFile` test proved
  FastAPI translated the interruption to 400. The final pure ASGI limiter authenticates first, then
  pre-spools at most the 51 MiB request envelope, returns RFC9457 413 before invoking FastAPI when
  either declared or actual bytes exceed the bound, replays allowed bodies to the real multipart
  parser, and closes its spool in `finally`. Five focused tests pass, including chunked/no-length
  actual multipart and tracked temporary-file closure. PDF magic is rejected even with an allowed
  extension; Office extensions require a ZIP container signature.
- P15: Final static and boundary evidence: focused Ruff clean; mypy clean across 127 source files;
  all seven import-linter contracts kept over 187 files/1016 dependencies; route audit checked 61
  routes and found every non-public route guarded. The import-linter Windows console shim failed to
  canonicalize its script path, so the same installed Click entrypoint was invoked directly.
- P16: Cleared only the dedicated `cairn_acceptance` PostgreSQL database on port 39170 and Redis DB 0
  on port 38694 after the final real-service suite, then released the window to the parent. No DB
  tests or service access ran afterward. The final post-cleanup checks were unit/static only.

## Remaining release-level work

Package B is complete. The parent owns the independent full-suite regression, regenerated OpenAPI/
TypeScript artifacts, Compose/browser persistence, Redis-flush recovery, index retirement, worker
bootstrap, and combined package-E acceptance. Dynamic registered models intentionally fail closed
while their Redis provider projection is absent; the bounded maintenance refresher repairs it.
Historical environment-configured models retain their environment endpoint fallback. PDF/OCR remain
explicitly unsupported.
