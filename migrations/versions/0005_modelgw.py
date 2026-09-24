"""modelgw: secrets, providers, and the model registry

Revision ID: 0005_modelgw
Revises: 0004_pgvector
Create Date: 2026-09-03

The registry half of M10, landing ahead of invocation because a knowledge base
must validate its embedding model at creation time (ADR-0006) rather than
discovering a mismatch mid-ingest.

``secret`` stores envelope-encrypted credentials (NFR-SEC-02): a per-secret DEK
wrapped by a KEK, with the ciphertext bound to its workspace and purpose through
the AAD. The plaintext never appears in a column, so a database dump — or a
replica, or a backup — is not a credential leak.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_modelgw"
down_revision: str | None = "0004_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        # Which KEK wrapped the DEK. A rotation re-wraps DEKs without having to
        # decrypt and re-encrypt every ciphertext.
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_secret"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_secret_workspace_id_workspace",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "model_provider",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("secret_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_provider"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_model_provider_name"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_model_provider_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        # SET NULL, not CASCADE: losing a credential must disable the provider,
        # not silently delete the models configured against it.
        sa.ForeignKeyConstraint(
            ["secret_ref"],
            ["secret.id"],
            name="fk_model_provider_secret_ref_secret",
            ondelete="SET NULL",
        ),
    )

    op.create_table(
        "model",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_key", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("capability", sa.String(16), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=True),
        sa.Column("max_input_tokens", sa.Integer(), nullable=True),
        sa.Column("normalize", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("query_prefix", sa.Text(), nullable=True),
        sa.Column("optimal_batch_size", sa.Integer(), nullable=False, server_default="64"),
        sa.Column("tokenizer_id", sa.String(255), nullable=True),
        sa.Column("cost_per_1k_input", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_per_1k_output", sa.Numeric(12, 6), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("health_state", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model"),
        sa.UniqueConstraint("provider_id", "model_key", name="uq_model_provider_key"),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["model_provider.id"],
            name="fk_model_provider_id_model_provider",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "capability IN ('chat','embedding','rerank')", name="ck_model_capability"
        ),
        # ADR-0006, and the earliest of the three enforcement layers: an
        # embedding model with no declared dimension can never become a
        # knowledge base's immutable vector width.
        sa.CheckConstraint(
            "capability <> 'embedding' OR dimension IS NOT NULL", name="ck_model_embedding_dim"
        ),
    )
    op.create_index("ix_model_workspace_capability", "model", ["workspace_id", "capability"])


def downgrade() -> None:
    op.drop_table("model")
    op.drop_table("model_provider")
    op.drop_table("secret")
