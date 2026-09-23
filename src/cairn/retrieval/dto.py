"""Public request, response, and immutable query-plan values for retrieval."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.core.retrieval_runtime import FusionSpec, MmrSpec, SearchMode, SearchWeights

__all__ = [
    "DegradationNotice",
    "FilterSpec",
    "RerankRequest",
    "RetrievalHit",
    "RetrievalOptions",
    "RetrievalRequest",
    "RetrievalResponse",
    "RetrievalTarget",
    "SourceInfo",
    "TargetFailure",
    "UsageInfo",
]

_STRICT = ConfigDict(extra="forbid")
MAX_QUERY_VECTOR_DIMENSION = 65_536


class RetrievalTarget(BaseModel):
    model_config = _STRICT

    knowledge_base_id: str
    weight: float = Field(default=1.0, ge=0.0, le=10.0, allow_inf_nan=False)

    @field_validator("weight", mode="before")
    @classmethod
    def _reject_boolean_weight(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("weight must be a number, not a boolean")
        return value


class RerankRequest(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    model: str | None = None
    model_id: str | None = None
    top_n: int = Field(default=5, ge=1, le=100)
    timeout_s: float = Field(default=1.5, gt=0, le=30, allow_inf_nan=False)


class FilterSpec(BaseModel):
    model_config = _STRICT

    metadata: dict[str, Any] | None = None
    document_ids: list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class RetrievalOptions(BaseModel):
    model_config = _STRICT

    expand_parent: bool | None = None
    include_highlights: bool = False
    include_metadata: bool = True
    max_context_tokens: int | None = Field(default=None, ge=100, le=200_000)
    dedupe: Literal["none", "by_chunk", "by_document"] | None = None
    mmr: MmrSpec | None = None
    explain: bool = False


class RetrievalRequest(BaseModel):
    model_config = _STRICT

    targets: list[RetrievalTarget] = Field(min_length=1, max_length=10)
    query: str | None = Field(default=None, min_length=1, max_length=8192)
    query_vector: list[float] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_QUERY_VECTOR_DIMENSION,
    )
    top_k: int | None = Field(default=None, ge=1, le=100)
    candidate_k: int | None = Field(default=None, ge=1, le=1000)
    search_mode: SearchMode | None = None
    fusion: FusionSpec | None = None
    weights: SearchWeights | None = None
    score_threshold: float | None = Field(default=None, allow_inf_nan=False)
    rerank: RerankRequest | None = None
    filters: FilterSpec | None = None
    options: RetrievalOptions = Field(default_factory=RetrievalOptions)
    strict: bool = False

    @field_validator("query")
    @classmethod
    def _query_must_contain_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("query must contain non-whitespace text")
        return value

    @field_validator("query_vector", mode="before")
    @classmethod
    def _query_vector_must_be_numeric(cls, value: object) -> object:
        if isinstance(value, list) and any(isinstance(component, bool) for component in value):
            raise ValueError("query_vector components must be numbers, not booleans")
        return value

    @field_validator("query_vector")
    @classmethod
    def _query_vector_must_be_finite(cls, value: list[float] | None) -> list[float] | None:
        if value is not None:
            import math

            if not all(math.isfinite(component) for component in value):
                raise ValueError("query_vector components must be finite")
        return value

    @model_validator(mode="after")
    def _check_request_coherence(self) -> RetrievalRequest:
        if self.query is None and self.query_vector is None:
            raise ValueError("one of query or query_vector is required")
        if self.search_mode in {"fulltext", "hybrid"} and self.query is None:
            raise ValueError(f"{self.search_mode} search requires query text")
        if (
            self.top_k is not None
            and self.candidate_k is not None
            and self.candidate_k < self.top_k
        ):
            raise ValueError("candidate_k must be at least top_k")
        return self


class SourceInfo(BaseModel):
    model_config = _STRICT

    title: str | None = None
    url: str | None = None
    type: str | None = None
    updated_at: datetime | None = None


class RetrievalHit(BaseModel):
    model_config = _STRICT

    chunk_id: str
    document_id: str
    knowledge_base_id: str
    content: str
    token_count: int | None = None
    score: float
    scores: dict[str, float] = Field(default_factory=dict)
    highlights: list[dict[str, int]] = Field(default_factory=list)
    matched_child_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: SourceInfo | None = None


class UsageInfo(BaseModel):
    model_config = _STRICT

    embedding_tokens: int = 0
    rerank_units: int = 0
    cached_embedding: bool | None = None
    index_versions: dict[str, int] = Field(default_factory=dict)
    latency_ms: dict[str, int] = Field(default_factory=dict)


class DegradationNotice(BaseModel):
    model_config = _STRICT

    stage: str
    reason: str
    detail: str


class TargetFailure(BaseModel):
    model_config = _STRICT

    knowledge_base_id: str
    code: str
    detail: str


class RetrievalResponse(BaseModel):
    model_config = _STRICT

    request_id: str
    results: list[RetrievalHit]
    usage: UsageInfo
    degraded: list[DegradationNotice] = Field(default_factory=list)
    partial_failures: list[TargetFailure] = Field(default_factory=list)
    truncated_to_token_budget: bool = False
    explain: None = None
