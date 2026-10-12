"""Append-only deduplicated model usage; first version deliberately unpartitioned."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_model_usage"
down_revision = "0015_read_model_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_record",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost", sa.Numeric(24, 12), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("event", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_usage_workspace_at", "usage_record", ["workspace_id", "at"])


def downgrade() -> None:
    op.drop_table("usage_record")
