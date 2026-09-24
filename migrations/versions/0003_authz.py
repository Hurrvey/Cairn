"""authz: api keys and resource grants

Revision ID: 0003_authz
Revises: 0002_tasks
Create Date: 2026-08-31

FR-B-04, FR-B-08, FR-B-13.

Note what is *absent*: there is no column holding an API key's effective
permissions. That set is ``key_scopes ∩ owner_permissions``, recomputed at every
authentication. Materialising it would create exactly the stale-privilege
problem the intersection exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_authz"
down_revision: str | None = "0002_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_key",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_prefix", sa.String(32), nullable=False),
        # SHA-256 only. The key is 190 bits of true randomness, so there is
        # nothing to brute-force and the data plane needs an O(1) lookup.
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("last_four", sa.String(8), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column(
            "kb_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=True),
        sa.Column("ip_allowlist", postgresql.ARRAY(postgresql.CIDR()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_key"),
        sa.UniqueConstraint("key_hash", name="uq_api_key_key_hash"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_api_key_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], name="fk_api_key_owner_user_id_user", ondelete="CASCADE"
        ),
    )
    op.execute("CREATE INDEX ix_api_key_prefix ON api_key (key_prefix) WHERE revoked_at IS NULL")
    op.create_index("ix_api_key_owner", "api_key", ["owner_user_id"])

    op.create_table(
        "resource_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("permissions", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("is_break_glass", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_resource_grant"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_resource_grant_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "subject_type IN ('user','api_key')", name="ck_resource_grant_subject_type"
        ),
        sa.CheckConstraint(
            "resource_type IN ('workspace','knowledge_base','pipeline','function',"
            "'model','golden_set')",
            name="ck_resource_grant_resource_type",
        ),
        # A break-glass grant that never expires is not break-glass (FR-B-10).
        sa.CheckConstraint(
            "NOT is_break_glass OR (reason IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_resource_grant_break_glass_requires_reason_and_expiry",
        ),
    )
    op.execute(
        "CREATE INDEX ix_grant_subject ON resource_grant (subject_type, subject_id) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_grant_resource ON resource_grant (resource_type, resource_id) "
        "WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    op.drop_table("resource_grant")
    op.drop_table("api_key")
