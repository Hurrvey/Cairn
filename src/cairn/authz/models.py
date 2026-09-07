"""Authorization ORM models. Private to this module."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CIDR
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811 — dialect type
from sqlalchemy.orm import Mapped, mapped_column

from cairn.core.db import Base
from cairn.core.ids import new_uuid

__all__ = ["ApiKey", "ResourceGrant"]


class ApiKey(Base):
    """A first-class principal, not user impersonation (FR-B-08).

    Only the SHA-256 is stored. Not Argon2: the key is 190 bits of true
    randomness, so there is nothing to brute-force, and the data plane needs an
    O(1) lookup rather than a 100 ms KDF on every request.
    """

    __tablename__ = "api_key"
    __table_args__ = (
        Index("ix_api_key_prefix", "key_prefix", postgresql_where=text("revoked_at IS NULL")),
        Index("ix_api_key_owner", "owner_user_id"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    last_four: Mapped[str] = mapped_column(String(8), nullable=False)

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, default=list, server_default="{}"
    )
    #: Empty means "every knowledge base the owner can reach" — resolved at
    #: authentication time, so it narrows automatically when the owner's access does.
    kb_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )

    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_allowlist: Mapped[list[str] | None] = mapped_column(ARRAY(CIDR), nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def snapshot(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "name": self.name,
            "key_prefix": self.key_prefix,
            "scopes": list(self.scopes),
            "kb_ids": [str(k) for k in self.kb_ids],
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked_at is not None,
        }


class ResourceGrant(Base):
    __tablename__ = "resource_grant"
    __table_args__ = (
        Index(
            "ix_grant_subject",
            "subject_type",
            "subject_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_grant_resource",
            "resource_type",
            "resource_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint("subject_type IN ('user','api_key')", name="subject_type"),
        CheckConstraint(
            "resource_type IN ('workspace','knowledge_base','pipeline','function',"
            "'model','golden_set')",
            name="resource_type",
        ),
        CheckConstraint(
            "NOT is_break_glass OR (reason IS NOT NULL AND expires_at IS NOT NULL)",
            name="break_glass_requires_reason_and_expiry",
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )

    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: NULL means workspace-wide for that resource type.
    resource_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    permissions: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)

    granted_by: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_break_glass: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: Expiry is enforced by the resolution query, so an expired grant stops
    #: working the moment it lapses — no cleanup job is required for correctness.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def snapshot(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "subject_type": self.subject_type,
            "subject_id": str(self.subject_id),
            "resource_type": self.resource_type,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "permissions": list(self.permissions),
            "is_break_glass": self.is_break_glass,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
