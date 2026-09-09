"""ingestion: revision and index scoped stage ledger

Revision ID: 0007_ingestion_pipeline
Revises: 0006_catalog
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_ingestion_pipeline"
down_revision: str | None = "0006_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_ingestion",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="registered"),
        sa.Column("parsed_object_key", sa.Text(), nullable=True),
        sa.Column("chunks_object_key", sa.Text(), nullable=True),
        sa.Column("embeddings_object_key", sa.Text(), nullable=True),
        sa.Column("prior_embeddings_object_key", sa.Text(), nullable=True),
        sa.Column("point_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "stale_point_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint(
            "document_id", "revision", "index_version", name="pk_document_ingestion"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_document_ingestion_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id", "index_version"],
            ["kb_index_version.kb_id", "kb_index_version.version"],
            name="fk_document_ingestion_index_version",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('registered','parsed','chunked','embedded','indexed','failed')",
            name="ck_document_ingestion_state",
        ),
        sa.CheckConstraint("revision > 0", name="ck_document_ingestion_revision_positive"),
        sa.CheckConstraint(
            "index_version > 0", name="ck_document_ingestion_index_version_positive"
        ),
        sa.CheckConstraint(
            "point_count >= 0", name="ck_document_ingestion_point_count_nonnegative"
        ),
    )
    op.create_index(
        "ix_document_ingestion_build_state",
        "document_ingestion",
        ["kb_id", "index_version", "state"],
    )


def downgrade() -> None:
    op.drop_table("document_ingestion")
