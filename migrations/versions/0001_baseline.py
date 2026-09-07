"""baseline: workspace, user, session, system_bootstrap, audit_log

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-28

Phase 0 schema. Two things here are deliberate and expensive to retrofit:

* ``audit_log`` is created **already partitioned by month**. A plain table
  cannot be converted to a partitioned one in place, so doing this later would
  mean a full rewrite of the largest append-only table in the system.
* Every table carries ``workspace_id`` (NFR-S-04), even though Phase 0 has a
  single workspace. Adding it later means rewriting every query in the codebase.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Months of audit partitions created up front. T-M15-04 (Phase 1) takes over
#: rolling creation; until then this is the runway.
_PARTITION_MONTHS = 12


def _month_bounds(offset: int) -> tuple[str, str, str]:
    base = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year = base.year + (base.month - 1 + offset) // 12
    month = (base.month - 1 + offset) % 12 + 1
    start = datetime(year, month, 1, tzinfo=UTC)
    end_year = year + (month // 12)
    end_month = month % 12 + 1
    end = datetime(end_year, end_month, 1, tzinfo=UTC)
    return (
        f"{start:%Y_%m}",
        start.isoformat(),
        end.isoformat(),
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ workspace
    op.create_table(
        "workspace",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column(
            "settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "quota", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace"),
        sa.UniqueConstraint("slug", name="uq_workspace_slug"),
    )

    # ------------------------------------------------------- system_bootstrap
    op.create_table(
        "system_bootstrap",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "initialized_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_system_bootstrap"),
        # A second row is structurally impossible, so no bug in the bootstrap
        # path can produce two initialisations.
        sa.CheckConstraint("id = 1", name="ck_system_bootstrap_singleton"),
    )

    # ----------------------------------------------------------------------- user
    op.create_table(
        "user",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        # FR-A-06: durable, so the requirement survives restart and redeploy.
        sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        # FR-A-13: bumping invalidates every session and change token.
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("perm_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_user"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_user_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "username", name="uq_user_workspace_id_username"),
        sa.CheckConstraint("role IN ('admin','user')", name="ck_user_role"),
    )
    op.create_index(
        "ix_user_workspace_active",
        "user",
        ["workspace_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Case-insensitive uniqueness: "Admin" and "admin" must not be two accounts.
    op.execute(
        'CREATE UNIQUE INDEX ix_user_username_lower ON "user" '
        "(workspace_id, lower(username)) WHERE deleted_at IS NULL"
    )

    # -------------------------------------------------------------------- session
    op.create_table(
        "session",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # SHA-256 of the opaque token; the token itself is never stored.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("csrf_token_hash", sa.String(64), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_session"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name="fk_session_user_id_user", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_session_token_hash"),
    )
    op.create_index(
        "ix_session_user", "session", ["user_id"], postgresql_where=sa.text("revoked_at IS NULL")
    )
    op.create_index(
        "ix_session_expiry",
        "session",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ------------------------------------------------------------------ audit_log
    # Raw SQL: Alembic cannot emit PARTITION BY. The primary key must include
    # the partition key, hence (at, id).
    op.execute(
        """
        CREATE TABLE audit_log (
            at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
            id              BIGINT       GENERATED BY DEFAULT AS IDENTITY,
            workspace_id    UUID         NOT NULL,
            actor_type      VARCHAR(16)  NOT NULL,
            actor_id        UUID,
            actor_label     VARCHAR(255) NOT NULL,
            action          VARCHAR(64)  NOT NULL,
            resource_type   VARCHAR(32),
            resource_id     UUID,
            outcome         VARCHAR(16)  NOT NULL DEFAULT 'success',
            ip              INET,
            user_agent      TEXT,
            request_id      VARCHAR(64),
            before          JSONB,
            after           JSONB,
            detail          JSONB        NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT pk_audit_log PRIMARY KEY (at, id),
            CONSTRAINT ck_audit_log_actor_type
                CHECK (actor_type IN ('user','api_key','system')),
            CONSTRAINT ck_audit_log_outcome
                CHECK (outcome IN ('success','failure','denied'))
        ) PARTITION BY RANGE (at)
        """
    )
    for offset in range(_PARTITION_MONTHS):
        suffix, start, end = _month_bounds(offset)
        op.execute(
            f"CREATE TABLE audit_log_{suffix} PARTITION OF audit_log "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )

    op.execute(
        "CREATE INDEX ix_audit_log_actor ON audit_log (workspace_id, actor_id, at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_resource "
        "ON audit_log (workspace_id, resource_type, resource_id, at DESC)"
    )
    op.execute("CREATE INDEX ix_audit_log_action ON audit_log (workspace_id, action, at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE")
    op.drop_table("session")
    op.execute("DROP INDEX IF EXISTS ix_user_username_lower")
    op.drop_table("user")
    op.drop_table("system_bootstrap")
    op.drop_table("workspace")
