"""catalog: storage bindings, knowledge bases, index versions, documents, chunks

Revision ID: 0006_catalog
Revises: 0005_modelgw
Create Date: 2026-09-03

FR-C-*, FR-D-*, FR-F-*. Three structural decisions are made here and are
expensive to revisit later:

1. ``chunk`` is HASH-partitioned by ``kb_id`` from its first migration. It is
   the largest table in the system by two orders of magnitude, and retrofitting
   partitioning onto a live one means rewriting every row.

2. ``kb_index_version`` carries a partial UNIQUE index on ``state='building'``,
   so a knowledge base can have at most one build in flight. Two concurrent
   rebuilds would race to flip ``active_index_version`` and the loser's
   namespace would leak (ADR-0007).

3. ``guard_kb_embedding_immutable`` is the third and last enforcement layer for
   ADR-0006. The API rejects an embedding change first (better message) and the
   service rejects it second (better message still); this trigger is what makes
   the invariant true for *every* writer, including a psql session, a data fix
   script, or a future module that has not read the ADR.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_catalog"
down_revision: str | None = "0005_modelgw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOCUMENT_STATES = (
    "('registered','fetching','fetched','parsing','parsed','chunking','chunked',"
    "'embedding','embedded','indexing','indexed','failed','skipped','deleting')"
)
KB_STATUSES = "('active','indexing','error','deleting','archived')"
INDEX_STATES = "('building','active','retired','failed')"

#: Powers of two only — changing this later requires a full table rewrite, so it
#: is sized for the ceiling rather than for today. 16 keeps each partition's
#: indexes comfortably in cache at ~100M chunks while keeping planning time for
#: a single-KB query (which prunes to exactly one partition) negligible.
CHUNK_PARTITIONS = 16

_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION guard_kb_embedding_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Nothing has been embedded yet, so there is no space to be inconsistent with.
    IF OLD.active_index_version IS NULL THEN
        RETURN NEW;
    END IF;

    IF NEW.embedding_model_id IS NOT DISTINCT FROM OLD.embedding_model_id
       AND NEW.embedding_dim IS NOT DISTINCT FROM OLD.embedding_dim
       AND NEW.metric IS NOT DISTINCT FROM OLD.metric THEN
        RETURN NEW;
    END IF;

    -- A change IS permitted, but only as part of opening a new index build:
    -- that is the reindex path, and it writes into a fresh namespace rather
    -- than mixing embedding spaces inside the live one.
    IF NEW.building_index_version IS NOT NULL
       AND NEW.building_index_version IS DISTINCT FROM OLD.building_index_version THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        USING ERRCODE = '23514',
              MESSAGE = 'embedding configuration is immutable for an indexed knowledge base',
              DETAIL  = format(
                  'knowledge_base %s is serving index version %s; changing the embedding '
                  'model, dimension, or metric in place would mix embedding spaces.',
                  OLD.id, OLD.active_index_version),
              HINT    = 'Start a rebuild instead, which opens a new index version.';
END;
$$;
"""

_GUARD_TRIGGER = """
CREATE TRIGGER trg_kb_embedding_immutable
    BEFORE UPDATE OF embedding_model_id, embedding_dim, metric ON knowledge_base
    FOR EACH ROW EXECUTE FUNCTION guard_kb_embedding_immutable()
"""


def upgrade() -> None:
    op.create_table(
        "storage_binding",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("driver", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("secret_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("health_state", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_storage_binding"),
        sa.UniqueConstraint("workspace_id", "kind", "name", name="uq_storage_binding_name"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_storage_binding_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["secret_ref"],
            ["secret.id"],
            name="fk_storage_binding_secret_ref_secret",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("kind IN ('vector','object')", name="ck_storage_binding_kind"),
    )

    op.create_table(
        "knowledge_base",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_object_key", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # --- immutable once indexed (ADR-0006) ---
        sa.Column("embedding_model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(16), nullable=False, server_default="cosine"),
        # -----------------------------------------
        sa.Column("vector_binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "chunk_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "retrieval_config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ingest_pipeline_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("retrieval_pipeline_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active_index_version", sa.Integer(), nullable=True),
        sa.Column("building_index_version", sa.Integer(), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("doc_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_base"),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_knowledge_base_slug"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_knowledge_base_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        # No ondelete: a model that a knowledge base was built against cannot be
        # deleted, because its vectors are unreproducible without it.
        sa.ForeignKeyConstraint(
            ["embedding_model_id"],
            ["model.id"],
            name="fk_knowledge_base_embedding_model_id_model",
        ),
        # Likewise for bindings — this is the database half of `BindingInUse`.
        sa.ForeignKeyConstraint(
            ["vector_binding_id"],
            ["storage_binding.id"],
            name="fk_knowledge_base_vector_binding_id_storage_binding",
        ),
        sa.ForeignKeyConstraint(
            ["object_binding_id"],
            ["storage_binding.id"],
            name="fk_knowledge_base_object_binding_id_storage_binding",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], name="fk_knowledge_base_owner_user_id_user"
        ),
        sa.CheckConstraint(f"status IN {KB_STATUSES}", name="ck_knowledge_base_status"),
        sa.CheckConstraint("metric IN ('cosine','dot','l2')", name="ck_knowledge_base_metric"),
        sa.CheckConstraint(
            "embedding_dim > 0", name="ck_knowledge_base_embedding_dim_positive"
        ),
    )

    op.create_table(
        "kb_index_version",
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="building"),
        sa.Column("layout", sa.String(16), nullable=False, server_default="shared"),
        sa.Column("physical_ref", sa.Text(), nullable=True),
        sa.Column("chunk_total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("chunk_done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retire_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("kb_id", "version", name="pk_kb_index_version"),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_base.id"],
            name="fk_kb_index_version_kb_id_knowledge_base",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"state IN {INDEX_STATES}", name="ck_kb_index_version_state"),
        sa.CheckConstraint("layout IN ('shared','dedicated')", name="ck_kb_index_version_layout"),
    )
    # At most one build in flight per knowledge base (ADR-0007).
    op.execute(
        "CREATE UNIQUE INDEX uq_kb_index_building ON kb_index_version (kb_id) "
        "WHERE state = 'building'"
    )

    op.create_table(
        "document",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("crawl_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("parsed_object_key", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("state", sa.String(16), nullable=False, server_default="registered"),
        sa.Column("stage_detail", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("progress_pct", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_document"),
        # Dedup within a knowledge base (FR-D-07). Re-uploading identical bytes
        # returns the existing document rather than creating a duplicate.
        sa.UniqueConstraint("kb_id", "content_hash", name="uq_document_kb_content_hash"),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_base.id"],
            name="fk_document_kb_id_knowledge_base",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"state IN {DOCUMENT_STATES}", name="ck_document_state"),
        sa.CheckConstraint(
            "source_type IN ('upload','crawl','s3_sync','api','connector')",
            name="ck_document_source_type",
        ),
    )
    # Partial: the document list is always scoped to live documents, and the
    # soft-deleted tail would otherwise grow without bound inside the index.
    op.execute(
        "CREATE INDEX ix_document_kb_state ON document (kb_id, state) WHERE deleted_at IS NULL"
    )
    op.create_index("ix_document_source", "document", ["kb_id", "source_type", "source_ref"])

    # --- chunk: the big one -------------------------------------------------
    #
    # No foreign key to `knowledge_base` or `document`. On a partitioned table
    # of this size the per-row trigger cost on delete is what makes purging a
    # knowledge base take hours; cascade is done explicitly by the purge task,
    # which can batch it.
    op.create_table(
        "chunk",
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("is_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # The partition key must be part of the primary key, which is why the
        # column order is (kb_id, id) rather than the usual id-first.
        sa.PrimaryKeyConstraint("kb_id", "id", name="pk_chunk"),
        postgresql_partition_by="HASH (kb_id)",
    )
    for remainder in range(CHUNK_PARTITIONS):
        op.execute(
            f"CREATE TABLE chunk_p{remainder:02d} PARTITION OF chunk "
            f"FOR VALUES WITH (MODULUS {CHUNK_PARTITIONS}, REMAINDER {remainder})"
        )
    # Created after the partitions so PostgreSQL propagates them downward in one
    # step rather than the partitions inheriting them piecemeal.
    op.create_index("ix_chunk_document", "chunk", ["kb_id", "document_id", "index_version"])
    op.create_index("ix_chunk_hash", "chunk", ["kb_id", "content_hash"])

    # --- ADR-0006, layer 3 of 3 ---------------------------------------------
    op.execute(_GUARD_FUNCTION)
    op.execute(_GUARD_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kb_embedding_immutable ON knowledge_base")
    op.execute("DROP FUNCTION IF EXISTS guard_kb_embedding_immutable()")
    # Dropping the parent drops every partition with it.
    op.drop_table("chunk")
    op.drop_table("document")
    op.drop_table("kb_index_version")
    op.drop_table("knowledge_base")
    op.drop_table("storage_binding")
