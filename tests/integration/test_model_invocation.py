from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from tests.integration.test_ingestion_pipeline import pipeline_admin

from cairn.core.errors import NotFound, PermissionDenied
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec
from cairn.modelgw.runtime import GatewayRuntime
from cairn.modelgw.service import ModelManagementService

__all__ = ["pipeline_admin"]


async def test_invocation_snapshot_is_permission_and_workspace_scoped(pipeline_admin):
    catalog = ModelCatalog()
    provider = await catalog.create_provider(
        pipeline_admin.workspace_id,
        name="chat",
        family="openai_compatible",
        base_url="http://localhost:39099/v1",
        config={"allow_private": True, "binding_revision": "1"},
    )
    model = await catalog.register_model(
        pipeline_admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id, model_key="text", display_name="Text", capability="chat"
        ),
    )
    service = ModelManagementService(catalog=catalog)
    snapshot = await service.invocation(pipeline_admin, model.id, "chat", request_id="req_t")
    assert snapshot.model.id == model.id
    assert snapshot.provider.id == provider.id
    denied = replace(pipeline_admin, role="user", permissions=frozenset(), resource_permissions={})
    with pytest.raises(PermissionDenied):
        await service.invocation(denied, model.id, "chat", request_id="req_t")
    allowed = replace(denied, resource_permissions={model.id: frozenset({"model:use"})})
    assert (
        await service.invocation(allowed, model.id, "chat", request_id="req_t")
    ).model.id == model.id
    with pytest.raises(NotFound):
        await service.invocation(
            replace(pipeline_admin, workspace_id=uuid4()), model.id, "chat", request_id="req_t"
        )
    with pytest.raises(PermissionDenied):
        await service.invocation(
            replace(allowed, type="api_key"), model.id, "chat", request_id="req_t"
        )
    assert await service.usable_models(denied) == []
    assert [item.id for item in await service.usable_models(allowed)] == [model.id]
    with pytest.raises(PermissionDenied):
        await service.usable_models(replace(allowed, type="api_key"))


async def test_chat_and_rerank_capability_probes_use_runtime(pipeline_admin):
    catalog = ModelCatalog()
    provider = await catalog.create_provider(
        pipeline_admin.workspace_id,
        name="chat",
        family="openai_compatible",
        base_url="https://provider.test/v1",
        config={"allow_private": False, "binding_revision": "1"},
    )
    model = await catalog.register_model(
        pipeline_admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id, model_key="text", display_name="Text", capability="chat"
        ),
    )
    runtime = GatewayRuntime(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        )
    )
    service = ModelManagementService(catalog=catalog, gateway=runtime)
    result = await service.test_model(pipeline_admin.workspace_id, model.id)
    assert result.healthy and result.tokens == 4


async def test_http_stream_through_local_provider_and_usage_pipeline(pipeline_admin):
    import asyncio
    import json

    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from tests.integration.test_ingestion_pipeline import STRONG_PASSWORD
    from tests.integration.test_mcp_acceptance import network_server

    from apps.api.main import create_app
    from cairn.core.config import get_settings
    from cairn.platform.usage import UsageConsumer, usage_summary

    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        if not body.get("stream"):
            return {
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }

        async def chunks():
            for text in ("Local ", "stream passed"):
                yield (
                    "data: "
                    + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": None}]})
                    + "\n\n"
                )
                await asyncio.sleep(0.01)
            yield (
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
            )

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with network_server(upstream) as provider_url:
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver"
            ) as client,
        ):
            await client.post(
                "/v1/auth/login", json={"username": "admin", "password": STRONG_PASSWORD}
            )
            client.headers["X-CSRF-Token"] = client.cookies["cairn_csrf"]
            response = await client.post(
                "/v1/model-providers",
                json={
                    "name": "Live chat",
                    "family": "openai_compatible",
                    "base_url": provider_url + "/v1",
                    "config": {"allow_private": True},
                },
            )
            assert response.status_code == 201, response.text
            model = await client.post(
                "/v1/models",
                json={
                    "provider_id": response.json()["id"],
                    "model_key": "local-chat",
                    "display_name": "Local chat",
                    "capability": "chat",
                    "cost_per_1k_input": "0.01",
                    "cost_per_1k_output": "0.02",
                },
            )
            assert model.status_code == 201, model.text
            id_ = model.json()["id"]
            result = await client.post(
                f"/v1/models/{id_}/chat",
                json={"messages": [{"role": "user", "content": "test"}], "stream": True},
            )
            assert result.status_code == 200, result.text
            assert (
                "Local " in result.text
                and "stream passed" in result.text
                and "event: completed" in result.text
            )
            await app.state.usage_publisher._queue.join()
            consumer = UsageConsumer(get_settings().redis_url)
            try:
                await consumer.flush_once()
                summary = await usage_summary(pipeline_admin.workspace_id)
                assert summary["events"] == 1 and summary["input_tokens"] == 4
            finally:
                await consumer.close()
