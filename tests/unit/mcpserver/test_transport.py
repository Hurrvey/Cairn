from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api.main import create_app
from cairn.authz.errors import ApiKeyInvalid
from cairn.authz.model import Principal
from cairn.authz.ratelimit import RateLimiter
from cairn.core.cache import InMemoryCache
from cairn.core.config import Settings


def settings(role: str = "data", **overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "role": role,
            "database_url": "postgresql+asyncpg://unused:unused@localhost/unused",
            "redis_url": "redis://localhost/0",
            "master_key": "unit-test-key",
            "retrieval": {"embedding_endpoints": {}, "tokenizer_files": {}},
            "telemetry": {"metrics_enabled": False},
            **overrides,
        }
    )


@pytest.mark.parametrize("role,status", [("data", 401), ("all", 401), ("control", 404)])
async def test_mcp_mount_and_bearer_requirement(role: str, status: int) -> None:
    app = create_app(settings(role))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        assert response.status_code == status
        capabilities = (await client.get("/v1/meta")).json()["capabilities"]
        assert capabilities["mcp"] is (role != "control")


@pytest.fixture
def authenticated(monkeypatch: pytest.MonkeyPatch) -> Principal:
    principal = Principal(
        type="api_key",
        id=uuid4(),
        workspace_id=uuid4(),
        role="user",
        username="mcp-test",
        accessible_kb_ids=frozenset(),
    )

    class Auth:
        async def authenticate_api_key(self, raw_key: str, *, ip: str | None) -> Principal:
            if raw_key != "cairn_sk_test":
                raise ApiKeyInvalid()
            return principal

    monkeypatch.setattr("cairn.authz.dataplane.get_dataplane_authz", lambda: Auth())
    monkeypatch.setattr(
        "cairn.authz.ratelimit.get_rate_limiter", lambda: RateLimiter(InMemoryCache())
    )
    return principal


HEADERS = {"Authorization": "Bearer cairn_sk_test", "Accept": "application/json, text/event-stream"}


def test_cookie_carried_api_key_cannot_authorize_mcp(authenticated: Principal) -> None:
    from cairn.mcpserver.server import current_mcp_principal

    request = Request(
        {
            "type": "http",
            "headers": [],
            "state": {
                "principal": authenticated,
                "auth_source": "api_key",
            },
        }
    )
    with pytest.raises(HTTPException) as caught:
        current_mcp_principal(request)
    assert caught.value.status_code == 401


async def test_initialize_list_and_tool_error_use_mcp_envelope(authenticated: Principal) -> None:
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "acceptance", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["protocolVersion"] == "2025-11-25"
        assert "tools" in response.json()["result"]["capabilities"]
        assert "mcp-session-id" not in response.headers
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert response.status_code == 200, response.text
        assert [tool["name"] for tool in response.json()["result"]["tools"]] == [
            "search_knowledge_base",
            "list_knowledge_bases",
            "get_document",
        ]
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_knowledge_base", "arguments": {"query": "Saturn"}},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["isError"] is True


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Origin": "https://evil.example"}, 403),
        ({"Host": "evil.example"}, 421),
        ({"MCP-Protocol-Version": "1900-01-01"}, 400),
        ({"Accept": "text/plain"}, 406),
    ],
)
async def test_transport_security(
    authenticated: Principal, headers: dict[str, str], status: int
) -> None:
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert response.status_code == status, response.text


async def test_body_bound_and_unsupported_stream(authenticated: Principal) -> None:
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.post(
            "/mcp", content=b"x" * (64 * 1024 + 1), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        assert (await client.get("/mcp")).status_code == 405
        assert (await client.delete("/mcp")).status_code == 405


async def test_chunked_body_malformed_rpc_and_notifications(authenticated: Principal) -> None:
    app = create_app(settings())

    async def oversized():
        for _block in range(65):
            yield b"x" * 1024

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.post(
            "/mcp", content=oversized(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        malformed = await client.post(
            "/mcp", content=b"not-json", headers={"Content-Type": "application/json"}
        )
        assert malformed.status_code == 400
        unknown = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "not-a-method"}
        )
        assert "error" in unknown.json()
        notification = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert notification.status_code == 202
        assert not notification.content


async def test_allowed_origin_and_host_can_be_configured(authenticated: Principal) -> None:
    app = create_app(
        settings(
            mcp={
                "allowed_hosts": ["kb.example.com"],
                "allowed_origins": ["https://agent.example.com"],
            }
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="https://kb.example.com",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={"Origin": "https://agent.example.com"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 200


async def test_origin_is_checked_even_for_unsupported_get(authenticated: Principal) -> None:
    app = create_app(settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.get("/mcp", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403


async def test_stateless_calls_do_not_require_the_initializing_replica(
    authenticated: Principal,
) -> None:
    first = create_app(settings())
    second = create_app(settings())
    async with (
        first.router.lifespan_context(first),
        second.router.lifespan_context(second),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(first),
            base_url="http://localhost",
            headers=HEADERS,
        ) as initial,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(second),
            base_url="http://localhost",
            headers=HEADERS,
        ) as replica,
    ):
        initialized = await initial.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "replica-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert "mcp-session-id" not in initialized.headers
        listed = await replica.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert listed.status_code == 200
        called = await replica.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge_base",
                    "arguments": {"query": "test"},
                },
            },
        )
        assert called.status_code == 200
        assert called.json()["result"]["isError"] is True


async def test_deadline_cancels_pending_tool(
    authenticated: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings(mcp={"request_timeout_s": 0.1}))
    cancelled = asyncio.Event()
    entered = asyncio.Event()

    async def slow_tool(*args, **kwargs):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(app.state.mcp_server._tools, "call_tool", slow_tool)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers=HEADERS,
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_knowledge_base", "arguments": {"query": "test"}},
            },
        )
        assert entered.is_set(), response.text
        assert response.status_code == 503, response.text
        await asyncio.wait_for(cancelled.wait(), timeout=2)
