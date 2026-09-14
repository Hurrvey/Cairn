"""catalog: durable reindex fan-out enrollment

Revision ID: 0009_reindex_fanout
Revises: 0008_version_snapshots
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_reindex_fanout"
down_revision: str | None = "0008_version_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "kb_index_version",
        sa.Column(
            "enrollment_state",
            sa.String(length=16),
            nullable=False,
            server_default="complete",
        ),
    )
    op.add_column(
        "kb_index_version",
        sa.Column("enrollment_cursor", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "kb_index_version",
        sa.Column("enrollment_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "document_ingestion",
        sa.Column("prior_parsed_object_key", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_kb_index_version_enrollment_state",
        "kb_index_version",
        "enrollment_state IN ('scanning','reconciling','complete')",
    )
    op.create_check_constraint(
        "ck_kb_index_version_enrollment_generation_nonnegative",
        "kb_index_version",
        "enrollment_generation >= 0",
    )
    op.execute(
        """
        UPDATE kb_index_version version
           SET enrollment_state = 'scanning'
          FROM knowledge_base kb
         WHERE version.kb_id = kb.id
           AND version.version = kb.building_index_version
           AND version.state = 'building'
           AND kb.active_index_version IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE document_ingestion DROP COLUMN IF EXISTS prior_parsed_object_key")
    op.drop_constraint(
        "ck_kb_index_version_enrollment_generation_nonnegative",
        "kb_index_version",
        type_="check",
    )
    op.drop_constraint("ck_kb_index_version_enrollment_state", "kb_index_version", type_="check")
    op.drop_column("kb_index_version", "enrollment_generation")
    op.drop_column("kb_index_version", "enrollment_cursor")
    op.drop_column("kb_index_version", "enrollment_state")
