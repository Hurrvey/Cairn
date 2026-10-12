"""Pure, SSRF-guarded model calls; no catalog, ORM or platform dependencies."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx

from cairn.core.config import get_settings
from cairn.core.errors import UpstreamUnavailable
from cairn.core.http import SafeTransport, read_capped
from cairn.core.ids import encode_id
from cairn.core.usage import NullUsageSink, UsageEvent, UsageSink, usage_delivery_total
from cairn.modelgw.chat_protocols import CHAT_FAMILIES, RERANK_FAMILIES, chat_body, chat_event
from cairn.modelgw.invocation import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    InvocationSnapshot,
    RerankRequest,
    RerankResponse,
)


class ProviderCallError(UpstreamUnavailable):
    code = "PROVIDER_UNREACHABLE"
    title = "Model provider unavailable"

    def __init__(self, detail: str, *, retryable: bool = True, retry_after: float = 0) -> None:
        super().__init__(detail)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class ProviderState:
    semaphore: asyncio.Semaphore
    failures: list[float] = field(default_factory=list)
    open_until: float = 0
    probing: bool = False


class ProviderCircuitOpen(ProviderCallError):
    code = "PROVIDER_CIRCUIT_OPEN"
    http_status = 503


class ProviderAuthFailed(ProviderCallError):
    code = "PROVIDER_AUTH_FAILED"


class ProviderRateLimited(ProviderCallError):
    code = "PROVIDER_RATE_LIMITED"
    http_status = 429


@dataclass
class Attempt:
    headers: dict[str, str]
    input_tokens: int | None = None
    output_tokens: int | None = None


class GatewayRuntime:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        usage: UsageSink | None = None,
        timeout_s: float = 30,
        concurrency: int = 8,
        retries: int = 2,
    ) -> None:
        self._transport, self._usage = transport, usage or NullUsageSink()
        self._timeout_s, self._concurrency, self._retries = timeout_s, concurrency, retries
        self._states: dict[UUID, ProviderState] = {}

    def provider_state(self, provider_id: UUID) -> ProviderState:
        return self._states.setdefault(
            provider_id, ProviderState(asyncio.Semaphore(self._concurrency))
        )

    @staticmethod
    def _validate(snapshot: InvocationSnapshot, capability: str) -> None:
        model, provider = snapshot.model, snapshot.provider
        families = CHAT_FAMILIES if capability == "chat" else RERANK_FAMILIES
        if (
            model.capability != capability
            or provider.family not in families
            or snapshot.workspace_id != provider.workspace_id
            or model.provider_id != provider.id
        ):
            raise ProviderCallError(
                "Model capability or provider identity is invalid.", retryable=False
            )
        url = httpx.URL(provider.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ProviderCallError("The provider endpoint is invalid.", retryable=False)

    @staticmethod
    def _admit(state: ProviderState) -> bool:
        now = time.monotonic()
        if state.open_until > now or state.probing:
            raise ProviderCircuitOpen("The provider circuit is open.", retryable=False)
        probe = state.open_until > 0
        state.probing = probe
        return probe

    @staticmethod
    def _failed(state: ProviderState) -> None:
        now = time.monotonic()
        state.failures = [stamp for stamp in state.failures if now - stamp < 60] + [now]
        if len(state.failures) >= 5:
            state.open_until = now + 30

    def _headers(self, snapshot: InvocationSnapshot) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        provider = snapshot.provider
        if provider.family == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        if provider.credential is not None:
            key = provider.credential.open(
                get_settings().master_key, snapshot.workspace_id
            ).get_secret_value()
            if provider.family == "anthropic":
                headers["x-api-key"] = key
            elif provider.family == "google":
                headers["x-goog-api-key"] = key
            else:
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def _record(
        self,
        snapshot: InvocationSnapshot,
        attempt: int,
        started: float,
        outcome: str,
        inp: int | None,
        out: int | None,
    ) -> None:
        try:
            self._usage.record(
                UsageEvent(
                    workspace_id=snapshot.workspace_id,
                    principal_id=snapshot.principal_id,
                    knowledge_base_id=snapshot.knowledge_base_id,
                    provider_id=snapshot.provider.id,
                    model_id=snapshot.model.id,
                    request_id=snapshot.request_id,
                    attempt=attempt,
                    operation=snapshot.model.capability,
                    outcome=outcome,
                    input_tokens=inp,
                    output_tokens=out,
                    input_price_per_1k=snapshot.input_price_per_1k,
                    output_price_per_1k=snapshot.output_price_per_1k,
                    currency=snapshot.currency,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception:
            usage_delivery_total.labels(outcome="sink_error").inc()

    async def chat(self, snapshot: InvocationSnapshot, request: ChatRequest) -> ChatResponse:
        content, terminal = [], None
        async for chunk in self._chat(snapshot, request, stream=False):
            if chunk.type == "delta":
                content.append(chunk.delta)
            else:
                terminal = chunk
        assert terminal is not None
        return ChatResponse(
            model_id=terminal.model_id,
            content="".join(content),
            finish_reason=terminal.finish_reason,
            input_tokens=terminal.input_tokens,
            output_tokens=terminal.output_tokens,
        )

    async def stream(
        self, snapshot: InvocationSnapshot, request: ChatRequest
    ) -> AsyncGenerator[ChatChunk]:
        source = self._chat(snapshot, request, stream=True)
        try:
            async for chunk in source:
                yield chunk
        finally:
            await source.aclose()

    async def _chat(
        self,
        snapshot: InvocationSnapshot,
        request: ChatRequest,
        *,
        stream: bool,
    ) -> AsyncGenerator[ChatChunk]:
        queue: asyncio.Queue[ChatChunk | BaseException | None] = asyncio.Queue(maxsize=16)

        async def produce() -> None:
            source = self._produce_chat(snapshot, request, stream=stream)
            terminal: ChatChunk | None = None
            failure: Exception | None = None
            try:
                async with asyncio.timeout(self._timeout_s):
                    async for chunk in source:
                        if chunk.type == "completed":
                            terminal = chunk
                        else:
                            await queue.put(chunk)
            except TimeoutError:
                failure = ProviderCallError("The model call exceeded its time limit.")
            except Exception as exc:
                failure = (
                    exc
                    if isinstance(exc, ProviderCallError)
                    else ProviderCallError("The model stream failed.", retryable=False)
                )
            finally:
                # Closing the source finalizes usage and closes the upstream socket
                # before a potentially backpressured terminal/error delivery.
                try:
                    await source.aclose()
                except Exception:
                    failure = ProviderCallError(
                        "The model stream could not close cleanly.", retryable=False
                    )
            if failure is not None:
                await queue.put(failure)
            elif terminal is not None:
                await queue.put(terminal)
            await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                getting = asyncio.create_task(queue.get())
                try:
                    done, _pending = await asyncio.wait(
                        {getting, producer}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if getting not in done and producer.done() and queue.empty():
                        if producer.cancelled() or producer.exception() is not None:
                            raise ProviderCallError(
                                "The model stream stopped unexpectedly.", retryable=False
                            )
                        break
                    item = await getting
                finally:
                    if not getting.done():
                        getting.cancel()
                    await asyncio.gather(getting, return_exceptions=True)
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    @asynccontextmanager
    async def _attempt(
        self, snapshot: InvocationSnapshot, number: int, deadline: float
    ) -> AsyncIterator[Attempt]:
        state = self.provider_state(snapshot.provider.id)
        async with state.semaphore:
            probe = self._admit(state)
            started, outcome, called = time.monotonic(), "error", False
            attempt = Attempt(headers={})
            try:
                attempt.headers = self._headers(snapshot)
                called = True
                yield attempt
                outcome = "success"
                if probe:
                    state.failures.clear()
                    state.open_until = 0
            except (asyncio.CancelledError, GeneratorExit):
                outcome = "error" if time.monotonic() >= deadline else "cancelled"
                if outcome == "error" and called:
                    self._failed(state)
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ProviderCallError) as exc:
                if called:
                    self._failed(state)
                if probe:
                    state.open_until = time.monotonic() + 30
                if isinstance(exc, ProviderCallError):
                    raise
                raise ProviderCallError(
                    "The provider returned an invalid or unavailable response.",
                    retryable=isinstance(exc, httpx.HTTPError),
                ) from exc
            finally:
                if probe:
                    state.probing = False
                if called:
                    self._record(
                        snapshot,
                        number,
                        started,
                        outcome,
                        attempt.input_tokens,
                        attempt.output_tokens,
                    )

    async def _produce_chat(
        self, snapshot: InvocationSnapshot, request: ChatRequest, *, stream: bool
    ) -> AsyncGenerator[ChatChunk]:
        self._validate(snapshot, "chat")
        deadline, emitted = time.monotonic() + self._timeout_s, False
        path, body = chat_body(
            snapshot.provider.family, snapshot.model.model_key, request, stream=stream
        )
        base = snapshot.provider.base_url.rstrip("/")
        if snapshot.provider.family == "dashscope" and "dashscope.aliyuncs.com/api/v1" in base:
            base = base.split("/api/v1", 1)[0] + "/compatible-mode/v1"
        for number in range(1, self._retries + 2):
            try:
                reason = None
                async with self._attempt(snapshot, number, deadline) as attempt:
                    transport = self._transport or SafeTransport(
                        allow_private=snapshot.provider.allow_private, retries=0
                    )
                    async with (
                        httpx.AsyncClient(
                            transport=transport,
                            timeout=self._timeout_s,
                            follow_redirects=False,
                            trust_env=False,
                        ) as client,
                        client.stream(
                            "POST", base + path, json=body, headers=attempt.headers
                        ) as response,
                    ):
                        self._check_status(response)
                        done = False
                        if stream:
                            async for event in _events(
                                response, jsonl=snapshot.provider.family == "ollama"
                            ):
                                if event is None:
                                    if snapshot.provider.family in {
                                        "anthropic",
                                        "google",
                                        "ollama",
                                    }:
                                        raise ValueError("unexpected completion marker")
                                    done = True
                                    break
                                text, finish, inp, out, ended = chat_event(
                                    snapshot.provider.family, event, stream=True
                                )
                                attempt.input_tokens = (
                                    inp if inp is not None else attempt.input_tokens
                                )
                                attempt.output_tokens = (
                                    out if out is not None else attempt.output_tokens
                                )
                                reason, done = finish or reason, done or ended
                                if text:
                                    emitted = True
                                    yield ChatChunk(
                                        type="delta",
                                        delta=text,
                                        model_id=encode_id("mdl", snapshot.model.id),
                                    )
                        else:
                            raw = await read_capped(response, 2 * 1024 * 1024)
                            (
                                text,
                                reason,
                                attempt.input_tokens,
                                attempt.output_tokens,
                                done,
                            ) = chat_event(snapshot.provider.family, json.loads(raw), stream=False)
                            if text:
                                emitted = True
                                yield ChatChunk(
                                    type="delta",
                                    delta=text,
                                    model_id=encode_id("mdl", snapshot.model.id),
                                )
                        if not done:
                            raise ProviderCallError("The provider stream ended before completion.")
                yield ChatChunk(
                    type="completed",
                    model_id=encode_id("mdl", snapshot.model.id),
                    finish_reason=reason,
                    input_tokens=attempt.input_tokens,
                    output_tokens=attempt.output_tokens,
                )
                return
            except ProviderCallError as exc:
                if emitted or not exc.retryable or number > self._retries:
                    raise
                await asyncio.sleep(max(exc.retry_after, 0.1 * 2 ** (number - 1)))

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        retry = response.status_code == 429 or response.status_code >= 500
        try:
            delay = float(response.headers.get("retry-after", "0"))
        except ValueError:
            try:
                delay = (
                    parsedate_to_datetime(response.headers["retry-after"]) - datetime.now(UTC)
                ).total_seconds()
            except (TypeError, ValueError, KeyError):
                delay = 0
        error_class = (
            ProviderRateLimited
            if response.status_code == 429
            else ProviderAuthFailed
            if response.status_code in {401, 403}
            else ProviderCallError
        )
        error = error_class(
            "The provider rejected the model request.",
            retryable=retry,
            retry_after=delay if math.isfinite(delay) and delay >= 0 else 0,
        )
        raise error

    async def rerank(self, snapshot: InvocationSnapshot, request: RerankRequest) -> RerankResponse:
        self._validate(snapshot, "rerank")
        body: dict[str, Any] = {
            "query": request.query,
            "texts": request.documents,
            "raw_scores": False,
            "return_text": False,
        }
        if snapshot.provider.family == "infinity":
            body = {
                "query": request.query,
                "documents": request.documents,
                "model": snapshot.model.model_key,
            }
        deadline = time.monotonic() + self._timeout_s
        try:
            async with asyncio.timeout(self._timeout_s):
                for number in range(1, self._retries + 2):
                    try:
                        async with self._attempt(snapshot, number, deadline) as attempt:
                            transport = self._transport or SafeTransport(
                                allow_private=snapshot.provider.allow_private, retries=0
                            )
                            async with (
                                httpx.AsyncClient(
                                    transport=transport,
                                    timeout=self._timeout_s,
                                    follow_redirects=False,
                                    trust_env=False,
                                ) as client,
                                client.stream(
                                    "POST",
                                    snapshot.provider.base_url.rstrip("/") + "/rerank",
                                    json=body,
                                    headers=attempt.headers,
                                ) as response,
                            ):
                                self._check_status(response)
                                raw = json.loads(await read_capped(response, 2 * 1024 * 1024))
                            rows = raw if snapshot.provider.family == "tei" else raw["results"]
                            if not isinstance(rows, list) or len(rows) != len(request.documents):
                                raise ValueError("incomplete rerank")
                            indexed = {}
                            for row in rows:
                                index, score = (
                                    row["index"],
                                    row[
                                        "score"
                                        if snapshot.provider.family == "tei"
                                        else "relevance_score"
                                    ],
                                )
                                if (
                                    type(index) is not int
                                    or not 0 <= index < len(rows)
                                    or index in indexed
                                    or type(score) not in (int, float)
                                    or not math.isfinite(score)
                                ):
                                    raise ValueError("invalid rerank")
                                indexed[index] = float(score)
                            return RerankResponse(
                                model_id=encode_id("mdl", snapshot.model.id),
                                scores=[indexed[i] for i in range(len(rows))],
                            )
                    except ProviderCallError as exc:
                        if not exc.retryable or number > self._retries:
                            raise
                        await asyncio.sleep(max(exc.retry_after, 0.1 * 2 ** (number - 1)))
        except TimeoutError as exc:
            raise ProviderCallError("The rerank call exceeded its time limit.") from exc
        raise AssertionError("unreachable")


async def _events(response: httpx.Response, *, jsonl: bool) -> AsyncIterator[dict[str, Any] | None]:
    buffer, total, data = b"", 0, []
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > 2 * 1024 * 1024:
            raise ProviderCallError("Model output exceeds the size limit.", retryable=False)
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            if len(raw) > 65536:
                raise ValueError("event line too long")
            line = raw.rstrip(b"\r")
            if jsonl and line:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("invalid event")
                yield value
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload, data = b"\n".join(data), []
                if payload == b"[DONE]":
                    yield None
                else:
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise ValueError("invalid event")
                    yield value
        if len(buffer) > 65536:
            raise ValueError("event line too long")
    if buffer.strip() and jsonl:
        value = json.loads(buffer)
        if not isinstance(value, dict):
            raise ValueError("invalid event")
        yield value
