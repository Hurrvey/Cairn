"""platform: durable managed MCP state and bounded logs"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_mcp_management"
down_revision = "0011_stage_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_service",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("observed_generation", sa.BigInteger(), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("effective_port", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(255)),
        sa.CheckConstraint("id = 1", name="ck_mcp_service_singleton"),
    )
    op.create_table(
        "mcp_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("detail", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("status", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_table("mcp_log")
    op.drop_table("mcp_service")
