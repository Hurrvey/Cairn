"""Authorization public surface, loaded lazily to preserve data-plane isolation."""

from __future__ import annotations

from importlib import import_module
from typing import Any

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

_MODULE_BY_NAME = {
    "ADMIN_PLATFORM_CAPABILITIES": "cairn.authz.model",
    "ALL_PERMISSIONS": "cairn.authz.model",
    "KB_PERMISSIONS": "cairn.authz.model",
    "ApiKeySpec": "cairn.authz.service",
    "AuthzService": "cairn.authz.service",
    "DataPlaneAuthz": "cairn.authz.dataplane",
    "GrantSpec": "cairn.authz.service",
    "Principal": "cairn.authz.model",
    "PrincipalType": "cairn.authz.model",
    "RateLimitState": "cairn.authz.ratelimit",
    "RateLimiter": "cairn.authz.ratelimit",
    "ResourceType": "cairn.authz.model",
    "Role": "cairn.authz.model",
    "current_principal": "cairn.authz.deps",
    "get_authz_service": "cairn.authz.service",
    "get_dataplane_authz": "cairn.authz.dataplane",
    "get_rate_limiter": "cairn.authz.ratelimit",
    "hash_api_key": "cairn.authz.dataplane",
    "optional_principal": "cairn.authz.deps",
    "require_permission": "cairn.authz.deps",
    "require_role": "cairn.authz.deps",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
