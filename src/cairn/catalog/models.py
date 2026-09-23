"""Catalog ORM models. Private to this module."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811 — dialect type
from sqlalchemy.orm import Mapped, mapped_column

from cairn.core.db import Base
from cairn.core.ids import new_uuid

__all__ = [
    "Chunk",
    "Document",
    "DocumentIngestion",
    "KbIndexVersion",
    "KnowledgeBase",
    "StorageBinding",
]

DOCUMENT_STATES = (
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
)

KB_STATUSES = ("active", "indexing", "error", "deleting", "archived")
INDEX_STATES = ("building", "active", "retired", "failed")


class StorageBinding(Base):
    __tablename__ = "storage_binding"
    __table_args__ = (
        UniqueConstraint("workspace_id", "kind", "name", name="uq_storage_binding_name"),
        CheckConstraint("kind IN ('vector','object')", name="kind"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    driver: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret_ref: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("secret.id", ondelete="SET NULL"), nullable=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    health_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_knowledge_base_slug"),
        CheckConstraint(f"status IN {KB_STATUSES}", name="status"),
        CheckConstraint("metric IN ('cosine','dot','l2')", name="metric"),
        CheckConstraint("embedding_dim > 0", name="embedding_dim_positive"),
        CheckConstraint(
            "index_version_high_water >= 0", name="index_version_high_water_nonnegative"
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    kb_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    # IMMUTABLE once `active_index_version` is set — enforced at the API, in the
    # service, and by a database trigger (ADR-0006). Three layers because the
    # failure mode is silent: mixed embedding spaces produce plausible-looking
    # scores and quietly wrong results.
    embedding_model_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("model.id"), nullable=False
    )
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    metric: Mapped[str] = mapped_column(String(16), nullable=False, default="cosine")

    vector_binding_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("storage_binding.id"), nullable=False
    )
    object_binding_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("storage_binding.id"), nullable=False
    )

    chunk_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    retrieval_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    ingest_pipeline_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    retrieval_pipeline_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    #: Blue/green (ADR-0007). Retrieval reads `active`; a rebuild writes
    #: `building`; the switch is one atomic UPDATE.
    active_index_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_index_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Durable allocator state. Historical version rows may be purged, but this
    #: value is never decremented, so a namespace identity is never reused.
    index_version_high_water: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    owner_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("user.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: Bumped on every config change so the data-plane cache key changes and
    #: stale entries become unreachable rather than needing enumeration.
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "embedding_model_id": str(self.embedding_model_id),
            "embedding_dim": self.embedding_dim,
            "metric": self.metric,
            "status": self.status,
            "active_index_version": self.active_index_version,
        }


class KbIndexVersion(Base):
    __tablename__ = "kb_index_version"
    __table_args__ = (
        CheckConstraint(f"state IN {INDEX_STATES}", name="state"),
        CheckConstraint("layout IN ('shared','dedicated')", name="layout"),
        CheckConstraint(
            "(snapshot_unavailable AND config_snapshot IS NULL) OR "
            "(NOT snapshot_unavailable AND config_snapshot IS NOT NULL)",
            name="snapshot_coherent",
        ),
        CheckConstraint(
            "enrollment_state IN ('scanning','reconciling','complete')",
            name="enrollment_state",
        ),
        CheckConstraint("enrollment_generation >= 0", name="enrollment_generation_nonnegative"),
        # At most one build in flight per knowledge base. Two concurrent
        # rebuilds would race to flip `active_index_version`, and the loser's
        # namespace would leak.
        Index(
            "uq_kb_index_building",
            "kb_id",
            unique=True,
            postgresql_where=text("state = 'building'"),
        ),
    )

    kb_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("knowledge_base.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="building")
    layout: Mapped[str] = mapped_column(String(16), nullable=False, default="shared")
    physical_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Credential-free, immutable version provenance. NULL is reserved for
    #: legacy rows whose exact historical configuration cannot be proven.
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    snapshot_unavailable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    enrollment_state: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    enrollment_cursor: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    enrollment_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    chunk_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chunk_done: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retire_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        # Dedup within a knowledge base (FR-D-07). Re-uploading identical bytes
        # returns the existing document instead of a duplicate.
        UniqueConstraint("kb_id", "content_hash", name="uq_document_kb_content_hash"),
        CheckConstraint(f"state IN {DOCUMENT_STATES}", name="state"),
        CheckConstraint(
            "source_type IN ('upload','crawl','s3_sync','api','connector')", name="source_type"
        ),
        Index(
            "ix_document_kb_state",
            "kb_id",
            "state",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_document_source", "kb_id", "source_type", "source_ref"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("knowledge_base.id", ondelete="CASCADE"), nullable=False
    )

    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    crawl_job_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    stage_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Machine-readable, mapped to a contributor-facing message in the UI. A bare
    #: "processing failed" is the most-complained-about thing in every RAG product.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "title": self.title,
            "source_type": self.source_type,
            "content_hash": self.content_hash,
            "state": self.state,
            "revision": self.revision,
        }


class DocumentIngestion(Base):
    __tablename__ = "document_ingestion"
    __table_args__ = (
        ForeignKeyConstraint(
            ["kb_id", "index_version"],
            ["kb_index_version.kb_id", "kb_index_version.version"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('registered','parsed','chunked','embedded','indexed','failed')",
            name="state",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("index_version > 0", name="index_version_positive"),
        CheckConstraint("point_count >= 0", name="point_count_nonnegative"),
        CheckConstraint(
            "failed_stage IS NULL OR failed_stage IN ('parse','chunk','embed','index')",
            name="failed_stage",
        ),
        CheckConstraint(
            "previous_committed_state IS NULL OR previous_committed_state IN "
            "('registered','parsed','chunked','embedded','indexed')",
            name="previous_committed_state",
        ),
        CheckConstraint("recovery_generation >= 0", name="recovery_generation_nonnegative"),
        Index("ix_document_ingestion_build_state", "kb_id", "index_version", "state"),
    )

    document_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document.id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    index_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    failed_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    previous_committed_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recovery_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prior_parsed_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunks_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    embeddings_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    prior_embeddings_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    point_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_point_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Chunk(Base):
    """A retrievable unit of text.

    PostgreSQL is the source of truth; the vector store holds a denormalised
    copy for the read path (ADR-0005). Hash-partitioned by ``kb_id`` from the
    first migration — retrofitting partitioning onto the largest table in the
    system is a rewrite.
    """

    __tablename__ = "chunk"
    __table_args__ = (
        CheckConstraint("edit_generation >= 0", name="edit_generation_nonnegative"),
        CheckConstraint(
            "reembed_applied_generation >= 0",
            name="reembed_applied_generation_nonnegative",
        ),
        CheckConstraint(
            "reembed_applied_generation <= edit_generation",
            name="reembed_generation_order",
        ),
        Index("ix_chunk_document", "kb_id", "document_id", "index_version"),
        Index("ix_chunk_hash", "kb_id", "content_hash"),
        {"postgresql_partition_by": "HASH (kb_id)"},
    )

    kb_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)

    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    index_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: NULL for a parent or standalone chunk. Children are embedded; parents are
    #: returned when `expand_parent` is on.
    parent_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    #: A manual fix to a badly-parsed table must survive the next reindex, or
    #: the user loses the work every time chunking is retuned.
    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    edit_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reembed_applied_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
