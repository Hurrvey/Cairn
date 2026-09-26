"""Where a knowledge base's lexical (sparse) vectors come from.

Shared by both planes: the control plane snapshots it per index version, the
ingestion workers encode documents with it, and the data plane encodes queries
with it. A version's documents and queries must use the same source, which is
why it lives in the version snapshot and not in mutable settings.

* ``bm25`` — the built-in BM25 encoder (``cairn.embedding.sparse``). Term
  frequencies only; Qdrant applies IDF, so the namespace uses the ``idf``
  modifier.
* ``model`` — a registered model that emits learned sparse weights (bge-m3,
  DashScope v3/v4). The weights are final, so the modifier is ``none``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from cairn.core.modelref import ModelRef

__all__ = ["BM25_ENCODER_ID", "SparseKind", "SparseModifier", "SparseSpec"]

SparseKind = Literal["bm25", "model"]
SparseModifier = Literal["idf", "none"]

#: Kept in step with ``cairn.embedding.sparse.ENCODER_ID``; core cannot import
#: the embedding module, and a unit test asserts the two agree.
BM25_ENCODER_ID = "bm25-jieba-v1"


class SparseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SparseKind = "bm25"
    model: ModelRef | None = None

    @model_validator(mode="after")
    def _coherent(self) -> SparseSpec:
        if self.kind == "bm25" and self.model is not None:
            raise ValueError("a BM25 sparse source takes no model")
        if self.kind == "model":
            if self.model is None:
                raise ValueError("a model sparse source requires its model")
            if not self.model.sparse or self.model.capability != "embedding":
                raise ValueError("the sparse source model must emit sparse vectors")
        return self

    @property
    def modifier(self) -> SparseModifier:
        return "idf" if self.kind == "bm25" else "none"

    @property
    def encoder_id(self) -> str:
        """Identity recorded with stored sparse vectors, so reuse never crosses sources."""
        if self.model is None:
            return BM25_ENCODER_ID
        return f"model:{self.model.id}"
