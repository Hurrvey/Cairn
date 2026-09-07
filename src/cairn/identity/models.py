"""Identity ORM models. Private to this module (import contract ``no_cross_orm``)."""

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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811 — dialect type
from sqlalchemy.orm import Mapped, mapped_column

from cairn.core.db import Base
from cairn.core.ids import new_uuid

__all__ = ["Session", "SystemBootstrap", "User"]


class SystemBootstrap(Base):
    """Singleton row guarding first-boot initialisation (FR-A-02).

    The ``id = 1`` check constraint makes a second row impossible, so even a
    bug in the bootstrap path cannot produce two initialisations.
    """

    __tablename__ = "system_bootstrap"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    initialized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)


class User(Base):
    __tablename__ = "user"
    __table_args__ = (
        UniqueConstraint("workspace_id", "username", name="uq_user_workspace_id_username"),
        CheckConstraint("role IN ('admin','user')", name="role"),
        Index(
            "ix_user_workspace_active",
            "workspace_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )

    username: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")

    #: FR-A-06 — durable, so it survives restart, redeploy, and session loss.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: FR-A-13 — bumping this invalidates every session and change token at once.
    credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Cache-key component for M02 permission resolution (FR-B-12).
    perm_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def snapshot(self) -> dict[str, object]:
        """Audit-safe projection. Never includes ``password_hash``."""
        return {
            "id": str(self.id),
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "credential_version": self.credential_version,
        }


class Session(Base):
    """A session, or — with ``scopes`` populated — a scope-limited change token.

    A change token is deliberately the *same* row type: it expires, it is
    revocable, and it is invalidated by a ``credential_version`` bump, all for
    free. What makes it restricted is its scope set, which the middleware
    enforces (FR-A-05).
    """

    __tablename__ = "session"
    __table_args__ = (
        Index("ix_session_user", "user_id", postgresql_where=text("revoked_at IS NULL")),
        Index("ix_session_expiry", "expires_at", postgresql_where=text("revoked_at IS NULL")),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=new_uuid)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )

    #: SHA-256 of the opaque token. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default="{}"
    )
    csrf_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
