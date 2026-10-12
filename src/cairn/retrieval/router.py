"""HTTP adapter for the public retrieval endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from cairn.authz.dataplane import current_dataplane_principal
from cairn.authz.model import Principal
from cairn.authz.ratelimit import get_rate_limiter
from cairn.core.config import get_settings
from cairn.core.ids import new_public_id
from cairn.retrieval.dto import BatchRequest, BatchResponse, RetrievalRequest, RetrievalResponse
from cairn.retrieval.service import RetrievalService

__all__ = ["router"]

router = APIRouter(prefix="/v1/retrieval", tags=["retrieval"])
PROBLEM: dict[int | str, dict[str, Any]] = {
    status: {"content": {"application/problem+json": {}}}
    for status in (400, 401, 403, 429, 502, 503)
}


def _service(request: Request) -> RetrievalService:
    service: RetrievalService = request.app.state.retrieval_service
    return service


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", None) or new_public_id("req"))


@router.post(
    "/query",
    response_model=RetrievalResponse,
    responses=PROBLEM,
    summary="Retrieve cited chunks from knowledge bases",
    description=(
        "Requires kb:query on every target. Full-text (BM25 or learned sparse), vector and "
        "hybrid search with filters, reranking, parent expansion, deduplication, MMR, "
        "highlights and a token budget. Any reduced-quality result is reported in `degraded` "
        "or `partial_failures`. `options.explain` returns the query plan and per-stage trace "
        "and has its own, smaller per-key rate limit."
    ),
)
async def query(
    body: RetrievalRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_dataplane_principal)],
    service: Annotated[RetrievalService, Depends(_service)],
) -> RetrievalResponse:
    if body.options.explain:
        await get_rate_limiter().check_scoped(
            principal, "explain", limit=get_settings().retrieval.explain_rpm
        )
    return await service.query(principal, body, request_id=_request_id(request))


@router.post(
    "/batch",
    response_model=BatchResponse,
    responses=PROBLEM,
    summary="Run up to 20 retrieval queries in one request",
    description=(
        "Each query counts against the key's rate limit. A malformed or unauthorized query "
        "rejects the whole batch; a query that fails at run time returns no results and one "
        "partial failure per target while the others succeed. Explain is not available here."
    ),
)
async def batch(
    body: BatchRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_dataplane_principal)],
    service: Annotated[RetrievalService, Depends(_service)],
) -> BatchResponse:
    if len(body.queries) > 1:
        # The request itself was already counted by the authentication middleware.
        await get_rate_limiter().check(principal, cost=len(body.queries) - 1)
    return await service.batch(principal, body, request_id=_request_id(request))
