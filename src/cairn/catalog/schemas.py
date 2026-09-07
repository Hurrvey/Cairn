"""Request/response models for the catalog endpoints.

Every write body sets ``extra="forbid"`` (NFR-SEC-05) — an unknown field is far
more likely to be a client bug or a probe than a harmless extra.

Identifiers cross this boundary as prefixed public ids (``kb_01H...``), never as
raw UUIDs. ``decode_id`` then rejects a document id passed where a knowledge
base id belongs, which turns a whole class of confused-deputy mistakes into a
400 at the edge.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn.catalog.config import ChunkConfig, RetrievalConfig
from cairn.catalog.dto import (
    BindingRef,
    ChunkView,
    DocumentRegistration,
    DocumentView,
    IndexProgressView,
    IndexVersionView,
    KnowledgeBaseView,
    ReindexEstimate,
)
from cairn.core.ids import encode_id

__all__ = [
    "BindingResponse",
    "ChunkResponse",
    "CreateBindingRequest",
    "CreateKbRequest",
    "DocumentRegistrationResponse",
    "DocumentResponse",
    "EditChunkRequest",
    "IndexProgressResponse",
    "IndexVersionResponse",
    "KnowledgeBaseResponse",
    "RegisterUploadRequest",
    "ReindexEstimateResponse",
    "ReindexRequest",
    "UpdateKbRequest",
]

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_HASH_PATTERN = r"^[a-f0-9]{64}$"


# --------------------------------------------------------------- requests


class CreateKbRequest(BaseModel):
    model_config = _Strict

    name: str = Field(min_length=1, max_length=255)
    embedding_model_id: str = Field(description="Prefixed model id (`mdl_...`).")
    vector_binding_id: str = Field(description="Prefixed storage binding id (`bind_...`).")
    object_binding_id: str
    description: str | None = Field(default=None, max_length=4000)
    slug: str | None = Field(default=None, max_length=200, pattern=_SLUG_PATTERN)
    #: Immutable once the knowledge base has been indexed, together with the
    #: embedding model (ADR-0006).
    metric: Literal["cosine", "dot", "l2"] = "cosine"
    chunk_config: ChunkConfig | None = None
    retrieval_config: RetrievalConfig | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateKbRequest(BaseModel):
    """A partial update: every field is optional and ``None`` means "unchanged".

    ``embedding_model_id`` and ``metric`` are accepted rather than rejected by
    the schema so the service can answer with the reindex path (ADR-0006);
    silently dropping them would leave the caller believing a change landed.
    """

    model_config = _Strict

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    chunk_config: ChunkConfig | None = None
    retrieval_config: RetrievalConfig | None = None
    metadata: dict[str, Any] | None = None
    embedding_model_id: str | None = None
    metric: Literal["cosine", "dot", "l2"] | None = None


class ReindexRequest(BaseModel):
    model_config = _Strict

    embedding_model_id: str | None = Field(
        default=None, description="The only supported way to change the embedding model."
    )
    chunk_config: ChunkConfig | None = None
    #: Without this the request is costed and nothing is started. See
    #: `ReindexEstimateResponse`.
    confirm: bool = False
    reason: str | None = Field(default=None, max_length=1000)


class RegisterUploadRequest(BaseModel):
    """Registers content already written to object storage.

    The bytes do not pass through this endpoint: the client uploads to a
    presigned URL and then registers the result, so a 500 MB PDF never occupies
    an API worker.
    """

    model_config = _Strict

    filename: str = Field(min_length=1, max_length=1024)
    content_hash: str = Field(pattern=_HASH_PATTERN, description="SHA-256, lowercase hex.")
    size_bytes: int = Field(ge=0)
    mime_type: str = Field(max_length=255)
    object_key: str = Field(min_length=1, max_length=1024)
    title: str | None = Field(default=None, max_length=1024)
    source_type: Literal["upload", "crawl", "s3_sync", "api", "connector"] = "upload"
    source_ref: str | None = Field(default=None, max_length=2048)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EditChunkRequest(BaseModel):
    model_config = _Strict
    content: str = Field(min_length=1, max_length=100_000)


class CreateBindingRequest(BaseModel):
    model_config = _Strict

    kind: Literal["vector", "object"]
    driver: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    config: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


# -------------------------------------------------------------- responses


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    icon_url: str | None
    embedding_model_id: str
    embedding_dim: int
    metric: str
    vector_binding_id: str
    object_binding_id: str
    chunk_config: ChunkConfig
    retrieval_config: RetrievalConfig
    active_index_version: int | None
    building_index_version: int | None
    config_version: int
    owner_user_id: str
    status: str
    doc_count: int
    chunk_count: int
    bytes_used: int
    last_indexed_at: datetime | None
    created_at: datetime
    #: True when a config change needs a rebuild before it takes effect, so the
    #: UI can prompt instead of leaving the user wondering why nothing changed.
    reindex_required: bool = False

    @classmethod
    def from_dto(cls, kb: KnowledgeBaseView) -> KnowledgeBaseResponse:
        return cls(
            id=encode_id("kb", kb.id),
            name=kb.name,
            slug=kb.slug,
            description=kb.description,
            icon_url=kb.icon_url,
            embedding_model_id=encode_id("mdl", kb.embedding_model_id),
            embedding_dim=kb.embedding_dim,
            metric=kb.metric,
            vector_binding_id=encode_id("bind", kb.vector_binding_id),
            object_binding_id=encode_id("bind", kb.object_binding_id),
            chunk_config=kb.chunk_config,
            retrieval_config=kb.retrieval_config,
            active_index_version=kb.active_index_version,
            building_index_version=kb.building_index_version,
            config_version=kb.config_version,
            owner_user_id=encode_id("usr", kb.owner_user_id),
            status=kb.status,
            doc_count=kb.doc_count,
            chunk_count=kb.chunk_count,
            bytes_used=kb.bytes_used,
            last_indexed_at=kb.last_indexed_at,
            created_at=kb.created_at,
            reindex_required=kb.reindex_required,
        )


class DocumentResponse(BaseModel):
    id: str
    kb_id: str
    title: str | None
    source_type: str
    source_ref: str | None
    mime_type: str | None
    size_bytes: int | None
    content_hash: str
    state: str
    stage_detail: str | None
    #: Machine-readable; the UI maps it to a contributor-facing message. A bare
    #: "processing failed" is the most-complained-about thing in RAG products.
    error_code: str | None
    error_detail: str | None
    progress_pct: int
    revision: int
    page_count: int | None
    chunk_count: int
    created_at: datetime
    indexed_at: datetime | None

    @classmethod
    def from_dto(cls, document: DocumentView) -> DocumentResponse:
        return cls(
            id=encode_id("doc", document.id),
            kb_id=encode_id("kb", document.kb_id),
            title=document.title,
            source_type=document.source_type,
            source_ref=document.source_ref,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            content_hash=document.content_hash,
            state=document.state,
            stage_detail=document.stage_detail,
            error_code=document.error_code,
            error_detail=document.error_detail,
            progress_pct=document.progress_pct,
            revision=document.revision,
            page_count=document.page_count,
            chunk_count=document.chunk_count,
            created_at=document.created_at,
            indexed_at=document.indexed_at,
        )


class DocumentRegistrationResponse(BaseModel):
    """One entry in a bulk registration result.

    ``skipped`` is not a failure: re-registering identical content is the normal
    outcome of a retried sync, and reporting it as an error trains users to
    ignore errors.
    """

    filename: str
    status: Literal["accepted", "skipped", "rejected"]
    document: DocumentResponse | None = None
    reason: str | None = None
    existing_document_id: str | None = None

    @classmethod
    def from_dto(cls, result: DocumentRegistration) -> DocumentRegistrationResponse:
        return cls(
            filename=result.filename,
            status=result.status,
            document=DocumentResponse.from_dto(result.document) if result.document else None,
            reason=result.reason,
            existing_document_id=(
                encode_id("doc", result.existing_document_id)
                if result.existing_document_id
                else None
            ),
        )


class ChunkResponse(BaseModel):
    id: str
    document_id: str
    index_version: int
    parent_id: str | None
    ordinal: int
    content: str
    token_count: int
    metadata: dict[str, Any]
    is_edited: bool

    @classmethod
    def from_dto(cls, chunk: ChunkView) -> ChunkResponse:
        return cls(
            id=encode_id("chk", chunk.id),
            document_id=encode_id("doc", chunk.document_id),
            index_version=chunk.index_version,
            parent_id=encode_id("chk", chunk.parent_id) if chunk.parent_id else None,
            ordinal=chunk.ordinal,
            content=chunk.content,
            token_count=chunk.token_count,
            metadata=chunk.metadata,
            is_edited=chunk.is_edited,
        )


class IndexVersionResponse(BaseModel):
    version: int
    state: str
    layout: str
    chunk_total: int
    chunk_done: int
    started_at: datetime
    completed_at: datetime | None
    error: str | None

    @classmethod
    def from_dto(cls, row: IndexVersionView) -> IndexVersionResponse:
        return cls(
            version=row.version,
            state=row.state,
            layout=row.layout,
            chunk_total=row.chunk_total,
            chunk_done=row.chunk_done,
            started_at=row.started_at,
            completed_at=row.completed_at,
            error=row.error,
        )


class IndexProgressResponse(BaseModel):
    active_index_version: int | None
    building_index_version: int | None
    state: str
    chunk_total: int
    chunk_done: int
    percent: float
    eta_seconds: int | None
    started_at: datetime | None
    error: str | None = None

    @classmethod
    def from_dto(cls, progress: IndexProgressView) -> IndexProgressResponse:
        return cls(
            active_index_version=progress.active_index_version,
            building_index_version=progress.building_index_version,
            state=progress.state,
            chunk_total=progress.chunk_total,
            chunk_done=progress.chunk_done,
            percent=progress.percent,
            eta_seconds=progress.eta_seconds,
            started_at=progress.started_at,
            error=progress.error,
        )


class ReindexEstimateResponse(BaseModel):
    """Returned when ``confirm`` is absent. Nothing has been started.

    Spending hours of compute and provider budget on a UI click nobody costed is
    a footgun, so the cost is shown before the rebuild rather than after.
    """

    confirmation_required: Literal[True] = True
    chunks: int
    embedding_tokens: int
    estimated_cost_usd: float | None
    estimated_minutes: int
    peak_storage_bytes: int

    @classmethod
    def from_dto(cls, estimate: ReindexEstimate) -> ReindexEstimateResponse:
        return cls(
            chunks=estimate.chunks,
            embedding_tokens=estimate.embedding_tokens,
            estimated_cost_usd=estimate.estimated_cost_usd,
            estimated_minutes=estimate.estimated_minutes,
            peak_storage_bytes=estimate.peak_storage_bytes,
        )


class BindingResponse(BaseModel):
    id: str
    kind: str
    driver: str
    name: str
    #: Non-secret configuration only. Credentials live in the secret store and
    #: never appear in an API response.
    config: dict[str, Any]
    health_state: str

    @classmethod
    def from_dto(cls, binding: BindingRef) -> BindingResponse:
        return cls(
            id=encode_id("bind", binding.id),
            kind=binding.kind,
            driver=binding.driver,
            name=binding.name,
            config=binding.config,
            health_state=binding.health_state,
        )
