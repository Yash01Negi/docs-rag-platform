"""SQLAlchemy ORM models for documents, chunks, ingestion jobs and conversations.

The schema itself is owned by Alembic (``alembic/versions/``); these models mirror it for
queries. ``tests/test_schema.py`` checks that the two stay in sync.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    """State of a message. User messages are always ``complete``.

    An assistant message is ``streaming`` while its answer is generated, then ``complete``,
    ``failed`` (provider or server error) or ``cancelled`` (the client disconnected or
    stopped it). Failed and cancelled answers keep the text generated before the stop.
    """

    COMPLETE = "complete"
    STREAMING = "streaming"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Grounding(StrEnum):
    """How an assistant answer relates to the documents."""

    GROUNDED = "grounded"  # at least one valid citation to a retrieved passage
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # the model said the documents do not say
    NO_SOURCES = "no_sources"  # retrieval found nothing; the model was not called
    UNCITED = "uncited"  # an answer without any valid citation


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


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # True until someone renames the conversation; the first question then becomes the title.
    title_is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="ck_conversations_title_not_empty"),
        Index("ix_conversations_updated_at_id", "updated_at", "id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    # 1, 2, 3, ... within the conversation: the display and prompt order.
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(String(16))
    # Idempotency key chosen by the client for a question; a retry with the same key never
    # creates a second question.
    client_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The question an assistant message answers.
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE")
    )
    grounding: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Generation metadata (assistant messages). ``model`` is the configured model and
    # ``served_model`` the one that answered (they differ after a provider fallback).
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    served_model: Mapped[str | None] = mapped_column(String(128))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer)
    retrieval_mode: Mapped[str | None] = mapped_column(String(16))
    retrieval_ms: Mapped[float | None] = mapped_column(Float)
    first_token_ms: Mapped[float | None] = mapped_column(Float)
    total_ms: Mapped[float | None] = mapped_column(Float)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_sequence"),
        CheckConstraint("sequence >= 1", name="ck_messages_sequence"),
        CheckConstraint(_in("role", MessageRole), name="ck_messages_role"),
        CheckConstraint(_in("status", MessageStatus), name="ck_messages_status"),
        CheckConstraint(
            f"grounding IS NULL OR {_in('grounding', Grounding)}", name="ck_messages_grounding"
        ),
        CheckConstraint(
            "role <> 'user' OR (status = 'complete' AND reply_to_id IS NULL "
            "AND length(content) > 0)",
            name="ck_messages_user_shape",
        ),
        CheckConstraint(
            "role <> 'assistant' OR (reply_to_id IS NOT NULL AND client_message_id IS NULL)",
            name="ck_messages_assistant_shape",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0)",
            name="ck_messages_tokens",
        ),
        Index(
            "uq_messages_client_message_id",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        # One answer per question, and one answer being generated per conversation.
        Index(
            "uq_messages_reply_to",
            "reply_to_id",
            unique=True,
            postgresql_where=text("reply_to_id IS NOT NULL"),
        ),
        Index(
            "uq_messages_one_streaming_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'streaming'"),
        ),
    )


class MessageCitation(Base):
    """A retrieved passage given to the model for an answer.

    ``supporting`` is true when the answer cites the passage, false when it was only
    provided as context. The chunk and document IDs are kept without foreign keys so the
    record survives a reindex (which replaces chunk IDs) or a document deletion; the other
    columns are a snapshot of the passage's location at answer time.
    """

    __tablename__ = "message_citations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE")
    )
    # The N in the [S<N>] marker used in the answer text.
    source_index: Mapped[int] = mapped_column(Integer)
    supporting: Mapped[bool] = mapped_column(Boolean)
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    filename: Mapped[str] = mapped_column(String(255))
    chunk_ordinal: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))
    retrieval_rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    score_type: Mapped[str] = mapped_column(String(32))
    # SHA-256 of the chunk text, to find the same passage again after a reindex.
    chunk_text_sha256: Mapped[str] = mapped_column(String(64))
    excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("message_id", "source_index", name="uq_message_citations_source"),
        CheckConstraint("source_index >= 1", name="ck_message_citations_source_index"),
        CheckConstraint("chunk_ordinal >= 0", name="ck_message_citations_ordinal"),
        CheckConstraint(
            "chunk_text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_message_citations_sha256_hex"
        ),
        Index("ix_message_citations_chunk_id", "chunk_id"),
        Index("ix_message_citations_document_id", "document_id"),
    )
