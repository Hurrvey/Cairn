"""The permission vocabulary and the authenticated actor.

Two orthogonal things live here and must not be conflated:

* **Roles** (``admin`` / ``user``) govern *platform* capability — managing users,
  providers, storage, settings, audit. They are not grantable: there is no
  "power user" who accumulates their way to administrator (FR-B-05).
* **Grants** govern *resource* permission — this knowledge base, that pipeline.

``Principal`` is a frozen value object with no ORM attachment, so it can be
cached, serialised, and used by the data plane without importing anything from
the control plane (NFR-M-02).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

__all__ = [
    "ADMIN_PLATFORM_CAPABILITIES",
    "ALL_PERMISSIONS",
    "IMPLIED_BY",
    "KB_PERMISSIONS",
    "Principal",
    "PrincipalType",
    "ResourceType",
    "Role",
    "expand",
    "is_platform_capability",
    "permissions_for_resource_type",
]

Role = Literal["admin", "user"]
PrincipalType = Literal["user", "api_key"]
ResourceType = Literal["workspace", "knowledge_base", "pipeline", "function", "model", "golden_set"]

ADMIN_PLATFORM_CAPABILITIES: frozenset[str] = frozenset(
    {
        "platform:users",
        "platform:models",
        "platform:storage",
        "platform:settings",
        "platform:audit",
    }
)

KB_PERMISSIONS: frozenset[str] = frozenset({"kb:read", "kb:query", "kb:write", "kb:manage"})

_RESOURCE_PERMISSIONS: dict[str, frozenset[str]] = {
    "workspace": frozenset({"kb:create"}),
    "knowledge_base": KB_PERMISSIONS,
    "pipeline": frozenset({"pipeline:read", "pipeline:edit", "pipeline:run"}),
    "function": frozenset({"function:use", "function:edit"}),
    "model": frozenset({"model:use", "model:manage"}),
    "golden_set": frozenset({"eval:read", "eval:run"}),
}

ALL_PERMISSIONS: frozenset[str] = frozenset().union(*_RESOURCE_PERMISSIONS.values())

#: Implication is resolved at CHECK time, never expanded at write time.
#:
#: Storing the expansion would mean revoking `kb:manage` silently leaves
#: `kb:read` behind — the grant table would no longer say what was actually
#: granted, and an audit of "who can read this" would be wrong.
IMPLIED_BY: dict[str, frozenset[str]] = {
    "kb:query": frozenset({"kb:query", "kb:read", "kb:write", "kb:manage"}),
    "kb:read": frozenset({"kb:read", "kb:write", "kb:manage"}),
    "kb:write": frozenset({"kb:write", "kb:manage"}),
    "kb:manage": frozenset({"kb:manage"}),
    "pipeline:read": frozenset({"pipeline:read", "pipeline:edit"}),
    "pipeline:run": frozenset({"pipeline:run", "pipeline:edit"}),
    "pipeline:edit": frozenset({"pipeline:edit"}),
    "function:use": frozenset({"function:use", "function:edit"}),
    "function:edit": frozenset({"function:edit"}),
    "model:use": frozenset({"model:use", "model:manage"}),
    "model:manage": frozenset({"model:manage"}),
    "eval:read": frozenset({"eval:read", "eval:run"}),
    "eval:run": frozenset({"eval:run"}),
    "kb:create": frozenset({"kb:create"}),
}


def permissions_for_resource_type(resource_type: str) -> frozenset[str]:
    return _RESOURCE_PERMISSIONS.get(resource_type, frozenset())


def is_platform_capability(permission: str) -> bool:
    return permission in ADMIN_PLATFORM_CAPABILITIES


def expand(permission: str) -> frozenset[str]:
    """The set of held permissions that would satisfy `permission`."""
    return IMPLIED_BY.get(permission, frozenset({permission}))


@dataclass(frozen=True, slots=True)
class Principal:
    type: PrincipalType
    id: UUID
    workspace_id: UUID
    role: Role
    username: str

    must_change_password: bool = False
    credential_version: int = 1
    session_id: UUID | None = None
    #: Empty for a full session; ``{"credential:bootstrap"}`` for a change token.
    scopes: frozenset[str] = field(default_factory=frozenset)

    #: Workspace-wide permissions, e.g. ``{"kb:create"}``.
    permissions: frozenset[str] = field(default_factory=frozenset)
    #: resource_id -> permissions held on that specific resource.
    resource_permissions: dict[UUID, frozenset[str]] = field(default_factory=dict)
    #: Precomputed so the data plane can authorize a retrieval target with a set
    #: membership test rather than a lookup (NFR-P-06).
    accessible_kb_ids: frozenset[UUID] = field(default_factory=frozenset)

    # api_key only
    owner_user_id: UUID | None = None
    rate_limit_rpm: int | None = None
    perm_version: int = 1

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_restricted(self) -> bool:
        """Holds a scope-limited token rather than a full session."""
        return bool(self.scopes)

    def can(self, permission: str, resource_id: UUID | None = None) -> bool:
        # A credential-change token authorizes nothing but the change itself.
        if self.is_restricted:
            return False

        if is_platform_capability(permission):
            # Role-gated and not grantable, so an API key can never hold one
            # even if its owner is an administrator.
            return self.is_admin and self.type == "user"

        satisfying = expand(permission)

        if self.permissions & satisfying:
            return True

        if resource_id is not None:
            held = self.resource_permissions.get(resource_id, frozenset())
            return bool(held & satisfying)

        # No resource named: true if the permission is held on anything at all.
        # Used for "can this principal do X somewhere?" list filtering.
        return any(held & satisfying for held in self.resource_permissions.values())

    def can_query_kb(self, kb_id: UUID) -> bool:
        """Hot-path shortcut used by retrieval."""
        return not self.is_restricted and kb_id in self.accessible_kb_ids

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes
