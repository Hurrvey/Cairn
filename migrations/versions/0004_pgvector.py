"""vectorstore: enable the pgvector extension

Revision ID: 0004_pgvector
Revises: 0003_authz
Create Date: 2026-09-01

Only the extension. Vector tables are created per namespace at runtime by
``PgVectorStore.ensure_namespace`` (ADR-0007) — a namespace is
``(kb_id, index_version)``, so its lifetime is tied to a knowledge base's index
build rather than to a schema version, and Alembic is the wrong place to manage
it.

``CREATE EXTENSION`` needs superuser on first run. The pgvector Docker image
grants it; a managed service usually requires enabling the extension from its
console first, in which case ``IF NOT EXISTS`` makes this a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_pgvector"
down_revision: str | None = "0003_authz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Deliberately not dropped: other databases in the cluster may share it, and
    # dropping it would cascade away every vector column in the system.
    pass
