"""Add documents, chunks and ingestion_jobs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12

The constraints encode the ingestion state machine (see docs/ingestion-pipeline.md):

* a ``completed`` document always has at least one chunk and a recorded embedding model;
* a ``failed`` document never has chunks and always has an error code;
* identical file contents (same SHA-256) are stored once;
* at most one pending or processing job exists per document.

No approximate-nearest-neighbour index is created on ``chunks.embedding`` yet. The index
type and its parameters will be chosen with retrieval measurements (ADR 0002).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(64), nullable=False),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("embedding_model", sa.String(255), nullable=True),
        sa.Column("embedding_model_revision", sa.String(64), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", _TIMESTAMP, nullable=True),
        sa.UniqueConstraint("sha256", name="uq_documents_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_documents_status",
        ),
        sa.CheckConstraint(
            "file_type IN ('pdf', 'markdown', 'text')", name="ck_documents_file_type"
        ),
        sa.CheckConstraint(
            "language IS NULL OR language IN ('ja', 'en', 'mixed', 'unknown')",
            name="ck_documents_language",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256_hex"),
        sa.CheckConstraint("chunk_count >= 0", name="ck_documents_chunk_count"),
        sa.CheckConstraint(
            "status <> 'completed' OR (chunk_count > 0 AND processed_at IS NOT NULL "
            "AND embedding_model IS NOT NULL)",
            name="ck_documents_completed_has_index",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR (chunk_count = 0 AND error_code IS NOT NULL)",
            name="ck_documents_failed_has_no_index",
        ),
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_created_at_id", "documents", ["created_at", "id"])

    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        sa.CheckConstraint("token_count > 0", name="ck_chunks_token_count"),
        sa.CheckConstraint("length(text) > 0", name="ck_chunks_text_not_empty"),
        sa.CheckConstraint(
            "language IN ('ja', 'en', 'mixed', 'unknown')", name="ck_chunks_language"
        ),
        sa.CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start >= 1 AND page_end >= page_start)",
            name="ck_chunks_page_range",
        ),
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", _TIMESTAMP, nullable=True),
        sa.Column("heartbeat_at", _TIMESTAMP, nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", _TIMESTAMP, nullable=True),
        sa.Column("completed_at", _TIMESTAMP, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_ingestion_jobs_status",
        ),
        sa.CheckConstraint("kind IN ('ingest', 'reindex')", name="ck_ingestion_jobs_kind"),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name="ck_ingestion_jobs_attempts"
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_ingestion_jobs_max_attempts"),
        sa.CheckConstraint(
            "status <> 'processing' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND locked_by IS NOT NULL)",
            name="ck_ingestion_jobs_processing_has_lease",
        ),
    )
    op.create_index(
        "uq_ingestion_jobs_one_active_per_document",
        "ingestion_jobs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
    op.create_index(
        "ix_ingestion_jobs_claimable",
        "ingestion_jobs",
        ["run_after", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_ingestion_jobs_expired_leases",
        "ingestion_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_ingestion_jobs_document_created", "ingestion_jobs", ["document_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_table("chunks")
    op.drop_table("documents")
