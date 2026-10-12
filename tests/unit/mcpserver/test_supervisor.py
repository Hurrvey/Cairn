import asyncio
import socket

import httpx
import pytest

from cairn.core.mcp_management import ManagedMCPConfig


async def test_listener_really_opens_closes_and_gateway_tracks_it() -> None:
    from starlette.responses import Response

    from apps.mcp.supervisor import Gateway, MCPListener

    async def application(scope, receive, send):
        await Response("ready")(scope, receive, send)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    listener = MCPListener(application, port, host="127.0.0.1")
    await listener.start()
    gateway = Gateway()
    gateway.port = port
    gateway.app = application
    async with httpx.AsyncClient() as client:
        assert (await client.post(f"http://127.0.0.1:{port}/mcp")).text == "ready"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(gateway), base_url="http://localhost"
    ) as client:
        assert (await client.post("/mcp")).text == "ready"
        gateway.port = None
        assert (await client.post("/mcp")).status_code == 503
    await gateway.close()
    await listener.stop()
    assert listener.task.done()


async def test_reconcile_failure_and_restart_generations() -> None:
    from apps.mcp.supervisor import Supervisor
    from cairn.core.mcp_management import MCPServiceView
    from cairn.core.time import utcnow

    class Store:
        def __init__(self):
            self.view = MCPServiceView(
                config=ManagedMCPConfig(),
                generation=1,
                observed_generation=0,
                desired_state="running",
                state="unknown",
                effective_port=None,
                started_at=None,
                heartbeat_at=None,
                last_error=None,
                allowed_ports=list(range(8081, 8091)),
                managed=True,
            )
            self.events = []

        async def get(self):
            return self.view

        async def observe(self, generation, **values):
            if generation != self.view.generation:
                return False
            update = {key: value for key, value in values.items() if key != "acknowledged"}
            update["heartbeat_at"] = utcnow()
            if values.get("acknowledged", True):
                update["observed_generation"] = generation
            self.view = self.view.model_copy(update=update)
            return True

        async def append_log(self, **values):
            self.events.append(values)

    class Listener:
        app = None

        def __init__(self):
            self.closed = False

        async def start(self):
            pass

        async def stop(self):
            self.closed = True

        @property
        def alive(self):
            return not self.closed

    store = Store()
    created = []

    async def factory(config):
        listener = Listener()
        created.append(listener)
        return listener

    supervisor = Supervisor(store, factory)
    await supervisor.reconcile()
    assert store.view.state == "running" and supervisor.gateway.port == 8081
    store.view = store.view.model_copy(update={"generation": 2, "desired_state": "stopped"})
    await supervisor.reconcile()
    assert store.view.state == "stopped" and supervisor.gateway.port is None and created[0].closed
    store.view = store.view.model_copy(update={"generation": 3, "desired_state": "running"})

    async def failure(config):
        raise OSError("private path secret")

    supervisor.factory = failure
    await supervisor.reconcile()
    assert store.view.state == "error" and "secret" not in store.view.last_error
    await supervisor.close()


async def test_port_collision_does_not_claim_running() -> None:
    from starlette.responses import Response

    from apps.mcp.supervisor import MCPListener

    async def app(scope, receive, send):
        await Response("ok")(scope, receive, send)

    bound = socket.socket()
    bound.bind(("127.0.0.1", 0))
    bound.listen(1)
    listener = MCPListener(app, bound.getsockname()[1], host="127.0.0.1")
    try:
        with pytest.raises(OSError):
            await listener.start()
        assert not listener.alive
    finally:
        await listener.stop()
        bound.close()


async def test_failed_listener_task_can_be_cleaned_up() -> None:
    from apps.mcp.supervisor import MCPListener

    async def app(scope, receive, send):
        pass

    async def fail():
        raise RuntimeError("private process detail")

    listener = MCPListener(app, 8081)
    listener.task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    await listener.stop()
    assert not listener.alive


async def test_managed_sdk_lifecycle_can_restart_without_cancelling_supervisor() -> None:
    from fastapi import FastAPI

    from apps.mcp.main import ManagedListener
    from cairn.core.config import MCPSettings
    from cairn.mcpserver.server import MCPServer, router
    from cairn.retrieval.service import RetrievalService

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    for _attempt in range(2):
        app = FastAPI()
        app.state.mcp_server = MCPServer(RetrievalService(), MCPSettings())
        app.include_router(router)

        @app.get("/healthz")
        async def health():
            return {"status": "ok"}

        listener = ManagedListener(app, port, app.state.mcp_server)
        await listener.start()
        async with httpx.AsyncClient() as client:
            assert (await client.post(f"http://127.0.0.1:{port}/mcp", json={})).status_code == 401
        await listener.stop()
