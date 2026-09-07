"""Catalog DTOs.

:class:`KnowledgeBaseRuntime` is the important one. It is the *only* shape in
which a knowledge base reaches the data plane (ADR-0002): M09 never sees a
``KnowledgeBase`` ORM object, so it never inherits the control plane's models,
migrations, or startup cost.

Adding a field here is an interface change requiring the M09 owner's sign-off.
That friction is deliberate — it is what stops the boundary eroding one
"just one field" at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from cairn.catalog.config import ChunkConfig, RetrievalConfig
from cairn.modelgw.dto import ModelRef

__all__ = [
    "BindingRef",
    "BindingRefModel",
    "ChunkSpec",
    "ChunkView",
    "CreateKbSpec",
    "DocumentRegistration",
    "DocumentView",
    "IndexProgressView",
    "IndexVersionView",
    "KnowledgeBaseRuntime",
    "KnowledgeBaseView",
    "ReindexEstimate",
    "ReindexSpec",
    "UpdateKbSpec",
    "UploadSpec",
]

KbStatus = Literal["active", "indexing", "error", "deleting", "archived"]
DocumentState = Literal[
    "registered",
    "fetching",
    "fetched",
    "parsing",
    "parsed",
    "chunking",
    "chunked",
    "embedding",
    "embedded",
    "indexing",
    "indexed",
    "failed",
    "skipped",
    "deleting",
]


@dataclass(frozen=True, slots=True)
class BindingRef:
    id: UUID
    kind: Literal["vector", "object"]
    driver: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    health_state: str = "unknown"


@dataclass(frozen=True, slots=True)
class KnowledgeBaseView:
    id: UUID
    workspace_id: UUID
    name: str
    slug: str
    description: str | None
    icon_url: str | None
    embedding_model_id: UUID
    embedding_dim: int
    metric: str
    vector_binding_id: UUID
    object_binding_id: UUID
    chunk_config: ChunkConfig
    retrieval_config: RetrievalConfig
    active_index_version: int | None
    building_index_version: int | None
    config_version: int
    owner_user_id: UUID
    status: KbStatus
    doc_count: int
    chunk_count: int
    bytes_used: int
    last_indexed_at: datetime | None
    created_at: datetime
    #: True when a config change needs a rebuild before it takes effect. Set on
    #: an update response so the UI can prompt rather than leaving the user to
    #: wonder why nothing changed.
    reindex_required: bool = False


class BindingRefModel(BaseModel):
    """A storage binding as the data plane sees it: driver and non-secret
    configuration. Credentials stay in the gateway."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    driver: str
    config: dict[str, Any] = {}


class KnowledgeBaseRuntime(BaseModel):
    """The minimal projection the data plane needs — published to Redis.

    Pydantic rather than a dataclass because it is serialised on every config
    change and deserialised on every cache hit.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    workspace_id: UUID
    #: (kb_id, ACTIVE index_version). Never "latest" — that is what would let a
    #: query see a half-built index mid-rebuild.
    index_version: int
    embedding_model: ModelRef
    metric: str
    vector_binding: BindingRefModel
    retrieval_config: RetrievalConfig
    config_version: int
    status: KbStatus


@dataclass(frozen=True, slots=True)
class CreateKbSpec:
    name: str
    embedding_model_id: UUID
    vector_binding_id: UUID
    object_binding_id: UUID
    description: str | None = None
    slug: str | None = None
    metric: Literal["cosine", "dot", "l2"] = "cosine"
    chunk_config: ChunkConfig | None = None
    retrieval_config: RetrievalConfig | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UpdateKbSpec:
    name: str | None = None
    description: str | None = None
    chunk_config: ChunkConfig | None = None
    retrieval_config: RetrievalConfig | None = None
    metadata: dict[str, Any] | None = None
    #: Present only so the service can reject them with a useful message rather
    #: than silently ignoring them (ADR-0006).
    embedding_model_id: UUID | None = None
    metric: str | None = None

    def changed_fields(self) -> set[str]:
        return {
            name
            for name in (
                "name",
                "description",
                "chunk_config",
                "retrieval_config",
                "metadata",
                "embedding_model_id",
                "metric",
            )
            if getattr(self, name) is not None
        }


@dataclass(frozen=True, slots=True)
class ReindexSpec:
    #: The only way to change a knowledge base's embedding model.
    embedding_model_id: UUID | None = None
    chunk_config: ChunkConfig | None = None
    confirm: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReindexEstimate:
    """Shown before a rebuild starts. Spending hours of compute and provider
    budget on a UI click nobody costed is a footgun."""

    chunks: int
    embedding_tokens: int
    estimated_cost_usd: float | None
    estimated_minutes: int
    peak_storage_bytes: int


@dataclass(frozen=True, slots=True)
class IndexVersionView:
    kb_id: UUID
    version: int
    state: Literal["building", "active", "retired", "failed"]
    layout: str
    chunk_total: int
    chunk_done: int
    started_at: datetime
    completed_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class IndexProgressView:
    active_index_version: int | None
    building_index_version: int | None
    state: str
    chunk_total: int
    chunk_done: int
    percent: float
    eta_seconds: int | None
    started_at: datetime | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UploadSpec:
    filename: str
    content_hash: str
    size_bytes: int
    mime_type: str
    object_key: str
    metadata: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    source_type: Literal["upload", "crawl", "s3_sync", "api", "connector"] = "upload"
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentView:
    id: UUID
    kb_id: UUID
    title: str | None
    source_type: str
    source_ref: str | None
    mime_type: str | None
    size_bytes: int | None
    content_hash: str
    state: DocumentState
    stage_detail: str | None
    error_code: str | None
    error_detail: str | None
    progress_pct: int
    revision: int
    page_count: int | None
    chunk_count: int
    created_at: datetime
    indexed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DocumentRegistration:
    """One entry in a bulk-upload result.

    ``skipped`` is not a failure: re-uploading identical content is the expected
    outcome of a retried sync, and reporting it as an error trains users to
    ignore errors.
    """

    filename: str
    status: Literal["accepted", "skipped", "rejected"]
    document: DocumentView | None = None
    reason: str | None = None
    existing_document_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ChunkView:
    id: UUID
    kb_id: UUID
    document_id: UUID
    index_version: int
    parent_id: UUID | None
    ordinal: int
    content: str
    token_count: int
    metadata: dict[str, Any]
    is_edited: bool


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    """What the ingestion pipeline writes."""

    id: UUID
    document_id: UUID
    ordinal: int
    content: str
    content_hash: str
    token_count: int
    parent_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
