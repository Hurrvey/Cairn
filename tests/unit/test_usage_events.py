from datetime import datetime
from uuid import uuid4

import pytest

from cairn.core.usage import UsageEvent
from cairn.platform.usage import UsageConsumer


def event(**kwargs):
    return UsageEvent(
        workspace_id=uuid4(),
        provider_id=uuid4(),
        model_id=uuid4(),
        request_id="req_t",
        attempt=1,
        operation="chat",
        outcome="success",
        latency_ms=1,
        **kwargs,
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"input_tokens": 2**80},
        {"output_tokens": 2**80},
        {"at": datetime(2026, 1, 1)},
        {
            "input_tokens": 2**63 - 1,
            "output_tokens": 1,
            "input_price_per_1k": "999999",
            "output_price_per_1k": "1",
        },
        {"input_price_per_1k": "NaN"},
        {"input_price_per_1k": "Infinity"},
    ],
)
def test_usage_event_refuses_values_that_cannot_be_persisted(fields):
    with pytest.raises(ValueError):
        event(**fields)


@pytest.mark.parametrize("request_id", ["req\x00bad", "req\ud800bad"])
def test_usage_rejects_strings_postgres_jsonb_cannot_store(request_id):
    data = event().model_dump()
    data["request_id"] = request_id
    with pytest.raises(ValueError):
        UsageEvent.model_validate(data)


async def test_reclaim_resumes_from_returned_cursor_before_reading_new_events():
    class Redis:
        def __init__(self):
            self.cursors = []

        reads = 0

        async def xautoclaim(self, *args, **kwargs):
            self.cursors.append(kwargs["start_id"])
            return [b"123-0" if len(self.cursors) == 1 else b"0-0", [], []]

        async def xreadgroup(self, *args, **kwargs):
            self.reads += 1
            return []

    consumer = UsageConsumer("redis://localhost/15")
    original = consumer._redis
    fake = Redis()
    consumer._redis = fake
    consumer._ready = True
    try:
        await consumer.flush_once()
        assert fake.reads == 0
        await consumer.flush_once()
        assert fake.cursors == ["0-0", "123-0"] and fake.reads == 1
    finally:
        await original.aclose()
