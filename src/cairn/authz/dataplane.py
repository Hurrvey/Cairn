"""Data-plane authentication — the only authz surface M09/M13 may import.

Budget: **< 5 ms p99 on a cache hit** (NFR-P-06). That means one Redis GET and
no database round trip on the happy path, which is what lets the data plane keep
serving while PostgreSQL is unavailable (ADR-0002).

This module deliberately imports no control-plane module. It reads Redis, and on
a miss falls back to a narrow read-only repository.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Request

from cairn.authz.errors import ApiKeyInvalid, ApiKeyIpNotAllowed
from cairn.authz.model import Principal
from cairn.core.cache import Cache, get_cache
from cairn.core.errors import AuthenticationFailed
from cairn.core.logging import get_logger
from cairn.core.telemetry import authz_cache_total, authz_resolve_seconds

__all__ = [
    "KEY_PREFIX",
    "ApiKeyEnvelope",
    "DataPlaneAuthz",
    "current_dataplane_principal",
    "encode_api_key_envelope",
    "get_dataplane_authz",
    "hash_api_key",
]

log = get_logger(__name__)

KEY_PREFIX = "cairn_sk_"
_CACHE_TTL_SECONDS = 60
_INVALIDATION_CHANNEL = "cairn:authz:invalidate"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def current_dataplane_principal(request: Request) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationFailed("Authentication is required.")
    return principal


@dataclass(frozen=True, slots=True)
class ApiKeyEnvelope:
    principal: Principal
    ip_allowlist: tuple[str, ...]
    expires_at: datetime | None


def encode_api_key_envelope(envelope: ApiKeyEnvelope) -> bytes:
    principal = envelope.principal
    return json.dumps(
        {
            "version": 2,
            "type": principal.type,
            "id": str(principal.id),
            "workspace_id": str(principal.workspace_id),
            "role": principal.role,
            "username": principal.username,
            "owner_user_id": str(principal.owner_user_id) if principal.owner_user_id else None,
            "rate_limit_rpm": principal.rate_limit_rpm,
            "perm_version": principal.perm_version,
            "permissions": sorted(principal.permissions),
            "resource_permissions": {
                str(rid): sorted(perms) for rid, perms in principal.resource_permissions.items()
            },
            "accessible_kb_ids": [str(k) for k in principal.accessible_kb_ids],
            "ip_allowlist": list(envelope.ip_allowlist),
            "expires_at": envelope.expires_at.isoformat() if envelope.expires_at else None,
        },
        separators=(",", ":"),
    ).encode()


def _decode_envelope(raw: bytes) -> ApiKeyEnvelope | None:
    data: dict[str, Any] = json.loads(raw)
    if data.get("version") != 2 or "ip_allowlist" not in data or "expires_at" not in data:
        return None
    expires_at = datetime.fromisoformat(data["expires_at"]) if data["expires_at"] else None
    return ApiKeyEnvelope(
        principal=Principal(
            type=data["type"],
            id=UUID(data["id"]),
            workspace_id=UUID(data["workspace_id"]),
            role=data["role"],
            username=data["username"],
            owner_user_id=UUID(data["owner_user_id"]) if data["owner_user_id"] else None,
            rate_limit_rpm=data["rate_limit_rpm"],
            perm_version=data["perm_version"],
            permissions=frozenset(data["permissions"]),
            resource_permissions={
                UUID(rid): frozenset(perms) for rid, perms in data["resource_permissions"].items()
            },
            accessible_kb_ids=frozenset(UUID(k) for k in data["accessible_kb_ids"]),
        ),
        ip_allowlist=tuple(data["ip_allowlist"]),
        expires_at=expires_at,
    )


class DataPlaneAuthz:
    def __init__(self, cache: Cache | None = None) -> None:
        self._cache = cache

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    async def authenticate_api_key(self, raw_key: str, *, ip: str | None = None) -> Principal:
        started = time.perf_counter()

        if not raw_key.startswith(KEY_PREFIX):
            raise ApiKeyInvalid("Invalid API key.")

        key_hash = hash_api_key(raw_key)
        cache_key = f"authz:key:{key_hash}"

        envelope: ApiKeyEnvelope | None = None
        source = "cache"
        try:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                envelope = _decode_envelope(cached)
                if envelope is not None:
                    authz_cache_total.labels(result="hit").inc()
        except Exception as exc:
            log.warning("authz.cache_unavailable", error=type(exc).__name__)

        if envelope is None:
            authz_cache_total.labels(result="miss").inc()
            source = "database"
            envelope = await self._resolve_from_database(key_hash)
            if envelope is None:
                raise ApiKeyInvalid("Invalid API key.")
            self._validate_envelope(envelope, ip)
            ttl = self._cache_ttl(envelope)
            with contextlib.suppress(Exception):
                await self.cache.set(cache_key, encode_api_key_envelope(envelope), ttl)
        else:
            self._validate_envelope(envelope, ip)

        authz_resolve_seconds.labels(source=source).observe(time.perf_counter() - started)
        return envelope.principal

    async def _resolve_from_database(self, key_hash: str) -> ApiKeyEnvelope | None:
        from cairn.authz.repository import AuthzRepository
        from cairn.authz.resolver import PermissionResolver
        from cairn.core.db import session_scope
        from cairn.core.time import utcnow

        repo = AuthzRepository()
        async with session_scope() as session:
            key = await repo.get_key_by_hash(session, key_hash)
            if key is None or key.revoked_at is not None:
                return None
            if key.expires_at is not None and key.expires_at <= utcnow():
                return None

            # The workspace policy affects an admin owner's implicit content
            # access, so it must be part of resolution rather than a later check.
            #
            # Read through the authz repository rather than platform's settings
            # service: M09 imports this module, and pulling a control-plane
            # service onto the data-plane path would break the isolation
            # contract the moment retrieval lands (NFR-M-02).
            policy = await repo.workspace_admin_content_access(session, key.workspace_id)

            principal = await PermissionResolver(repo).principal_for_key(
                session, key, admin_content_access=policy
            )
            if principal is None:
                return None
            return ApiKeyEnvelope(
                principal=principal,
                ip_allowlist=tuple(key.ip_allowlist or ()),
                expires_at=key.expires_at,
            )

    def _cache_ttl(self, envelope: ApiKeyEnvelope) -> int:
        if envelope.expires_at is None:
            return _CACHE_TTL_SECONDS
        from cairn.core.time import utcnow

        remaining = (envelope.expires_at - utcnow()).total_seconds()
        if remaining <= 0:
            raise ApiKeyInvalid("Invalid API key.")
        return max(1, min(_CACHE_TTL_SECONDS, math.ceil(remaining)))

    def _validate_envelope(self, envelope: ApiKeyEnvelope, ip: str | None) -> None:
        from cairn.core.time import utcnow

        if envelope.expires_at is not None and envelope.expires_at <= utcnow():
            raise ApiKeyInvalid("Invalid API key.")
        if not envelope.ip_allowlist:
            return
        if ip is None:
            raise ApiKeyIpNotAllowed("This API key requires a known client address.")
        try:
            address = ipaddress.ip_address(ip)
            allowed = any(
                address in ipaddress.ip_network(network, strict=False)
                for network in envelope.ip_allowlist
            )
        except ValueError as exc:
            raise ApiKeyIpNotAllowed("This API key may not be used from this address.") from exc
        if not allowed:
            log.warning("authz.ip_rejected", ip=ip)
            raise ApiKeyIpNotAllowed("This API key may not be used from this address.")

    # --- invalidation -------------------------------------------------------

    async def invalidate_key(self, key_hash: str) -> None:
        """Revocation must be immediate, not eventually consistent.

        The 60 s TTL bounds staleness for ordinary permission drift, but a
        revoked key has to stop working now — so this deletes locally and
        publishes to every other `api-data` replica.
        """
        for suffix in ("key", "ip"):
            with contextlib.suppress(Exception):
                await self.cache.delete(f"authz:{suffix}:{key_hash}")
        await self._publish(key_hash)

    async def _publish(self, key_hash: str) -> None:
        try:
            from redis.asyncio import Redis

            from cairn.core.config import get_settings

            client: Redis = Redis.from_url(get_settings().redis_url)
            try:
                await client.publish(_INVALIDATION_CHANNEL, key_hash)
            finally:
                await client.aclose()
        except Exception as exc:
            log.warning("authz.invalidation_publish_failed", error=type(exc).__name__)


_authz: DataPlaneAuthz | None = None


def get_dataplane_authz() -> DataPlaneAuthz:
    global _authz
    if _authz is None:
        _authz = DataPlaneAuthz()
    return _authz


def reset_dataplane_authz() -> None:
    global _authz
    _authz = None
