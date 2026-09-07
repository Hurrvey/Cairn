"""Authorization.

Roles govern platform capability; grants govern resource permission. The two are
kept separate on purpose (FR-B-01, FR-B-05).

The rule worth remembering: an API key's effective permissions are
``key_scopes ∩ owner_permissions``, computed at authentication time rather than
stored. Revoking a user's access therefore narrows every key they ever issued,
instantly and with no bookkeeping.
"""

from cairn.authz.dataplane import DataPlaneAuthz, get_dataplane_authz, hash_api_key
from cairn.authz.deps import current_principal, optional_principal, require_permission, require_role
from cairn.authz.model import (
    ADMIN_PLATFORM_CAPABILITIES,
    ALL_PERMISSIONS,
    KB_PERMISSIONS,
    Principal,
    PrincipalType,
    ResourceType,
    Role,
)
from cairn.authz.ratelimit import RateLimiter, RateLimitState, get_rate_limiter
from cairn.authz.service import ApiKeySpec, AuthzService, GrantSpec, get_authz_service

__all__ = [
    "ADMIN_PLATFORM_CAPABILITIES",
    "ALL_PERMISSIONS",
    "KB_PERMISSIONS",
    "ApiKeySpec",
    "AuthzService",
    "DataPlaneAuthz",
    "GrantSpec",
    "Principal",
    "PrincipalType",
    "RateLimitState",
    "RateLimiter",
    "ResourceType",
    "Role",
    "current_principal",
    "get_authz_service",
    "get_dataplane_authz",
    "get_rate_limiter",
    "hash_api_key",
    "optional_principal",
    "require_permission",
    "require_role",
]
