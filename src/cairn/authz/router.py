"""Grant and API key endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from cairn.authz.deps import current_principal, require_role
from cairn.authz.model import Principal
from cairn.authz.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    BreakGlassRequest,
    CreateApiKeyRequest,
    CreateGrantRequest,
    EffectivePermissionsResponse,
    GrantResponse,
)
from cairn.authz.service import ApiKeySpec, AuthzService, GrantSpec, get_authz_service
from cairn.core.errors import NotFound, PermissionDenied
from cairn.core.ids import decode_id, encode_id
from cairn.platform.settings import get_settings_service

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["authorization"])
PROBLEM: dict[int | str, dict[str, Any]] = {
    400: {"content": {"application/problem+json": {}}},
    403: {"content": {"application/problem+json": {}}},
}

_PREFIX_BY_RESOURCE = {
    "workspace": "ws",
    "knowledge_base": "kb",
    "pipeline": "pipe",
    "function": "fn",
    "model": "mdl",
    "golden_set": "gs",
}


def _service() -> AuthzService:
    return get_authz_service()


# --- grants -------------------------------------------------------------------


@router.post(
    "/grants",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
    summary="Grant permissions on a resource",
    description=(
        "Administrators may grant anything. Other principals may only delegate "
        "permissions they hold themselves, on resources they manage."
    ),
)
async def create_grant(
    body: CreateGrantRequest,
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
) -> GrantResponse:
    subject_prefix = "usr" if body.subject_type == "user" else "key"
    resource_prefix = _PREFIX_BY_RESOURCE[body.resource_type]
    grant = await service.grant(
        actor,
        GrantSpec(
            subject_type=body.subject_type,
            subject_id=decode_id(subject_prefix, body.subject_id),
            resource_type=body.resource_type,
            resource_id=(
                decode_id(resource_prefix, body.resource_id) if body.resource_id else None
            ),
            permissions=body.permissions,
            expires_at=body.expires_at,
            reason=body.reason,
        ),
    )
    return GrantResponse.from_model(grant)


@router.get(
    "/users/{user_id}/grants",
    response_model=list[GrantResponse],
    responses=PROBLEM,
    summary="Grants held by a user",
)
async def list_user_grants(
    user_id: str,
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
) -> list[GrantResponse]:
    subject_id = decode_id("usr", user_id)
    if subject_id != actor.id and not actor.can("platform:users"):
        raise PermissionDenied("This action requires platform:users.")
    grants = await service.list_grants(actor.workspace_id, "user", subject_id)
    return [GrantResponse.from_model(g) for g in grants]


@router.get(
    "/users/{user_id}/permissions",
    response_model=EffectivePermissionsResponse,
    responses=PROBLEM,
    summary="Resolved effective permissions",
    description=(
        "The union of a subject's live grants, plus any role-implied "
        "capabilities. This is what the system actually evaluates."
    ),
)
async def effective_permissions(
    user_id: str,
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
) -> EffectivePermissionsResponse:
    subject_id = decode_id("usr", user_id)
    if subject_id != actor.id and not actor.can("platform:users"):
        raise PermissionDenied("This action requires platform:users.")

    principal = await service.principal_for_user(subject_id)
    if principal is None:
        raise NotFound("User not found.")

    return EffectivePermissionsResponse(
        subject_id=encode_id("usr", principal.id),
        role=principal.role,
        workspace_permissions=sorted(principal.permissions),
        resource_permissions={
            encode_id("kb", rid): sorted(perms)
            for rid, perms in principal.resource_permissions.items()
        },
        accessible_knowledge_bases=[
            encode_id("kb", k) for k in sorted(principal.accessible_kb_ids)
        ],
    )


@router.delete(
    "/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEM,
    summary="Revoke a grant",
)
async def revoke_grant(
    grant_id: str,
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
) -> None:
    await service.revoke_grant(actor, decode_id("grant", grant_id))


# --- break-glass ---------------------------------------------------------------


@router.post(
    "/knowledge-bases/{kb_id}/break-glass",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
    summary="Open time-boxed administrator access to knowledge base content",
    description=(
        "Under the default `break_glass` policy an administrator manages a "
        "knowledge base but cannot read its content. This opens a time-boxed, "
        "reasoned, audited window. The owner is notified."
    ),
)
async def open_break_glass(
    kb_id: str,
    body: BreakGlassRequest,
    actor: Annotated[Principal, Depends(require_role("admin"))],
    service: Annotated[AuthzService, Depends(_service)],
) -> GrantResponse:
    workspace_settings = await get_settings_service().get(actor.workspace_id)
    grant = await service.open_break_glass(
        actor,
        decode_id("kb", kb_id),
        body.reason,
        ttl_minutes=body.ttl_minutes or workspace_settings.break_glass_ttl_minutes,
    )
    return GrantResponse.from_model(grant)


# --- api keys ------------------------------------------------------------------


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
    summary="Create an API key",
    description=(
        "The key's effective permissions are the intersection of the requested "
        "scopes with the owner's own permissions, recomputed at every request. "
        "The plaintext key is returned once and is not recoverable."
    ),
)
async def create_api_key(
    body: CreateApiKeyRequest,
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
) -> ApiKeyCreatedResponse:
    key, raw = await service.create_api_key(
        actor,
        ApiKeySpec(
            name=body.name,
            scopes=body.scopes,
            kb_ids=[decode_id("kb", k) for k in (body.knowledge_base_ids or [])],
            rate_limit_rpm=body.rate_limit_rpm,
            ip_allowlist=body.ip_allowlist,
            expires_at=body.expires_at,
        ),
    )
    return ApiKeyCreatedResponse(api_key=ApiKeyResponse.from_model(key), key=raw)


@router.get(
    "/api-keys",
    response_model=list[ApiKeyResponse],
    responses=PROBLEM,
    summary="List API keys",
)
async def list_api_keys(
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
    all_users: Annotated[bool, Query(description="Administrators only.")] = False,
) -> list[ApiKeyResponse]:
    if all_users and not actor.can("platform:users"):
        raise PermissionDenied("Listing all keys requires platform:users.")
    owner = None if all_users else actor.id
    keys = await service.list_api_keys(actor.workspace_id, owner)
    return [ApiKeyResponse.from_model(k) for k in keys]


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEM,
    summary="Revoke an API key",
    description="Takes effect immediately across every replica, not after the cache TTL.",
)
async def revoke_api_key(
    key_id: str,
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[AuthzService, Depends(_service)],
) -> None:
    await service.revoke_api_key(actor, decode_id("key", key_id))


@router.get(
    "/workspace/permissions",
    response_model=dict[str, list[str]],
    summary="The permission vocabulary",
    description="What can be granted, by resource type. Drives the admin UI.",
)
async def permission_catalogue(
    _: Annotated[Principal, Depends(current_principal)],
) -> dict[str, list[str]]:
    from cairn.authz.model import _RESOURCE_PERMISSIONS

    return {resource: sorted(perms) for resource, perms in _RESOURCE_PERMISSIONS.items()}


@router.get(
    "/me/rate-limit",
    response_model=dict[str, int],
    summary="Current rate limit state",
)
async def rate_limit_state(
    request: Request,
    _: Annotated[Principal, Depends(current_principal)],
) -> dict[str, int]:
    state = getattr(request.state, "rate_limit", None)
    if state is None:
        return {}
    return {
        "limit": state.limit,
        "remaining": state.remaining,
        "reset_seconds": state.reset_seconds,
    }
