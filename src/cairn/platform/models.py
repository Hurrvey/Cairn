"""Platform ORM models. Private to this module."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811 — dialect type
from sqlalchemy.orm import Mapped, mapped_column

from cairn.core.db import Base
from cairn.core.ids import new_uuid

__all__ = ["AuditLog", "QuotaAccount", "QuotaAllocation", "Workspace"]

CAPACITY_PERIOD = "capacity"
QUOTA_KINDS = ("knowledge_bases", "documents", "storage_bytes", "retrieval_rpm")
QUOTA_ALLOCATION_KINDS = ("knowledge_bases", "documents", "storage_bytes")


class UsageRecord(Base):
    __tablename__ = "usage_record"
    __table_args__ = (Index("ix_usage_workspace_at", "workspace_id", "at"),)

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    principal_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    knowledge_base_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Workspace(Base):
    """The tenancy boundary. Single-tenant deployments have exactly one."""

    __tablename__ = "workspace"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    quota: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class QuotaAccount(Base):
    """Authoritative durable occupancy and configuration for one quota kind."""

    __tablename__ = "quota_account"
    __table_args__ = (
        CheckConstraint(f"kind IN {QUOTA_KINDS}", name="kind"),
        CheckConstraint(f"period = '{CAPACITY_PERIOD}'", name="period"),
        CheckConstraint("used >= 0", name="used_nonnegative"),
        CheckConstraint("reserved = 0", name="reserved_zero_first_slice"),
        CheckConstraint('"limit" IS NULL OR "limit" >= 0', name="limit_nonnegative"),
        CheckConstraint("unknown_count >= 0", name="unknown_count_nonnegative"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    period: Mapped[str] = mapped_column(
        String(16), primary_key=True, default=CAPACITY_PERIOD, server_default=CAPACITY_PERIOD
    )
    used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    limit: Mapped[int | None] = mapped_column("limit", BigInteger, nullable=True)
    unknown_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class QuotaAllocation(Base):
    """Idempotent resource charge retained as a tombstone after release."""

    __tablename__ = "quota_allocation"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "kind",
            "period",
            "resource_kind",
            "resource_id",
            name="uq_quota_allocation_resource",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "kind", "period"],
            ["quota_account.workspace_id", "quota_account.kind", "quota_account.period"],
            name="fk_quota_allocation_account",
        ),
        CheckConstraint(f"kind IN {QUOTA_ALLOCATION_KINDS}", name="kind"),
        CheckConstraint(f"period = '{CAPACITY_PERIOD}'", name="period"),
        CheckConstraint("resource_kind IN ('knowledge_base','document')", name="resource_kind"),
        CheckConstraint("state IN ('active','released')", name="state"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "(is_unknown AND quantity IS NULL) OR "
            "(NOT is_unknown AND quantity IS NOT NULL AND quantity >= 0)",
            name="quantity_known_or_unknown",
        ),
        CheckConstraint(
            "(state = 'active' AND released_at IS NULL) OR "
            "(state = 'released' AND released_at IS NOT NULL)",
            name="released_tombstone",
        ),
        CheckConstraint(
            "(resource_kind = 'knowledge_base' AND knowledge_base_id = resource_id "
            "AND kind = 'knowledge_bases' AND quantity = 1 AND NOT is_unknown) OR "
            "(resource_kind = 'document' AND knowledge_base_id <> resource_id "
            "AND ((kind = 'documents' AND quantity = 1 AND NOT is_unknown) "
            "OR kind = 'storage_bytes'))",
            name="resource_kind_matches_account",
        ),
        Index(
            "ix_quota_allocation_kb_active",
            "workspace_id",
            "knowledge_base_id",
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CAPACITY_PERIOD, server_default=CAPACITY_PERIOD
    )
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_unknown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Append-only. There is deliberately no update or delete path (FR-O-01).

    ``actor_label`` is denormalised because the audit trail must stay readable
    after the actor is deleted — which is precisely when it matters most.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_actor", "workspace_id", "actor_id", "at"),
        Index("ix_audit_log_resource", "workspace_id", "resource_type", "resource_id", "at"),
        Index("ix_audit_log_action", "workspace_id", "action", "at"),
        {"postgresql_partition_by": "RANGE (at)"},
    )

    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, server_default=func.now()
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(255), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")

    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class MCPService(Base):
    __tablename__ = "mcp_service"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    observed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    effective_port: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))


class MCPLog(Base):
    __tablename__ = "mcp_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
