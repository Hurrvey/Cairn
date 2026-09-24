"""vectorstore: move vectors to Qdrant

Revision ID: 0013_qdrant
Revises: 0012_mcp_management
Create Date: 2026-09-24

Vectors now live in Qdrant (ADR-0009). This drops any per-namespace pgvector
tables left behind, drops the extension, and points existing vector bindings at
the Qdrant driver. Nothing is copied: indexes are rebuildable from chunks, and
no deployment carries vector data that must survive the switch.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_qdrant"
down_revision: str | None = "0012_mcp_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT tablename FROM pg_tables
             WHERE schemaname = current_schema() AND tablename LIKE 'cairn\_vec\_%'
          LOOP
            EXECUTE format('DROP TABLE IF EXISTS %I', r.tablename);
          END LOOP;
        END $$;
        """
    )
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute(
        "UPDATE storage_binding SET driver = 'qdrant', config = '{}'::jsonb "
        "WHERE kind = 'vector' AND driver = 'pgvector'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE storage_binding SET driver = 'pgvector' WHERE kind = 'vector' AND driver = 'qdrant'"
    )
