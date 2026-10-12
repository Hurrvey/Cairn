from __future__ import annotations

from uuid import uuid4

import httpx
import pytest


async def test_tei_rerank_orders_scores_by_input_index() -> None:
    from cairn.core.config import RerankEndpointSettings
    from cairn.retrieval.rerank import RuntimeReranker

    workspace = uuid4()
    calls = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}])

    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="https://rerank.example",
                workspace_ids=[workspace],
                dialect="tei",
            )
        },
        transport=httpx.MockTransport(serve),
    )
    scores = await engine.score(workspace, "company", "planet", ["ocean", "Saturn"], timeout_s=1)
    assert scores == [0.1, 0.9]
    assert calls[0].url.path == "/rerank"


async def test_rerank_rejects_wrong_workspace_before_http() -> None:
    from cairn.core.config import RerankEndpointSettings
    from cairn.core.errors import PermissionDenied
    from cairn.retrieval.rerank import RuntimeReranker

    def serve(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unauthorized workspace reached rerank endpoint")

    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="https://rerank.example", workspace_ids=[uuid4()]
            )
        },
        transport=httpx.MockTransport(serve),
    )
    with pytest.raises(PermissionDenied):
        await engine.score(uuid4(), "company", "planet", ["Saturn"], timeout_s=1)


@pytest.mark.parametrize(
    "response",
    [
        [{"index": 0, "score": 0.1}, {"index": 0, "score": 0.2}],
        [{"index": 8, "score": 0.1}],
        [{"index": 0, "score": "oops"}],
        [{"index": True, "score": 0.1}],
    ],
)
async def test_rerank_rejects_invalid_or_incomplete_scores(response: object) -> None:
    from cairn.core.config import RerankEndpointSettings
    from cairn.retrieval.rerank import RerankUnavailable, RuntimeReranker

    workspace = uuid4()
    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="https://rerank.example", workspace_ids=[workspace]
            )
        },
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    with pytest.raises(RerankUnavailable):
        await engine.score(workspace, "company", "planet", ["Saturn", "Mars"], timeout_s=1)


async def test_infinity_rerank_uses_named_model_and_results() -> None:
    import json

    from cairn.core.config import RerankEndpointSettings
    from cairn.retrieval.rerank import RuntimeReranker

    workspace = uuid4()

    def serve(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "company-cross-encoder"
        assert body["documents"] == ["Saturn"] and body["top_n"] == 1
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.8}]})

    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="https://rerank.example/v1",
                workspace_ids=[workspace],
                dialect="infinity",
                model_name="company-cross-encoder",
            )
        },
        transport=httpx.MockTransport(serve),
    )
    assert await engine.score(workspace, "company", "planet", ["Saturn"], timeout_s=1) == [0.8]


async def test_rerank_deadline_bounds_unresponsive_provider() -> None:
    import asyncio

    from cairn.core.config import RerankEndpointSettings
    from cairn.retrieval.rerank import RerankUnavailable, RuntimeReranker

    workspace = uuid4()

    async def serve(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="https://rerank.example", workspace_ids=[workspace]
            )
        },
        transport=httpx.MockTransport(serve),
    )
    with pytest.raises(RerankUnavailable):
        await asyncio.wait_for(
            engine.score(workspace, "company", "planet", ["Saturn"], timeout_s=0.01),
            1,
        )


async def test_rerank_uses_ssrf_guard_for_untrusted_private_endpoint() -> None:
    from cairn.core.config import RerankEndpointSettings
    from cairn.retrieval.rerank import RerankUnavailable, RuntimeReranker

    workspace = uuid4()
    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="http://127.0.0.1:9",
                workspace_ids=[workspace],
            )
        }
    )
    with pytest.raises(RerankUnavailable):
        await engine.score(workspace, "company", "planet", ["Saturn"], timeout_s=1)


async def test_rerank_rejects_request_byte_overflow_before_network() -> None:
    from cairn.core.config import RerankEndpointSettings
    from cairn.retrieval.rerank import RerankUnavailable, RuntimeReranker

    workspace = uuid4()

    def serve(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Oversized request reached the network")

    engine = RuntimeReranker(
        {
            "company": RerankEndpointSettings(
                base_url="https://rerank.example",
                workspace_ids=[workspace],
                max_request_bytes=1024,
            )
        },
        transport=httpx.MockTransport(serve),
    )
    with pytest.raises(RerankUnavailable):
        await engine.score(workspace, "company", "planet", ["文档" * 500], timeout_s=1)
