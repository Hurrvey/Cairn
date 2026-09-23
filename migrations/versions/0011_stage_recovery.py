"""catalog: durable failed-stage recovery identity

Revision ID: 0011_stage_recovery
Revises: 0010_chunk_reembed
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_stage_recovery"
down_revision: str | None = "0010_chunk_reembed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_ingestion",
        sa.Column("failed_stage", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "document_ingestion",
        sa.Column("previous_committed_state", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "document_ingestion",
        sa.Column(
            "recovery_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_document_ingestion_failed_stage",
        "document_ingestion",
        "failed_stage IS NULL OR failed_stage IN ('parse','chunk','embed','index')",
    )
    op.create_check_constraint(
        "ck_document_ingestion_previous_committed_state",
        "document_ingestion",
        "previous_committed_state IS NULL OR previous_committed_state IN "
        "('registered','parsed','chunked','embedded','indexed')",
    )
    op.create_check_constraint(
        "ck_document_ingestion_recovery_generation_nonnegative",
        "document_ingestion",
        "recovery_generation >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_document_ingestion_recovery_generation_nonnegative",
        "document_ingestion",
        type_="check",
    )
    op.drop_constraint(
        "ck_document_ingestion_previous_committed_state",
        "document_ingestion",
        type_="check",
    )
    op.drop_constraint(
        "ck_document_ingestion_failed_stage",
        "document_ingestion",
        type_="check",
    )
    op.drop_column("document_ingestion", "recovery_generation")
    op.drop_column("document_ingestion", "previous_committed_state")
    op.drop_column("document_ingestion", "failed_stage")
