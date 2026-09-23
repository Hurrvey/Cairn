"""Credential-free model reference shared by control and data planes."""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

Capability = Literal["chat", "embedding", "rerank"]


@dataclass(frozen=True, slots=True)
class ModelRef:
    id: UUID
    provider_family: str
    model_key: str
    capability: Capability
    dimension: int | None = None
    max_input_tokens: int | None = None
    normalize: bool = True
    query_prefix: str | None = None
    optimal_batch_size: int = 64
    tokenizer_id: str | None = None
    provider_id: UUID | None = None
    dynamic_provider: bool = field(default=False, compare=False)
