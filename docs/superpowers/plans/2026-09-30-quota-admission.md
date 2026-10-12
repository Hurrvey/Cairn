# Quota admission implementation and acceptance plan

Status: Task1 active after October8 SDK acceptance. Design approved with scoped registration semantics in
`../specs/2026-09-30-quota-admission-design.md`. Coordinator plans; fixed developer implements;
same reviewer reviews. Do not start during a frozen backend regression.

## Task 1 — persistent account/allocation facade and migration

Platform owns account/allocation tables and DTO/error vocabulary. Account identity is
workspace/kind/fixed capacity period. Allocation identity is workspace/resource-kind/resource-ID;
quantities include known bytes or explicit unknown bytes. No cascading deletion silently removes
charges. Backfill KB/doc rows including soft deletes. New workspaces explicitly bootstrap all
capacity accounts in the workspace-creation transaction; limit0 is distinct from null. Versioned limit updates and audit
are atomic. Reject invalid/negative/bool/oversized configuration values.

Tests: backfill exact totals, unknown legacy bytes, idempotent allocate/release, concurrent
different-resource allocations to one workspace, finite/infinite/zero/downward changes, tenant
isolation. Facade accepts caller session and locks account kinds in sorted order; no catalog
ORM imports or callback into owner services.

October8 foundation review: public operations express resource identities and absolute target
quantities (allocate KB/document, replace size, release document/KB), not arbitrary counter
increments. Domain locks precede all sorted account locks, which precede allocation locks.
Keep released tombstones or equivalent lifecycle fencing so replay cannot resurrect occupancy.
Same resource/workspace/KB mismatch is rejected, never silently reassigned; subtraction must
not clamp negative totals to zero. First slice reserved stays0.

Add a distinct owner-verified unknown-size resolution operation: unknown→known records actual
existing occupancy even when this reveals over-limit usage. This is repair, not new allocation.
Finite incomplete accounts still reject positive new/replacement allocations. Migration derives
totals directly from all actual rows including soft deletes, not KB counters. Recognized legacy
quota fields with invalid/bool/negative/overflow values fail explicitly, never become unlimited.
DB constraints cover identity/kind/state/nonnegative quantities/version; validate bigint bounds
before arithmetic. Missing accounts in ordinary admission fail closed as ledger unavailable;
no lazy zero initialization. Platform bootstrap is called explicitly by workspace creation and
test fixture setup; migration covers existing workspaces. An extra initialized boolean does
not replace checking every required account. Maintenance repair alone may fill missing accounts
after reading real resources under the write freeze.

Deployment boundary approved by the reviewer: Task1 migration only establishes/backfills the
ledger; configured limits are not yet enforced. Task2 activation requires a maintenance write
freeze covering API and workers (replacement/purge included), draining/stopping old writers,
upgrading every writer, then checking/repairing ledger against actual resource rows with no
concurrent writes before enabling enforcement. An explicit activation operation must require
these preconditions; routine background counter reset is not a substitute. Mixed-version
rolling writes are not supported by this first activation workflow.

## Task 2 — all catalog admission and release paths

Current integration points verified by coordinator:

- `CatalogService.create_kb`: charge KB allocation last after existing domain/permission work,
  within same transaction as KB and ownership grants.
  The October8 ownership correction must land first: its initial owner grant uses the caller
  session instead of the former independent generic-grant transaction. Quota refusal must roll
  back that grant, owner permission-version bump, KB row and audit together; cache invalidation
  stays strictly after the successful outer commit.
  Explicitly flush all domain/grant/audit SQL before acquiring quota account locks.
- `_register_upload_locked`: charge document/known-byte allocation last after document/run/task
  and current catalog counters. Same-KB duplicate returns without a new allocation.
- `upload_document`: nested SQL savepoint surrounds registration/final charge; on refusal undo
  savepoint (and account locks), then perform existing canonical-object compensation under KB
  lock. Record commit-failure/orphan limitations; do not invent ownership-free deletion.
  Start the savepoint around the entire `_register_upload_locked`, including its first building
  version/status changes. Flush domain/run/task/counter SQL before final quota charge.
- `CatalogIngestionFacade.start_revision`: document→KB locks already exist; update current
  allocation to new size as final admission in same revision/task transaction. Preserve old
  quantity if registration fails. Fix/verify catalog bytes counters if required by this path.
- `handle_document_purge`/`handle_kb_purge`: verified external cleanup precedes SQL allocation
  release. Include all remaining child allocations on KB purge; duplicate/racing purge cannot
  release twice. Soft delete alone leaves quota occupancy unchanged.
  Finalize and flush domain deletion before release, in the same transaction. KB child release
  enumerates allocations by stored KB ownership even after document cascades. Existing separate
  authz cleanup transaction/Redis work remains before account locks; do not expand this slice
  into a rewrite of that pre-existing authorization cleanup path.

Tests: blocked upload commits no doc/run/task; concurrent uploads across KBs; hash dedup;
replacement grows/shrinks; deleted KB versus upload; purge failure retains capacity; purge success
restores capacity; nested savepoint releases account locks before network compensation. Reuse
existing canonical-upload race tests and actual local/S3 storage integration.
Specifically reject the first upload without leaving an empty building index version; prove
another KB can acquire quota accounts while failed-upload object compensation is awaiting I/O.
Replacement rejection keeps old revision/source/counters; purge SQL failure keeps allocation
until one successful retry releases it.

## Task 3 — quotas API and Settings UI

GET/PATCH `/v1/quotas` admin-only, current workspace only. Response exposes implemented kinds,
limit/used/unknown/pending-delete/available/config-version; no unsupported fields labelled active.
Finite unknown-byte account is incomplete and refuses positive byte increments. Save optimistic
version; return stable quota error with kind/current/requested/limit facts (serialized explicitly,
not just CairnError.context which currently is not included in problem JSON).

UI accepts blank=unlimited and0=disabled additional capacity, labels source bytes accurately,
shows pending cleanup and over-limit preserved data. Include changed limit success/error states,
keyboard use, zh/en and ordinary-user denial. Regenerate OpenAPI/web/SDK schemas together.

Existing UI seam is `features/settings/SettingsView.vue` with a separate quota section/form,
using its current Field/Input/Notice/Section components and `api/admin` facade. Preserve the
distinction between per-key abuse RPM (`default_rate_limit_rpm`) and workspace retrieval RPM;
neither form may imply it configures the other. Blank quota input maps to null and numeric0
remains0. Approved quota wire representation is decimal string/null, not JSON number; include
all growing counters/config versions and safe quota error facts. Internal int64 stays unchanged,
UI compares BigInt or strings, rejects invalid input and avoids Number conversion/formatting.
API currently
retains only known problem fields in ApiError; explicitly map safe quota facts if UI displays
current/requested/limit instead of depending on discarded generic context.

## Task 4 — workspace retrieval RPM

Follow the October8 publication resolution in the design: PG account/version + atomic audit/
outbox, publisher fresh reads and fixed30s projection lease with5s normal refresh target. Lua
checks Redis TIME, explicit mode/schema/expiry and charges workspace-minute counters independent
of configuration version. Missing/invalid/expired/Redis failure is503, never implicit unlimited.
Finite initialization after state loss waits to the next minute boundary; do not claim exact
history across undetected Redis counter rollback. UI distinguishes configured, observed and
last-published versions, and effective/pending/unavailable states.

Publish explicit quota config through control/maintenance to a data-safe projection. Use Redis
Lua atomic charge; batch cost known before any query work, query/MCP search/Dify cost1, do not
charge adapters twice. Include fail-closed finite/unknown config503 behavior and Retry-After429.
Do not import quota SQL services into retrieval or MCP. Missing projection must be repairable
without a per-request SQL connection.

Tests: two principals/replicas share one workspace budget; batch charges atomically; invalid
requests/adapters follow documented charging rule; Redis loss and configuration updates; existing
per-key limit remains distinct; Dify/MCP envelopes retain failures and retry metadata.

## Combined gate

Ruff/mypy/import contracts/route auth/schema drift, affected real PG/Redis/object-store tests,
frontend unit/build and real browser limit→upload refusal→raise→upload→purge→capacity return.
Full backend after source stabilizes; keep all explicit env-dependent test evidence. Separate
scope from vector/token reservation, blocked worker recovery and strict pre-write upload saga.
