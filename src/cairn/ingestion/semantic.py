"""Validated semantic-boundary detection over explicitly injected embeddings."""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol, cast

from cairn.core.modelref import ModelRef
from cairn.embedding.errors import TokenizerUnavailable
from cairn.embedding.service import EmbeddingService
from cairn.ingestion.errors import ChunkError

SEMANTIC_MAX_BATCH_SIZE = 64
SEMANTIC_MAX_SEGMENTS = 4096
SEMANTIC_MAX_INPUT_CHARS = 2_000_000
SEMANTIC_MAX_VECTOR_DIMENSION = 65_536


class SemanticChunkingError(ChunkError):
    code = "CHUNK_SEMANTIC_INVALID"
    title = "Semantic chunking returned invalid embeddings."


class SemanticEmbedding(Protocol):
    @property
    def identity(self) -> str: ...

    @property
    def max_batch_size(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


SemanticEmbeddingCallback = Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]]


@dataclass(frozen=True, slots=True)
class CallbackSemanticEmbedding:
    identity: str
    callback: SemanticEmbeddingCallback
    max_batch_size: int = SEMANTIC_MAX_BATCH_SIZE

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return await self.callback(texts)


@dataclass(frozen=True, slots=True)
class EmbeddingServiceSemanticEmbedding:
    service: EmbeddingService
    model: ModelRef
    binding_identity: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not self.binding_identity
            or self.model.optimal_batch_size <= 0
            or self.model.max_input_tokens is None
            or self.model.max_input_tokens <= 0
        ):
            raise ValueError(
                "semantic model binding identity, batch size and token limit are required"
            )
        model_identity = json.dumps(
            [
                str(self.model.id),
                self.model.provider_family,
                self.model.model_key,
                self.model.capability,
                self.model.dimension,
                self.model.max_input_tokens,
                self.model.normalize,
                self.model.query_prefix,
                self.model.optimal_batch_size,
                self.model.tokenizer_id,
            ],
            separators=(",", ":"),
        )
        digest = sha256(model_identity.encode()).hexdigest()
        object.__setattr__(self, "identity", f"{self.binding_identity}:{digest}")

    @property
    def max_batch_size(self) -> int:
        return self.model.optimal_batch_size

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        assert self.model.max_input_tokens is not None
        for text in texts:
            normalized = " ".join(unicodedata.normalize("NFKC", text).split())
            if not normalized:
                raise SemanticChunkingError("Semantic input must not be blank.")
            try:
                tokens = self.service.count_tokens(self.model, normalized)
            except TokenizerUnavailable as exc:
                raise SemanticChunkingError("The semantic model tokenizer is unavailable.") from exc
            if tokens > self.model.max_input_tokens:
                raise SemanticChunkingError(
                    "Semantic input exceeds the model token limit; truncation is forbidden."
                )
        vectors = await self.service.embed_documents(
            self.model,
            texts,
            batch_size=self.max_batch_size,
        )
        return [vector.values for vector in vectors]


class SemanticBoundaryDetector:
    def __init__(self, embedding: SemanticEmbedding) -> None:
        if not embedding.identity or embedding.max_batch_size <= 0:
            raise ValueError("semantic embedding identity and batch size are required")
        self._embedding = embedding

    @property
    def identity(self) -> str:
        return self._embedding.identity

    async def boundaries(self, texts: Sequence[str]) -> frozenset[int]:
        validated_texts = self._validate_texts(texts)
        if len(validated_texts) > SEMANTIC_MAX_SEGMENTS:
            raise SemanticChunkingError("Semantic input exceeds the segment limit.")
        if sum(len(text) for text in validated_texts) > SEMANTIC_MAX_INPUT_CHARS:
            raise SemanticChunkingError("Semantic input exceeds the character limit.")
        if len(validated_texts) < 2:
            return frozenset()
        batch_size = min(self._embedding.max_batch_size, SEMANTIC_MAX_BATCH_SIZE)
        rows: list[object] = []
        for offset in range(0, len(validated_texts), batch_size):
            batch = validated_texts[offset : offset + batch_size]
            raw_result: object = await self._embedding.embed_documents(batch)
            if (
                not isinstance(raw_result, Sequence)
                or isinstance(raw_result, (str, bytes))
                or len(raw_result) != len(batch)
            ):
                raise SemanticChunkingError()
            rows.extend(cast("Sequence[object]", raw_result))
        vectors = self._validate(rows)
        distances = [
            1.0
            - sum(
                left * right for left, right in zip(vectors[index], vectors[index + 1], strict=True)
            )
            for index in range(len(vectors) - 1)
        ]
        ordered = sorted(distances)
        threshold = ordered[math.ceil(len(ordered) * 0.8) - 1]
        peaks = set()
        for index, distance in enumerate(distances):
            left = distances[index - 1] if index else -math.inf
            right = distances[index + 1] if index + 1 < len(distances) else -math.inf
            if distance > 0 and distance >= threshold and distance >= left and distance >= right:
                peaks.add(index + 1)
        return frozenset(peaks)

    def _validate_texts(self, texts: object) -> tuple[str, ...]:
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)):
            raise SemanticChunkingError()
        raw_texts = cast("Sequence[object]", texts)
        if any(not isinstance(text, str) or not text.strip() for text in raw_texts):
            raise SemanticChunkingError("Semantic input must contain non-blank strings.")
        return tuple(cast("str", text) for text in raw_texts)

    def _validate(self, rows: Sequence[object]) -> list[tuple[float, ...]]:
        vectors: list[tuple[float, ...]] = []
        dimension: int | None = None
        for raw_row in rows:
            if (
                not isinstance(raw_row, Sequence)
                or isinstance(raw_row, (str, bytes))
                or len(raw_row) > SEMANTIC_MAX_VECTOR_DIMENSION
            ):
                raise SemanticChunkingError()
            row = cast("Sequence[object]", raw_row)
            try:
                if any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) for value in row
                ):
                    raise TypeError("embedding components must be real numbers")
                values = tuple(float(cast("float | int", value)) for value in row)
            except (TypeError, ValueError, OverflowError) as exc:
                raise SemanticChunkingError() from exc
            if not values or any(not math.isfinite(value) for value in values):
                raise SemanticChunkingError()
            if dimension is None:
                dimension = len(values)
            elif len(values) != dimension:
                raise SemanticChunkingError()
            norm = math.hypot(*values)
            if not math.isfinite(norm) or norm == 0:
                raise SemanticChunkingError()
            vectors.append(tuple(value / norm for value in values))
        return vectors
