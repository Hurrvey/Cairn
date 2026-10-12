"""Independent MCP SDK/network acceptance over actual PostgreSQL and Redis."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp.client.streamable_http import streamable_http_client
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from apps.api.main import create_app
from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.catalog.dto import CreateKbSpec
from cairn.core.config import get_settings
from cairn.core.ids import encode_id
from mcp import ClientSession

__all__ = ["pipeline_admin"]


@asynccontextmanager
async def network_server(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if running.done():
                    await running
                    raise RuntimeError("Server stopped during startup")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(15):
                await running
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            listener.close()


async def test_sdk_network_search_scopes_revocation_and_no_key_leak(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cairn.authz.dataplane._CACHE_TTL_SECONDS", 1)
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    other = await catalog.create_kb(
        pipeline_admin,
        CreateKbSpec(
            name="Other MCP scope",
            embedding_model_id=kb.embedding_model_id,
            vector_binding_id=kb.vector_binding_id,
            object_binding_id=kb.object_binding_id,
        ),
    )
    authz = AuthzService()
    actor = await authz.principal_for_user(pipeline_admin.id)
    assert actor is not None
    key, raw = await authz.create_api_key(
        actor, ApiKeySpec(name="mcp-search", scopes=["kb:query"], kb_ids=[kb.id])
    )
    _other_key, other_raw = await authz.create_api_key(
        actor, ApiKeySpec(name="mcp-other-scope", scopes=["kb:query"], kb_ids=[other.id])
    )
    _ip_key, ip_raw = await authz.create_api_key(
        actor,
        ApiKeySpec(
            name="mcp-ip-restricted",
            scopes=["kb:query"],
            kb_ids=[kb.id],
            ip_allowlist=["192.0.2.0/24"],
        ),
    )
    app = create_app(get_settings().model_copy(update={"role": "data"}))

    async def embed_query(model, query, **kwargs):
        return [1.0, 1.0, 1.0], 1, None

    app.state.retrieval_runtime.embed_query = embed_query
    async with network_server(app) as base_url:

        async def exercise(token: str, allowed: bool) -> None:
            async with (
                httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
                streamable_http_client(f"{base_url}/mcp", http_client=http) as (
                    reader,
                    writer,
                    session_id,
                ),
                ClientSession(reader, writer) as session,
            ):
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "Cairn"
                assert session_id() is None
                listed = await session.list_tools()
                assert len(listed.tools) == 3
                assert raw not in listed.model_dump_json()
                scoped = kb.id if allowed else other.id
                foreign = other.id if allowed else kb.id
                schema = listed.tools[0].inputSchema["properties"]["knowledge_base_id"]
                assert schema["enum"] == [encode_id("kb", scoped)]
                result = await session.call_tool("search_knowledge_base", {"query": "paragraph"})
                if allowed:
                    assert not result.isError, result
                    assert result.structuredContent is not None
                    hit = result.structuredContent["results"][0]
                    assert hit["document_id"] == encode_id("doc", registration.document.id)
                    assert hit["knowledge_base_id"] == encode_id("kb", kb.id)
                    assert hit["content"]
                else:
                    assert result.isError
                denied = await session.call_tool(
                    "search_knowledge_base",
                    {
                        "query": "paragraph",
                        "knowledge_base_id": encode_id("kb", foreign),
                    },
                )
                assert denied.isError
                assert raw not in denied.model_dump_json()

                listing = await session.call_tool("list_knowledge_bases", {})
                assert not listing.isError
                entries = listing.structuredContent["knowledge_bases"]
                assert [entry["id"] for entry in entries] == [encode_id("kb", scoped)]
                unreadable = await session.call_tool(
                    "get_document", {"document_id": encode_id("doc", registration.document.id)}
                )
                assert unreadable.isError

        await asyncio.gather(exercise(raw, True), exercise(other_raw, False))
        _reader_key, reader_raw = await authz.create_api_key(
            actor, ApiKeySpec(name="mcp-reader", scopes=["kb:read"], kb_ids=[kb.id])
        )
        async with (
            httpx.AsyncClient(headers={"Authorization": f"Bearer {reader_raw}"}) as http,
            streamable_http_client(f"{base_url}/mcp", http_client=http) as (reader, writer, _sid),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            document = await session.call_tool(
                "get_document", {"document_id": encode_id("doc", registration.document.id)}
            )
            assert not document.isError, document
            assert document.structuredContent["passages"]
            resources = await session.list_resources()
            assert len(resources.resources) == 1
            templates = await session.list_resource_templates()
            assert len(templates.resourceTemplates) == 1
            content = await session.read_resource(
                f"cairn://kb/{encode_id('kb', kb.id)}/documents/"
                f"{encode_id('doc', registration.document.id)}"
            )
            assert "passages" in content.contents[0].text
        await authz.revoke_api_key(actor, key.id)
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"{base_url}/mcp",
                headers={"Authorization": f"Bearer {raw}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        grant = next(
            item
            for item in await authz.list_grants(actor.workspace_id, "user", actor.id)
            if item.resource_id == other.id
        )
        await authz.revoke_grant(actor, grant.id)
        await asyncio.sleep(1.1)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {other_raw}", "Accept": "application/json"}
        ) as http:
            listed = await http.post(
                f"{base_url}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            assert listed.status_code == 200
            assert encode_id("kb", other.id) not in listed.text
            response = await http.post(
                f"{base_url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge_base",
                        "arguments": {"query": "paragraph"},
                    },
                },
            )
            assert response.json()["result"]["isError"] is True
            denied_ip = await http.post(
                f"{base_url}/mcp",
                headers={"Authorization": f"Bearer {ip_raw}"},
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            )
            assert denied_ip.status_code == 403


async def test_mcp_rejects_cookie_session_and_applies_key_rate_limit(
    pipeline_admin: Principal,
    client: httpx.AsyncClient,
) -> None:
    from tests.integration.test_ingestion_pipeline import STRONG_PASSWORD

    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": STRONG_PASSWORD}
    )
    assert login.status_code == 200
    response = await client.get("/mcp")
    assert response.status_code == 401
    actor = await AuthzService().principal_for_user(pipeline_admin.id)
    assert actor is not None
    _key, raw = await AuthzService().create_api_key(
        actor, ApiKeySpec(name="mcp-rate", scopes=["kb:query"], rate_limit_rpm=1)
    )
    app = create_app(get_settings().model_copy(update={"role": "data"}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://localhost",
            headers={"Authorization": f"Bearer {raw}", "Accept": "application/json"},
        ) as http,
    ):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        first = await http.post("/mcp", json=body)
        assert first.status_code == 200, first.text
        assert first.headers["RateLimit-Limit"] == "1"
        assert (await http.post("/mcp", json=body)).status_code == 429
