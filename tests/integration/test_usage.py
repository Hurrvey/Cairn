from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import func, select
from tests.integration.test_ingestion_pipeline import pipeline_admin

from cairn.core.config import get_settings
from cairn.core.db import session_scope
from cairn.core.usage import STREAM, UsageEvent, UsagePublisher
from cairn.platform.models import UsageRecord
from cairn.platform.usage import UsageConsumer, usage_summary

__all__ = ["pipeline_admin"]


async def test_usage_delivery_replays_idempotently_and_reports_unknown_cost(pipeline_admin):
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    await redis.delete(STREAM)
    event = UsageEvent(
        workspace_id=pipeline_admin.workspace_id,
        principal_id=pipeline_admin.id,
        provider_id=uuid4(),
        model_id=uuid4(),
        request_id="req_test",
        attempt=1,
        operation="chat",
        outcome="success",
        input_tokens=100,
        output_tokens=20,
        input_price_per_1k=Decimal(".01"),
        output_price_per_1k=Decimal(".03"),
        latency_ms=10,
    )
    publisher = UsagePublisher(settings.redis_url)
    publisher.start()
    publisher.record(event)
    publisher.record(event)
    publisher.record(
        event.model_copy(update={"id": uuid4(), "input_tokens": None, "output_tokens": None})
    )
    await publisher.close()
    consumer = UsageConsumer(settings.redis_url)
    try:
        assert await consumer.flush_once() == 3
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(UsageRecord)) == 2
        summary = await usage_summary(pipeline_admin.workspace_id)
        assert summary["events"] == 2 and summary["unknown_cost_events"] == 1
        assert Decimal(summary["costs"]["USD"]) == Decimal(".0016")
        pending = await redis.xpending(STREAM, consumer.GROUP)
        assert pending["pending"] == 0
    finally:
        await consumer.close()
        await redis.aclose()


async def test_poison_usage_event_is_quarantined_and_acknowledged(pipeline_admin):
    from cairn.core.usage import DEAD_LETTER

    redis = Redis.from_url(get_settings().redis_url)
    await redis.delete(STREAM, DEAD_LETTER)
    await redis.xadd(STREAM, {"event": "not-json"})
    consumer = UsageConsumer(get_settings().redis_url)
    try:
        assert await consumer.flush_once() == 1
        assert await redis.xlen(DEAD_LETTER) == 1
        assert (await redis.xpending(STREAM, consumer.GROUP))["pending"] == 0
    finally:
        await consumer.close()
        await redis.aclose()


async def test_storage_overflow_event_is_quarantined_without_blocking_next_event(pipeline_admin):
    import json

    from cairn.core.usage import DEAD_LETTER

    redis = Redis.from_url(get_settings().redis_url)
    await redis.delete(STREAM, DEAD_LETTER)
    event = UsageEvent(
        workspace_id=pipeline_admin.workspace_id,
        provider_id=uuid4(),
        model_id=uuid4(),
        request_id="req_t",
        attempt=1,
        operation="chat",
        outcome="success",
        latency_ms=1,
    )
    raw = event.model_dump(mode="json")
    raw["input_tokens"] = 2**80
    await redis.xadd(STREAM, {"event": json.dumps(raw)})
    await redis.xadd(STREAM, {"event": event.model_dump_json()})
    consumer = UsageConsumer(get_settings().redis_url)
    try:
        assert await consumer.flush_once() == 2
        assert await redis.xlen(DEAD_LETTER) == 1
        assert (await usage_summary(pipeline_admin.workspace_id))["events"] == 1
    finally:
        await consumer.close()
        await redis.aclose()
