from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from cairn_client import AsyncCairnClient, CairnClient, CairnError
from cairn_client.adapters import (
    CairnLangChainRetriever,
    CairnLlamaIndexRetriever,
    CairnRetrievalWarning,
)


def payload(*, failures=False, degraded=True, hits=True, all_failures=False):
    return {
        "request_id": "req_adapter",
        "results": [
            {
                "chunk_id": "chk_1",
                "document_id": "doc_1",
                "knowledge_base_id": "kb_1",
                "content": "adapter content",
                "score": 4.25,
                "scores": {"dense": 4.25},
                "metadata": {"index_version": 3, "cairn": {"hostile": True}},
            }
        ]
        if hits
        else [],
        "usage": {"index_versions": {"kb_1": 3}},
        "degraded": [{"stage": "rerank", "reason": "load_shed", "detail": "safe"}]
        if degraded
        else [],
        "partial_failures": (
            [
                {"knowledge_base_id": kb, "code": "UPSTREAM_FAILED", "detail": "safe"}
                for kb in (["kb_1", "kb_2"] if all_failures else ["kb_2"])
            ]
            if failures or all_failures
            else []
        ),
        "truncated_to_token_budget": False,
        "explain": None,
    }


def sync_client(response):
    return CairnClient(
        "https://example.test",
        "private-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        ),
    )


def async_client(response):
    return AsyncCairnClient(
        "https://example.test",
        "private-key",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        ),
    )


def test_langchain_actual_base_contract_metadata_warning_and_secret_hiding():
    from langchain_core.retrievers import BaseRetriever

    adapter = CairnLangChainRetriever(client=sync_client(payload()), knowledge_base_ids=["kb_1"])
    assert isinstance(adapter, BaseRetriever)
    with pytest.warns(CairnRetrievalWarning, match="rerank:load_shed"):
        docs = adapter.invoke("query-secret")
    assert docs[0].page_content == "adapter content"
    assert docs[0].metadata["cairn"]["score"] == 4.25
    assert docs[0].metadata["cairn"]["chunk_id"] == "chk_1"
    assert docs[0].metadata["cairn"]["scores"] == {"dense": 4.25}
    serialized = json.dumps(adapter.model_dump(mode="json"), default=str)
    assert "private-key" not in repr(adapter)
    assert "private-key" not in serialized
    assert "private-key" not in adapter.model_dump_json()
    assert "private-key" not in json.dumps(adapter.model_dump(mode="json"), default=str)
    assert "private-key" not in json.dumps(adapter.to_json(), default=str)
    assert "query-secret" not in str(docs[0].metadata)


def test_langchain_strict_partial_failure_raises_after_safe_warning():
    adapter = CairnLangChainRetriever(
        client=sync_client(payload(failures=True, degraded=False, hits=False)),
        knowledge_base_ids=["kb_1", "kb_2"],
        strict=True,
    )
    with pytest.warns(CairnRetrievalWarning, match="UPSTREAM_FAILED"):
        with pytest.raises(CairnError, match="target failures"):
            adapter.invoke("secret query")


def test_langchain_non_strict_empty_partial_failure_warns_and_returns_empty():
    adapter = CairnLangChainRetriever(
        client=sync_client(payload(failures=True, degraded=False, hits=False)),
        knowledge_base_ids=["kb_1", "kb_2"],
        strict=False,
    )
    with pytest.warns(CairnRetrievalWarning):
        assert adapter.invoke("query") == []


@pytest.mark.asyncio
async def test_langchain_async_uses_native_async_client_and_requires_it():
    adapter = CairnLangChainRetriever(
        client=async_client(payload(degraded=False)), knowledge_base_ids=["kb_1"]
    )
    docs = await adapter.ainvoke("query")
    assert docs[0].page_content == "adapter content"
    wrong = CairnLangChainRetriever(client=sync_client(payload()), knowledge_base_ids=["kb_1"])
    with pytest.raises(TypeError, match="AsyncCairnClient"):
        await wrong.ainvoke("query")


def test_llamaindex_actual_base_contract_native_score_and_hidden_client():
    from llama_index.core.base.base_retriever import BaseRetriever

    adapter = CairnLlamaIndexRetriever(sync_client(payload()), ["kb_1"])
    assert isinstance(adapter, BaseRetriever)
    with pytest.warns(CairnRetrievalWarning):
        nodes = adapter.retrieve("query-secret")
    assert nodes[0].text == "adapter content"
    assert nodes[0].score == 4.25
    assert nodes[0].metadata["cairn"]["document_id"] == "doc_1"
    assert "private-key" not in repr(adapter)


@pytest.mark.asyncio
async def test_llamaindex_async_contract_and_cancellation():
    client = async_client(payload(degraded=False))
    adapter = CairnLlamaIndexRetriever(client, ["kb_1"])
    nodes = await adapter.aretrieve("query")
    assert nodes[0].score == 4.25
    wrong = CairnLlamaIndexRetriever(sync_client(payload()), ["kb_1"])
    with pytest.raises(TypeError, match="AsyncCairnClient"):
        await wrong.aretrieve("query")


@pytest.mark.asyncio
async def test_async_adapter_propagates_client_cancellation():
    entered = asyncio.Event()
    closed = asyncio.Event()

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    client = AsyncCairnClient(
        "https://example.test",
        "private-key",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=StalledStream())
            )
        ),
    )
    adapter = CairnLlamaIndexRetriever(client, ["kb_1"])
    task = asyncio.create_task(adapter.aretrieve("query"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.parametrize("adapter_kind", ["langchain", "llamaindex"])
@pytest.mark.asyncio
async def test_async_non_strict_all_target_failure_raises(adapter_kind):
    response = payload(all_failures=True, degraded=False, hits=False)
    client = async_client(response)
    if adapter_kind == "langchain":
        adapter = CairnLangChainRetriever(
            client=client, knowledge_base_ids=["kb_1", "kb_2"], strict=False
        )

        async def invoke():
            return await adapter.ainvoke("query")
    else:
        adapter = CairnLlamaIndexRetriever(client, ["kb_1", "kb_2"], strict=False)

        async def invoke():
            return await adapter.aretrieve("query")

    with pytest.warns(CairnRetrievalWarning):
        with pytest.raises(CairnError, match="target failures"):
            await invoke()


@pytest.mark.parametrize("adapter_kind", ["langchain", "llamaindex"])
def test_non_strict_all_target_failure_raises(adapter_kind):
    response = payload(all_failures=True, degraded=False, hits=False)
    if adapter_kind == "langchain":
        adapter = CairnLangChainRetriever(
            client=sync_client(response),
            knowledge_base_ids=["kb_1", "kb_2"],
            strict=False,
        )

        def invoke():
            return adapter.invoke("query")
    else:
        adapter = CairnLlamaIndexRetriever(sync_client(response), ["kb_1", "kb_2"], strict=False)

        def invoke():
            return adapter.retrieve("query")

    with pytest.warns(CairnRetrievalWarning):
        with pytest.raises(CairnError, match="target failures"):
            invoke()


def test_llamaindex_strict_partial_failure_raises():
    adapter = CairnLlamaIndexRetriever(
        sync_client(payload(failures=True, degraded=False, hits=False)),
        ["kb_1", "kb_2"],
        strict=True,
    )
    with pytest.warns(CairnRetrievalWarning):
        with pytest.raises(CairnError, match="target failures"):
            adapter.retrieve("query")


def test_warning_is_bounded_and_sanitized():
    response = payload(failures=True, hits=False)
    echo_value = "AlphaNumericSecret123"
    response["request_id"] = echo_value
    response["degraded"] = [
        {"stage": echo_value, "reason": echo_value, "detail": echo_value} for _ in range(20)
    ]
    response["partial_failures"] = [
        {"knowledge_base_id": "kb_2", "code": echo_value, "detail": echo_value}
    ]
    adapter = CairnLangChainRetriever(
        client=sync_client(response), knowledge_base_ids=["kb_1"], strict=False
    )
    with pytest.warns(CairnRetrievalWarning) as caught:
        adapter.invoke("private query")
    message = str(caught[0].message)
    assert echo_value not in message
    assert "other:other" in message
    assert "private query" not in message
    assert len(message) < 600


def test_warning_preserves_known_runtime_failure_code():
    response = payload(failures=True, degraded=False, hits=False)
    response["partial_failures"][0]["code"] = "KB_RUNTIME_UNAVAILABLE"
    adapter = CairnLangChainRetriever(
        client=sync_client(response), knowledge_base_ids=["kb_1", "kb_2"], strict=False
    )
    with pytest.warns(CairnRetrievalWarning, match="KB_RUNTIME_UNAVAILABLE"):
        adapter.invoke("query")


def test_langchain_actual_callbacks_do_not_receive_client_secret():
    from langchain_core.callbacks import BaseCallbackHandler

    seen: list[str] = []

    class Recorder(BaseCallbackHandler):
        def on_retriever_start(self, serialized, query, **kwargs):
            seen.append(json.dumps(serialized, default=str))

        def on_retriever_error(self, error, **kwargs):
            seen.append(repr(error))

    echo_value = "AlphaNumericSecret123"
    response = payload(all_failures=True, degraded=False, hits=False)
    response["request_id"] = echo_value
    for failure in response["partial_failures"]:
        failure["code"] = echo_value
        failure["detail"] = echo_value
    adapter = CairnLangChainRetriever(
        client=CairnClient(
            "https://example.test",
            echo_value,
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
            ),
        ),
        knowledge_base_ids=["kb_1", "kb_2"],
        strict=False,
    )
    with pytest.warns(CairnRetrievalWarning):
        with pytest.raises(CairnError):
            adapter.invoke("private query", config={"callbacks": [Recorder()]})
    assert seen
    assert echo_value not in " ".join(seen)
    assert "private query" not in seen[0]


def test_llamaindex_dispatcher_span_instance_repr_hides_client_secret():
    from llama_index.core.instrumentation import get_dispatcher
    from llama_index.core.instrumentation.span_handlers.base import BaseSpanHandler

    seen: list[str] = []

    class Recorder(BaseSpanHandler[str]):
        def new_span(self, id_, bound_args, instance=None, **kwargs):
            seen.append(repr(instance))
            return id_

        def prepare_to_exit_span(self, id_, bound_args, instance=None, result=None, **kwargs):
            return id_

        def prepare_to_drop_span(self, id_, bound_args, instance=None, err=None, **kwargs):
            seen.append(repr(err))
            return id_

    handler = Recorder()
    dispatcher = get_dispatcher("llama_index.core.base.base_retriever")
    dispatcher.add_span_handler(handler)
    echo_value = "AlphaNumericSecret123"
    adapter = CairnLlamaIndexRetriever(
        CairnClient(
            "https://example.test",
            echo_value,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=payload(degraded=False))
                )
            ),
        ),
        ["kb_1"],
    )
    try:
        adapter.retrieve("query")
        assert seen
        assert echo_value not in " ".join(seen)
    finally:
        dispatcher.span_handlers.remove(handler)
