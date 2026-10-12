# First bounded M09 public retrieval API

Date: 2026-09-15. Status: bounded first public retrieval slice independently ACCEPTED.

Final coordinator evidence:138 focused passed (50.34s), including both real TEI HTTP paths and all
16 public acceptance cases; full backend856 passed,2 known PDF/OCR collection errors,no assertion
failures/skips,coverage85.20% (210.54s). Full command remains nonzero. Final lint/format191,mypy119,
import contracts7 and route authorization51/8 passed. Reports are retained under ignored
`data/acceptance-retrieval-20260915/`; see ledger L112-L113. Aristotle is closed. Historical design
and intermediate failures below are audit evidence, not current status. Full M09/product E2E,
automatic runtime refresh after300s, UI/deployment and advanced features remain OPEN.
Baseline: `dev` at `45ab477` with the accepted dirty recovery/documentation work preserved.
This journal is the durable record for design decisions, RED/GREEN evidence, review corrections,
and final bounded limitations. No commit, revert, clean, or parent-owned-file edit is permitted.

## Objective and ownership

Implement only `POST /v1/retrieval/query` as a runnable supported-format product path. The first
slice authenticates, authorizes `kb:query` on every target, reads the coherent ACTIVE runtime from
Redis, and searches the ACTIVE pgvector namespace using full-text, vector, or hybrid retrieval.
PDF/OCR, batch, SDK, MCP, UI, parent expansion, reranking, MMR, explain, custom retrieval, and other
advanced M09 surfaces remain visibly unsupported.

Owned here:

- new `src/cairn/retrieval/` package and router;
- narrow credential-free runtime/config migration with catalog compatibility exports;
- data-plane embedding/runtime configuration, API role composition, authz fail-closed correction,
  `.importlinter`, `tests/unit/retrieval/`, `tests/integration/test_retrieval.py`, and this document;
- the smallest read-model write change needed to persist exact `token_count` on newly indexed and
  manually re-embedded vector payloads.

Parent-owned and never edited here: `docs/04-plan/15-execution-ledger.md`,
`tests/integration/test_retrieval_acceptance.py`, and
`tests/integration/test_live_model_pipeline.py` unless the parent explicitly transfers ownership.

## Current evidence

- No `cairn.retrieval` package or mounted data-plane route exists.
- `KnowledgeBaseRuntime`, its binding projection, and retrieval configuration are under catalog,
  which M09 is forbidden to import.
- Catalog already publishes `kb:runtime:{uuid}` from the ACTIVE index-version snapshot. Retrieval
  must consume that Redis value directly and must not hydrate a cache miss from catalog/ORM.
- pgvector already supports dense-vector and PostgreSQL full-text queries. Hybrid requires two
  concurrent driver calls and fusion above the driver.
- Indexed payloads preserve chunk/document/KB IDs, exact content, index version, parent ID, and
  chunk citation metadata. They currently omit exact `token_count`; source titles/types/timestamps
  are not present and therefore must not be invented.
- `DataPlaneAuthz` stores the principal and IP allowlist in separate cache entries. A principal hit
  plus missing/unavailable allowlist entry currently allows the request, which is fail-open.
- `apps/api/main.py` imports control routers and identity middleware globally even for `role=data`.
  A data-only process therefore does not yet have an honest control-service initialization boundary.

Parent acceptance RED, recorded before production edits:

```text
Artifact: data/acceptance-retrieval-20260915/http-red.xml
Result: 3 failed in 5.44s, all because POST /v1/retrieval/query returned 404
Cases: missing auth -> 401; scoped API key full-text Saturn hit -> 200 with cited IDs/version;
       mixed allowed+ungranted targets -> 403 before runtime lookup, even when allowed KB unindexed
Database: fresh migrations 0001..0011
```

The parent granted this worker the exclusive PostgreSQL/Redis window after that RED.
The parent then expanded the independent HTTP acceptance matrix to seven cases: foreign-workspace
runtime, deleting runtime, missing runtime cache, strict failures without query-text leakage, and a
restricted API key whose legacy `authz:ip:*` entry is deleted after a warm allowed-IP request. A
subsequent request from a disallowed IP must return 401, 403, or 503, never pass through. Mode/default
configuration resolution remains after all-target authorization, so even an unsupported configured
default cannot move a runtime/config read ahead of the 403 barrier.

## Considered approaches

### A. Reuse catalog and ingestion runtimes in retrieval

Rejected. It is quick but imports catalog/model gateway/ingestion into M09, permits ORM hydration on
cache misses, and makes the data role initialize control services. It contradicts the principal
reason for the data-plane boundary.

### B. Put endpoints and credentials in each cached KB runtime

Rejected. It couples ACTIVE index publication to deployment secrets, duplicates credentials per KB,
and makes secret rotation a catalog/runtime-cache operation. It also exposes more than the immutable
index/model identity retrieval needs.

### C. Shared immutable projection plus environment-bound data runtime

Selected. Move credential-free config/runtime values to a lower shared module and keep catalog
imports as compatibility re-exports. The Redis runtime carries the immutable `ModelRef` (including
provider identity), ACTIVE index version, vector binding, retrieval defaults, workspace, and status.
The data process maps provider identity to an operator-configured endpoint/tokenizer registry. A
full-text or caller-vector request does not require any embedding endpoint.

## Selected architecture

### Shared projection and compatibility

Create a focused shared runtime/config module under `cairn.core` containing the frozen Pydantic
retrieval config types, credential-free vector binding reference, and `KnowledgeBaseRuntime`.
`cairn.catalog.config` and `cairn.catalog.dto` keep their existing public imports as aliases, so
stored JSON, current callers, and dirty recovery work remain compatible. The shared module imports
only core `ModelRef` and Pydantic; retrieval imports no catalog code.

Extend `ModelRef` with optional immutable `provider_id`. Newly captured index-version snapshots
populate it. Legacy snapshots without it remain usable for full-text and supplied-vector queries;
query embedding fails clearly until a coherent snapshot with provider identity is published. The
endpoint URL and credentials never enter `ModelRef` or the KB runtime.
The new optional field is appended after all historical dataclass fields so existing positional
construction remains compatible. Legacy snapshots are never silently rewritten: because ingestion
also compares prepared gateway refs with immutable version refs, source updates/manual re-embedding
on a legacy snapshot may require a fresh rebuild before they can use a newly provider-qualified ref.

### Data-plane composition

Add retrieval settings with these exact startup environment shapes:

```powershell
$env:CAIRN_RETRIEVAL__EMBEDDING_ENDPOINTS = '{"<provider-uuid>":{"dialect":"tei","base_url":"http://127.0.0.1:38742","allow_private":true,"namespace":"retrieval-20260915","max_batch_size":16}}'
$env:CAIRN_RETRIEVAL__TOKENIZER_FILES = '{"<tokenizer-id>":"D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json"}'
$env:CAIRN_RETRIEVAL__REQUEST_TIMEOUT_S = '5'
$env:CAIRN_RETRIEVAL__RUNTIME_TIMEOUT_S = '0.25'
$env:CAIRN_RETRIEVAL__SEARCH_TIMEOUT_S = '2'
```

`dialect` is `tei` or `infinity`. An optional endpoint `api_key` is a `SecretStr`; secrets remain
process configuration. Configured providers/tokenizers are validated and opened during data/all
lifespan startup, then closed during shutdown. No endpoint is required when requests use full-text
or a supplied query vector.

The parent's live HTTP acceptance exports:

```powershell
$env:CAIRN_TEST_TEI_URL = 'http://127.0.0.1:38742'
$env:CAIRN_TEST_TEI_TOKENIZER = 'D:/code/Cairn/data/acceptance-20260908/tei-model/tokenizer.json'
```

The fixture maps those test-only values into the public production seam before creating the app:
`CAIRN_RETRIEVAL__EMBEDDING_ENDPOINTS` is keyed by the ACTIVE snapshot's `provider_id`, and
`CAIRN_RETRIEVAL__TOKENIZER_FILES` is keyed by that snapshot's `tokenizer_id`. Retrieval production
code does not read `CAIRN_TEST_*`. The endpoint namespace includes provider ID, endpoint URL, dialect,
and an operator-supplied immutable revision string, preventing embedding-cache reuse across changed
deployments. The parent TEI server is ready and its model/tokenizer hashes already match.

`apps/api/main.py` performs role-local imports. `control` and `all` retain session authentication,
CSRF, forced-change, control routers, and current behavior. `data` mounts only common middleware,
a data-plane bearer/API-key authenticator and limiter, health/meta/metrics, and retrieval. It does
not import or initialize identity/catalog/platform services. pgvector SQL remains an allowed data
store operation; the first local binding may initialize the shared engine after authorization.

### Authentication and authorization order

The route declares a data-plane principal dependency recognizable by `scripts/check_route_authz.py`;
it is never added to the public-path exception list.

`DataPlaneAuthz` caches one versioned envelope containing both the principal and the key's IP
allowlist. Old principal-only entries are treated as cache misses and resolved through the existing
narrow read-only authz repository. The in-memory envelope is checked directly. Invalid/malformed
networks, missing allowlist state on a purported hit, or cache failure cannot bypass a restricted
key. Unrestricted keys are represented explicitly by an empty list, not by missing data.
The envelope also contains key expiry. Cache TTL is capped to the remaining key lifetime and every
cache hit rechecks expiry, so a short-lived key cannot remain valid because its principal was cached.
Database fallback returns principal, allowlist, and expiry as one value; a failed Redis write cannot
discard the allowlist policy for the current request.

For every request, the service performs this strict sequence:

1. Validate/decode all target IDs without I/O and reject duplicate targets.
2. Check `principal.can("kb:query", kb_id)` for every target in memory.
3. If any target is unauthorized, reject the whole request with 403 and perform no runtime,
   embedding, vector-registry, or storage read for any target.
4. Load all `kb:runtime:{uuid}` entries from Redis with a bounded timeout and no database fallback.
5. Verify every runtime belongs to the principal workspace, has status `active` or `indexing`, has a
   positive published ACTIVE index version, and has coherent model dimension/metric/binding fields.
   `indexing` is valid during blue/green rebuild because the published runtime still points to the
   old ACTIVE namespace; retrieval never reads or constructs a namespace from the building pointer.
6. Only then resolve embedding providers/vector stores and execute searches.

A missing, malformed, stale, or unavailable runtime is a fail-closed readiness/upstream error. It
must not silently fetch desired KB state or a building namespace from PostgreSQL.

### Request planning and supported behavior

The Pydantic request follows the normative field names and bounds: 1–10 targets, query at most 8192
characters, `top_k` 1–100, `candidate_k` 1–1000 and not below `top_k`, and finite query-vector
components. Query-vector length has a hard request-level ceiling of 65,536 before its exact ACTIVE
model dimension is checked. Target weights reject booleans, NaN, infinity, and values outside
0.0–10.0. At least one of `query` or `query_vector` is required. Full-text requires query text;
hybrid may combine query text with a supplied vector. A supplied vector bypasses embedding.

Request fields override each KB runtime's retrieval defaults. A federated request may use one
request-level plan, but each target keeps its immutable ACTIVE runtime and weight. Supplied vectors
require all targets to have the same immutable embedding identity and exact dimension. Query text
is embedded once per distinct immutable model identity, never through ingestion `PipelineRuntime`
or a model-gateway database lookup.

Supported in this slice:

- `fulltext`: one bounded pgvector text query per target; no embedding provider required;
- `vector`: supplied vector or query embedding, then one bounded dense query per target;
- `hybrid`: dense and full-text searches run concurrently per target and across targets;
- RRF fusion with request/target weights, score threshold, stable deterministic tie breaking,
  exact whole-hit token budgeting, and `strict` target-failure semantics;
- exact payload metadata/citations and only known source values.

Explicit `fusion.method="weighted"`, enabled rerank, enabled parent expansion, enabled MMR,
`explain`, highlights, unsupported filter fields, or custom behavior return a stable 400 rather than
being ignored. When those features are enabled only by existing KB defaults (not explicitly by the
caller), the minimal request remains usable: the planner disables them and appends a precise
`degraded` notice. Thus the shipped catalog defaults `rerank.enabled=true` and
`expand_parent=true` do not make the first endpoint unusable or silently misrepresent quality.

### Search, assembly, and failure behavior

Each search uses `Namespace(runtime.id, runtime.index_version)` and the credential-free runtime
binding. Overall, runtime, embedding, individual search, and request deadlines are bounded.
Non-strict storage/provider failure produces results from successful targets and a named
`partial_failures` entry; `strict=true` raises a 502. If every target fails, the request is a 502 even
when `strict=false`, rather than a misleading empty 200. Zero matches from successful searches is a
normal 200.

Results encode `chunk_id`, `document_id`, and `knowledge_base_id` with public prefixes and preserve
exact content, exact persisted `token_count`, ACTIVE `index_version`, final score, and available
per-stage scores. Metadata retains known heading/page/bbox/citation/language/source URL values.
`source` contains only fields already present in the read model; absent title/type/updated time stay
absent/null. A malformed or incomplete hit payload is never patched with invented values; it becomes
a named target/read-model failure. Legacy payloads without `token_count` may still be returned when
no token budget is requested, with `token_count: null`; if a token budget is requested, retrieval
fails clearly because it cannot prove the budget instead of treating the unknown count as zero.

The response always contains `request_id`, `results`, `usage`, `degraded`, `partial_failures`,
`truncated_to_token_budget`, and `explain: null`. Usage records ACTIVE index versions, bounded stage
latencies, embedding tokens when embedding occurred, rerank units `0`, and embedding-cache state
when the embedding service can prove it.

New ingestion/index writes add exact `token_count` to the denormalized payload, including manual
re-embedding. No catalog hydration is added for legacy points; legacy incomplete payloads require
reindex/re-embedding for exact budgeting. Unit tests cover both ordinary indexing and manual
re-embedding payloads.

The runtime cache currently expires after 300 seconds and retrieval deliberately has no catalog
fallback. Therefore an idle KB can return runtime-unavailable after expiry until the control plane
republishes it. This bounded slice documents that 503 instead of weakening isolation. A control-plane
republish/refresh task is required before product-release readiness; this endpoint is not by itself a
deploy-ready release claim.

## Planned file boundaries

- `src/cairn/core/retrieval_runtime.py`: shared frozen runtime/config DTOs only.
- `src/cairn/core/config.py`, `src/cairn/core/modelref.py`: retrieval process settings and provider ID.
- `src/cairn/catalog/config.py`, `src/cairn/catalog/dto.py`, narrow catalog snapshot construction:
  compatibility exports and publication of the shared shape.
- `src/cairn/retrieval/dto.py`: public request/response and immutable query-plan values.
- `src/cairn/retrieval/runtime.py`: Redis-only runtime loader and data-plane embedding registry.
- `src/cairn/retrieval/planner.py`: precedence, support/degradation decisions, dimensions, bounds.
- `src/cairn/retrieval/fusion.py`: deterministic RRF and score bookkeeping.
- `src/cairn/retrieval/service.py`: authorization-first orchestration, concurrent search, assembly.
- `src/cairn/retrieval/router.py`: HTTP translation and explicit auth dependency.
- `src/cairn/retrieval/errors.py`, `src/cairn/retrieval/__init__.py`: stable errors/public facade.
- `apps/api/main.py`, `apps/api/middleware.py`: role-local composition and data bearer auth.
- `src/cairn/authz/dataplane.py`, `scripts/check_route_authz.py`: atomic auth cache/IP enforcement and
  route-audit recognition.
- `src/cairn/ingestion/pipeline.py`: persist exact token count in the vector read model.
- `.importlinter`: activate retrieval data-plane and ORM/control-plane prohibitions.
- `tests/unit/retrieval/`, `tests/integration/test_retrieval.py`: owned RED/GREEN coverage.

## TDD and acceptance sequence

No production edit precedes its focused failing test. The implementation proceeds in small RED →
GREEN cycles and appends commands/results to this journal:

1. shared DTO JSON/backward-export compatibility and immutable provider identity;
2. authorization of every target before any runtime/provider/storage call;
3. fail-closed atomic API-key/IP envelope including legacy-cache behavior;
4. Redis-only runtime hit/miss/malformed/workspace/status/ACTIVE-version handling;
5. request bounds, finite/exact query-vector dimension, unsupported explicit options, and default
   degradation;
6. full-text search and exact result/citation/token assembly (parent's first usable path);
7. supplied-vector dense search with no provider;
8. environment-configured TEI/Infinity query embedding and tokenizer startup;
9. concurrent hybrid search, deterministic RRF, weighted targets, threshold/token budget;
10. multi-target partial/strict failure behavior and no catalog/control hydration;
11. role mount/isolation, route authorization audit, import-linter, and payload token-count writes;
12. targeted real PostgreSQL/Redis tests in the exclusive window, then release the DB to the parent.

The runtime tests include blue/green behavior: a published runtime with `status="indexing"` and
`index_version=1` continues to query namespace v1 while v2 is building.

Initial parent acceptance is GREEN only when all seven parent-owned cases pass exactly: unauthenticated
401; scoped-key Saturn full-text 200 with cited public IDs and ACTIVE version; mixed target 403 before
any runtime read; foreign-workspace runtime rejection; deleting-runtime rejection; missing-cache
fail-closed behavior; and no restricted-key IP bypass after deleting the legacy IP cache key. Strict
failure responses must not leak query text. Subsequent parent acceptance adds session and role
mounting, no-control-hydration, and real TEI query coverage independently.

Local verification uses the granted disposable services only:

```powershell
$env:CAIRN_TEST_USE_EXTERNAL_SERVICES = '1'
$env:CAIRN_DATABASE_URL = 'postgresql+asyncpg://cairn:cairn-disposable-only@127.0.0.1:39170/cairn_acceptance'
$env:CAIRN_REDIS_URL = 'redis://127.0.0.1:38694/0'
uv run python -m pytest -o addopts=... <targeted tests>
uv run python -m ruff check <owned paths>
uv run python -m mypy
uv run python -m importlinter
uv run python scripts/check_route_authz.py
```

The worker will not run the duplicate parent full suite. The accepted baseline remains 780 passing
backend tests with two known PDF/OCR collection errors; this slice does not hide or reclassify them.

## Pre-code self-review

- No placeholder or unresolved interface choice remains for the first endpoint.
- Authorization ordering is stronger than ordinary request planning: all-target denial precedes every
  cache/config/model/storage read, including reads for otherwise allowed targets.
- Full-text and supplied-vector queries have no accidental embedding-provider dependency.
- ACTIVE snapshot identity, not desired KB/model settings, determines namespace/model/dimension.
- Runtime status `indexing` remains queryable only through its published positive ACTIVE version;
  no building pointer exists in the shared DTO or retrieval plan.
- Unsupported default quality stages degrade explicitly; explicit unsupported requests fail.
- Retrieval has no catalog, identity, model-gateway, ingestion, platform, or ORM import.
- pgvector SQL is intentionally allowed and is not misreported as catalog/control-plane hydration.
- Data-only role isolation is an actual import/composition change, not a claim based only on mounts.
- PDF/OCR and the rest of M09 remain out of scope and visibly unsupported.
- The 300-second idle runtime expiry is an explicit 503 limitation pending a control-plane refresh
  task, so this slice does not claim deployment/product-release readiness.

## Evidence log

### 2026-09-15 design checkpoint

Read documents 21, 23, M09, the normative data-plane API, module boundaries, current runtime
publication, authz data-plane path, role composition, embedding adapters/tokenizers, pgvector driver,
route audit, and indexed payload construction. Selected approach C and recorded the parent's initial
three-case HTTP RED. No production code or test file was edited, and no database test was run by this
worker before parent review.

### 2026-09-15 coordinator design corrections

Coordinator approved the bounded direction with corrections now incorporated: blue/green runtime
status permits `active` or `indexing` while always using the published ACTIVE version; auth envelopes
carry and enforce key expiry; new model-gateway references populate optional `provider_id`; legacy
snapshots fail query embedding clearly but remain usable for text/supplied-vector retrieval; legacy
unknown token counts are nullable without a budget and fail closed with a budget; vector length and
all numeric weights are strictly bounded/finite; all-target failure is 502; new payload tests are
required; runtime idle expiry is documented as a pre-release limitation; and the TEI example uses
`max_batch_size=16`. Coordinator authorized immediate TDD without another approval pause.

### 2026-09-15 shared runtime contract RED

```text
uv run python -m pytest -o addopts= tests/unit/retrieval/test_runtime_contract.py -q
ERROR during collection: ModuleNotFoundError: No module named 'cairn.core.retrieval_runtime'
```

The RED proves the shared lower-layer runtime/config module and compatibility exports do not yet
exist. No production edit preceded this failure.

The minimal shared implementation then exposed and corrected a nested MMR alias round-trip issue.
The same command completed with `3 passed in 1.22s`.

### 2026-09-15 atomic API-key envelope RED

```text
uv run python -m pytest -o addopts= tests/unit/retrieval/test_dataplane_authz.py -q
ERROR during collection: cannot import name 'ApiKeyEnvelope' from cairn.authz.dataplane
```

The RED covers cached restricted-IP enforcement without the legacy IP key, expiry rechecking on
cache hits, and preservation of the database-resolved IP policy when write-through fails.

The atomic envelope implementation completed the same command with `3 passed in 6.05s`.

### 2026-09-15 ModelRef positional compatibility review

Coordinator identified that inserting `provider_id` before `dimension` would reinterpret the public
dataclass's historical fifth positional argument. A regression test now fixes the old positional
order, and the new optional provider identity must be appended at the end. Legacy immutable
snapshots remain unmodified and may require rebuild for ingestion/re-embedding as well as query
embedding once gateway refs carry provider identity.

The positional compatibility test first failed with `dimension == 128` because `provider_id` had
shifted the constructor. Appending the field restored the full contract command to `4 passed in
1.11s`.

### 2026-09-15 public retrieval configuration seam RED

```text
uv run python -m pytest -o addopts= <provider-ref test> <settings-seam test> -q
2 failed: ModelCatalog.get_ref returned provider_id=None; Settings had no retrieval attribute
```

The RED fixes the exact parent live-test environment contract before retrieval runtime composition.

New model-gateway refs now populate provider identity and `Settings` parses the exact JSON endpoint
and tokenizer dictionaries. The shared DTO/config suite completed with `16 passed in 1.28s`.

### 2026-09-15 request DTO RED/GREEN

The first request-contract run failed during collection because `cairn.retrieval` did not exist.
After adding only public DTO validation, the focused command completed with `10 passed in 0.39s`.
Coverage includes query/vector presence, full-text text requirement, candidate/top-k coherence,
boolean/non-finite/out-of-range target weights, finite vector components, and the 65,536 component
request ceiling.

### 2026-09-15 authorization/runtime/search service RED/GREEN

The central service test first failed during collection because retrieval errors/runtime/service did
not exist. The minimal implementation then completed with `12 passed in 1.26s`, proving all-target
authorization before loader/storage access, Redis-only coherent runtime loading, `indexing` serving
published ACTIVE v1, full-text/vector/hybrid supplied-vector paths without embedding, explicit
unsupported/default-degraded behavior, all-target 502 behavior, and nullable legacy token counts
that fail closed when budgeting is requested.

A follow-up malformed-hit RED produced three expected failures: a payload claiming index version 2
while querying ACTIVE v1, a NaN driver score, and an infinite driver score all passed. Per-target
driver validation now checks payload KB/version/chunk identity and finite scores before fusion. The
full service test completed with `15 passed in 1.26s`.

Follow-up scoring/default review added four REDs: single-mode score incorrectly applied sparse
weight, RRF incorrectly applied dense/sparse weights, configured threshold/dedupe were ignored, and
request weights were accepted despite weighted fusion being unsupported. Correcting those semantics
and moving complete hit-shape validation into the isolated target stage produced `19 passed in
1.20s`.

### 2026-09-15 exact read-model token payload RED/GREEN

The focused payload run failed twice: ordinary indexing lacked `token_count`, and manual
re-embedding could not receive the already remeasured count. Both write paths now persist exact
counts; legacy helper behavior remains compatible. The retrieval payload tests plus existing manual
re-embedding tests completed with `9 passed in 2.74s`.

### 2026-09-15 HTTP composition RED

The first attempt exited during collection because the module-level app loaded before unit service
URLs; this was an invalid setup failure and was corrected without production changes. The valid RED
then produced `5 failed, 1 passed`: data/all returned 404 instead of required 401, and `/v1/meta`
lacked role-aware `retrieval_query` capability. Control already returned 404 as required.

Remaining before parent HTTP review: mount route/dependency and role-aware metadata; implement and
test lifecycle-owned endpoint/tokenizer startup with over-budget query rejection and cleanup; enforce
data-role import isolation/route audit/import-linter; then run owned integration and the parent HTTP
acceptance in the exclusive database window. Parent independently added hot-path traps for authz,
catalog, and model-gateway control hydration; pgvector SQL remains allowed.

### 2026-09-15 first concrete HTTP checkpoint

Route and role-aware metadata composition turned the HTTP unit test GREEN with `6 passed in 1.69s`:
control returns 404, data/all return unauthenticated 401, and `/v1/meta` advertises retrieval/hybrid
only where mounted while keeping rerank false.

The parent-owned routed acceptance then completed against the exclusive disposable services:

```text
CAIRN_TEST_USE_EXTERNAL_SERVICES=1
PostgreSQL 127.0.0.1:39170 / Redis 127.0.0.1:38694
uv run python -m pytest -o addopts= tests/integration/test_retrieval_acceptance.py -q
16 passed, 1 Alembic configuration deprecation warning in 15.99s
```

This is the first concrete provider-free public HTTP slice. It covers authenticated pgvector
full-text/vector/hybrid queries in active and rebuilding states, always serving published ACTIVE v1;
all-target authorization ordering; wrong KB/workspace/status/missing runtime rejection; atomic IP
policy behavior; role mounts; and parent traps proving no authz/catalog/model-gateway control
hydration on the warm happy path. It is not final acceptance: query-embedding lifecycle, per-target
runtime/model partial failures, data-role import isolation, static contracts, and owned integration
remain.

### 2026-09-15 post-HTTP implementation checkpoint

Implemented after the first HTTP checkpoint:

- atomic auth envelope now rejects restricted keys when the peer address is unknown (`4 passed`);
- per-target runtime and query-model provider failures preserve successful targets, while
  authorization, foreign workspace, and corrupt runtime identity fail the whole request (`21 passed`);
- query embedding runtime loads declared tokenizer files and TEI/Infinity endpoints, includes
  provider ID in the cache namespace, enforces dialect/model-family identity, rejects prefixed
  over-budget queries without provider calls or truncation, and closes all providers on partial
  startup/cancellation (`3 passed`, then expanded retrieval unit coverage);
- `role=data` uses API-key-only middleware and no longer initializes identity/catalog/platform/
  model-gateway modules; role composition test is GREEN;
- overall retrieval deadline is applied in both HTTP middleware (including authentication) and
  service orchestration; unknown embedding-cache state remains `null` rather than false;
- manual re-embedding now distinguishes normalized embedding-input token count from exact raw
  returned-content token count; only the raw count enters the vector read model while the existing
  normalized catalog commit semantics remain unchanged;
- explicit hybrid/full-text without query text rejects before storage tasks; a vector-only request
  inheriting a hybrid KB default deliberately degrades to one dense search with a `sparse` notice.

Focused retrieval units reached `59 passed in 4.86s` before the final orphan/default-mode case.
The parent HTTP suite remained `16 passed`. Combined parent HTTP + live TEI initially produced
`16 passed, 2 failed` before retrieval at `document.parse`. Systematic diagnosis found the new
`ModelRef.provider_id` UUID was left unconverted in ingestion's JSON binding fingerprint. Converting
that immutable ID to text fixed the isolated live markdown lifecycle test (`1 passed in 6.44s`).

At this handoff, router, provider configuration/lifespan, data-role composition, auth/runtime/search
service, payload token counts, and owned tests are implemented. No further M09 feature expansion is
planned. Parent should independently rerun the final orphan/default-mode unit, both live TEI
parameters, the 16-case HTTP acceptance, route audit/import-linter, and its broader acceptance scope.

### 2026-09-15 immediate coordinator checkpoint

The final orphan/default-mode RED is corrected and the complete owned retrieval unit directory now
reports:

```text
uv run python -m pytest -o addopts= tests/unit/retrieval -q
60 passed in 4.61s
```

The correction rejects explicit `hybrid`/`fulltext` without query text during DTO validation and
degrades an implicit hybrid KB default to one vector search before creating any sparse task.

Latest broader evidence and remaining concrete failures:

- parent provider-free HTTP acceptance remains GREEN at `16 passed in 15.99s` from the prior run;
- combined parent HTTP + live TEI later produced `16 passed, 2 failed` before retrieval because
  ingestion binding-fingerprint JSON did not convert the newly added `provider_id` UUID;
- systematic diagnosis fixed that source and the isolated markdown live lifecycle, including its
  final authorized HTTP query embedding, passed in `6.44s`; semantic has not been rerun after fix;
- query runtime/provider identity, over-budget no-truncation, cleanup, raw response token counts,
  per-target partial failures, unknown cache state, and request deadline are covered by focused units;
- data-role import isolation unit is GREEN; route audit and import-linter have not yet been rerun
  after the last syntax correction;
- targeted Ruff currently reports 33 style/static findings (mostly line wrapping/import ordering in
  new retrieval code/tests, one test subprocess security annotation, and one unused import). These
  are not behavioral failures but remain unresolved at this stop;
- `python -m importlinter` is not a valid entrypoint for the installed package; parent should use the
  repository-supported `uv run lint-imports` command when independently accepting boundaries;
- README has not yet received the public retrieval startup example; exact variables and
  `max_batch_size=16` remain recorded in this document;
- no full backend suite was run. The accepted historical baseline remains 780 passing with two known
  PDF/OCR collection errors.

Per coordinator instruction, implementation stops here for independent parent review/acceptance.

### 2026-09-15 independent acceptance and final static handoff

The parent independently ran the combined bounded behavioral scope after the fingerprint and query
runtime corrections:

```text
138 passed in 50.34s
```

That parent-owned run includes both actual TEI HTTP query-embedding parameters, the complete
16-case public endpoint matrix, owned retrieval units, authz/catalog compatibility, and recovery
regressions. The parent then started the full suite separately. This worker released the exclusive
database window and ran no further database commands.

Static/documentation cleanup made no behavioral changes. The exact public retrieval startup seam
and limitations are now present in both `README.md` and `.env.example`, including provider-ID keyed
TEI/Infinity endpoint JSON, tokenizer-ID keyed file JSON, `max_batch_size=16`, request/runtime/search
deadlines, provider-free full-text/supplied-vector behavior, unsupported advanced features, and the
300-second idle runtime-cache 503 limitation.

Fresh no-database static evidence after formatting the owned Python files:

```text
uv run python -m ruff format <owned Python files>
11 files reformatted, 4 files left unchanged

uv run python -m ruff check <owned Python files and retrieval tests>
All checks passed!

uv run python -m mypy
Success: no issues found in 119 source files

uv run python -c "from importlinter.cli import lint_imports; raise SystemExit(0 if lint_imports() else 1)"
Analyzed 179 files, 916 dependencies; Contracts: 7 kept, 0 broken

uv run python scripts/check_route_authz.py
Audited 51 routes: 8 intentionally public.
Every non-public route declares an authorization dependency.
```

No `tests/integration/test_retrieval.py` was created; the parent-owned integration/acceptance files
exercise the actual bounded HTTP and real-model scope. No full-suite result is claimed by this
worker, and no commit, revert, reset, clean, or database mutation was performed during this final
static handoff.
