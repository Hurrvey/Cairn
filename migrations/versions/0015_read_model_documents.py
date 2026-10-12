"""documents: stable revision time; chunks: lookup by ID alone

Revision ID: 0015_read_model_documents
Revises: 0014_sparse_providers
Create Date: 2026-09-28

* ``document.revised_at`` — when the current revision was registered. Points
  carry it as ``doc_updated_at``; ``updated_at`` moves with every processing
  state change, so it cannot be copied into the read model. Existing rows take
  their creation time, the best value available.
* ``ix_chunk_id`` — ``GET /v1/chunks/{id}`` resolves a citation from the chunk
  ID alone; the primary key leads with ``kb_id``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_read_model_documents"
down_revision: str | None = "0014_sparse_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column(
            "revised_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute("UPDATE document SET revised_at = created_at")
    op.create_index("ix_chunk_id", "chunk", ["id"])


def downgrade() -> None:
    op.drop_index("ix_chunk_id", table_name="chunk")
    op.drop_column("document", "revised_at")
