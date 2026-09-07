"""Authentication and forced-credential-change middleware.

Two middlewares, in this order:

1. :class:`AuthenticationMiddleware` resolves the bearer token or session cookie
   into ``request.state.principal`` (or ``None``).
2. :class:`ForcedCredentialChangeMiddleware` rejects every non-allowlisted route
   for a principal that must change its credentials.

Why (2) exists when :meth:`IdentityService.login` already refuses to mint a
session for such a principal: defence in depth. If a future code path — a
refresh endpoint, an SSO callback, a bug — ever issues a full session for a
flagged user, this makes that session inert for everything except the change
flow. The cost is one dictionary lookup per request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from cairn.authz.dataplane import KEY_PREFIX as API_KEY_PREFIX
from cairn.authz.model import Principal
from cairn.core.errors import PermissionDenied
from cairn.core.logging import bind_request_context, get_logger
from cairn.identity.errors import PasswordChangeRequired
from cairn.identity.hashing import constant_time_equals, hash_token
from cairn.identity.service import IdentityService, get_identity_service

__all__ = [
    "CREDENTIAL_SETUP_ALLOWLIST",
    "PUBLIC_PATHS",
    "AuthenticationMiddleware",
    "CsrfMiddleware",
    "ForcedCredentialChangeMiddleware",
    "RateLimitMiddleware",
]

log = get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]

#: The ONLY routes reachable while a credential change is outstanding (FR-A-05).
CREDENTIAL_SETUP_ALLOWLIST: frozenset[str] = frozenset(
    {
        "/v1/auth/complete-initial-setup",
        "/v1/auth/logout",
        "/v1/me",
    }
)

#: Routes that require no authentication at all (FR-A-01).
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/metrics",
        "/v1/meta",
        "/v1/auth/login",
        "/v1/openapi.json",
        "/docs",
        "/redoc",
        "/docs/oauth2-redirect",
    }
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Resolve credentials into ``request.state.principal``."""

    def __init__(self, app: object, *, service: IdentityService | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]  # reason: starlette ASGIApp is untyped
        self._service = service

    @property
    def service(self) -> IdentityService:
        return self._service or get_identity_service()

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        request.state.principal = None
        request.state.session = None
        request.state.auth_source = None

        token, source = _extract_token(request)
        if token is not None:
            principal = None
            if token.startswith(API_KEY_PREFIX):
                # Machine clients. Resolved through the cache-only data-plane
                # path so this branch stays within its < 5 ms budget even when
                # a control-plane endpoint is what is being called.
                principal = await self._resolve_api_key(request, token)
            else:
                resolved = await self.service.resolve_session(token)
                if resolved is not None:
                    principal, session_view = resolved
                    request.state.session = session_view

            if principal is not None:
                request.state.principal = principal
                request.state.auth_source = (
                    "api_key" if token.startswith(API_KEY_PREFIX) else source
                )
                bind_request_context(
                    principal_id=str(principal.id),
                    principal_type=principal.type,
                    workspace_id=str(principal.workspace_id),
                )

        return await call_next(request)

    async def _resolve_api_key(self, request: Request, token: str) -> Principal | None:
        from cairn.authz.dataplane import get_dataplane_authz
        from cairn.authz.errors import ApiKeyInvalid, ApiKeyIpNotAllowed

        client_ip = request.client.host if request.client else None
        try:
            return await get_dataplane_authz().authenticate_api_key(token, ip=client_ip)
        except ApiKeyIpNotAllowed:
            raise
        except ApiKeyInvalid:
            # Fall through as unauthenticated: the route's own dependency
            # produces the 401, so an invalid key and a missing one look the
            # same to a caller probing for valid key formats.
            return None


class ForcedCredentialChangeMiddleware(BaseHTTPMiddleware):
    """Block a flagged principal from everything except the change flow."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        principal = getattr(request.state, "principal", None)
        path = request.url.path

        if (
            principal is not None
            and principal.must_change_password
            and path not in CREDENTIAL_SETUP_ALLOWLIST
        ):
            log.info(
                "auth.blocked.password_change_required",
                path=path,
                principal_id=str(principal.id),
            )
            raise PasswordChangeRequired(
                "The credentials for this account must be changed before continuing."
            )

        # A scope-limited token may never reach anything outside the allowlist,
        # even if the flag were somehow cleared without the token being revoked.
        if (
            principal is not None
            and principal.is_restricted
            and path not in CREDENTIAL_SETUP_ALLOWLIST
        ):
            log.warning("auth.blocked.restricted_scope", path=path, scopes=sorted(principal.scopes))
            raise PermissionDenied("This token is not valid for that operation.")

        return await call_next(request)


class CsrfMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF for cookie-authenticated state changes (NFR-SEC-04).

    Bearer-authenticated requests are exempt: a bearer token is not an ambient
    credential, so a cross-site form post cannot carry it.
    """

    def __init__(self, app: object, *, header_name: str = "X-CSRF-Token") -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._header = header_name

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        if getattr(request.state, "auth_source", None) != "cookie":
            return await call_next(request)

        session_view = getattr(request.state, "session", None)
        if session_view is None:
            return await call_next(request)

        supplied = request.headers.get(self._header)
        origin = request.headers.get("Origin")
        host = request.headers.get("Host")

        if origin is not None and host is not None and origin.split("://")[-1] != host:
            log.warning("csrf.origin_mismatch", origin=origin, host=host)
            raise PermissionDenied("Cross-origin request rejected.")

        expected_hash = session_view.csrf_token_hash
        if supplied is None or expected_hash is None:
            raise PermissionDenied("A CSRF token is required for this request.")
        if not constant_time_equals(hash_token(supplied), expected_hash):
            raise PermissionDenied("The CSRF token is invalid.")

        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-principal limits and advertise the budget (FR-I-07).

    Headers go on every response, not only 429s: a client that can see its
    remaining budget throttles itself, whereas one that cannot retries into a
    wall and takes the service down with it.
    """

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        from cairn.authz.ratelimit import get_rate_limiter

        principal = getattr(request.state, "principal", None)
        if principal is None or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        state = await get_rate_limiter().check(principal)
        request.state.rate_limit = state
        response = await call_next(request)
        for header, value in state.headers().items():
            response.headers.setdefault(header, value)
        return response


def _extract_token(request: Request) -> tuple[str | None, str | None]:
    header = request.headers.get("Authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip(), "bearer"

    from cairn.core.config import get_settings

    cookie = request.cookies.get(get_settings().auth.cookie_name)
    if cookie:
        return cookie, "cookie"
    return None, None
