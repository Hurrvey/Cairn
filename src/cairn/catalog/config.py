"""Knowledge base configuration.

``ChunkConfig`` and ``RetrievalConfig`` are stored as JSONB but validated as
Pydantic models, so a malformed config is rejected at write rather than
discovered by a worker three stages into an ingest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ChunkConfig",
    "ChunkStrategy",
    "FusionSpec",
    "MmrSpec",
    "RerankSpec",
    "RetrievalConfig",
    "SearchMode",
    "SearchWeights",
]

ChunkStrategy = Literal["fixed", "recursive", "markdown", "semantic", "parent_child", "custom"]
SearchMode = Literal["vector", "fulltext", "hybrid"]
DedupeMode = Literal["none", "by_chunk", "by_document"]

_Strict = ConfigDict(extra="forbid")


class ChunkConfig(BaseModel):
    model_config = _Strict

    #: parent_child is the default because it is usually the single largest
    #: quality win available: small chunks match precisely because they are not
    #: diluted by surrounding text, and the larger parent gives the model enough
    #: context to actually answer.
    strategy: ChunkStrategy = "parent_child"

    child_tokens: int = Field(default=512, ge=32, le=8192)
    child_overlap: int = Field(default=64, ge=0, le=2048)
    parent_tokens: int = Field(default=2048, ge=64, le=32768)

    separators: list[str] = Field(
        default_factory=lambda: ["\n## ", "\n### ", "\n\n", "。", ". ", "\n"]
    )
    #: A table split mid-row is useless to a model, so an oversized table
    #: becomes its own chunk regardless of the token target.
    keep_tables_intact: bool = True
    #: A 12-token orphan is noise in the index; sub-minimum chunks merge forward.
    min_chunk_tokens: int = Field(default=32, ge=1, le=512)
    #: Each chunk begins with its heading path, so a chunk retrieved in isolation
    #: still says what it is about. Cheap, and a consistent measurable gain.
    prepend_heading_path: bool = True

    function_id: str | None = None
    function_version: int | None = None

    @model_validator(mode="after")
    def _check_coherence(self) -> ChunkConfig:
        if self.child_overlap >= self.child_tokens:
            raise ValueError("child_overlap must be smaller than child_tokens")
        if self.strategy == "parent_child" and self.parent_tokens <= self.child_tokens:
            raise ValueError("parent_tokens must exceed child_tokens for parent_child chunking")
        if self.min_chunk_tokens >= self.child_tokens:
            raise ValueError("min_chunk_tokens must be smaller than child_tokens")
        if self.strategy == "custom" and not self.function_id:
            raise ValueError("custom chunking requires function_id")
        return self


class FusionSpec(BaseModel):
    model_config = _Strict
    #: RRF by default because dense and sparse scores are on incomparable
    #: scales — cosine sits in [0,1] while BM25 is unbounded and
    #: corpus-dependent. RRF uses rank alone, so it needs no calibration.
    method: Literal["rrf", "weighted"] = "rrf"
    k: int = Field(default=60, ge=1, le=1000)


class SearchWeights(BaseModel):
    model_config = _Strict
    dense: float = Field(default=0.7, ge=0.0, le=1.0)
    sparse: float = Field(default=0.3, ge=0.0, le=1.0)


class RerankSpec(BaseModel):
    model_config = _Strict
    enabled: bool = True
    model_id: str | None = None
    top_n: int = Field(default=5, ge=1, le=100)
    timeout_s: float = Field(default=1.5, gt=0, le=30)


class MmrSpec(BaseModel):
    model_config = _Strict
    enabled: bool = False
    lambda_: float = Field(default=0.5, ge=0.0, le=1.0, alias="lambda")


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    search_mode: SearchMode = "hybrid"
    fusion: FusionSpec = Field(default_factory=FusionSpec)
    weights: SearchWeights = Field(default_factory=SearchWeights)

    top_k: int = Field(default=5, ge=1, le=100)
    #: Retrieve wide with cheap ANN, rank narrow with the expensive
    #: cross-encoder. 100 is the usual sweet spot.
    candidate_k: int = Field(default=100, ge=1, le=1000)
    score_threshold: float = 0.0

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
