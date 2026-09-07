"""FastAPI authorization dependencies.

Authorization is expressed as a *dependency*, never as an ``if`` inside a
handler. Two reasons: it appears in the generated OpenAPI, and a route that
forgot one is detectable by walking the route table — which CI does
(``scripts/check_route_authz.py``, T-OPS-08).

Deny by default. There is no implicit-allow path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Request

from cairn.authz.model import Principal
from cairn.core.errors import AuthenticationFailed, PermissionDenied
from cairn.core.ids import InvalidIdError, decode_id
from cairn.core.logging import get_logger
from cairn.platform.audit import get_audit_service

__all__ = [
    "PUBLIC_ROUTE_MARKER",
    "current_principal",
    "optional_principal",
    "require_permission",
    "require_role",
]

log = get_logger(__name__)

#: Applied to routes that are intentionally unauthenticated, so the CI route
#: audit can tell "public on purpose" from "someone forgot".
PUBLIC_ROUTE_MARKER = "cairn:public"


def optional_principal(request: Request) -> Principal | None:
    value: Principal | None = getattr(request.state, "principal", None)
    return value


def current_principal(request: Request) -> Principal:
    principal = optional_principal(request)
    if principal is None:
        raise AuthenticationFailed("Authentication is required.")
    return principal


def require_role(role: str) -> Callable[[Request], Awaitable[Principal]]:
    async def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if principal.role != role or principal.type != "user":
            await _record_denial(request, principal, f"role:{role}", None)
            raise PermissionDenied(f"This action requires the {role} role.")
        return principal

    return dependency


def require_permission(
    permission: str, resource_param: str | None = None
) -> Callable[[Request], Awaitable[Principal]]:
    """Guard a route.

    ``resource_param`` names a path parameter holding a prefixed public id; when
    given, the check is scoped to that specific resource.
    """

    async def dependency(request: Request) -> Principal:
        principal = current_principal(request)

        resource_id: UUID | None = None
        if resource_param is not None:
            resource_id = _resource_id_from_path(request, resource_param)

        if not principal.can(permission, resource_id):
            await _record_denial(request, principal, permission, resource_id)
            # Name the missing permission. A bare "forbidden" turns a
            # five-second configuration fix into a support ticket.
            raise PermissionDenied(f"This action requires {permission}.")
        return principal

    return dependency


_PREFIX_BY_PARAM = {
    "kb_id": "kb",
    "knowledge_base_id": "kb",
    "document_id": "doc",
    "chunk_id": "chk",
    "pipeline_id": "pipe",
    "function_id": "fn",
    "model_id": "mdl",
    "golden_set_id": "gs",
    "user_id": "usr",
    "key_id": "key",
    "grant_id": "grant",
}


def _resource_id_from_path(request: Request, param: str) -> UUID | None:
    raw = request.path_params.get(param)
    if raw is None:
        return None
    prefix = _PREFIX_BY_PARAM.get(param)
    if prefix is None:
        return None
    try:
        return decode_id(prefix, str(raw))
    except InvalidIdError:
        # Malformed ids fail the permission check rather than 400ing here, so a
        # caller cannot probe which ids exist by comparing error shapes.
        return None


async def _record_denial(
    request: Request, principal: Principal, permission: str, resource_id: UUID | None
) -> None:
    log.info(
        "authz.denied",
        permission=permission,
        principal_id=str(principal.id),
        principal_type=principal.type,
        resource_id=str(resource_id) if resource_id else None,
        path=request.url.path,
    )
    try:
        await get_audit_service().record(
            workspace_id=principal.workspace_id,
            action="authz.denied",
            outcome="denied",
            actor_type=principal.type,
            actor_id=principal.id,
            actor_label=principal.username,
            resource_id=resource_id,
            request_id=getattr(request.state, "request_id", None),
            detail={"permission": permission, "path": request.url.path},
        )
    except Exception as exc:
        log.warning("authz.denial_audit_failed", error=type(exc).__name__)
