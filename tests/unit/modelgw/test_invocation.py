from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection
from cairn.modelgw.invocation import ChatRequest, InvocationSnapshot
from cairn.modelgw.runtime import GatewayRuntime, ProviderCallError


@pytest.mark.parametrize("capability", ["chat", "rerank"])
def test_model_registration_accepts_nonembedding_capabilities(capability):
    from cairn.modelgw.schemas import CreateModelRequest

    request = CreateModelRequest(
        provider_id="prov_test", model_key="model", display_name="Model", capability=capability
    )
    assert request.dimension is None and request.tokenizer_id is None


@pytest.mark.parametrize(
    "family,body,expected_path",
    [
        (
            "anthropic",
            {
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
            "/v1/messages",
        ),
        (
            "google",
            {
                "candidates": [{"content": {"parts": [{"text": "Hello"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
            },
            "/v1/models/test:generateContent",
        ),
        (
            "ollama",
            {
                "message": {"content": "Hello"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 7,
                "eval_count": 2,
            },
            "/v1/api/chat",
        ),
    ],
)
async def test_native_protocols_map_text_and_usage(family, body, expected_path):
    async def handler(request):
        assert request.url.path == expected_path
        return httpx.Response(200, json=body)

    result = await GatewayRuntime(transport=httpx.MockTransport(handler)).chat(
        snapshot(family),
        ChatRequest(
            messages=[{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hi"}]
        ),
    )
    assert result.content == "Hello" and result.input_tokens == 7 and result.output_tokens == 2


@pytest.mark.parametrize(
    "family,events",
    [
        (
            "anthropic",
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
                {"type": "message_stop"},
            ],
        ),
        (
            "google",
            [
                {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]},
                {
                    "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
                },
            ],
        ),
        (
            "ollama",
            [
                {"message": {"content": "Hello"}, "done": False},
                {"message": {"content": ""}, "done": True, "prompt_eval_count": 7, "eval_count": 2},
            ],
        ),
    ],
)
async def test_native_stream_protocols_map_terminal_and_usage(family, events):
    import json

    wire = "".join(
        ("" if family == "ollama" else "data: ")
        + json.dumps(event)
        + ("\n" if family == "ollama" else "\n\n")
        for event in events
    )
    runtime = GatewayRuntime(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=wire))
    )
    result = [
        event
        async for event in runtime.stream(
            snapshot(family), ChatRequest(messages=[{"role": "user", "content": "Hi"}])
        )
    ]
    assert result[0].delta == "Hello" and result[-1].type == "completed"
    assert result[-1].input_tokens == 7 and result[-1].output_tokens == 2


@pytest.mark.parametrize(
    "body", [{"choices": [None]}, {"choices": [{"message": {"content": "ok"}}], "usage": [1]}]
)
def test_malformed_nested_protocol_shape_is_a_validation_error(body):
    from cairn.modelgw.chat_protocols import chat_event

    with pytest.raises(ValueError):
        chat_event("openai_compatible", body, stream=False)


def test_gemini_thought_is_not_returned_as_answer():
    from cairn.modelgw.chat_protocols import chat_event

    with pytest.raises(ValueError):
        chat_event(
            "google",
            {
                "candidates": [
                    {
                        "content": {"parts": [{"thought": True, "text": "private thought"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
            stream=False,
        )


def snapshot(family="openai_compatible"):
    workspace, provider, model = uuid4(), uuid4(), uuid4()
    return InvocationSnapshot(
        model=ModelRef(
            id=model,
            provider_id=provider,
            provider_family=family,
            model_key="test",
            capability="chat",
        ),
        provider=ProviderRuntimeProjection(
            id=provider,
            workspace_id=workspace,
            family=family,
            base_url="https://provider.test/v1",
            binding_revision="1",
        ),
        workspace_id=workspace,
        principal_id=uuid4(),
        request_id="req_test",
    )


class Sink:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


async def test_chat_returns_domain_response_and_records_actual_usage():
    sink = Sink()

    async def handler(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        )

    runtime = GatewayRuntime(transport=httpx.MockTransport(handler), usage=sink)
    result = await runtime.chat(
        snapshot(), ChatRequest(messages=[{"role": "user", "content": "Hi"}])
    )
    assert result.content == "Hello"
    assert result.input_tokens == 7 and result.output_tokens == 2
    assert sink.events[0].outcome == "success"
    assert "Hi" not in sink.events[0].model_dump_json()


async def test_stream_maps_sse_and_usage_without_native_payloads():
    body = (
        b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
        b"data: [DONE]\n\n"
    )
    runtime = GatewayRuntime(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    events = [
        event
        async for event in runtime.stream(
            snapshot(), ChatRequest(messages=[{"role": "user", "content": "Hi"}])
        )
    ]
    assert events[0].type == "delta" and events[0].delta == "Hi"
    assert events[-1].type == "completed" and events[-1].input_tokens == 3


async def test_retry_records_each_attempt_and_does_not_echo_provider_error():
    sink, calls = Sink(), []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="private provider token")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        )

    result = await GatewayRuntime(transport=httpx.MockTransport(handler), usage=sink).chat(
        snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}])
    )
    assert result.content == "OK" and len(sink.events) == 2
    assert sink.events[0].outcome == "error" and sink.events[0].input_tokens is None
    assert "private provider token" not in str(sink.events)


async def test_deadline_includes_waiting_for_provider_slot():
    runtime = GatewayRuntime(
        timeout_s=0.03,
        concurrency=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    snap = snapshot()
    state = runtime.provider_state(snap.provider.id)
    await state.semaphore.acquire()
    try:
        with pytest.raises(ProviderCallError, match="time limit"):
            await runtime.chat(snap, ChatRequest(messages=[{"role": "user", "content": "test"}]))
    finally:
        state.semaphore.release()


async def test_stream_cancellation_closes_upstream_and_records_unknown_usage():
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"},"finish_reason":null}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    sink = Sink()
    runtime = GatewayRuntime(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
        usage=sink,
    )
    stream = runtime.stream(snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}]))
    assert (await anext(stream)).delta == "first"
    await stream.aclose()
    assert closed.is_set()
    assert sink.events[0].outcome == "cancelled" and sink.events[0].output_tokens is None


async def test_capability_mismatch_never_calls_provider():
    snap = snapshot()
    snap = replace(snap, model=replace(snap.model, capability="embedding"))
    with pytest.raises(ProviderCallError):
        await GatewayRuntime().chat(
            snap, ChatRequest(messages=[{"role": "user", "content": "test"}])
        )


async def test_stream_deadline_does_not_cancel_consumer_between_events():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"},"finish_reason":null}]}\n\n'
            await asyncio.Event().wait()

    runtime = GatewayRuntime(
        timeout_s=0.03,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow())),
    )
    stream = runtime.stream(snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}]))
    assert (await anext(stream)).type == "delta"
    await asyncio.sleep(0.06)
    with pytest.raises(ProviderCallError):
        await anext(stream)


async def test_closing_after_terminal_does_not_turn_success_into_cancelled():
    sink = Sink()
    body = (
        b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )
    runtime = GatewayRuntime(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)), usage=sink
    )
    stream = runtime.stream(snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}]))
    await anext(stream)
    assert (await anext(stream)).type == "completed"
    await stream.aclose()
    assert [event.outcome for event in sink.events] == ["success"]


@pytest.mark.parametrize(
    "family,body",
    [
        ("openai_compatible", {}),
        ("google", {}),
        ("anthropic", {"content": [{"type": "tool_use", "input": {}}], "stop_reason": "tool_use"}),
        (
            "openai_compatible",
            {"choices": [{"message": {"audio": {}, "content": ""}, "finish_reason": "stop"}]},
        ),
    ],
)
def test_non_text_or_missing_output_is_not_silently_successful(family, body):
    from cairn.modelgw.chat_protocols import chat_event

    with pytest.raises(ValueError):
        chat_event(family, body, stream=False)


async def test_retry_after_longer_than_deadline_never_retries_early():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    with pytest.raises(ProviderCallError):
        await GatewayRuntime(timeout_s=0.03, transport=httpx.MockTransport(handler)).chat(
            snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}])
        )
    assert len(calls) == 1


async def test_closed_circuit_rejection_does_not_extend_the_breaker():
    import time

    snap = snapshot()
    runtime = GatewayRuntime()
    state = runtime.provider_state(snap.provider.id)
    state.open_until = time.monotonic() + 20
    prior = state.open_until
    with pytest.raises(ProviderCallError):
        await runtime.chat(snap, ChatRequest(messages=[{"role": "user", "content": "test"}]))
    assert state.open_until == prior and state.failures == []


async def test_usage_sink_failure_does_not_replace_a_successful_response():
    class Broken:
        def record(self, event):
            raise RuntimeError("reporting unavailable")

    response = httpx.Response(
        200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
    )
    result = await GatewayRuntime(
        transport=httpx.MockTransport(lambda request: response), usage=Broken()
    ).chat(snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}]))
    assert result.content == "OK"


async def test_registered_rerank_reuses_retry_policy_and_orders_scores():
    from cairn.modelgw.invocation import RerankRequest

    snap = snapshot("tei")
    snap = replace(snap, model=replace(snap.model, capability="rerank"))
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"index": 1, "score": 0.2}, {"index": 0, "score": 0.9}])

    sink = Sink()
    result = await GatewayRuntime(transport=httpx.MockTransport(handler), usage=sink).rerank(
        snap, RerankRequest(query="test", documents=["a", "b"])
    )
    assert result.scores == [0.9, 0.2] and len(sink.events) == 2
