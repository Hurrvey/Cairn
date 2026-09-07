"""Authorization service — grants, API keys, break-glass."""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from cairn.authz.dataplane import KEY_PREFIX, get_dataplane_authz, hash_api_key
from cairn.authz.errors import (
    BreakGlassReasonRequired,
    GrantExceedsOwner,
    PlatformCapabilityNotGrantable,
)
from cairn.authz.model import (
    ALL_PERMISSIONS,
    Principal,
    is_platform_capability,
    permissions_for_resource_type,
)
from cairn.authz.models import ApiKey, ResourceGrant
from cairn.authz.repository import AuthzRepository
from cairn.authz.resolver import PermissionResolver
from cairn.core.db import session_scope, transaction
from cairn.core.errors import NotFound, PermissionDenied, ValidationFailed
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.platform.audit import AuditService, get_audit_service

__all__ = ["ApiKeySpec", "AuthzService", "GrantSpec", "generate_api_key", "get_authz_service"]

log = get_logger(__name__)

_ALPHABET = string.ascii_letters + string.digits
_KEY_RANDOM_CHARS = 32  # ~190 bits
MIN_BREAK_GLASS_REASON = 20


def generate_api_key(environment: str = "live") -> str:
    """``cairn_sk_live_<32 base62 chars>``.

    The prefix is not decoration: it lets secret scanners recognise a leaked key
    in a commit or a log, and lets the edge rate-limit by key family before any
    database lookup.
    """
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_KEY_RANDOM_CHARS))
    return f"{KEY_PREFIX}{environment}_{body}"


@dataclass(frozen=True, slots=True)
class GrantSpec:
    subject_type: str
    subject_id: UUID
    resource_type: str
    permissions: list[str]
    resource_id: UUID | None = None
    expires_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApiKeySpec:
    name: str
    scopes: list[str]
    kb_ids: list[UUID] | None = None
    rate_limit_rpm: int | None = None
    ip_allowlist: list[str] | None = None
    expires_at: datetime | None = None


class AuthzService:
    def __init__(
        self,
        repository: AuthzRepository | None = None,
        resolver: PermissionResolver | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._repo = repository or AuthzRepository()
        self._resolver = resolver or PermissionResolver(self._repo)
        self._audit = audit or get_audit_service()

    # --- principals ---------------------------------------------------------

    async def principal_for_user(
        self,
        user_id: UUID,
        *,
        session_id: UUID | None = None,
        scopes: frozenset[str] | None = None,
    ) -> Principal | None:
        async with session_scope() as session:
            user = await self._repo.user_summary(session, user_id)
            if user is None or not user["is_active"]:
                return None
            policy = await self._repo.workspace_admin_content_access(
                session,
                user["workspace_id"],
            )
            return await self._resolver.principal_for_user(
                session,
                user=user,
                admin_content_access=policy,
                session_id=session_id,
                scopes=scopes,
            )

    # --- grants -------------------------------------------------------------

    async def grant(self, actor: Principal, spec: GrantSpec) -> ResourceGrant:
        self._validate_permissions(spec)
        await self._assert_may_grant(actor, spec)

        async with transaction() as session:
            grant = await self._repo.add_grant(
                session,
                ResourceGrant(
                    workspace_id=actor.workspace_id,
                    subject_type=spec.subject_type,
                    subject_id=spec.subject_id,
                    resource_type=spec.resource_type,
                    resource_id=spec.resource_id,
                    permissions=list(spec.permissions),
                    granted_by=actor.id,
                    reason=spec.reason,
                    expires_at=spec.expires_at,
                ),
            )
            if spec.subject_type == "user":
                await self._repo.bump_perm_version(session, spec.subject_id)
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="grant.create",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type=spec.resource_type,
                resource_id=spec.resource_id,
                after=grant.snapshot(),
            )
        return grant

    async def revoke_grant(self, actor: Principal, grant_id: UUID) -> None:
        async with transaction() as session:
            grant = await self._repo.get_grant(session, grant_id)
            if grant is None or grant.workspace_id != actor.workspace_id:
                raise NotFound("Grant not found.")
            before = grant.snapshot()
            if not await self._repo.revoke_grant(session, grant_id):
                raise NotFound("Grant not found or already revoked.")
            if grant.subject_type == "user":
                await self._repo.bump_perm_version(session, grant.subject_id)
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="grant.revoke",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type=grant.resource_type,
                resource_id=grant.resource_id,
                before=before,
            )

    async def list_grants(
        self, workspace_id: UUID, subject_type: str, subject_id: UUID
    ) -> list[ResourceGrant]:
        async with session_scope() as session:
            grants = await self._repo.live_grants(session, subject_type, subject_id)
        return [g for g in grants if g.workspace_id == workspace_id]

    def _validate_permissions(self, spec: GrantSpec) -> None:
        if not spec.permissions:
            raise ValidationFailed("A grant must carry at least one permission.")

        for permission in spec.permissions:
            if is_platform_capability(permission):
                # FR-B-05. There is no accumulating your way to administrator.
                raise PlatformCapabilityNotGrantable(
                    f"{permission} is role-based and cannot be granted. "
                    "Change the account's role instead."
                )
            if permission not in ALL_PERMISSIONS:
                raise ValidationFailed(f"Unknown permission: {permission}")

        valid = permissions_for_resource_type(spec.resource_type)
        invalid = set(spec.permissions) - valid
        if invalid:
            raise ValidationFailed(
                f"{sorted(invalid)} cannot be granted on a {spec.resource_type}."
            )

        if spec.resource_type != "workspace" and spec.resource_id is None:
            raise ValidationFailed(f"A {spec.resource_type} grant requires a resource id.")

    async def _assert_may_grant(self, actor: Principal, spec: GrantSpec) -> None:
        if actor.is_admin and actor.type == "user":
            return
        # A non-administrator may only delegate what they hold themselves, and
        # only where they hold management rights.
        manage = {"knowledge_base": "kb:manage", "pipeline": "pipeline:edit"}.get(
            spec.resource_type
        )
        if manage is None or not actor.can(manage, spec.resource_id):
            raise PermissionDenied("You do not manage this resource.")
        for permission in spec.permissions:
            if not actor.can(permission, spec.resource_id):
                raise GrantExceedsOwner(
                    f"You cannot grant {permission} because you do not hold it."
                )

    # --- break-glass --------------------------------------------------------

    async def open_break_glass(
        self, actor: Principal, kb_id: UUID, reason: str, *, ttl_minutes: int = 60
    ) -> ResourceGrant:
        """Time-boxed, reasoned, audited, and notified (FR-B-10).

        This is the escape hatch that makes `break_glass` workable rather than
        obstructive: an administrator investigating an incident can get in, and
        everyone can see that they did.
        """
        if not actor.is_admin or actor.type != "user":
            raise PermissionDenied("Only administrators may open break-glass access.")
        if len(reason.strip()) < MIN_BREAK_GLASS_REASON:
            raise BreakGlassReasonRequired(
                f"A substantive reason is required (at least {MIN_BREAK_GLASS_REASON} characters)."
            )

        expires_at = utcnow() + timedelta(minutes=ttl_minutes)
        async with transaction() as session:
            grant = await self._repo.add_grant(
                session,
                ResourceGrant(
                    workspace_id=actor.workspace_id,
                    subject_type="user",
                    subject_id=actor.id,
                    resource_type="knowledge_base",
                    resource_id=kb_id,
                    permissions=["kb:read", "kb:query"],
                    granted_by=actor.id,
                    reason=reason.strip(),
                    is_break_glass=True,
                    expires_at=expires_at,
                ),
            )
            await self._repo.bump_perm_version(session, actor.id)
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="break_glass.open",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="knowledge_base",
                resource_id=kb_id,
                detail={"reason": reason.strip(), "expires_at": expires_at.isoformat()},
            )
        log.warning(
            "authz.break_glass_opened",
            actor_id=str(actor.id),
            kb_id=str(kb_id),
            expires_at=expires_at.isoformat(),
        )
        return grant

    # --- api keys -----------------------------------------------------------

    async def create_api_key(self, actor: Principal, spec: ApiKeySpec) -> tuple[ApiKey, str]:
        """Returns the record and the plaintext key. Plaintext is shown once."""
        if actor.type != "user":
            raise PermissionDenied("API keys cannot create further API keys.")

        for scope in spec.scopes:
            if is_platform_capability(scope):
                raise PlatformCapabilityNotGrantable(
                    f"{scope} is role-based and cannot be delegated to an API key."
                )
            if scope not in ALL_PERMISSIONS:
                raise ValidationFailed(f"Unknown permission: {scope}")

        # Reject up front what the intersection would silently drop later. A key
        # that quietly does less than requested is worse than a clear error.
        for kb_id in spec.kb_ids or []:
            for scope in spec.scopes:
                if not actor.can(scope, kb_id):
                    raise GrantExceedsOwner(
                        f"You cannot issue a key with {scope} on that knowledge base "
                        "because you do not hold it."
                    )

        raw_key = generate_api_key()
        async with transaction() as session:
            key = await self._repo.add_key(
                session,
                ApiKey(
                    workspace_id=actor.workspace_id,
                    owner_user_id=actor.id,
                    name=spec.name,
                    key_prefix=raw_key[:18],
                    key_hash=hash_api_key(raw_key),
                    last_four=raw_key[-4:],
                    scopes=list(spec.scopes),
                    kb_ids=list(spec.kb_ids or []),
                    rate_limit_rpm=spec.rate_limit_rpm,
                    ip_allowlist=spec.ip_allowlist,
                    expires_at=spec.expires_at,
                    created_by=actor.id,
                ),
            )
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="apikey.create",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="api_key",
                resource_id=key.id,
                after=key.snapshot(),
            )
        return key, raw_key

    async def list_api_keys(
        self, workspace_id: UUID, owner_user_id: UUID | None = None
    ) -> list[ApiKey]:
        async with session_scope() as session:
            return await self._repo.list_keys(session, workspace_id, owner_user_id)

    async def revoke_api_key(self, actor: Principal, key_id: UUID) -> None:
        async with transaction() as session:
            key = await self._repo.get_key(session, key_id)
            if key is None or key.workspace_id != actor.workspace_id:
                raise NotFound("API key not found.")
            if key.owner_user_id != actor.id and not actor.is_admin:
                raise PermissionDenied("You may only revoke your own API keys.")

            before = key.snapshot()
            key_hash = key.key_hash
            if not await self._repo.revoke_key(session, key_id):
                raise NotFound("API key not found or already revoked.")
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="apikey.revoke",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="api_key",
                resource_id=key_id,
                before=before,
            )

        # Outside the transaction: revocation must take effect immediately
        # across every replica, not after the 60 s cache TTL.
        await get_dataplane_authz().invalidate_key(key_hash)

    async def revoke_all_for_user(self, user_id: UUID) -> None:
        """Called when a user is deleted or deactivated."""
        async with transaction() as session:
            summary = await self._repo.user_summary(session, user_id)
            if summary is None:
                return
            keys = await self._repo.list_keys(
                session, summary["workspace_id"], owner_user_id=user_id
            )
            hashes = [k.key_hash for k in keys if k.revoked_at is None]
            await self._repo.revoke_keys_for_owner(session, user_id)
            await self._repo.revoke_grants_for_subject(session, "user", user_id)
            await self._repo.bump_perm_version(session, user_id)

        authz = get_dataplane_authz()
        for key_hash in hashes:
            await authz.invalidate_key(key_hash)


_service: AuthzService | None = None


def get_authz_service() -> AuthzService:
    global _service
    if _service is None:
        _service = AuthzService()
    return _service


def reset_authz_service() -> None:
    global _service
    _service = None
