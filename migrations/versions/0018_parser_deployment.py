"""Reject mixed parser deployments within a building index for one MIME type."""

from alembic import op
import sqlalchemy as sa

revision = "0018_parser_deployment"
down_revision = "0017_parse_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_ingestion", sa.Column("parser_fingerprint", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("document_ingestion", "parser_fingerprint")
