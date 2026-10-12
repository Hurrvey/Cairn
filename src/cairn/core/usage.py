"""Content-free model usage events; reporting is separate from quota reservation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from prometheus_client import Counter
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis

from cairn.core.ids import new_uuid
from cairn.core.telemetry import REGISTRY
from cairn.core.time import utcnow

STREAM = "cairn:usage:events"
DEAD_LETTER = "cairn:usage:invalid"
MAX_STREAM_EVENTS = 100_000
MAX_EVENT_BYTES = 8192
usage_delivery_total = Counter(
    "cairn_usage_delivery_total",
    "Usage delivery outcomes.",
    labelnames=("outcome",),
    registry=REGISTRY,
)


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID = Field(default_factory=new_uuid)
    at: AwareDatetime = Field(default_factory=utcnow)
    workspace_id: UUID
    principal_id: UUID | None = None
    knowledge_base_id: UUID | None = None
    provider_id: UUID
    model_id: UUID
    request_id: str = Field(max_length=128)
    attempt: int = Field(ge=1)
    operation: Literal["chat", "rerank", "embedding"]
    outcome: Literal["success", "error", "cancelled"]
    input_tokens: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    estimated: bool = False
    input_price_per_1k: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    output_price_per_1k: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=6)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    latency_ms: int = Field(ge=0)

    @field_validator("request_id")
    @classmethod
    def jsonb_string(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Request ID cannot contain NUL.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Request ID must be valid Unicode.") from exc
        return value

    @model_validator(mode="after")
    def storage_bounds(self) -> UsageEvent:
        cost = self.cost
        if cost is not None and (not cost.is_finite() or cost >= Decimal("1000000000000")):
            raise ValueError("Usage cost exceeds storage precision.")
        return self

    @property
    def cost(self) -> Decimal | None:
        if self.input_tokens is None or self.input_price_per_1k is None:
            return None
        if self.operation == "chat" and (
            self.output_tokens is None or self.output_price_per_1k is None
        ):
            return None
        return (
            Decimal(self.input_tokens) * self.input_price_per_1k
            + Decimal(self.output_tokens or 0) * (self.output_price_per_1k or Decimal(0))
        ) / 1000


class UsageSink(Protocol):
    def record(self, event: UsageEvent) -> None: ...


class NullUsageSink:
    def record(self, event: UsageEvent) -> None:
        return None


class UsagePublisher:
    """Bounded nonblocking producer, with a finite flush on shutdown."""

    def __init__(self, redis_url: str, *, capacity: int = 1024) -> None:
        self._redis: Redis = Redis.from_url(redis_url)
        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=capacity)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._inflight = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def record(self, event: UsageEvent) -> None:
        if self._closed:
            usage_delivery_total.labels(outcome="closed").inc()
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            usage_delivery_total.labels(outcome="queue_full").inc()

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            self._inflight = True
            try:
                raw = event.model_dump_json()
                if len(raw.encode()) > MAX_EVENT_BYTES:
                    usage_delivery_total.labels(outcome="too_large").inc()
                    continue
                for attempt in range(3):
                    try:
                        async with asyncio.timeout(1):
                            # Exact bounded retention: reporting may lose old unconsumed
                            # events, so an explicit counter accompanies the trim.
                            result = await self._redis.eval(
                                "local id=redis.call('XADD',KEYS[1],'*','event',ARGV[1]); "
                                "local n=redis.call('XTRIM',KEYS[1],'MAXLEN',ARGV[2]); return n",
                                1,
                                STREAM,
                                raw,
                                MAX_STREAM_EVENTS,
                            )
                        if int(result):
                            usage_delivery_total.labels(outcome="retention_trimmed").inc(
                                int(result)
                            )
                        usage_delivery_total.labels(outcome="published").inc()
                        self._inflight = False
                        break
                    except Exception:
                        if attempt == 2:
                            usage_delivery_total.labels(outcome="unavailable").inc()
                            self._inflight = False
                        else:
                            await asyncio.sleep(0.1 * (attempt + 1))
            except asyncio.CancelledError:
                if self._inflight:
                    usage_delivery_total.labels(outcome="shutdown_dropped").inc()
                raise
            finally:
                self._inflight = False
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            with suppress(TimeoutError):
                async with asyncio.timeout(5):
                    await self._queue.join()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._queue.qsize():
            usage_delivery_total.labels(outcome="shutdown_dropped").inc(self._queue.qsize())
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
        await self._redis.aclose()
