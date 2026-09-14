"""catalog: durable manual chunk re-embedding identity

Revision ID: 0010_chunk_reembed
Revises: 0009_reindex_fanout
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_chunk_reembed"
down_revision: str | None = "0009_reindex_fanout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chunk",
        sa.Column("edit_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "chunk",
        sa.Column(
            "reembed_applied_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_chunk_edit_generation_nonnegative",
        "chunk",
        "edit_generation >= 0",
    )
    op.create_check_constraint(
        "ck_chunk_reembed_applied_generation_nonnegative",
        "chunk",
        "reembed_applied_generation >= 0",
    )
    op.create_check_constraint(
        "ck_chunk_reembed_generation_order",
        "chunk",
        "reembed_applied_generation <= edit_generation",
    )


def downgrade() -> None:
    op.drop_constraint("ck_chunk_reembed_generation_order", "chunk", type_="check")
    op.drop_constraint("ck_chunk_reembed_applied_generation_nonnegative", "chunk", type_="check")
    op.drop_constraint("ck_chunk_edit_generation_nonnegative", "chunk", type_="check")
    op.drop_column("chunk", "reembed_applied_generation")
    op.drop_column("chunk", "edit_generation")
