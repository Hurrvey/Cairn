"""catalog: immutable index-version configuration snapshots

Revision ID: 0008_version_snapshots
Revises: 0007_ingestion_pipeline
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_version_snapshots"
down_revision: str | None = "0007_ingestion_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_GUARD = """
CREATE FUNCTION guard_kb_index_version_snapshot_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.config_snapshot IS DISTINCT FROM OLD.config_snapshot
       OR NEW.snapshot_unavailable IS DISTINCT FROM OLD.snapshot_unavailable THEN
        RAISE EXCEPTION
            USING ERRCODE = '23514',
                  MESSAGE = 'index version snapshot is immutable',
                  DETAIL = format(
                      'kb_index_version (%s, %s) already owns its configuration provenance.',
                      OLD.kb_id, OLD.version);
    END IF;
    RETURN NEW;
END;
$$;
"""

_HIGH_WATER_GUARD = """
CREATE FUNCTION guard_kb_index_version_high_water() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.index_version_high_water < OLD.index_version_high_water THEN
        RAISE EXCEPTION
            USING ERRCODE = '23514',
                  MESSAGE = 'index version high-water mark cannot decrease',
                  DETAIL = format(
                      'knowledge_base %s attempted to lower the mark from %s to %s.',
                      OLD.id, OLD.index_version_high_water, NEW.index_version_high_water);
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.add_column(
        "knowledge_base",
        sa.Column(
            "index_version_high_water", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_knowledge_base_index_version_high_water_nonnegative",
        "knowledge_base",
        "index_version_high_water >= 0",
    )
    op.add_column(
        "kb_index_version",
        sa.Column("config_snapshot", postgresql.JSONB(), nullable=True),
    )
    # Existing rows are deliberately unavailable. Once the default changes
    # below, every newly inserted row must carry a snapshot to satisfy the
    # coherence check.
    op.add_column(
        "kb_index_version",
        sa.Column(
            "snapshot_unavailable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_check_constraint(
        "ck_kb_index_version_snapshot_coherent",
        "kb_index_version",
        "(snapshot_unavailable AND config_snapshot IS NULL) OR "
        "(NOT snapshot_unavailable AND config_snapshot IS NOT NULL)",
    )

    # JSON evidence is accepted only as a bounded JSON number whose text is a
    # positive integer. Nested CASE expressions establish each conversion
    # precondition before the next cast, avoiding blind casts of payloads.
    op.execute(
        """
        WITH task_evidence AS (
            SELECT kb_id,
                   max(
                       CASE
                           WHEN jsonb_typeof(payload->'index_version') = 'number'
                            AND payload->>'index_version' ~ '^[1-9][0-9]{0,9}$'
                           THEN CASE
                               WHEN (payload->>'index_version')::bigint <= 2147483647
                               THEN (payload->>'index_version')::integer
                           END
                       END
                   ) AS version
              FROM task
             WHERE kb_id IS NOT NULL
             GROUP BY kb_id
        ),
        audit_evidence AS (
            SELECT resource_id AS kb_id,
                   max(
                       CASE
                           WHEN jsonb_typeof(detail->'index_version') = 'number'
                            AND detail->>'index_version' ~ '^[1-9][0-9]{0,9}$'
                           THEN CASE
                               WHEN (detail->>'index_version')::bigint <= 2147483647
                               THEN (detail->>'index_version')::integer
                           END
                       END
                   ) AS version
              FROM audit_log
             WHERE resource_type = 'knowledge_base'
               AND resource_id IS NOT NULL
             GROUP BY resource_id
        ),
        version_evidence AS (
            SELECT kb_id, max(version) AS version
              FROM kb_index_version
             GROUP BY kb_id
        ),
        chunk_evidence AS (
            SELECT kb_id, max(index_version) AS version
              FROM chunk
             GROUP BY kb_id
        )
        UPDATE knowledge_base kb
           SET index_version_high_water = greatest(
               CASE
                   WHEN kb.active_index_version IS NULL THEN 1
                   WHEN kb.active_index_version < 2147483647
                       THEN kb.active_index_version + 1
                   ELSE kb.active_index_version
               END,
               coalesce(kb.building_index_version, 0),
               coalesce(versions.version, 0),
               coalesce(chunks.version, 0),
               coalesce(tasks.version, 0),
               coalesce(audits.version, 0)
           )
          FROM version_evidence versions
          FULL JOIN chunk_evidence chunks USING (kb_id)
          FULL JOIN task_evidence tasks USING (kb_id)
          FULL JOIN audit_evidence audits USING (kb_id)
         WHERE kb.id = coalesce(versions.kb_id, chunks.kb_id, tasks.kb_id, audits.kb_id)
        """
    )
    # KBs with no retained evidence still reserve initial version 1.
    op.execute(
        """
        UPDATE knowledge_base
           SET index_version_high_water = greatest(
               CASE
                   WHEN active_index_version IS NULL THEN 1
                   WHEN active_index_version < 2147483647 THEN active_index_version + 1
                   ELSE active_index_version
               END,
               coalesce(building_index_version, 0)
           )
         WHERE index_version_high_water = 0
        """
    )
    op.alter_column(
        "kb_index_version", "snapshot_unavailable", server_default=sa.text("false")
    )
    op.execute(_SNAPSHOT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_kb_index_version_snapshot_immutable "
        "BEFORE UPDATE OF config_snapshot, snapshot_unavailable ON kb_index_version "
        "FOR EACH ROW EXECUTE FUNCTION guard_kb_index_version_snapshot_immutable()"
    )
    op.execute(_HIGH_WATER_GUARD)
    op.execute(
        "CREATE TRIGGER trg_kb_index_version_high_water "
        "BEFORE UPDATE OF index_version_high_water ON knowledge_base "
        "FOR EACH ROW EXECUTE FUNCTION guard_kb_index_version_high_water()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kb_index_version_high_water ON knowledge_base")
    op.execute("DROP FUNCTION IF EXISTS guard_kb_index_version_high_water()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kb_index_version_snapshot_immutable ON kb_index_version"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_kb_index_version_snapshot_immutable()")
    op.drop_constraint(
        "ck_kb_index_version_snapshot_coherent", "kb_index_version", type_="check"
    )
    op.drop_column("kb_index_version", "snapshot_unavailable")
    op.drop_column("kb_index_version", "config_snapshot")
    op.execute(
        "ALTER TABLE knowledge_base DROP CONSTRAINT IF EXISTS "
        "ck_knowledge_base_index_version_high_water_nonnegative"
    )
    op.drop_column("knowledge_base", "index_version_high_water")
