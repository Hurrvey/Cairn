"""Auth endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from cairn.authz.model import ADMIN_PLATFORM_CAPABILITIES, Principal
from cairn.core.config import Settings, get_settings
from cairn.core.errors import AuthenticationFailed, ValidationFailed
from cairn.core.ids import encode_id
from cairn.core.logging import get_logger
from cairn.identity.schemas import (
    ChangePasswordRequest,
    CompleteSetupRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    UserResponse,
)
from cairn.identity.service import IdentityService, get_identity_service

__all__ = ["router"]

log = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["authentication"])

PROBLEM: dict[str, Any] = {"content": {"application/problem+json": {}}}


def _service() -> IdentityService:
    return get_identity_service()


def current_principal(request: Request) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise AuthenticationFailed("Authentication is required.")
    return principal


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_session_cookies(
    response: Response, *, session_token: str, csrf_token: str, settings: Settings
) -> None:
    auth = settings.auth
    response.set_cookie(
        auth.cookie_name,
        session_token,
        max_age=auth.session_ttl_minutes * 60,
        httponly=True,  # not readable by JavaScript
        secure=auth.cookie_secure,
        samesite="lax",
        path="/",
        domain=auth.cookie_domain,
    )
    # Readable by the SPA on purpose: it must echo this in X-CSRF-Token.
    response.set_cookie(
        auth.csrf_cookie_name,
        csrf_token,
        max_age=auth.session_ttl_minutes * 60,
        httponly=False,
        secure=auth.cookie_secure,
        samesite="lax",
        path="/",
        domain=auth.cookie_domain,
    )


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.auth.cookie_name, settings.auth.csrf_cookie_name):
        response.delete_cookie(name, path="/", domain=settings.auth.cookie_domain)


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    responses={401: PROBLEM},
    summary="Authenticate with a username and password",
    description=(
        'Returns `status: "ok"` and sets a session cookie on success.\n\n'
        "When the account must change its credentials first, returns "
        '`status: "password_change_required"` with a short-lived, scope-limited '
        "`change_token` and **no session cookie**. That token is accepted only by "
        "`POST /v1/auth/complete-initial-setup`; every other endpoint responds "
        "`403 PASSWORD_CHANGE_REQUIRED` until the change is completed."
    ),
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[IdentityService, Depends(_service)],
) -> LoginResponse:
    result = await service.login(
        body.username,
        body.password,
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    if result.status == "ok" and result.session_token and result.csrf_token:
        _set_session_cookies(
            response,
            session_token=result.session_token,
            csrf_token=result.csrf_token,
            settings=settings,
        )
    return LoginResponse.from_dto(result)


@router.post(
    "/auth/complete-initial-setup",
    response_model=LoginResponse,
    responses={400: PROBLEM, 401: PROBLEM, 409: PROBLEM},
    summary="Complete the mandatory credential change",
    description=(
        "Requires the `change_token` from login as a bearer token. Updates the "
        "password and, optionally, the username in a single transaction; if the "
        "username is taken the whole operation rolls back and the password is "
        "unchanged. On success every existing session is revoked and a new "
        "session cookie is issued."
    ),
)
async def complete_initial_setup(
    body: CompleteSetupRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[IdentityService, Depends(_service)],
) -> LoginResponse:
    if body.confirm_password is not None and body.confirm_password != body.new_password:
        raise ValidationFailed("The new password and its confirmation do not match.")

    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise AuthenticationFailed("A setup token is required.")

    result = await service.complete_initial_setup(
        header[7:].strip(),
        current_password=body.current_password,
        new_password=body.new_password,
        new_username=body.new_username,
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    if result.session_token and result.csrf_token:
        _set_session_cookies(
            response,
            session_token=result.session_token,
            csrf_token=result.csrf_token,
            settings=settings,
        )
    return LoginResponse.from_dto(result)


@router.post(
    "/auth/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={400: PROBLEM, 401: PROBLEM},
    summary="Change your own password",
)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(current_principal)],
    service: Annotated[IdentityService, Depends(_service)],
) -> None:
    await service.change_password(
        principal.id,
        current_password=body.current_password,
        new_password=body.new_password,
        ip=_client_ip(request),
    )
    # credential_version moved on, so the current cookie is now invalid.
    _clear_session_cookies(response, settings)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
)
async def logout(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[IdentityService, Depends(_service)],
) -> None:
    header = request.headers.get("Authorization", "")
    token = (
        header[7:].strip()
        if header.lower().startswith("bearer ")
        else request.cookies.get(settings.auth.cookie_name)
    )
    if token:
        await service.logout(token)
    _clear_session_cookies(response, settings)


@router.get(
    "/me",
    response_model=MeResponse,
    responses={401: PROBLEM},
    summary="The current principal",
    description=(
        "Allowlisted during a mandatory credential change so the UI can render "
        "the dialog for the correct account."
    ),
)
async def me(
    principal: Annotated[Principal, Depends(current_principal)],
    service: Annotated[IdentityService, Depends(_service)],
) -> MeResponse:
    user = await service.get_user(principal.id)
    permissions = sorted(ADMIN_PLATFORM_CAPABILITIES) if principal.is_admin else []
    return MeResponse(
        user=UserResponse.from_dto(user),
        workspace_id=encode_id("ws", principal.workspace_id),
        permissions=permissions,
        must_change_password=principal.must_change_password,
    )
