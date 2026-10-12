"""Persist reporting events after Redis delivery; SQL commit precedes ACK."""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert

from cairn.core.db import session_scope, transaction
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.core.usage import DEAD_LETTER, MAX_EVENT_BYTES, STREAM, UsageEvent, usage_delivery_total
from cairn.platform.models import UsageRecord, Workspace

log = get_logger(__name__)


class UsageConsumer:
    GROUP = "usage-persist-v1"

    def __init__(self, redis_url: str) -> None:
        self._redis: Redis = Redis.from_url(redis_url)
        self._name = f"{socket.gethostname()}-{uuid4().hex[:12]}"
        self._ready = False
        self._reclaim_cursor = "0-0"

    async def flush_once(self) -> int:
        if not self._ready:
            try:
                await self._redis.xgroup_create(STREAM, self.GROUP, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._ready = True
        reclaimed = await self._redis.xautoclaim(
            STREAM, self.GROUP, self._name, 60_000, start_id=self._reclaim_cursor, count=100
        )
        self._reclaim_cursor = (
            reclaimed[0].decode() if isinstance(reclaimed[0], bytes) else str(reclaimed[0])
        )
        rows = list(reclaimed[1])
        if len(reclaimed) > 2 and reclaimed[2]:
            usage_delivery_total.labels(outcome="pending_trimmed").inc(len(reclaimed[2]))
        if not rows and self._reclaim_cursor == "0-0":
            batches = await self._redis.xreadgroup(self.GROUP, self._name, {STREAM: ">"}, count=100)
            rows = [row for _key, batch in cast("list[Any]", batches) for row in batch]
        for message_id, fields in rows:
            raw = fields.get(b"event", b"")
            try:
                if len(raw) > MAX_EVENT_BYTES:
                    raise ValueError("oversized usage event")
                event = UsageEvent.model_validate_json(raw)
            except ValueError:
                await self._redis.xadd(
                    DEAD_LETTER,
                    {"source_id": message_id, "reason": "invalid_event"},
                    maxlen=1000,
                    approximate=False,
                )
                await self._redis.xack(STREAM, self.GROUP, message_id)
                usage_delivery_total.labels(outcome="invalid_event").inc()
                continue
            async with transaction() as session:
                # Deleted workspaces have no reporting owner; acknowledge rather than
                # leaving their events retrying a foreign-key failure forever.
                if await session.get(Workspace, event.workspace_id) is not None:
                    await session.execute(
                        insert(UsageRecord)
                        .values(
                            id=event.id,
                            workspace_id=event.workspace_id,
                            at=event.at,
                            model_id=event.model_id,
                            principal_id=event.principal_id,
                            knowledge_base_id=event.knowledge_base_id,
                            operation=event.operation,
                            outcome=event.outcome,
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                            cost=event.cost,
                            currency=event.currency,
                            estimated=event.estimated,
                            event=event.model_dump(mode="json"),
                        )
                        .on_conflict_do_nothing(index_elements=[UsageRecord.id])
                    )
            await self._redis.xack(STREAM, self.GROUP, message_id)
            usage_delivery_total.labels(outcome="persisted").inc()
        return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with asyncio.timeout(10):
                    count = await self.flush_once()
                if count:
                    continue
            except Exception as exc:
                log.warning("usage.persist_failed", error=type(exc).__name__)
                self._ready = False
            try:
                async with asyncio.timeout(1):
                    await stop.wait()
            except TimeoutError:
                pass

    async def close(self) -> None:
        await self._redis.aclose()


async def usage_summary(
    workspace_id: UUID,
    *,
    since: datetime | None = None,
    model_id: UUID | None = None,
    principal_id: UUID | None = None,
) -> dict[str, Any]:
    filters = [
        UsageRecord.workspace_id == workspace_id,
        UsageRecord.at >= (since or utcnow() - timedelta(days=30)),
    ]
    if model_id is not None:
        filters.append(UsageRecord.model_id == model_id)
    if principal_id is not None:
        filters.append(UsageRecord.principal_id == principal_id)
    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.sum(UsageRecord.input_tokens),
                    func.sum(UsageRecord.output_tokens),
                    func.sum(case((UsageRecord.cost.is_(None), 1), else_=0)),
                    func.sum(case((UsageRecord.estimated, 1), else_=0)),
                    func.sum(case((UsageRecord.input_tokens.is_(None), 1), else_=0)),
                    func.sum(case((UsageRecord.output_tokens.is_(None), 1), else_=0)),
                )
                .select_from(UsageRecord)
                .where(*filters)
            )
        ).one()
        costs = (
            await session.execute(
                select(UsageRecord.currency, func.sum(UsageRecord.cost))
                .where(*filters)
                .group_by(UsageRecord.currency)
            )
        ).all()
        recent = (
            await session.scalars(
                select(UsageRecord).where(*filters).order_by(UsageRecord.at.desc()).limit(100)
            )
        ).all()
    count, inp, out, unknown, estimated, unknown_input, unknown_output = row
    return {
        "events": count,
        "input_tokens": inp,
        "output_tokens": out,
        "unknown_cost_events": int(unknown or 0),
        "unknown_input_events": int(unknown_input or 0),
        "unknown_output_events": int(unknown_output or 0),
        "priced_events": count - int(unknown or 0),
        "cost_coverage": (count - int(unknown or 0)) / count if count else None,
        "estimated_events": int(estimated or 0),
        "costs": {currency: str(cost) for currency, cost in costs if cost is not None},
        "completeness": "best_effort_reporting",
        "recent": [record.event for record in recent],
    }
