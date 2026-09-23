"""Strict HTTP schemas for model and provider management."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn.core.ids import encode_id
from cairn.modelgw.dto import ModelView, ProviderView

__all__ = [
    "CreateModelRequest",
    "CreateProviderRequest",
    "ModelResponse",
    "ModelTestResponse",
    "ProviderResponse",
]

_STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderConfigRequest(BaseModel):
    model_config = _STRICT

    binding_revision: str = Field(min_length=1, max_length=255)
    allow_private: bool = False
    max_batch_size: int = Field(default=16, ge=1, le=1024)


class CreateProviderRequest(BaseModel):
    model_config = _STRICT

    name: str = Field(min_length=1, max_length=255)
    family: Literal["tei", "infinity"]
    base_url: str = Field(min_length=1, max_length=2048)
    config: ProviderConfigRequest


class CreateModelRequest(BaseModel):
    model_config = _STRICT

    provider_id: str
    model_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    capability: Literal["embedding"] = "embedding"
    dimension: int = Field(ge=1, le=65536)
    max_input_tokens: int = Field(ge=1, le=32768)
    normalize: bool = True
    query_prefix: str | None = Field(default=None, max_length=1000)
    optimal_batch_size: int = Field(default=16, ge=1, le=1024)
    tokenizer_id: str = Field(min_length=1, max_length=255)


class ProviderResponse(BaseModel):
    id: str
    name: str
    family: str
    base_url: str | None
    is_enabled: bool
    model_count: int

    @classmethod
    def from_dto(cls, provider: ProviderView) -> ProviderResponse:
        return cls(
            id=encode_id("prov", provider.id),
            name=provider.name,
            family=provider.family,
            base_url=provider.base_url,
            is_enabled=provider.is_enabled,
            model_count=provider.model_count,
        )


class ModelResponse(BaseModel):
    id: str
    provider_id: str
    display_name: str
    model_key: str
    capability: str
    dimension: int | None
    max_input_tokens: int | None
    tokenizer_id: str | None
    normalize: bool
    query_prefix: str | None
    optimal_batch_size: int
    is_enabled: bool
    health_state: str
    checked_at: datetime | None

    @classmethod
    def from_dto(cls, model: ModelView) -> ModelResponse:
        return cls(
            id=encode_id("mdl", model.id),
            provider_id=encode_id("prov", model.provider_id),
            display_name=model.display_name,
            model_key=model.model_key,
            capability=model.capability,
            dimension=model.dimension,
            max_input_tokens=model.max_input_tokens,
            tokenizer_id=model.tokenizer_id,
            normalize=model.normalize,
            query_prefix=model.query_prefix,
            optimal_batch_size=model.optimal_batch_size,
            is_enabled=model.is_enabled,
            health_state=model.health_state,
            checked_at=model.checked_at,
        )


class ModelTestResponse(BaseModel):
    healthy: bool
    dimensions: int
    tokens: int
