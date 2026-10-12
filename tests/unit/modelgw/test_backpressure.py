import asyncio
import json

import httpx
import pytest
from tests.unit.modelgw.test_invocation import snapshot

from cairn.modelgw.invocation import ChatRequest
from cairn.modelgw.runtime import GatewayRuntime, ProviderCallError


async def test_deadline_under_queue_backpressure_delivers_error_after_buffer():
    class Many(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(50):
                yield (
                    "data: "
                    + json.dumps({"choices": [{"delta": {"content": "x"}, "finish_reason": None}]})
                    + "\n\n"
                ).encode()

    runtime = GatewayRuntime(
        timeout_s=0.03,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Many())),
    )
    stream = runtime.stream(snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}]))
    await anext(stream)
    await asyncio.sleep(0.06)
    async with asyncio.timeout(0.5):
        with pytest.raises(ProviderCallError):
            async for _ in stream:
                pass


async def test_upstream_cleanup_failure_never_leaves_consumer_hanging():
    class BrokenClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(50):
                yield b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'

        async def aclose(self):
            raise OSError("close failed")

    runtime = GatewayRuntime(
        timeout_s=0.03,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=BrokenClose())),
    )
    stream = runtime.stream(snapshot(), ChatRequest(messages=[{"role": "user", "content": "test"}]))
    await anext(stream)
    await asyncio.sleep(0.06)
    async with asyncio.timeout(0.5):
        with pytest.raises(ProviderCallError):
            async for _ in stream:
                pass
