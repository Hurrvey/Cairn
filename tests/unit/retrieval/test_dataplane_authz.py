from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

import pytest

from cairn.authz.dataplane import (
    ApiKeyEnvelope,
    DataPlaneAuthz,
    encode_api_key_envelope,
    hash_api_key,
)
from cairn.authz.errors import ApiKeyInvalid, ApiKeyIpNotAllowed
from cairn.authz.model import Principal
from cairn.core.cache import InMemoryCache
from cairn.core.time import utcnow

RAW_KEY = "cairn_sk_unit-security-key"
KEY_HASH = hash_api_key(RAW_KEY)


def principal() -> Principal:
    kb_id = UUID("30000000-0000-0000-0000-000000000003")
    return Principal(
        type="api_key",
        id=UUID("10000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("20000000-0000-0000-0000-000000000002"),
        role="user",
        username="unit-key",
        resource_permissions={kb_id: frozenset({"kb:query"})},
        accessible_kb_ids=frozenset({kb_id}),
    )


@pytest.mark.anyio
async def test_atomic_cached_envelope_enforces_ip_without_legacy_ip_key() -> None:
    cache = InMemoryCache()
    envelope = ApiKeyEnvelope(
        principal=principal(),
        ip_allowlist=("192.0.2.0/24",),
        expires_at=utcnow() + timedelta(minutes=5),
    )
    await cache.set(f"authz:key:{KEY_HASH}", encode_api_key_envelope(envelope), 60)

    with pytest.raises(ApiKeyIpNotAllowed):
        await DataPlaneAuthz(cache).authenticate_api_key(RAW_KEY, ip="198.51.100.8")


@pytest.mark.anyio
async def test_restricted_cached_envelope_rejects_unknown_peer_ip() -> None:
    cache = InMemoryCache()
    envelope = ApiKeyEnvelope(
        principal=principal(),
        ip_allowlist=("192.0.2.0/24",),
        expires_at=utcnow() + timedelta(minutes=5),
    )
    await cache.set(f"authz:key:{KEY_HASH}", encode_api_key_envelope(envelope), 60)

    with pytest.raises(ApiKeyIpNotAllowed):
        await DataPlaneAuthz(cache).authenticate_api_key(RAW_KEY)


@pytest.mark.anyio
async def test_cached_envelope_rechecks_key_expiry() -> None:
    cache = InMemoryCache()
    envelope = ApiKeyEnvelope(
        principal=principal(),
        ip_allowlist=(),
        expires_at=utcnow() - timedelta(milliseconds=1),
    )
    await cache.set(f"authz:key:{KEY_HASH}", encode_api_key_envelope(envelope), 60)

    with pytest.raises(ApiKeyInvalid):
        await DataPlaneAuthz(cache).authenticate_api_key(RAW_KEY)


class LegacyThenWriteFailureCache(InMemoryCache):
    async def set(self, key: str, value: bytes, ttl: int) -> None:
        if key.startswith("authz:key:"):
            raise RuntimeError("redis write unavailable")
        await super().set(key, value, ttl)


@pytest.mark.anyio
async def test_legacy_cache_fallback_keeps_ip_policy_when_write_through_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = LegacyThenWriteFailureCache()
    legacy = {
        "type": "api_key",
        "id": str(principal().id),
        "workspace_id": str(principal().workspace_id),
        "role": "user",
        "username": "unit-key",
        "owner_user_id": None,
        "rate_limit_rpm": None,
        "perm_version": 1,
        "permissions": [],
        "resource_permissions": {},
        "accessible_kb_ids": [],
    }
    cache._data[f"authz:key:{KEY_HASH}"] = (json.dumps(legacy).encode(), float("inf"))
    dataplane = DataPlaneAuthz(cache)
    resolved = ApiKeyEnvelope(
        principal=principal(),
        ip_allowlist=("192.0.2.0/24",),
        expires_at=None,
    )

    async def resolve(key_hash: str) -> ApiKeyEnvelope | None:
        assert key_hash == KEY_HASH
        return resolved

    monkeypatch.setattr(dataplane, "_resolve_from_database", resolve)

    with pytest.raises(ApiKeyIpNotAllowed):
        await dataplane.authenticate_api_key(RAW_KEY, ip="198.51.100.8")
