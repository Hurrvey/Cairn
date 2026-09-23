"""Credential-free model-provider runtime projection shared with data workers."""

from __future__ import annotations

from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PROVIDER_RUNTIME_TTL",
    "ProviderRuntimeProjection",
    "provider_runtime_key",
    "provider_runtime_tombstone_key",
]

PROVIDER_RUNTIME_TTL = 300


def provider_runtime_key(provider_id: UUID) -> str:
    return f"model:provider-runtime:{provider_id}"


def provider_runtime_tombstone_key(provider_id: UUID) -> str:
    return f"model:provider-runtime-deleted:{provider_id}"


class ProviderRuntimeProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    workspace_id: UUID
    family: Literal["tei", "infinity"]
    base_url: str = Field(min_length=1, max_length=2048)
    allow_private: bool = False
    binding_revision: str = Field(min_length=1, max_length=255)
    max_batch_size: int = Field(default=16, ge=1, le=1024)

    @property
    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()
