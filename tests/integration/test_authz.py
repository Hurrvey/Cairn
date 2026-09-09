"""Authorization — TC-M02-01 … TC-M02-20.

The centrepiece is the key ∩ owner intersection (FR-B-08). Everything else in
this module is ordinary CRUD; that one property is what makes API keys safe to
hand out.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from cairn.authz.errors import GrantExceedsOwner, PlatformCapabilityNotGrantable
from cairn.authz.model import ADMIN_PLATFORM_CAPABILITIES, Principal
from cairn.authz.service import ApiKeySpec, AuthzService, GrantSpec
from cairn.core.db import session_scope, transaction
from cairn.core.errors import PermissionDenied
from cairn.core.time import utcnow
from cairn.identity.service import IdentityService

STRONG = "Correct-Horse-Battery-9"


@pytest.fixture
def authz() -> AuthzService:
    return AuthzService()


@pytest.fixture
async def admin(identity: IdentityService, monkeypatch: pytest.MonkeyPatch) -> Principal:
    monkeypatch.delenv("CAIRN_INITIAL_ADMIN_PASSWORD", raising=False)
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    login = await identity.login("admin", result.password)
    assert login.change_token is not None
    await identity.complete_initial_setup(
        login.change_token, current_password=result.password, new_password=STRONG
    )
    principal = await AuthzService().principal_for_user(result.user_id)
    assert principal is not None
    return principal


@pytest.fixture
async def member(identity: IdentityService, admin: Principal) -> Principal:
    from cairn.identity.dto import CreateUserSpec

    user, _ = await identity.create_user(admin, CreateUserSpec(username="wei", role="user"))
    principal = await AuthzService().principal_for_user(user.id)
    assert principal is not None
    return principal


async def _fake_kb(workspace_id: UUID) -> UUID:
    """A knowledge base id to grant against.

    `knowledge_base` does not exist until Phase 2, so grants reference a plain
    UUID. That is exactly how the grant table works — `resource_id` carries no
    foreign key, because a grant may outlive the resource it names.
    """
    return uuid4()


# --- TC-M02-01 … 03: grant resolution ----------------------------------------


async def test_grants_union_across_multiple_rows(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-01."""
    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin,
        GrantSpec("user", member.id, "knowledge_base", ["kb:query"], resource_id=kb),
    )
    await authz.grant(
        admin,
        GrantSpec("user", member.id, "knowledge_base", ["kb:write"], resource_id=kb),
    )

    resolved = await authz.principal_for_user(member.id)
    assert resolved is not None
    assert resolved.resource_permissions[kb] == frozenset({"kb:query", "kb:write"})


async def test_expired_grants_are_excluded(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-02 — expiry is enforced by the resolution query, so a window closes
    exactly when it says it will, with no sweeper involved."""
    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin,
        GrantSpec(
            "user",
            member.id,
            "knowledge_base",
            ["kb:read"],
            resource_id=kb,
            expires_at=utcnow() - timedelta(seconds=1),
        ),
    )
    resolved = await authz.principal_for_user(member.id)
    assert resolved is not None
    assert kb not in resolved.accessible_kb_ids


async def test_revoked_grants_are_excluded(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-03."""
    kb = await _fake_kb(admin.workspace_id)
    grant = await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:read"], resource_id=kb)
    )
    await authz.revoke_grant(admin, grant.id)

    resolved = await authz.principal_for_user(member.id)
    assert resolved is not None
    assert kb not in resolved.accessible_kb_ids


# --- TC-M02-04 / 05: the intersection ----------------------------------------


async def test_key_permissions_are_the_intersection_with_the_owner(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-04 / FR-B-08 — the property this whole module is built around."""
    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:query"], resource_id=kb)
    )
    owner = await authz.principal_for_user(member.id)
    assert owner is not None

    _key, raw = await authz.create_api_key(
        owner, ApiKeySpec(name="agent", scopes=["kb:query"], kb_ids=[kb])
    )

    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.core.cache import InMemoryCache

    principal = await DataPlaneAuthz(InMemoryCache()).authenticate_api_key(raw)
    assert principal.type == "api_key"
    assert principal.can_query_kb(kb)
    # The owner never held kb:write, so the key cannot either.
    assert principal.can("kb:write", kb) is False


async def test_revoking_the_owners_access_immediately_narrows_their_key(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-05 — no cascade update, no key bookkeeping, no window.

    This is the payoff for computing the intersection at authentication time
    rather than storing it: an orphaned over-privileged key is unreachable, not
    merely unlikely.
    """
    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.core.cache import InMemoryCache

    kb = await _fake_kb(admin.workspace_id)
    grant = await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:query"], resource_id=kb)
    )
    owner = await authz.principal_for_user(member.id)
    assert owner is not None
    _, raw = await authz.create_api_key(
        owner, ApiKeySpec(name="agent", scopes=["kb:query"], kb_ids=[kb])
    )

    dataplane = DataPlaneAuthz(InMemoryCache())
    before = await dataplane.authenticate_api_key(raw)
    assert before.can_query_kb(kb)

    await authz.revoke_grant(admin, grant.id)

    # Fresh cache stands in for the 60 s TTL having elapsed.
    after = await DataPlaneAuthz(InMemoryCache()).authenticate_api_key(raw)
    assert after.can_query_kb(kb) is False


async def test_a_key_cannot_be_issued_beyond_the_owners_permissions(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """Rejected up front rather than silently narrowed: a key that quietly does
    less than requested is worse than a clear error."""
    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:query"], resource_id=kb)
    )
    owner = await authz.principal_for_user(member.id)
    assert owner is not None

    with pytest.raises(GrantExceedsOwner):
        await authz.create_api_key(
            owner, ApiKeySpec(name="too-much", scopes=["kb:manage"], kb_ids=[kb])
        )


# --- TC-M02-06 … 08: the permission model ------------------------------------


async def test_kb_query_is_separable_from_kb_read(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-06 / FR-B-07 — an agent's key should search a knowledge base
    without being able to enumerate and export it."""
    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:query"], resource_id=kb)
    )
    resolved = await authz.principal_for_user(member.id)
    assert resolved is not None

    assert resolved.can("kb:query", kb) is True
    assert resolved.can("kb:read", kb) is False


async def test_kb_manage_implies_the_narrower_permissions(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """Implication resolves at check time. The stored grant still says exactly
    what was granted, so revoking kb:manage leaves nothing behind."""
    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:manage"], resource_id=kb)
    )
    resolved = await authz.principal_for_user(member.id)
    assert resolved is not None

    assert resolved.can("kb:query", kb)
    assert resolved.can("kb:read", kb)
    assert resolved.can("kb:write", kb)
    assert resolved.resource_permissions[kb] == frozenset({"kb:manage"})


async def test_admin_holds_platform_capabilities_implicitly(admin: Principal) -> None:
    """TC-M02-07."""
    for capability in ADMIN_PLATFORM_CAPABILITIES:
        assert admin.can(capability), capability


async def test_platform_capabilities_cannot_be_granted(
    authz: AuthzService, admin: Principal, member: Principal
) -> None:
    """TC-M02-08 / FR-B-05 — there is no accumulating your way to administrator."""
    with pytest.raises(PlatformCapabilityNotGrantable):
        await authz.grant(admin, GrantSpec("user", member.id, "workspace", ["platform:users"]))


async def test_a_key_never_carries_platform_capability(
    authz: AuthzService, admin: Principal
) -> None:
    """An administrator's key must not be able to create users."""
    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.core.cache import InMemoryCache

    _, raw = await authz.create_api_key(admin, ApiKeySpec(name="admin-key", scopes=["kb:query"]))
    principal = await DataPlaneAuthz(InMemoryCache()).authenticate_api_key(raw)

    assert principal.role == "user"
    for capability in ADMIN_PLATFORM_CAPABILITIES:
        assert principal.can(capability) is False


# --- TC-M02-09 … 11: break-glass ---------------------------------------------


async def test_break_glass_requires_a_substantive_reason(
    authz: AuthzService, admin: Principal
) -> None:
    """TC-M02-11."""
    from cairn.authz.errors import BreakGlassReasonRequired

    with pytest.raises(BreakGlassReasonRequired):
        await authz.open_break_glass(admin, uuid4(), "oops")


async def test_break_glass_grants_time_boxed_access_and_audits(
    authz: AuthzService, admin: Principal
) -> None:
    """TC-M02-10 / FR-B-10."""
    kb = uuid4()
    grant = await authz.open_break_glass(
        admin, kb, "Investigating retrieval incident INC-2026-0814 at the owner's request."
    )

    assert grant.is_break_glass is True
    assert grant.expires_at is not None
    assert grant.permissions == ["kb:read", "kb:query"]

    resolved = await authz.principal_for_user(admin.id)
    assert resolved is not None and resolved.can("kb:read", kb)

    async with session_scope() as session:
        rows = (
            await session.execute(
                text("SELECT action, detail FROM audit_log WHERE action = 'break_glass.open'")
            )
        ).all()
    assert len(rows) == 1
    assert "INC-2026-0814" in rows[0][1]["reason"]


async def test_break_glass_expires(authz: AuthzService, admin: Principal) -> None:
    kb = uuid4()
    grant = await authz.open_break_glass(
        admin, kb, "Investigating retrieval incident INC-2026-0814 at the owner's request."
    )
    async with transaction() as session:
        await session.execute(
            text("UPDATE resource_grant SET expires_at = now() - interval '1 minute' WHERE id=:id"),
            {"id": grant.id},
        )

    resolved = await authz.principal_for_user(admin.id)
    assert resolved is not None
    assert resolved.can("kb:read", kb) is False


async def test_a_non_admin_cannot_open_break_glass(authz: AuthzService, member: Principal) -> None:
    with pytest.raises(PermissionDenied):
        await authz.open_break_glass(
            member, uuid4(), "I would like to read this knowledge base please."
        )


# --- TC-M02-12 … 16: API keys ------------------------------------------------


async def test_key_plaintext_is_returned_once_and_only_the_hash_is_stored(
    authz: AuthzService, admin: Principal
) -> None:
    """TC-M02-12 / TC-M02-13 / FR-B-13."""
    key, raw = await authz.create_api_key(admin, ApiKeySpec(name="k", scopes=["kb:query"]))

    assert raw.startswith("cairn_sk_live_")
    assert len(raw) > 40

    async with session_scope() as session:
        stored = (
            (
                await session.execute(
                    text("SELECT key_hash, key_prefix, last_four FROM api_key WHERE id = :id"),
                    {"id": key.id},
                )
            )
            .mappings()
            .first()
        )

    assert stored is not None
    assert raw not in stored["key_hash"]
    assert len(stored["key_hash"]) == 64
    assert raw.startswith(stored["key_prefix"])
    assert raw.endswith(stored["last_four"])


async def test_revocation_takes_effect_immediately(
    authz: AuthzService, admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-M02-16 — the TTL bounds ordinary permission drift, but a revoked key
    has to stop working now, not in 60 seconds."""
    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.authz.errors import ApiKeyInvalid
    from cairn.core.cache import InMemoryCache

    key, raw = await authz.create_api_key(admin, ApiKeySpec(name="k", scopes=["kb:query"]))

    cache = InMemoryCache()
    dataplane = DataPlaneAuthz(cache)
    await dataplane.authenticate_api_key(raw)  # warms the cache

    monkeypatch.setattr("cairn.authz.service.get_dataplane_authz", lambda: dataplane)
    await authz.revoke_api_key(admin, key.id)

    with pytest.raises(ApiKeyInvalid):
        await dataplane.authenticate_api_key(raw)


async def test_an_expired_key_is_rejected(authz: AuthzService, admin: Principal) -> None:
    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.authz.errors import ApiKeyInvalid
    from cairn.core.cache import InMemoryCache

    key, raw = await authz.create_api_key(
        admin,
        ApiKeySpec(name="k", scopes=["kb:query"], expires_at=utcnow() - timedelta(minutes=1)),
    )
    assert key is not None

    with pytest.raises(ApiKeyInvalid):
        await DataPlaneAuthz(InMemoryCache()).authenticate_api_key(raw)


async def test_a_malformed_key_is_rejected_without_a_database_lookup(
    authz: AuthzService,
) -> None:
    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.authz.errors import ApiKeyInvalid
    from cairn.core.cache import InMemoryCache

    with pytest.raises(ApiKeyInvalid):
        await DataPlaneAuthz(InMemoryCache()).authenticate_api_key("not-a-cairn-key")


async def test_deleting_a_user_revokes_their_keys_and_grants(
    authz: AuthzService, identity: IdentityService, admin: Principal, member: Principal
) -> None:
    """A key that outlives its owner is a credential nobody is accountable for."""
    from cairn.authz.dataplane import DataPlaneAuthz
    from cairn.authz.errors import ApiKeyInvalid
    from cairn.core.cache import InMemoryCache

    kb = await _fake_kb(admin.workspace_id)
    await authz.grant(
        admin, GrantSpec("user", member.id, "knowledge_base", ["kb:query"], resource_id=kb)
    )
    owner = await authz.principal_for_user(member.id)
    assert owner is not None
    _, raw = await authz.create_api_key(owner, ApiKeySpec(name="k", scopes=["kb:query"]))

    await identity.delete_user(admin, member.id)

    with pytest.raises(ApiKeyInvalid):
        await DataPlaneAuthz(InMemoryCache()).authenticate_api_key(raw)


# --- rate limiting -----------------------------------------------------------


async def test_rate_limit_headers_and_enforcement(admin: Principal) -> None:
    """TC-M02-17 / TC-M02-18 / FR-I-07."""
    from cairn.authz.ratelimit import RateLimiter
    from cairn.core.cache import InMemoryCache
    from cairn.core.errors import RateLimitExceeded

    limiter = RateLimiter(InMemoryCache(), default_rpm=3)

    for expected_remaining in (2, 1, 0):
        state = await limiter.check(admin)
        assert state.remaining == expected_remaining
        assert state.headers()["RateLimit-Limit"] == "3"

    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.check(admin)
    assert exc.value.retry_after > 0


async def test_rate_limiter_fails_open_when_the_cache_is_down(admin: Principal) -> None:
    """A rate limiter is a protection, not a gate. An unavailable Redis must not
    take down every authenticated request."""
    from cairn.authz.ratelimit import RateLimiter

    class BrokenCache:
        async def get(self, key: str) -> bytes | None:
            raise ConnectionError

        async def set(self, key: str, value: bytes, ttl: int) -> None:
            raise ConnectionError

        async def set_if_absent(self, key: str, value: bytes, ttl: int) -> bool:
            raise ConnectionError

        async def delete(self, key: str) -> None:
            raise ConnectionError

        async def incr(self, key: str, ttl: int) -> int:
            raise ConnectionError

        async def ping(self) -> bool:
            return False

    state = await RateLimiter(BrokenCache(), default_rpm=10).check(admin)  # type: ignore[arg-type]
    assert state.remaining == 10
