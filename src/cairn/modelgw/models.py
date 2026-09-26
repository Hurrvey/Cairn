"""Model gateway ORM models. Private to this module."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811 — dialect type
from sqlalchemy.orm import Mapped, mapped_column

from cairn.core.db import Base
from cairn.core.ids import new_uuid

__all__ = ["Model", "ModelProvider", "Secret"]


class Secret(Base):
    """Envelope-encrypted credential (NFR-SEC-02).

    The AAD binds a secret to its workspace and purpose, so a row lifted from a
    database dump cannot be replayed into a different workspace.
    """

    __tablename__ = "secret"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Which KEK wrapped the DEK. Lets a key rotation re-wrap without touching
    #: ciphertexts.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelProvider(Base):
    __tablename__ = "model_provider"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_model_provider_name"),)

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ref: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("secret.id", ondelete="SET NULL"), nullable=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Model(Base):
    __tablename__ = "model"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_key", name="uq_model_provider_key"),
        # Every embedding-model lookup is "which models can this workspace use
        # for this capability", including the one on the knowledge-base creation
        # path.
        Index("ix_model_workspace_capability", "workspace_id", "capability"),
        CheckConstraint("capability IN ('chat','embedding','rerank')", name="capability"),
        # An embedding model without a dimension cannot be used to configure a
        # knowledge base, and discovering that at ingest time — after the KB is
        # created and documents are uploaded — is far too late.
        CheckConstraint("capability <> 'embedding' OR dimension IS NOT NULL", name="embedding_dim"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    provider_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("model_provider.id", ondelete="CASCADE"), nullable=False
    )

    model_key: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    capability: Mapped[str] = mapped_column(String(16), nullable=False)

    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalize: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    query_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    optimal_batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=64)
    tokenizer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Learned sparse output (bge-m3, DashScope v3/v4): usable as a sparse source.
    sparse: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Send the registered dimension to the provider instead of taking its default.
    send_dimension: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    cost_per_1k_input: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_per_1k_output: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    health_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
