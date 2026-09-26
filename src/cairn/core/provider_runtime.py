"""Model-provider runtime projection shared with data workers.

Credentials travel sealed (``cairn.core.secrets``): Redis holds ciphertext
only, and a data process opens it with the master key it already has.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cairn.core.secrets import SealedCredential

__all__ = [
    "DEFAULT_BASE_URLS",
    "FAMILY_BATCH_LIMITS",
    "HOSTED_FAMILIES",
    "PROVIDER_FAMILIES",
    "PROVIDER_RUNTIME_TTL",
    "SPARSE_FAMILIES",
    "ProviderFamily",
    "ProviderRuntimeProjection",
    "provider_runtime_key",
    "provider_runtime_tombstone_key",
]

PROVIDER_RUNTIME_TTL = 300

ProviderFamily = Literal[
    "tei", "infinity", "bge_m3", "dashscope", "volcengine", "openai_compatible"
]
#: Families with a runtime adapter, in the order the UI presents them.
PROVIDER_FAMILIES: tuple[ProviderFamily, ...] = (
    "tei",
    "infinity",
    "bge_m3",
    "dashscope",
    "volcengine",
    "openai_compatible",
)
#: Families whose models can return learned sparse weights.
SPARSE_FAMILIES: frozenset[str] = frozenset({"bge_m3", "dashscope"})
#: Public vendor endpoints: an API key is mandatory and private addresses are never allowed.
HOSTED_FAMILIES: frozenset[str] = frozenset({"dashscope", "volcengine"})
#: Where a family lives unless the administrator says otherwise.
DEFAULT_BASE_URLS: dict[str, str] = {
    "bge_m3": "http://bge-m3:8000",
    "dashscope": "https://dashscope.aliyuncs.com/api/v1",
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
}
#: Hard per-request input limits the vendor enforces; larger batches are split.
FAMILY_BATCH_LIMITS: dict[str, int] = {"dashscope": 10}


def provider_runtime_key(provider_id: UUID) -> str:
    return f"model:provider-runtime:{provider_id}"


def provider_runtime_tombstone_key(provider_id: UUID) -> str:
    return f"model:provider-runtime-deleted:{provider_id}"


class ProviderRuntimeProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    workspace_id: UUID
    family: ProviderFamily
    base_url: str = Field(min_length=1, max_length=2048)
    allow_private: bool = False
    binding_revision: str = Field(min_length=1, max_length=255)
    max_batch_size: int = Field(default=16, ge=1, le=1024)
    credential: SealedCredential | None = None

    @property
    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()
