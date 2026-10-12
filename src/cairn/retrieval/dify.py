"""Dify External Knowledge API adapter; native retrieval contracts stay unchanged."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from cairn.authz.dataplane import current_dataplane_principal
from cairn.authz.model import Principal
from cairn.core.errors import CairnError, ValidationFailed
from cairn.core.telemetry import dify_degraded_total
from cairn.retrieval.dto import FilterSpec, RetrievalRequest
from cairn.retrieval.errors import KbRuntimeNotPublished
from cairn.retrieval.router import _request_id, _service
from cairn.retrieval.service import RetrievalService


class RetrievalSetting(BaseModel):
    model_config = ConfigDict(extra="ignore")
    top_k: StrictInt = Field(default=5, ge=1)
    score_threshold: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @field_validator("top_k")
    @classmethod
    def cap(cls, value: int) -> int:
        return min(value, 100)


class DifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    knowledge_id: str = Field(max_length=100)
    query: str = Field(min_length=1, max_length=8192)
    retrieval_setting: RetrievalSetting = Field(default_factory=RetrievalSetting)
    metadata_condition: dict[str, Any] | None = None


class DifyRecord(BaseModel):
    content: str
    score: float = Field(ge=0, le=1)
    title: str
    metadata: dict[str, Any]


class DifyResponse(BaseModel):
    records: list[DifyRecord]


class DifyError(BaseModel):
    error_code: int
    error_msg: str


class Condition(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    comparison_operator: str
    value: Any = None


class MetadataConditions(BaseModel):
    logical_operator: Literal["and", "or"] | None = None
    conditions: list[Condition] | None = Field(default=None, max_length=64)


def metadata_filter(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    conditions = MetadataConditions.model_validate(raw)
    nodes: list[dict[str, Any]] = []
    operators = {
        "is": "$eq",
        "=": "$eq",
        ">": "$gt",
        "<": "$lt",
        "≥": "$gte",
        "≤": "$lte",
        ">=": "$gte",
        "<=": "$lte",
        "before": "$lt",
        "after": "$gt",
    }
    for condition in conditions.conditions or []:
        name, op, value = condition.name, condition.comparison_operator, condition.value
        if op in {"empty", "not empty"}:
            nodes.append({name: {"$exists": op == "not empty"}})
        elif op in {"contains", "not contains", "start with", "end with"}:
            raise ValidationFailed(f"Dify metadata operator {op!r} is not supported.")
        elif value is None:
            continue
        elif op in operators:
            nodes.append({name: {operators[op]: value}})
        elif op in {"is not", "≠"}:
            nodes.append({"$and": [{name: {"$ne": value}}, {name: {"$exists": True}}]})
        elif op in {"in", "not in"}:
            choices = [v.strip() for v in value.split(",")] if isinstance(value, str) else value
            if not isinstance(choices, list):
                raise ValidationFailed(
                    f"Dify operator {op!r} requires a list or comma-separated string."
                )
            nodes.append({name: {"$in" if op == "in" else "$nin": choices}})
        else:
            raise ValidationFailed(f"Dify metadata operator {op!r} is not supported.")
    return {f"${conditions.logical_operator or 'and'}": nodes} if nodes else None


def error_response(request: Request, exc: Exception) -> JSONResponse:
    headers = {"X-Request-Id": _request_id(request)}
    if isinstance(exc, CairnError):
        status, message = exc.http_status, exc.detail
        if getattr(exc, "retry_after", None) is not None:
            headers["Retry-After"] = str(exc.retry_after)  # type: ignore[attr-defined]
    elif isinstance(exc, HTTPException):
        status, message = exc.status_code, str(exc.detail)
        headers.update(exc.headers or {})
    elif isinstance(exc, (RequestValidationError, ValueError)):
        status, message = 400, "Invalid external knowledge request."
    else:
        status, message = 500, "External knowledge retrieval could not be completed."
    code = status
    if isinstance(exc, KbRuntimeNotPublished):
        status, code = 404, 2001
    elif status in {401, 403}:
        status, code = 403, 1002
        if isinstance(exc, HTTPException) and exc.detail == "Invalid Authorization header format.":
            code = 1001
    return JSONResponse(
        {"error_code": code, "error_msg": message}, status_code=status, headers=headers
    )


class DifyRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                # Validate credentials even for Dify's empty registration probe.
                dify_principal(request)
                return await original(request)
            except Exception as exc:
                return error_response(request, exc)

        return handle


def dify_principal(request: Request) -> Principal:
    header = request.headers.get("Authorization")
    if header and (not header.lower().startswith("bearer ") or not header[7:].strip()):
        raise HTTPException(403, "Invalid Authorization header format.")
    principal: Principal | None = getattr(request.state, "principal", None)
    if (
        not header
        or principal is None
        or principal.type != "api_key"
        or getattr(request.state, "auth_source", None) != "api_key"
    ):
        raise HTTPException(403, "Authorization failed.")
    return principal


router = APIRouter(tags=["compatibility"], route_class=DifyRoute)


@router.post(
    "/retrieval",
    response_model=DifyResponse,
    responses={
        status: {"description": "Dify error_code/error_msg response", "model": DifyError}
        for status in (400, 403, 404, 429, 502, 503)
    },
)
async def retrieval(
    body: DifyRequest,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_dataplane_principal)],
    service: Annotated[RetrievalService, Depends(_service)],
) -> dict[str, Any]:
    filters = metadata_filter(body.metadata_condition)
    query = RetrievalRequest(
        targets=[{"knowledge_base_id": body.knowledge_id}],
        query=body.query,
        top_k=body.retrieval_setting.top_k,
        strict=True,
        filters=FilterSpec(metadata=filters) if filters is not None else None,
    )
    result = await service.query_external(principal, query, request_id=_request_id(request))
    notices = [f"{notice.stage}:{notice.reason}" for notice in result.degraded]
    for stage, reason in {(item.stage, item.reason) for item in result.degraded}:
        dify_degraded_total.labels(
            stage=stage if stage in {"rerank", "search", "sparse", "expand"} else "other",
            reason=reason
            if reason in {"load_shed", "unavailable", "timeout", "manual_edit"}
            else "other",
        ).inc()
    if notices:
        response.headers["X-Cairn-Degraded"] = ",".join(notices)
    records = []
    for hit in result.results:
        score = hit.scores["relevance"]
        if score < (body.retrieval_setting.score_threshold or 0):
            continue
        heading = hit.metadata.get("heading_path")
        path = (
            " > ".join(heading)
            if isinstance(heading, list) and all(isinstance(part, str) for part in heading)
            else ""
        )
        records.append(
            {
                "content": hit.content,
                "score": score,
                "title": (hit.source.title if hit.source else None) or "",
                "metadata": {
                    **hit.metadata,
                    "document_id": hit.document_id,
                    "chunk_id": hit.chunk_id,
                    "knowledge_base_id": hit.knowledge_base_id,
                    "path": path,
                    "cairn_score": hit.score,
                    "cairn_score_source": "rerank" if "rerank" in hit.scores else "cosine",
                    "cairn_degraded": notices,
                },
            }
        )
    return {"records": records}
