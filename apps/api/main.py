"""Cairn API entrypoint.

One image, two roles (ADR-0002). ``CAIRN_ROLE`` selects which routers mount:

* ``data``    — retrieval, MCP. SLO-bound, read-only. (Phase 2+)
* ``control`` — identity, catalog, admin. Transactional, latency-tolerant.
* ``all``     — both; for development and the ``small`` deployment preset.

Zero code duplication, independent scaling, independent blast radius.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from apps.api.middleware import (
    DataPlaneAuthenticationMiddleware,
    DataPlaneRateLimitMiddleware,
    ErrorHandlingMiddleware,
    RequestContextMiddleware,
    RetrievalDeadlineMiddleware,
    UploadBodyLimitMiddleware,
)
from cairn.core import health
from cairn.core.config import Settings, get_settings
from cairn.core.db import dispose_engine
from cairn.core.errors import CairnError, FieldError, InternalError, ValidationFailed
from cairn.core.ids import new_public_id
from cairn.core.logging import configure_logging, get_logger
from cairn.core.telemetry import CONTENT_TYPE_LATEST, configure_tracing, render_metrics

__all__ = ["app", "create_app"]

log = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

DESCRIPTION = """
Cairn is a knowledge base platform and RAG infrastructure server.

Agents are clients: point at this URL, present an API key, send a query and a
knowledge base ID, and receive ranked, cited passages.

**Phase 0** ships authentication only. Knowledge base, ingestion, and retrieval
endpoints arrive in Phase 2.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = getattr(app.state, "settings", None) or get_settings()
    log.info(
        "app.starting",
        role=settings.role,
        environment=settings.environment,
        version="0.1.0",
    )
    configure_tracing(settings, app)
    retrieval_runtime = getattr(app.state, "retrieval_runtime", None)
    try:
        if retrieval_runtime is not None:
            await retrieval_runtime.start()
        if settings.serves_data_plane:
            # jieba loads its dictionary on first use (~1 s); pay that at
            # start-up, not inside the first full-text query's timeout.
            from cairn.embedding.sparse import warm_up

            await asyncio.to_thread(warm_up)
        async with AsyncExitStack() as stack:
            mcp_server = getattr(app.state, "mcp_server", None)
            if mcp_server is not None:
                await stack.enter_async_context(mcp_server.run())
            yield
    finally:
        if retrieval_runtime is not None:
            await retrieval_runtime.close()
        from cairn.core.cache import close_cache

        await close_cache()
        await dispose_engine()
        log.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(level=cfg.log_level, fmt=cfg.log_format)

    app = FastAPI(
        title="Cairn",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_url="/v1/openapi.json",
        docs_url="/docs" if not cfg.is_prod else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = cfg

    # ---- middleware ---------------------------------------------------------
    # Starlette applies user middleware with the LAST-ADDED as OUTERMOST, so
    # this block is written in reverse of execution order. Reading top-to-bottom
    # here gives innermost-to-outermost; a request traverses it bottom-up.
    app.add_middleware(UploadBodyLimitMiddleware)
    if cfg.serves_control_plane:
        from cairn.identity.middleware import (
            AuthenticationMiddleware,
            CsrfMiddleware,
            ForcedCredentialChangeMiddleware,
            RateLimitMiddleware,
        )

        app.add_middleware(RateLimitMiddleware)  # 5th
        app.add_middleware(CsrfMiddleware)  # 4th
        app.add_middleware(ForcedCredentialChangeMiddleware)  # 3rd
        app.add_middleware(AuthenticationMiddleware)  # 2nd
    else:
        app.add_middleware(DataPlaneRateLimitMiddleware)
        app.add_middleware(DataPlaneAuthenticationMiddleware)
    app.add_middleware(RequestContextMiddleware)  # 1st
    app.add_middleware(RetrievalDeadlineMiddleware, timeout_s=cfg.retrieval.request_timeout_s)
    app.add_middleware(ErrorHandlingMiddleware)  # outermost — catches the above

    # ---- exception handlers (for errors raised inside routes) ---------------
    app.add_exception_handler(CairnError, _cairn_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)

    # ---- routers ------------------------------------------------------------
    app.include_router(health.router)

    if cfg.serves_control_plane:
        from cairn.authz.router import router as authz_router
        from cairn.catalog.router import router as catalog_router
        from cairn.catalog.service import CatalogModelUsage
        from cairn.embedding.probe import EmbeddingModelProbe
        from cairn.identity.router import router as auth_router
        from cairn.identity.users_router import router as users_router
        from cairn.modelgw.router import router as model_router
        from cairn.modelgw.service import ModelManagementService
        from cairn.platform.mcp_router import router as mcp_management_router
        from cairn.platform.router import router as platform_router

        app.state.model_management_service = ModelManagementService(
            usage=CatalogModelUsage(), probe=EmbeddingModelProbe()
        )
        app.include_router(auth_router)
        app.include_router(users_router)
        app.include_router(authz_router)
        app.include_router(platform_router)
        app.include_router(mcp_management_router)
        app.include_router(catalog_router)
        app.include_router(model_router)
        log.info("app.routers_mounted", plane="control")

    if cfg.serves_data_plane:
        from cairn.retrieval.router import router as retrieval_router
        from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader, QueryEmbeddingRuntime
        from cairn.retrieval.service import RetrievalService

        app.state.retrieval_runtime = QueryEmbeddingRuntime(cfg.retrieval)
        app.state.retrieval_service = RetrievalService(
            runtime_loader=KnowledgeBaseRuntimeLoader(timeout_s=cfg.retrieval.runtime_timeout_s),
            embeddings=app.state.retrieval_runtime,
            search_timeout_s=cfg.retrieval.search_timeout_s,
            request_timeout_s=cfg.retrieval.request_timeout_s,
        )
        app.include_router(retrieval_router)
        from cairn.mcpserver.server import MCPServer
        from cairn.mcpserver.server import router as mcp_router

        if not cfg.mcp.managed:
            app.state.mcp_server = MCPServer(app.state.retrieval_service, cfg.mcp)
        app.include_router(mcp_router)
        log.info("app.routers_mounted", plane="data")

    if cfg.telemetry.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)

    return app


# --- handlers ----------------------------------------------------------------


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else new_public_id("req")


async def _cairn_error_handler(request: Request, exc: CairnError) -> JSONResponse:
    problem = exc.to_problem(instance=request.url.path, request_id=_request_id(request))
    headers: dict[str, str] = {}
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        problem,
        status_code=exc.http_status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        FieldError(
            field=".".join(str(part) for part in error["loc"][1:]) or "body",
            code=error["type"].upper(),
            detail=error["msg"],
        )
        for error in exc.errors()
    ]
    wrapped = ValidationFailed("The request contains invalid parameters.", errors=errors)
    return await _cairn_error_handler(request, wrapped)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # The detail is logged with the request id; the client gets only that id.
    # Never leak an internal message, SQL, or a stack trace (NFR-SEC-03).
    request_id = _request_id(request)
    log.exception(
        "request.unhandled_exception",
        request_id=request_id,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return await _cairn_error_handler(
        request,
        InternalError("An unexpected error occurred. Quote the request_id when reporting this."),
    )


app = create_app()
