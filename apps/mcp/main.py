"""Dedicated MCP supervisor; no Docker access or arbitrary command execution."""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress
from importlib import import_module
from time import monotonic
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from apps.api.middleware import (
    DataPlaneAuthenticationMiddleware,
    DataPlaneRateLimitMiddleware,
    ErrorHandlingMiddleware,
    RequestContextMiddleware,
)
from apps.mcp.supervisor import MCPListener, Supervisor
from cairn.core.cache import close_cache
from cairn.core.config import MCPSettings, get_settings
from cairn.core.db import dispose_engine, get_engine
from cairn.core.ids import new_public_id
from cairn.core.logging import configure_logging
from cairn.core.mcp_management import ManagedMCPConfig
from cairn.mcpserver.server import MCPServer, router
from cairn.platform.mcp import MCPManagement
from cairn.retrieval.runtime import QueryEmbeddingRuntime
from cairn.retrieval.service import RetrievalService


class RequestLogs:
    def __init__(self, app: Any, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.app = app
        self.queue = queue

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["headers"] = [
            (key, value)
            for key, value in scope.get("headers", [])
            if key.lower() != b"x-request-id"
        ]
        request_id = new_public_id("req")
        scope["headers"].append((b"x-request-id", request_id.encode()))
        start = monotonic()
        status = 500

        async def capture(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            if scope.get("path") == "/mcp":
                event = scope.get("state", {}).get("mcp_event", "request")
                record = {
                    "event": event
                    if event in {"discovery", "tool_error", "tool_success"}
                    else "request",
                    "request_id": request_id,
                    "status": status,
                    "duration_ms": int((monotonic() - start) * 1000),
                    "level": "ERROR"
                    if status >= 500
                    else "WARNING"
                    if status >= 400 or event == "tool_error"
                    else "INFO",
                }
                if not self.queue.full():
                    self.queue.put_nowait(record)


class ManagedListener(MCPListener):
    def __init__(self, app: Any, port: int, server: MCPServer) -> None:
        super().__init__(app, port)
        self.lifecycle = server.run()
        self.entered = False

    async def start(self) -> None:
        await self.lifecycle.__aenter__()
        self.entered = True
        try:
            await super().start()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        await super().stop()
        if self.entered:
            self.entered = False
            await self.lifecycle.__aexit__(None, None, None)


async def run() -> None:
    import_module("cairn.identity.models")
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    store = MCPManagement()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
    embeddings = QueryEmbeddingRuntime(settings.retrieval, master_key=settings.master_key)
    await embeddings.start()
    retrieval = RetrievalService(embeddings=embeddings)

    async def factory(config: ManagedMCPConfig) -> ManagedListener:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.add_middleware(DataPlaneRateLimitMiddleware)
        app.add_middleware(DataPlaneAuthenticationMiddleware)
        app.add_middleware(RequestContextMiddleware)
        app.add_middleware(ErrorHandlingMiddleware)
        mcp = MCPServer(retrieval, MCPSettings(**config.model_dump(exclude={"port", "auto_start"})))
        app.state.mcp_server = mcp
        app.include_router(router)

        @app.get("/healthz")
        async def health() -> Response:
            return Response("ok")

        return ManagedListener(RequestLogs(app, queue), config.port, mcp)

    supervisor = Supervisor(store, factory)
    gateway = MCPListener(supervisor.gateway, settings.mcp.gateway_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        await gateway.start()
        initialized = False
        while not stop.is_set():
            try:
                async with get_engine().connect() as connection:
                    leader = await connection.scalar(text("SELECT pg_try_advisory_lock(73439211)"))
                    await connection.commit()
                    if not leader:
                        raise RuntimeError("Another MCP supervisor owns this deployment")
                    try:
                        if not initialized:
                            await store.boot()
                            initialized = True
                        while not stop.is_set():
                            async with asyncio.timeout(8):
                                await connection.execute(text("SELECT 1"))
                                await connection.commit()
                                await supervisor.reconcile()
                                for _entry in range(min(queue.qsize(), 20)):
                                    await store.append_log(**queue.get_nowait())
                            with suppress(TimeoutError):
                                await asyncio.wait_for(stop.wait(), timeout=1)
                    finally:
                        await supervisor.pause()
                        with suppress(Exception):
                            await connection.execute(text("SELECT pg_advisory_unlock(73439211)"))
                            await connection.commit()
            except Exception:
                await supervisor.pause()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
    finally:
        await supervisor.close()
        await gateway.stop()
        await embeddings.close()
        await close_cache()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
