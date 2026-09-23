"""Request-scoped middleware: correlation, logging context, metrics, error rendering."""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from collections.abc import Awaitable, Callable

import anyio
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cairn.core.config import get_settings
from cairn.core.errors import CairnError, InternalError, PayloadTooLarge, UpstreamUnavailable
from cairn.core.ids import new_public_id
from cairn.core.logging import bind_request_context, clear_request_context, get_logger
from cairn.core.telemetry import http_request_duration_seconds, http_requests_total
from cairn.core.upload_limits import MAX_UPLOAD_REQUEST_BYTES

__all__ = [
    "DataPlaneAuthenticationMiddleware",
    "DataPlaneRateLimitMiddleware",
    "ErrorHandlingMiddleware",
    "RequestContextMiddleware",
    "RetrievalDeadlineMiddleware",
    "UploadBodyLimitMiddleware",
]

log = get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER = "X-Request-Id"
_UPLOAD_PATH = re.compile(r"^/v1/knowledge-bases/[^/]+/documents/upload$")


class UploadBodyLimitMiddleware:
    """Bound and pre-spool upload bytes before FastAPI parses multipart data."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_UPLOAD_REQUEST_BYTES) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not _UPLOAD_PATH.fullmatch(str(scope.get("path", "")))
        ):
            await self._app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self._max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass
        consumed = 0
        spool = _new_request_spool(self._max_body_bytes)
        try:
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    return
                body = message.get("body", b"")
                consumed += len(body)
                if consumed > self._max_body_bytes:
                    await self._reject(scope, receive, send)
                    return
                if body:
                    await anyio.to_thread.run_sync(spool.write, body)
                if not message.get("more_body", False):
                    break
            await anyio.to_thread.run_sync(spool.seek, 0)

            async def replay() -> Message:
                body = await anyio.to_thread.run_sync(spool.read, 1024 * 1024)
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": bool(body),
                }

            await self._app(scope, replay, send)
        finally:
            spool.close()

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive=receive)
        response = _problem_response(
            request,
            PayloadTooLarge("Document upload request exceeds the 51 MiB request limit."),
        )
        await response(scope, receive, send)


def _new_request_spool(max_body_bytes: int) -> tempfile.SpooledTemporaryFile[bytes]:
    return tempfile.SpooledTemporaryFile(max_size=min(2 * 1024 * 1024, max_body_bytes), mode="w+b")


class DataPlaneAuthenticationMiddleware(BaseHTTPMiddleware):
    """Authenticate API keys without importing identity/session services."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        from cairn.authz.dataplane import KEY_PREFIX, get_dataplane_authz
        from cairn.authz.errors import ApiKeyInvalid, ApiKeyIpNotAllowed

        request.state.principal = None
        request.state.auth_source = None
        header = request.headers.get("Authorization")
        token = header[7:].strip() if header and header.lower().startswith("bearer ") else None
        if token is not None and token.startswith(KEY_PREFIX):
            try:
                principal = await get_dataplane_authz().authenticate_api_key(
                    token, ip=request.client.host if request.client else None
                )
            except ApiKeyIpNotAllowed:
                raise
            except ApiKeyInvalid:
                principal = None
            if principal is not None:
                request.state.principal = principal
                request.state.auth_source = "api_key"
                bind_request_context(
                    principal_id=str(principal.id),
                    principal_type=principal.type,
                    workspace_id=str(principal.workspace_id),
                )
        return await call_next(request)


class DataPlaneRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            return await call_next(request)
        from cairn.authz.ratelimit import get_rate_limiter

        state = await get_rate_limiter().check(principal)
        response = await call_next(request)
        for header, value in state.headers().items():
            response.headers.setdefault(header, value)
        return response


class RetrievalDeadlineMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, timeout_s: float) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._timeout_s = timeout_s

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        if request.url.path != "/v1/retrieval/query":
            return await call_next(request)
        try:
            async with asyncio.timeout(self._timeout_s):
                return await call_next(request)
        except TimeoutError as exc:
            raise UpstreamUnavailable("The retrieval request exceeded its time limit.") from exc


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind log context, record timing.

    The id is accepted from the client so a caller can correlate their logs with
    ours, and always echoed back (FR-I-08).
    """

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_public_id("req")
        request.state.request_id = request_id

        clear_request_context()
        bind_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            role=get_settings().role,
        )

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = _route_label(request)
            role = get_settings().role
            http_requests_total.labels(
                method=request.method, route=route, status=str(status_code), role=role
            ).inc()
            http_request_duration_seconds.labels(
                method=request.method, route=route, role=role
            ).observe(elapsed)
            log.info(
                "request.completed",
                status=status_code,
                duration_ms=round(elapsed * 1000, 2),
                route=route,
            )
            clear_request_context()


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Render :class:`CairnError` raised *in middleware* as problem+json.

    FastAPI's exception handlers run inside ``ExceptionMiddleware``, which sits
    beneath the user middleware stack — so an error raised by, say, the forced
    credential-change guard would otherwise surface as a bare 500. This
    outermost layer closes that gap.
    """

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        try:
            return await call_next(request)
        except CairnError as exc:
            return _problem_response(request, exc)
        except Exception:
            log.exception("middleware.unhandled_exception", path=request.url.path)
            return _problem_response(
                request,
                InternalError(
                    "An unexpected error occurred. Quote the request_id when reporting this."
                ),
            )


def _problem_response(request: Request, exc: CairnError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or new_public_id("req")
    headers = {REQUEST_ID_HEADER: request_id}
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        exc.to_problem(instance=request.url.path, request_id=request_id),
        status_code=exc.http_status,
        media_type="application/problem+json",
        headers=headers,
    )


def _route_label(request: Request) -> str:
    """Use the *template* path, never the concrete one.

    ``/v1/users/{id}`` keeps metric cardinality bounded; ``/v1/users/usr_01HQ...``
    would create a new time series per user.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    return str(path_format) if path_format else "unmatched"
