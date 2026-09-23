"""Workspace-scoped TEI/Infinity reranking with bounded, SSRF-safe HTTP."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from uuid import UUID

import httpx

from cairn.core.config import RerankEndpointSettings
from cairn.core.errors import PermissionDenied, UpstreamUnavailable, ValidationFailed
from cairn.core.http import SafeTransport, read_capped


class RerankUnavailable(Exception):
    """Sanitized optional-stage failure; callers retain base ranking."""


class RuntimeReranker:
    def __init__(
        self,
        endpoints: Mapping[str, RerankEndpointSettings],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._transport = transport
        for endpoint in self._endpoints.values():
            url = httpx.URL(endpoint.base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.userinfo
                or url.query
                or url.fragment
            ):
                raise ValueError("Invalid rerank endpoint URL")
            if endpoint.dialect == "infinity" and not endpoint.model_name:
                raise ValueError("Infinity reranking requires an operator-configured model name")

    def supports(self, model_id: str | None) -> bool:
        return model_id in self._endpoints

    async def score(
        self,
        workspace_id: UUID,
        model_id: str,
        query: str,
        documents: Sequence[str],
        *,
        timeout_s: float,
    ) -> list[float]:
        endpoint = self._endpoints.get(model_id)
        if endpoint is None:
            raise RerankUnavailable("Rerank model is not configured.")
        if workspace_id not in endpoint.workspace_ids:
            raise PermissionDenied("The rerank model is not available to this workspace.")
        if (
            not query.strip()
            or len(query) > 8192
            or not 0 < len(documents) <= endpoint.max_candidates
            or any(not document for document in documents)
        ):
            raise RerankUnavailable("Rerank input is outside configured limits.")
        payload: dict[str, object] = {"query": query, "return_text": False}
        if endpoint.dialect == "tei":
            payload.update(texts=list(documents), raw_scores=False, truncate=False)
        else:
            payload.update(
                documents=list(documents), model=endpoint.model_name, top_n=len(documents)
            )
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(data) > endpoint.max_request_bytes:
            raise RerankUnavailable("Rerank request exceeds its byte limit.")
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key is not None:
            headers["Authorization"] = "Bearer " + endpoint.api_key.get_secret_value()
        transport = self._transport or SafeTransport(
            allow_private=endpoint.allow_private, retries=0
        )
        try:
            async with (
                asyncio.timeout(timeout_s),
                httpx.AsyncClient(
                    transport=transport,
                    timeout=timeout_s,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    endpoint.base_url.rstrip("/") + "/rerank",
                    content=data,
                    headers=headers,
                ) as response,
            ):
                if not 200 <= response.status_code < 300:
                    raise RerankUnavailable("Rerank provider rejected the request.")
                raw = await read_capped(response, 1024 * 1024)
        except (TimeoutError, httpx.HTTPError, UpstreamUnavailable, ValidationFailed) as exc:
            raise RerankUnavailable("Rerank provider is unavailable.") from exc
        try:
            body = json.loads(raw)
            rows = body if endpoint.dialect == "tei" else body["results"]
            if not isinstance(rows, list) or len(rows) != len(documents):
                raise ValueError("incomplete ranking")
            indexed: dict[int, float] = {}
            for row in rows:
                index = row["index"]
                score = row["score"] if endpoint.dialect == "tei" else row["relevance_score"]
                if (
                    type(index) is not int
                    or not 0 <= index < len(documents)
                    or index in indexed
                    or type(score) not in (float, int)
                    or not math.isfinite(score)
                ):
                    raise ValueError("invalid ranking")
                indexed[index] = float(score)
            return [indexed[index] for index in range(len(documents))]
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise RerankUnavailable("Rerank provider returned invalid scores.") from exc
