"""Immutable float32 vectors and the provider invocation contracts."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from cairn.core.modelref import ModelRef
from cairn.embedding.errors import EmbeddingDimensionMismatch, EmbeddingInvalidVector
from cairn.vectorstore.base import SparseVector

Purpose = Literal["query", "document"]


@dataclass(frozen=True, slots=True)
class Vector:
    values: tuple[float, ...]
    dim: int
    normalized: bool

    @classmethod
    def from_values(cls, values: Sequence[float], *, dim: int, normalize: bool) -> Vector:
        if dim <= 0 or len(values) != dim:
            raise EmbeddingDimensionMismatch()
        try:
            if any(isinstance(value, bool) for value in values):
                raise ValueError("boolean is not an embedding component")
            numbers = tuple(float(value) for value in values)
            if not all(math.isfinite(value) for value in numbers):
                raise ValueError("non-finite vector")
            if normalize:
                scale = max(abs(value) for value in numbers)
                if scale == 0:
                    raise ValueError("zero vector")
                scaled = tuple(value / scale for value in numbers)
                norm = math.hypot(*scaled)
                numbers = tuple(value / norm for value in scaled)
            raw = struct.pack(f"<{dim}f", *numbers)
            rounded = tuple(struct.unpack(f"<{dim}f", raw))
        except (TypeError, ValueError, OverflowError, struct.error) as exc:
            raise EmbeddingInvalidVector() from exc
        return cls(rounded, dim, normalize)

    @classmethod
    def from_bytes(cls, raw: bytes, *, dim: int, normalized: bool) -> Vector:
        if dim <= 0 or len(raw) != dim * 4:
            raise EmbeddingInvalidVector()
        values = struct.unpack(f"<{dim}f", raw)
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingInvalidVector()
        if normalized and not math.isclose(math.hypot(*values), 1, abs_tol=1e-6):
            raise EmbeddingInvalidVector()
        return cls(values, dim, normalized)

    def to_bytes(self) -> bytes:
        return struct.pack(f"<{self.dim}f", *self.values)


class EmbeddingProvider(Protocol):
    # Stable binding identity: include provider/account and model deployment revision.
    cache_namespace: str
    max_batch_size: int

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True, slots=True)
class HybridOutput:
    """One provider call's dense and/or learned sparse output, in input order."""

    dense: list[list[float]] | None
    sparse: list[SparseVector | None] | None


@runtime_checkable
class HybridEmbeddingProvider(EmbeddingProvider, Protocol):
    """A provider whose models can also return learned sparse (lexical) weights.

    One call returns whichever outputs are requested, so a knowledge base whose
    dense and sparse vectors come from the same model pays for one forward pass
    (or one billed request), not two.
    """

    async def embed_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
    ) -> HybridOutput: ...
