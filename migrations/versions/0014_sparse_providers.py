"""models and knowledge bases: learned sparse vectors and online providers

Revision ID: 0014_sparse_providers
Revises: 0013_qdrant
Create Date: 2026-09-27

* ``model.sparse`` — the model returns learned sparse weights (bge-m3, DashScope).
* ``model.send_dimension`` — request exactly the registered width from the provider.
* ``knowledge_base.sparse_kind`` / ``sparse_model_id`` — where the next index
  version's sparse vectors come from. Existing knowledge bases keep BM25, which
  is what they were built with; each version snapshots its own source.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_sparse_providers"
down_revision: str | None = "0013_qdrant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model", sa.Column("sparse", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "model",
        sa.Column("send_dimension", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "knowledge_base",
        sa.Column("sparse_kind", sa.String(16), nullable=False, server_default="bm25"),
    )
    op.add_column(
        "knowledge_base",
        sa.Column("sparse_model_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_base_sparse_model_id_model",
        "knowledge_base",
        "model",
        ["sparse_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_knowledge_base_sparse_source",
        "knowledge_base",
        "(sparse_kind = 'bm25' AND sparse_model_id IS NULL) OR "
        "(sparse_kind = 'model' AND sparse_model_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_knowledge_base_sparse_source", "knowledge_base", type_="check")
    op.drop_constraint(
        "fk_knowledge_base_sparse_model_id_model", "knowledge_base", type_="foreignkey"
    )
    op.drop_column("knowledge_base", "sparse_model_id")
    op.drop_column("knowledge_base", "sparse_kind")
    op.drop_column("model", "send_dimension")
    op.drop_column("model", "sparse")
