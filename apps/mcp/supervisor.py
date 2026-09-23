"""Isolated listener lifecycle and stable compatibility gateway."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol

import httpx
import uvicorn
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from cairn.core.mcp_management import ManagedMCPConfig
from cairn.core.time import utcnow


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class MCPListener:
    def __init__(self, app: ASGIApp, port: int, *, host: str = "0.0.0.0") -> None:
        self.app = app
        self.port = port
        self.host = host
        self.server: _Server | None = None
        self.task: asyncio.Task[None] | None = None
        self.socket: socket.socket | None = None

    @property
    def alive(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self) -> None:
        listener = socket.socket()
        try:
            listener.bind((self.host, self.port))
            listener.listen(128)
            self.socket = listener
            self.server = _Server(
                uvicorn.Config(
                    self.app,
                    log_level="warning",
                    access_log=False,
                    lifespan="off",
                    timeout_graceful_shutdown=3,
                    proxy_headers=False,
                )
            )
            self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
            async with asyncio.timeout(5):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise RuntimeError("Listener startup failed")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
                response = await http.get(f"http://127.0.0.1:{self.port}/healthz")
                response.raise_for_status()
        except BaseException:
            await self.stop()
            listener.close()
            raise

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.socket:
            self.socket.close()


class Gateway:
    def __init__(self) -> None:
        self.port: int | None = None
        self.app: ASGIApp | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        if scope["path"] == "/healthz":
            await Response("ok")(scope, receive, send)
            return
        port = self.port
        if scope["path"] != "/mcp":
            await Response(status_code=404)(scope, receive, send)
            return
        if port is None or self.app is None:
            await Response("MCP is stopped or unavailable.", status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def close(self) -> None:
        self.port = None
        self.app = None


class Listener(Protocol):
    app: ASGIApp

    @property
    def alive(self) -> bool: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class Supervisor:
    def __init__(
        self, store: Any, factory: Callable[[ManagedMCPConfig], Awaitable[Listener]]
    ) -> None:
        self.store = store
        self.factory = factory
        self.gateway = Gateway()
        self.listener: Listener | None = None
        self.generation = 0
        self.started_at: datetime | None = None
        self.state = "stopped"
        self.error: str | None = None

    async def reconcile(self) -> None:
        view = await self.store.get()
        changed = self.generation != view.generation
        crashed = self.listener is not None and not self.listener.alive
        if changed or crashed:
            self.gateway.port = None
            await self.store.observe(
                view.generation,
                state="stopping" if self.listener else "starting",
                effective_port=None,
                acknowledged=False,
            )
            if self.listener:
                await self.listener.stop()
                self.listener = None
                await self.store.append_log(
                    event="crashed" if crashed else "stopped", level="ERROR" if crashed else "INFO"
                )
            self.generation = view.generation
            self.started_at = None
            self.error = None
            self.state = "stopped"
            if view.desired_state == "running":
                try:
                    if view.config.port not in view.allowed_ports:
                        raise ValueError("Port not published")
                    listener = await self.factory(view.config)
                    await listener.start()
                    self.listener = listener
                    self.started_at = utcnow()
                    self.state = "running"
                    self.gateway.port = view.config.port
                    self.gateway.app = listener.app
                    await self.store.append_log(event="started")
                except Exception:
                    self.error = (
                        "MCP could not start. Check port availability and deployment configuration."
                    )
                    self.state = "error"
                    await self.store.append_log(event="start_failed", level="ERROR")
            accepted = await self.store.observe(
                view.generation,
                state=self.state,
                effective_port=self.gateway.port,
                started_at=self.started_at,
                last_error=self.error,
            )
            if not accepted:
                await self.pause()
        else:
            await self.store.observe(
                view.generation,
                state=self.state,
                effective_port=self.gateway.port,
                started_at=self.started_at,
                last_error=self.error,
            )

    async def pause(self) -> None:
        self.gateway.port = None
        if self.listener:
            await self.listener.stop()
            self.listener = None
        self.generation = 0

    async def close(self) -> None:
        await self.pause()
        await self.gateway.close()
