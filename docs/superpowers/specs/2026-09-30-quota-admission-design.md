# Workspace quota admission — design for review

Status: approved with scope revision by `/root/design_advisor`; first-slice resolutions below supersede conflicting upload proposals. Implementation follows SDK acceptance. Owner: coordinator `/root`; reviewer:
persistent `/root/design_advisor`; implementer: persistent `/root/developer` only.

## Separate accounting purposes

The gateway usage report is best-effort and may lose events. It is never authoritative for
quota enforcement. Quota admission uses durable PostgreSQL accounts and operation identities.
Redis supplies reconstructible configuration/projections and atomic retrieval rate windows.

First deliverable: atomic registered-resource admission for knowledge-base count, document count, registered original-source bytes,
administrator quota management UI/API, plus workspace retrieval RPM. Subsequent deliverables
add vector occupancy, embedding-attempt budgets and resumable blocked workers. Do not display
unimplemented quota fields as enforced.

## Capacity semantics

- `knowledge_bases`: allocated KB rows including soft-deleted KBs until successful purge.
- `documents`: allocated document rows including pending deletion until successful purge.
- `storage_bytes`: sum of declared original-source bytes for registered documents. This is a
  logical source allowance, not total disk/S3 occupancy; parsed/chunk/embedding artifacts,
  old revisions, temporary spools and multipart upload buffers are not included.
- Same-KB hash dedup does not allocate a second document or charge again. Same content in another
  KB is a separate logical document and is charged. Null legacy sizes remain explicitly unknown;
  migration/reporting must not claim exact source-byte enforcement while unknown rows exist.
- Limit null means unlimited,0 means no additional allocation. Lowering a limit below occupancy
  preserves existing data/retrieval; only new positive allocations are refused.
- Physical cleanup success, not delete request, releases capacity. A failed purge retains its
  charge; repeated purge/release is idempotent. UI shows pending-delete occupancy separately.

## Data model and ownership

Platform owns `quota_account(workspace_id, kind, period, used, reserved, limit, version)`;
capacity uses a fixed period, later token allowances use UTC month. A unique resource allocation
or operation ledger ties charges to KB/document identity and records quantities/state. Avoid
foreign keys cascading allocations away before account decrement; release is explicit.

Catalog controls resource identity, permission and lifecycle. A platform facade accepts the
caller's AsyncSession and primitive IDs/quantities; it does not import catalog ORM or open a
second transaction. Backfill existing accounts/allocations in migration from actual rows,
including deleting rows; record unknown source sizes explicitly. New registration with an
active finite source-byte limit requires a known verified size.

Account locks come after existing domain locks; acquire multiple kinds in fixed lexical order.
Quota never calls catalog/tasks while holding account locks. Limit updates lock accounts, audit
and write a wakeup/outbox marker; task wakeup happens after commit in an independent transaction.
Do not add workspace→KB locking to one path while other paths lock KB→workspace.

## Upload admission and external side effects

### Approved first-slice resolution

The first deliverable enforces registered logical occupancy atomically, not pre-write physical
occupancy. Retain the existing KB-locked canonical-source write and cleanup structure. Optional
nonlocking preflight can reject obvious over-limit uploads early, but only the final transaction
charge is authoritative. Execute document/run/task registration first, then lock quota accounts
and apply the allocation at the end of the same SQL transaction. task.enqueue may touch an
existing workspace-runtime row, so it must occur before account acquisition too.

Quota refusal rolls back all domain registration/task rows; no document becomes registered
without its charge. A source object may already have been written and must be cleaned with the
existing reference/ownership protection. Scope registration and final charge in a nested SQL
savepoint so refusal rolls it back and releases newly acquired account locks before external
cleanup while retaining the original KB lock. Do not await object deletion holding account locks,
and do not release the KB lock then blindly delete a content-hash key another upload may use.

This is a recorded staging deviation from the broad "before every external operation" goal:
source-byte allowance covers registered originals only, not orphan/spool/derived physical space.
Commit-time exceptions and ambiguous cleanup need explicit orphan reconciliation; the current
inner try around registration does not cover an outer transaction's commit failure. Preserve
that limitation honestly until the durable operation workflow below is implemented.

If legacy unknown-size documents exist, a finite source-byte account is **incomplete**. Saving
the limit is permitted, but all positive byte allocations are refused until sizes are verified;
zero/negative adjustments are allowed. Replacement is a required admission path: compare the
current resource allocation to the new verified size, charge its positive delta atomically,
and release a negative delta only on successful new-revision registration.

### Subsequent strict pre-write reservation workflow (not first-slice acceptance)

Current upload holds a KB lock, writes a canonical content-addressed object, then registers the
document/task. Strict pre-write admission cannot be bolted on by holding quota accounts across
S3 calls. Introduce a durable upload operation:

1. Short transaction validates KB/owner and canonical key/dedup, fixes operation/document UUID,
   locks accounts last and reserves one document plus verified source bytes. Persist hash, key,
   sizes, generation and state; same operation retry resolves its existing reservation.
2. Commit before S3 I/O. Mark dispatch through a fenced state transition. Write/verify the
   canonical source using existing bounded storage methods.
3. Short transaction locks operation/domain identity, confirms KB still accepts the upload,
   registers document/run/task and converts reservation to used atomically.
4. A known pre-write rejection releases reservation. Crash/timeout/uncertain external result
   retains an `uncertain` reservation for reconciliation; TTL alone never releases it.

Reconciliation can confirm registration, retry completion or purge an orphan only after proving
operation ownership and no registered source reference. If a user deletes the KB while upload is
in flight, registration cannot resurrect it. Two uploads of the same canonical key must converge
without one deleting the other's committed object. Registration-only API paths (already stored
objects) use the same admission rules but no external write transaction.

This lifecycle change is the main implementation risk; reviewer must settle exact operation
identity/fencing and recovery behavior before developer changes upload flow. Existing canonical
upload race/lifecycle tests remain required and should be expanded, never replaced by mock tests.

## Capacity release and reconciliation

Document purge commits domain deletion and allocation release together after external cleanup is
verified. KB purge releases all remaining child allocations and the KB allocation once; a racing
document purge cannot subtract twice. Reconciliation repairs derived account totals from the
allocation ledger while holding its account lock, preserving open reservations. Comparing to
domain rows is an audit/repair workflow through owner facades, not an unconditional counter reset.

## Workspace retrieval RPM

Add a dedicated workspace limit distinct from existing per-key abuse limiting. Publish explicit
unlimited/finite configuration in Redis with version and a refresh path. Missing projection is
unknown, never unlimited. Lua atomically charges the complete request cost: batch N queries=N;
normal query/Dify/MCP search=1. Do not double-charge query subcalls inside batch or MCP adapter.
Discovery/document resources are not retrieval invocations.

Limited-account Redis failure returns503, no false429 and no PostgreSQL read on retrieval hot
path. Existing normal limiter may retain fail-open behavior, but it cannot satisfy the strict
workspace quota. An operator fail-open option, if introduced, must be named and report lost
enforcement. Finite429 replies retain Retry-After/request ID, including Dify/MCP error shapes.

## Interfaces and UI

### October8 RPM publication resolution

Same reviewer approved PG account limit/version as the sole configuration authority. Import
legacy Workspace.quota fields once; no dual-write or Redis-miss fallback to old JSON. Update,
audit and workspace/version outbox commit together. Return configured_version and publication
state, not an assertion of immediate Redis effectiveness.

Publisher treats outbox as a workspace refresh signal and rereads current PG configuration.
Publish explicit finite/unlimited projection with schema,workspace,version,issued_at,valid_until.
Normal refresh target5s, lease30s fixed from that PG snapshot; retry cannot extend an old
snapshot. Redis compare-version Lua rejects older versions/snapshots; equal-version fresh reads
may renew. Observe clock skew and reject invalid leases. A late old snapshot after Redis loss
can survive at most its original lease; do not promise cross-store linearizability.

Admission Lua uses Redis TIME to validate projection and charge the entire batch against a
workspace UTC-minute counter. Counter identity excludes configuration version, so limit changes
never reset current usage. Missing/expired/corrupt projection or Redis failure returns503 even
when the last known policy was unlimited. Explicit valid unlimited remains distinct.

On known projection/counter loss, finite-policy recovery waits until the next minute boundary
(`not_before`) before admission. A conservative missing-to-finite initialization barrier is
acceptable. Quota keys must not be evicted. Undetected Redis rollback/failover counter loss is
not covered by an exact historical-usage guarantee; stronger durable metering is later scope.
Never rebuild authoritative counters from best-effort usage events.

Admin reads report configured_version, currently observed Redis version/expiry and diagnostic
last-published receipt separately. Effective/pending/unavailable reflects current observation;
an old successful PG receipt is not proof Redis still holds that policy. Outbox claims/retries
are bounded and idempotent; pending messages are not silently removed by retention.

Tests include failed publish with original-lease expiry, out-of-order events, no retry lease
extension, explicit unlimited versus absent, same-minute downward changes, atomic batch cost,
shared-replica counters and Redis-loss next-window recovery. Preserve Dify/MCP error contracts
and keep PostgreSQL absent from the retrieval hot path.

`GET /v1/quotas` returns each implemented kind's limit,used,reserved,available,unknown and
pending-delete occupancy. `PATCH /v1/quotas` requires platform:settings, validates nonnegative
finite integer quantities/null and optimistic configuration version, records audit. Capabilities state
which kinds are enforced. The Settings quota section explains source-byte scope and pending
deletion; lowering limits never promises immediate physical disk reclamation.

October8 wire decision: this new quota API encodes every growing integer quantity/version as
a canonical decimal string; unlimited limit is null. Internally use nonnegative signed-int64.
GET quantities, PATCH limit/expected version and safe quota error facts share that representation.
Accept digits only, at most19 digits and value<=9223372036854775807; reject JSON numbers/bools,
signs, fractions, exponent notation, empty strings and leading zeros except0. Output has no leading zeros. UI maps blank
input explicitly to null, retains string0, and uses BigInt/string comparison instead of Number.
Do not apply floating-point formatting to large exact quantities. This is a local quota contract;
existing unrelated integer APIs remain unchanged. Regenerate OpenAPI/web/SDK types when it lands.

## Acceptance

- Concurrent uploads across different KBs cannot exceed one workspace allowance; duplicate retry
  does not double charge; rejected admission commits no new document/run/task/allocation charge.
  Compensation deletes only a source proven exclusive to this attempt and unreferenced; existing
  or shared canonical objects remain intact. Object existence alone does not indicate failed admission.
- First-slice fault tests prove transactional rollback, idempotent charging/release and safe
  compensation under the existing KB lock. Ambiguous outer commit and durable orphan recovery
  remain explicit limitations pending the later upload-operation saga; do not claim those crash
  windows are fully recovered by the registration-only slice.
- Deletion does not release early; purge/retry/KB-delete races converge to exact ledger totals.
- Limit0/unlimited/downward change/unknown legacy bytes have explicit tested behavior.
- Storage/network/DB/Redis faults preserve the documented safety/availability split.
- Workspace RPM spans replicas, charges batch atomically, recovers projection loss, handles Dify
  and MCP correctly, and never acquires catalog/PG dependencies on retrieval hot path.
- Real PG/Redis/object storage concurrency tests, existing upload lifecycle suite, frontend form
  tests and actual browser over-limit→raise-limit→upload→purge→capacity-return sequence.

## Later quota slices (not implemented by first deliverable)

Vector quota counts physical embedded points in ACTIVE/BUILDING/RETIRED namespaces, with stable
point-set difference reservations and release only after verified deletion. Parent-only context
chunks are excluded. Blue/green rebuild reserves peak coexistence capacity.

Monthly embedding token quota measures tokenizer-counted prepared input for each actual outbound
attempt, not supplier invoice truth. Cache hits are free; retries reserve independently; uncertain
dispatch retains conservative charge. Worker blocked/resume must preserve artifacts and fencing,
separate failure budget from monotonically increasing attempt, include blocked in task dedup and
revalidate revision/index/generation on wakeup. No exactly-once supplier billing claim.
