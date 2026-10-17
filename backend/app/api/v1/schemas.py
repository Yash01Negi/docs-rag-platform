"""Response models for the document API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import DocumentSource, DocumentStatus, FileType, JobKind, JobStatus, Language


class _FromAttributes(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentSummary(_FromAttributes):
    id: uuid.UUID
    filename: str
    file_type: FileType
    mime_type: str
    size_bytes: int
    language: Language | None
    status: DocumentStatus
    source_type: DocumentSource = Field(
        description="'upload' for manual uploads, 's3' for documents imported by a connector."
    )
    chunk_count: int = Field(description="Chunks in the document's current (active) index.")
    created_at: datetime = Field(description="Upload time.")
    updated_at: datetime
    error_code: str | None
    error_message: str | None = Field(
        description=(
            "Set when the last ingestion failed. A completed document with an error means a "
            "reindex failed and the previous index is still in use."
        )
    )


class JobSummary(_FromAttributes):
    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    attempts: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    run_after: datetime = Field(description="Earliest time a pending job will be picked up.")
    last_error_code: str | None
    last_error_message: str | None


class DocumentSourceInfo(BaseModel):
    """Where an imported document came from (connector documents only)."""

    connector_id: uuid.UUID
    connector_name: str
    provider: str
    bucket: str
    object_key: str
    uri: str = Field(description="s3://bucket/key")
    etag: str | None = Field(description="Opaque version token of the imported object.")
    version_id: str | None = Field(description="S3 version ID, when the bucket is versioned.")
    size_bytes: int | None
    last_modified: datetime | None
    last_synced_at: datetime | None


class DocumentDetail(DocumentSummary):
    sha256: str
    page_count: int | None
    embedding_model: str | None
    embedding_model_revision: str | None
    embedding_dimension: int | None
    processed_at: datetime | None = Field(description="When the active index was committed.")
    latest_job: JobSummary | None
    source: DocumentSourceInfo | None = Field(
        default=None, description="The S3 object behind an imported document; null for uploads."
    )


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    total: int
    limit: int
    offset: int


class UploadResult(BaseModel):
    duplicate: bool = Field(
        description=(
            "True if a document with identical content already existed. In that case no new "
            "document is created and `document` is the existing one (HTTP 200)."
        )
    )
    document: DocumentSummary


class ChunkPreview(_FromAttributes):
    id: uuid.UUID
    ordinal: int
    text: str
    token_count: int
    language: Language
    page_start: int | None
    page_end: int | None
    section: str | None


class ChunkList(BaseModel):
    items: list[ChunkPreview]
    total: int
    limit: int
    offset: int


class UploadLimits(BaseModel):
    max_upload_bytes: int
    accepted_extensions: list[str]
    accepted_mime_types: list[str]


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None


class ErrorResponse(BaseModel):
    error: ErrorBody
