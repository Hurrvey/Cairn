"""Effective permission resolution.

The load-bearing rule is the **intersection** (FR-B-08): an API key's effective
permissions are ``key_grants ∩ owner_permissions``, computed at authentication
time rather than stored.

That single choice buys three properties that a stored copy cannot:

* Revoking a user's access to a knowledge base instantly narrows every key they
  ever issued — no cascade update, no bookkeeping, no window.
* An orphaned over-privileged key is unreachable, not merely unlikely.
* "What can this key do?" has exactly one answer, derived the same way every time.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cairn.authz.model import (
    ADMIN_PLATFORM_CAPABILITIES,
    KB_PERMISSIONS,
    Principal,
    Role,
)
from cairn.authz.models import ApiKey
from cairn.authz.repository import AuthzRepository, UserSummary
from cairn.core.logging import get_logger

__all__ = ["PermissionResolver", "ResolvedPermissions"]

log = get_logger(__name__)


class ResolvedPermissions:
    __slots__ = ("accessible_kb_ids", "by_resource", "workspace_wide")

    def __init__(
        self,
        workspace_wide: frozenset[str],
        by_resource: dict[UUID, frozenset[str]],
        accessible_kb_ids: frozenset[UUID],
    ) -> None:
        self.workspace_wide = workspace_wide
        self.by_resource = by_resource
        self.accessible_kb_ids = accessible_kb_ids


class PermissionResolver:
    def __init__(self, repository: AuthzRepository | None = None) -> None:
        self._repo = repository or AuthzRepository()

    # --- users --------------------------------------------------------------

    async def for_user(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        workspace_id: UUID,
        role: Role,
        admin_content_access: str = "break_glass",
    ) -> ResolvedPermissions:
        workspace_wide: set[str] = set()
        by_resource: dict[UUID, set[str]] = {}
        kb_ids: set[UUID] = set()

        for grant in await self._repo.live_grants(session, "user", user_id):
            permissions = set(grant.permissions)
            if grant.resource_id is None:
                workspace_wide |= permissions
            else:
                by_resource.setdefault(grant.resource_id, set()).update(permissions)
                if grant.resource_type == "knowledge_base" and permissions & KB_PERMISSIONS:
                    kb_ids.add(grant.resource_id)

        if role == "admin":
            workspace_wide |= ADMIN_PLATFORM_CAPABILITIES
            workspace_wide.add("kb:create")

            # An administrator always *manages* every knowledge base. Whether
            # they may *read its content* depends on the workspace policy
            # (FR-B-09) — that distinction is the whole point of break-glass.
            for kb_id in await self._repo.workspace_kb_ids(session, workspace_id):
                held = by_resource.setdefault(kb_id, set())
                held.add("kb:manage")
                if admin_content_access == "always":
                    held.update({"kb:read", "kb:query"})
                    kb_ids.add(kb_id)
                elif kb_id in kb_ids:
                    # An explicit or break-glass grant is already present.
                    pass

        return ResolvedPermissions(
            workspace_wide=frozenset(workspace_wide),
            by_resource={rid: frozenset(perms) for rid, perms in by_resource.items()},
            accessible_kb_ids=frozenset(kb_ids),
        )

    # --- api keys -----------------------------------------------------------

    async def for_api_key(
        self,
        session: AsyncSession,
        key: ApiKey,
        *,
        owner_role: Role,
        admin_content_access: str = "break_glass",
    ) -> ResolvedPermissions:
        """key ∩ owner. Never more than the owner holds."""
        owner = await self.for_user(
            session,
            user_id=key.owner_user_id,
            workspace_id=key.workspace_id,
            role=owner_role,
            admin_content_access=admin_content_access,
        )

        requested_scopes = set(key.scopes)

        # Platform capabilities are role-gated and not delegable to a key. An
        # administrator's key must not be able to create users.
        requested_scopes -= ADMIN_PLATFORM_CAPABILITIES

        # An empty kb_ids list means "whatever the owner can reach", so the key's
        # reach narrows automatically as the owner's does.
        target_kb_ids = set(key.kb_ids) if key.kb_ids else set(owner.accessible_kb_ids)

        by_resource: dict[UUID, frozenset[str]] = {}
        accessible: set[UUID] = set()
        for kb_id in target_kb_ids:
            owner_held = owner.by_resource.get(kb_id, frozenset())
            granted = frozenset(requested_scopes) & owner_held
            if granted:
                by_resource[kb_id] = granted
                if granted & KB_PERMISSIONS:
                    accessible.add(kb_id)

        workspace_wide = frozenset(requested_scopes) & owner.workspace_wide

        return ResolvedPermissions(
            workspace_wide=workspace_wide,
            by_resource=by_resource,
            accessible_kb_ids=frozenset(accessible),
        )

    # --- principal assembly -------------------------------------------------

    async def principal_for_user(
        self,
        session: AsyncSession,
        *,
        user: UserSummary,
        admin_content_access: str = "break_glass",
        session_id: UUID | None = None,
        scopes: frozenset[str] | None = None,
    ) -> Principal:
        role: Role = "admin" if user["role"] == "admin" else "user"
        resolved = await self.for_user(
            session,
            user_id=user["id"],
            workspace_id=user["workspace_id"],
            role=role,
            admin_content_access=admin_content_access,
        )
        return Principal(
            type="user",
            id=user["id"],
            workspace_id=user["workspace_id"],
            role=role,
            username=user["username"],
            must_change_password=user["must_change_password"],
            credential_version=user["credential_version"],
            perm_version=user["perm_version"],
            session_id=session_id,
            scopes=scopes or frozenset(),
            permissions=resolved.workspace_wide,
            resource_permissions=dict(resolved.by_resource),
            accessible_kb_ids=resolved.accessible_kb_ids,
        )

    async def principal_for_key(
        self,
        session: AsyncSession,
        key: ApiKey,
        *,
        admin_content_access: str = "break_glass",
    ) -> Principal | None:
        owner = await self._repo.user_summary(session, key.owner_user_id)
        if owner is None or not owner["is_active"]:
            # A key outlives its owner's account only until the next resolution.
            return None

        owner_role: Role = "admin" if owner["role"] == "admin" else "user"
        resolved = await self.for_api_key(
            session, key, owner_role=owner_role, admin_content_access=admin_content_access
        )

        return Principal(
            type="api_key",
            id=key.id,
            workspace_id=key.workspace_id,
            # A key never carries platform capability regardless of its owner.
            role="user",
            username=f"key:{key.name}",
            owner_user_id=key.owner_user_id,
            rate_limit_rpm=key.rate_limit_rpm,
            perm_version=owner["perm_version"],
            permissions=resolved.workspace_wide,
            resource_permissions=dict(resolved.by_resource),
            accessible_kb_ids=resolved.accessible_kb_ids,
        )
