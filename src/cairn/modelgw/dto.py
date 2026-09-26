"""Model gateway DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from cairn.core.modelref import Capability, ModelRef
from cairn.core.secrets import SealedCredential

__all__ = [
    "Capability",
    "EmbeddingRuntimeRef",
    "ModelRef",
    "ModelView",
    "ProviderView",
    "RegisterModelSpec",
]


@dataclass(frozen=True, slots=True)
class EmbeddingRuntimeRef:
    model: ModelRef
    provider_id: UUID
    provider_family: str
    base_url: str | None
    config: dict[str, Any]
    has_credentials: bool
    #: Sealed; open with the master key only where the call is made.
    credential: SealedCredential | None = None
    #: Bound into the credential's associated data; needed to open it.
    workspace_id: UUID | None = None


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
    tokenizer_id: str | None
    normalize: bool
    query_prefix: str | None
    optimal_batch_size: int
    is_enabled: bool
    health_state: str
    checked_at: datetime | None
    sparse: bool = False
    send_dimension: bool = False


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
    sparse: bool = False
    send_dimension: bool = False
