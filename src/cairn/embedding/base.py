"""Immutable float32 vectors and the provider invocation contract."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from cairn.core.modelref import ModelRef
from cairn.embedding.errors import EmbeddingDimensionMismatch, EmbeddingInvalidVector

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
