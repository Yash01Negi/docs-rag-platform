"""SQLAlchemy ORM models for documents, chunks and ingestion jobs.

The schema itself is owned by Alembic (``alembic/versions/``); these models mirror it for
queries. ``tests/test_schema.py`` checks that the two stay in sync.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import EMBEDDING_DIMENSION


class Base(DeclarativeBase):
    pass


class DocumentStatus(StrEnum):
    """Lifecycle of a document's most recent ingestion.

    Invariants (enforced by CHECK constraints):

    * ``completed`` means a complete index exists: ``chunk_count > 0``.
    * ``failed`` means no index exists: ``chunk_count = 0`` and an error code is set.
    * ``pending``/``processing`` may still serve a previous index while a reindex runs.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobKind(StrEnum):
    INGEST = "ingest"
    REINDEX = "reindex"


class FileType(StrEnum):
    PDF = "pdf"
    MARKDOWN = "markdown"
    TEXT = "text"


class Language(StrEnum):
    JAPANESE = "ja"
    ENGLISH = "en"
    MIXED = "mixed"
    UNKNOWN = "unknown"


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))
    mime_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(16))
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), server_default=DocumentStatus.PENDING.value)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding_model_revision: Mapped[str | None] = mapped_column(String(64))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("sha256", name="uq_documents_sha256"),
        UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        CheckConstraint(_in("status", DocumentStatus), name="ck_documents_status"),
        CheckConstraint(_in("file_type", FileType), name="ck_documents_file_type"),
        CheckConstraint(
            f"language IS NULL OR {_in('language', Language)}", name="ck_documents_language"
        ),
        CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256_hex"),
        CheckConstraint("chunk_count >= 0", name="ck_documents_chunk_count"),
        CheckConstraint(
            "status <> 'completed' OR (chunk_count > 0 AND processed_at IS NOT NULL "
            "AND embedding_model IS NOT NULL AND embedding_model_revision IS NOT NULL)",
            name="ck_documents_completed_has_index",
        ),
        CheckConstraint(
            "status <> 'failed' OR (chunk_count = 0 AND error_code IS NOT NULL)",
            name="ck_documents_failed_has_no_index",
        ),
        Index("ix_documents_status", "status"),
        Index("ix_documents_created_at_id", "created_at", "id"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(String(16))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))
    # Keyword-search terms from app.search.analyzer, with positions, and their count (the
    # document length in BM25). Written in the same transaction as the chunk itself.
    search_vector: Mapped[str] = mapped_column(TSVECTOR)
    search_length: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_chunks_ordinal"),
        CheckConstraint("token_count > 0", name="ck_chunks_token_count"),
        CheckConstraint("length(text) > 0", name="ck_chunks_text_not_empty"),
        CheckConstraint(_in("language", Language), name="ck_chunks_language"),
        CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start >= 1 AND page_end >= page_start)",
            name="ck_chunks_page_range",
        ),
        CheckConstraint("search_length >= 0", name="ck_chunks_search_length"),
        Index("ix_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default=JobStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Lease: the worker holding the job must renew lease_expires_at before it passes. The
    # lease_token changes on every claim and fences out a worker whose lease was taken over.
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", JobStatus), name="ck_ingestion_jobs_status"),
        CheckConstraint(_in("kind", JobKind), name="ck_ingestion_jobs_kind"),
        CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name="ck_ingestion_jobs_attempts"
        ),
        CheckConstraint("max_attempts >= 1", name="ck_ingestion_jobs_max_attempts"),
        CheckConstraint(
            "status <> 'processing' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND locked_by IS NOT NULL)",
            name="ck_ingestion_jobs_processing_has_lease",
        ),
        # At most one queued or running job per document: two concurrent reindex requests
        # cannot both be accepted.
        Index(
            "uq_ingestion_jobs_one_active_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        Index(
            "ix_ingestion_jobs_claimable",
            "run_after",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_ingestion_jobs_expired_leases",
            "lease_expires_at",
            postgresql_where=text("status = 'processing'"),
        ),
        Index("ix_ingestion_jobs_document_created", "document_id", "created_at"),
    )
