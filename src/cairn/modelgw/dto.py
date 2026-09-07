"""Model gateway DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

__all__ = ["Capability", "ModelRef", "ModelView", "ProviderView", "RegisterModelSpec"]

Capability = Literal["chat", "embedding", "rerank"]


@dataclass(frozen=True, slots=True)
class ModelRef:
    """The only model representation that crosses a module boundary.

    Carries no credential and no provider configuration — just what a caller
    needs to embed, rerank, or generate. That is what lets it be cached and
    handed to the data plane without dragging the gateway along.
    """

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


@dataclass(frozen=True, slots=True)
class ModelView:
    id: UUID
    provider_id: UUID
    provider_name: str
    provider_family: str
    model_key: str
    display_name: str
    capability: Capability
    dimension: int | None
    max_input_tokens: int | None
    is_enabled: bool
    health_state: str
    checked_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProviderView:
    id: UUID
    name: str
    family: str
    base_url: str | None
    is_enabled: bool
    has_credentials: bool
    model_count: int


@dataclass(frozen=True, slots=True)
class RegisterModelSpec:
    provider_id: UUID
    model_key: str
    display_name: str
    capability: Capability
    dimension: int | None = None
    max_input_tokens: int | None = None
    normalize: bool = True
    query_prefix: str | None = None
    optimal_batch_size: int = 64
    tokenizer_id: str | None = None
