"""Request/response models for grants and API keys."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn.authz.models import ApiKey, ResourceGrant
from cairn.core.ids import encode_id

__all__ = [
    "ApiKeyCreatedResponse",
    "ApiKeyResponse",
    "BreakGlassRequest",
    "CreateApiKeyRequest",
    "CreateGrantRequest",
    "EffectivePermissionsResponse",
    "GrantResponse",
]

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)

ResourceTypeLiteral = Literal[
    "workspace", "knowledge_base", "pipeline", "function", "model", "golden_set"
]

_PREFIX_BY_RESOURCE = {
    "workspace": "ws",
    "knowledge_base": "kb",
    "pipeline": "pipe",
    "function": "fn",
    "model": "mdl",
    "golden_set": "gs",
}


class CreateGrantRequest(BaseModel):
    model_config = _Strict
    subject_type: Literal["user", "api_key"]
    subject_id: str
    resource_type: ResourceTypeLiteral
    resource_id: str | None = None
    permissions: list[str] = Field(min_length=1, max_length=16)
    expires_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=2000)


class GrantResponse(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    resource_type: str
    resource_id: str | None
    permissions: list[str]
    is_break_glass: bool
    reason: str | None
    expires_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, grant: ResourceGrant) -> GrantResponse:
        prefix = _PREFIX_BY_RESOURCE.get(grant.resource_type, "kb")
        return cls(
            id=encode_id("grant", grant.id),
            subject_type=grant.subject_type,
            subject_id=encode_id(
                "usr" if grant.subject_type == "user" else "key", grant.subject_id
            ),
            resource_type=grant.resource_type,
            resource_id=encode_id(prefix, grant.resource_id) if grant.resource_id else None,
            permissions=list(grant.permissions),
            is_break_glass=grant.is_break_glass,
            reason=grant.reason,
            expires_at=grant.expires_at,
            created_at=grant.created_at,
        )


class EffectivePermissionsResponse(BaseModel):
    """Answers "why can this principal do that?" — the question an operator
    actually asks during a review."""

    subject_id: str
    role: str
    workspace_permissions: list[str]
    resource_permissions: dict[str, list[str]]
    accessible_knowledge_bases: list[str]


class CreateApiKeyRequest(BaseModel):
    model_config = _Strict
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(min_length=1, max_length=16)
    knowledge_base_ids: list[str] | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=100_000)
    ip_allowlist: list[str] | None = Field(default=None, max_length=32)
    expires_at: datetime | None = None


class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    last_four: str
    scopes: list[str]
    knowledge_base_ids: list[str]
    rate_limit_rpm: int | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    use_count: int
    created_at: datetime

    @classmethod
    def from_model(cls, key: ApiKey) -> ApiKeyResponse:
        return cls(
            id=encode_id("key", key.id),
            name=key.name,
            key_prefix=key.key_prefix,
            last_four=key.last_four,
            scopes=list(key.scopes),
            knowledge_base_ids=[encode_id("kb", k) for k in key.kb_ids],
            rate_limit_rpm=key.rate_limit_rpm,
            expires_at=key.expires_at,
            revoked_at=key.revoked_at,
            last_used_at=key.last_used_at,
            use_count=key.use_count,
            created_at=key.created_at,
        )


class ApiKeyCreatedResponse(BaseModel):
    api_key: ApiKeyResponse
    #: Returned exactly once. Not recoverable afterwards (FR-B-13).
    key: str
    warning: str = "This key is displayed once and cannot be retrieved again."


class BreakGlassRequest(BaseModel):
    model_config = _Strict
    reason: str = Field(
        min_length=20,
        max_length=2000,
        description=(
            "Why this access is needed. Recorded in the audit log and sent to "
            "the knowledge base owner."
        ),
    )
    ttl_minutes: int | None = Field(default=None, ge=5, le=1440)
