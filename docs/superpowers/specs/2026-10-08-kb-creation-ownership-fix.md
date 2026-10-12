# Knowledge-base creator ownership — reviewed defect correction

Status: approved direction by persistent design advisor; fixed developer implements before SDK
live create/upload/query lifecycle acceptance. This is a product correction, not a client workaround.

## Reproduced by source review; behavior tests required first

The create route requires kb:create. API key resolver supports workspace-wide kb:create as the
intersection with its owner's permissions. Catalog currently writes actor.id to owner_user_id
and invokes generic grant(actor, new KB). For an API key, actor.id is a key UUID, not a user FK.
For an ordinary user with creation permission, generic grant rejects the new KB because the actor
does not yet have its manage permission. Administrator session tests hide both problems.

## Correct behavior

- Owner is actor.id for user sessions, actor.owner_user_id for API keys. Owner must be an active
  user in the same workspace. Restricted/invalid/no-create actors are rejected before creation.
- Catalog receives owner identity through an authz/identity facade, not cross-module ORM.
- Initial owner grant is part of the same SQL transaction as KB creation, audit and later quota
  admission. Do not open a second independent grant transaction or leave orphan grants on rollback.
- Add a narrow creation bootstrap facade accepting caller session and the newly created KB ID.
  It is callable only from the authorized creation path, checks supplied actor/owner identity,
  and creates initial owner permissions without requiring pre-existing manage on the new resource.
- Ordinary grant/revoke permission rules remain unchanged. No role elevation, no arbitrary
  resource grant helper available to callers, no generic grant bypass outside creation.
- Audit actor remains actual user/key actor, with owner separately recorded if useful. Creating
  a resource on behalf of an owner does not make the API key into a user or administrator.
- Subsequent API-key access still intersects requested scopes with owner grants. A create-only
  key does not gain query/read/write/manage. An explicit key KB allowlist is not automatically
  extended to the new KB. Owner sees/manages the new KB through its initial grant.

## Acceptance

Real PostgreSQL tests first demonstrate failures for ordinary user kb:create and API key creation.
Then prove correct user FK, initial owner grant, owner permission-version/cache invalidation,
creation-only key denial on reads/mutations, scoped key boundaries, cross-workspace/missing/inactive
owner rejection and no-create rejection. Induce failure after bootstrap and verify KB+grant+audit
all roll back. Preserve existing administrator create, break-glass, delegated-grant and key∩owner
tests. Real SDK create operation must use its normal key authentication, not an injected session
that avoids the broken path.

## Post-commit cache visibility resolution

DataPlaneAuthz caches envelopes by key hash and does not compare cached owner perm_version with
current DB state. Bumping the owner version alone does not refresh existing key envelopes.
After successful KB/bootstrap transaction commit, call a narrow authz facade that pages through
the owner's non-revoked key hashes and invalidates their cache entries using the existing delete/
publish seam. Never revoke those keys, alter scopes, append to explicit KB allowlists, or perform
cache operations before commit. Re-resolving a scoped key must preserve its original restriction.

Cache invalidation failure after committed creation must be logged/metriced safely, not turn the
successful create into500 and encourage duplicate creation. Existing60s TTL remains the documented
fallback. Concurrent stale resolve→late cache SET is an existing race; this narrow fix does not
claim linearizable permission visibility under Redis failure/concurrency. A generation/CAS scheme
would be a separate cross-cutting authorization change.

Tests prewarm broad and explicit-allowlist keys, create KB, then immediately verify normal broad
scopes can access and explicit/create-only keys still cannot. Test rollback performs no cache
invalidation and post-commit invalidation outage does not convert creation into a failed mutation.

## October8 code-review refinements

Initial grant and KB creation audit entries explicitly pass actor_type, not the AuditService
default user. Owner cache invalidation pages through hashes, reports delete/publication failure
without logging hashes/credentials, and has one small total deadline; postcommit cleanup cannot
hold the response indefinitely.

New UUID KBs have no ACTIVE runtime to publish or invalidate. Remove the redundant create-only
publish_runtime call, which currently can fail a Redis delete after the SQL creation committed.
Later index activation already publishes the first runtime. Do not change generic runtime
invalidation semantics used for deletion or version changes. A real cache-delete failure test
must demonstrate that the newly committed KB is returned successfully, with cache degradation
observable rather than a misleading failed mutation.
