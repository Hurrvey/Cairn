"""HTTP adapter for the bounded public retrieval endpoint."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from cairn.authz.dataplane import current_dataplane_principal
from cairn.authz.model import Principal
from cairn.core.ids import new_public_id
from cairn.retrieval.dto import RetrievalRequest, RetrievalResponse
from cairn.retrieval.service import RetrievalService

__all__ = ["router"]

router = APIRouter(prefix="/v1/retrieval", tags=["retrieval"])
PROBLEM: dict[int | str, dict[str, Any]] = {
    status: {"content": {"application/problem+json": {}}} for status in (400, 401, 403, 502, 503)
}


def _service(request: Request) -> RetrievalService:
    service: RetrievalService = request.app.state.retrieval_service
    return service


@router.post(
    "/query",
    response_model=RetrievalResponse,
    responses=PROBLEM,
    summary="Retrieve cited chunks from knowledge bases",
    description=(
        "Requires kb:query on every target. Supports bounded full-text (BM25), vector, "
        "and hybrid search; unsupported advanced options are rejected or reported degraded."
    ),
)
async def query(
    body: RetrievalRequest,
    request: Request,
    principal: Annotated[Principal, Depends(current_dataplane_principal)],
    service: Annotated[RetrievalService, Depends(_service)],
) -> RetrievalResponse:
    request_id = str(getattr(request.state, "request_id", None) or new_public_id("req"))
    return await service.query(principal, body, request_id=request_id)
