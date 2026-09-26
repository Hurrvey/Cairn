"""Strict HTTP schemas for model and provider management."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from cairn.core.ids import encode_id
from cairn.core.provider_runtime import DEFAULT_BASE_URLS, HOSTED_FAMILIES, ProviderFamily
from cairn.modelgw.dto import ModelView, ProviderView

__all__ = [
    "CreateModelRequest",
    "CreateProviderRequest",
    "ModelResponse",
    "ModelTestResponse",
    "ProviderCredentialsRequest",
    "ProviderResponse",
]

_STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderConfigRequest(BaseModel):
    model_config = _STRICT

    binding_revision: str = Field(default="v1", min_length=1, max_length=255)
    allow_private: bool = False
    max_batch_size: int = Field(default=16, ge=1, le=1024)


def _check_key(api_key: SecretStr | None) -> None:
    # Checked here rather than with Field constraints: a length error must not
    # risk echoing any part of the value back in a validation message.
    if api_key is not None and not 1 <= len(api_key.get_secret_value().strip()) <= 4096:
        raise ValueError("the API key must be between 1 and 4096 characters")


class CreateProviderRequest(BaseModel):
    model_config = _STRICT

    name: str = Field(min_length=1, max_length=255)
    family: ProviderFamily
    #: Optional for families with a well-known endpoint (bge_m3, dashscope, volcengine).
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    #: Write-only. Stored encrypted; responses only report ``has_credentials``.
    api_key: SecretStr | None = None
    config: ProviderConfigRequest = Field(default_factory=ProviderConfigRequest)

    @model_validator(mode="after")
    def _family_rules(self) -> CreateProviderRequest:
        _check_key(self.api_key)
        if self.base_url is None:
            default = DEFAULT_BASE_URLS.get(self.family)
            if default is None:
                raise ValueError("base_url is required for this provider family")
            self.base_url = default
        if self.family in HOSTED_FAMILIES:
            if self.api_key is None:
                raise ValueError("an API key is required for this provider family")
            if self.config.allow_private:
                raise ValueError("hosted providers cannot use private networking")
        return self


class ProviderCredentialsRequest(BaseModel):
    model_config = _STRICT

    #: ``null`` removes the key, where the family allows running without one.
    api_key: SecretStr | None

    @model_validator(mode="after")
    def _bounded(self) -> ProviderCredentialsRequest:
        _check_key(self.api_key)
        return self


class CreateModelRequest(BaseModel):
    model_config = _STRICT

    provider_id: str
    model_key: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    capability: Literal["embedding"] = "embedding"
    #: Omit to detect it from the provider with one probe request.
    dimension: int | None = Field(default=None, ge=1, le=65536)
    max_input_tokens: int = Field(ge=1, le=32768)
    normalize: bool = True
    query_prefix: str | None = Field(default=None, max_length=1000)
    optimal_batch_size: int = Field(default=16, ge=1, le=1024)
    tokenizer_id: str = Field(min_length=1, max_length=255)
    #: The model returns learned sparse vectors (bge-m3, DashScope v3/v4).
    sparse: bool = False
    #: Send ``dimension`` to the provider (DashScope v3/v4, OpenAI text-embedding-3).
    send_dimension: bool = False


class ProviderResponse(BaseModel):
    id: str
    name: str
    family: str
    base_url: str | None
    is_enabled: bool
    model_count: int
    has_credentials: bool = False

    @classmethod
    def from_dto(cls, provider: ProviderView) -> ProviderResponse:
        return cls(
            id=encode_id("prov", provider.id),
            name=provider.name,
            family=provider.family,
            base_url=provider.base_url,
            is_enabled=provider.is_enabled,
            model_count=provider.model_count,
            has_credentials=provider.has_credentials,
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
    provider_family: str = ""
    sparse: bool = False
    send_dimension: bool = False

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
            provider_family=model.provider_family,
            sparse=model.sparse,
            send_dimension=model.send_dimension,
        )


class ModelTestResponse(BaseModel):
    healthy: bool
    dimensions: int
    tokens: int
    #: Terms in the probe text's sparse vector; absent for dense-only models.
    sparse_terms: int | None = None
