"""Credential-free retrieval configuration and ACTIVE runtime projection."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.core.modelref import ModelRef
from cairn.core.sparse import SparseSpec

__all__ = [
    "BindingRefModel",
    "DedupeMode",
    "FusionSpec",
    "KbStatus",
    "KnowledgeBaseRuntime",
    "MmrSpec",
    "RerankSpec",
    "RetrievalConfig",
    "SearchMode",
    "SearchWeights",
]

SearchMode = Literal["vector", "fulltext", "hybrid"]
DedupeMode = Literal["none", "by_chunk", "by_document"]
KbStatus = Literal["active", "indexing", "error", "deleting", "archived"]

_STRICT = ConfigDict(extra="forbid")


class FusionSpec(BaseModel):
    model_config = _STRICT

    method: Literal["rrf", "weighted"] = "rrf"
    k: int = Field(default=60, ge=1, le=1000)


class SearchWeights(BaseModel):
    model_config = _STRICT

    dense: float = Field(default=0.7, ge=0.0, le=1.0, allow_inf_nan=False)
    sparse: float = Field(default=0.3, ge=0.0, le=1.0, allow_inf_nan=False)


class RerankSpec(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    model_id: str | None = None
    top_n: int = Field(default=5, ge=1, le=100)
    timeout_s: float = Field(default=1.5, gt=0, le=30, allow_inf_nan=False)


class MmrSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    lambda_: float = Field(default=0.5, ge=0.0, le=1.0, alias="lambda", allow_inf_nan=False)


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    search_mode: SearchMode = "hybrid"
    fusion: FusionSpec = Field(default_factory=FusionSpec)
    weights: SearchWeights = Field(default_factory=SearchWeights)
    top_k: int = Field(default=5, ge=1, le=100)
    candidate_k: int = Field(default=100, ge=1, le=1000)
    score_threshold: float = Field(default=0.0, allow_inf_nan=False)
    rerank: RerankSpec = Field(default_factory=RerankSpec)
    expand_parent: bool = True
    dedupe: DedupeMode = "none"
    mmr: MmrSpec = Field(default_factory=MmrSpec)

    @model_validator(mode="after")
    def _check_coherence(self) -> RetrievalConfig:
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be at least top_k")
        if self.rerank.enabled and self.rerank.top_n > self.candidate_k:
            raise ValueError("rerank.top_n cannot exceed candidate_k")
        return self


class BindingRefModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    driver: str
    config: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseRuntime(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    workspace_id: UUID
    index_version: int
    embedding_model: ModelRef
    metric: Literal["cosine", "dot", "l2"]
    vector_binding: BindingRefModel
    retrieval_config: RetrievalConfig
    config_version: int
    status: KbStatus
    # Legacy projections fail closed. Manual chunk corrections invalidate parent context.
    parent_snapshots_safe: bool = False
    #: Where the ACTIVE version's sparse vectors came from; queries must match it.
    sparse: SparseSpec = SparseSpec()
