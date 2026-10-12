"""Locate immutable document IR from its committed ingestion run."""

from alembic import op
import sqlalchemy as sa

revision = "0017_parse_version"
down_revision = "0016_model_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_ingestion", sa.Column("parse_version_id", sa.String(64), nullable=True))
    op.create_index(
        "ix_document_ingestion_parse_version_id", "document_ingestion", ["parse_version_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_ingestion_parse_version_id", table_name="document_ingestion")
    op.drop_column("document_ingestion", "parse_version_id")
