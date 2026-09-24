"""vectorstore: enable the pgvector extension (superseded)

Revision ID: 0004_pgvector
Revises: 0003_authz
Create Date: 2026-09-01

Originally installed the pgvector extension. Vectors moved to Qdrant
(ADR-0009, ``0013_qdrant``), so this revision is now a no-op: it stays in the
chain so existing databases keep a linear history, and so a plain PostgreSQL
image — which has no ``vector`` extension to install — can run every migration.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0004_pgvector"
down_revision: str | None = "0003_authz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
