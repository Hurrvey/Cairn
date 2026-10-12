"""Credential-safe invocation snapshots and Cairn's text-only model wire types."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32_768)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=64)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0, le=2, allow_inf_nan=False)
    stream: bool = False

    @model_validator(mode="after")
    def bounded(self) -> "ChatRequest":
        if sum(len(message.content) for message in self.messages) > 65_536:
            raise ValueError("Messages exceed the combined character limit.")
        return self


class ChatResponse(BaseModel):
    model_id: str
    content: str
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatChunk(BaseModel):
    type: Literal["delta", "completed", "error"]
    delta: str = ""
    model_id: str = ""
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    code: str | None = None


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=8192)
    documents: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded(self) -> "RerankRequest":
        if sum(len(doc) for doc in self.documents) > 200_000:
            raise ValueError("Rerank documents exceed the combined character limit.")
        return self


class RerankResponse(BaseModel):
    model_id: str
    scores: list[float]


@dataclass(frozen=True, slots=True)
class InvocationSnapshot:
    model: ModelRef
    provider: ProviderRuntimeProjection
    workspace_id: UUID
    principal_id: UUID | None
    request_id: str
    knowledge_base_id: UUID | None = None
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    currency: str = "USD"
