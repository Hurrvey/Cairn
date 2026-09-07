"""Request-scoped middleware: correlation, logging context, metrics, error rendering."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cairn.core.config import get_settings
from cairn.core.errors import CairnError, InternalError
from cairn.core.ids import new_public_id
from cairn.core.logging import bind_request_context, clear_request_context, get_logger
from cairn.core.telemetry import http_request_duration_seconds, http_requests_total

__all__ = ["ErrorHandlingMiddleware", "RequestContextMiddleware"]

log = get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER = "X-Request-Id"


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
