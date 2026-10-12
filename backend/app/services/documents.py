"""Document operations used by the API: upload, list, retry, reindex and delete.

Nothing here loads the embedding model; the API only validates, stores and queues work.
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApiError
from app.db.models import Chunk, Document, DocumentStatus, IngestionJob, JobKind
from app.ingestion import jobs
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.validation import file_type_from_extension, sanitize_filename, validate_file
from app.storage import DocumentStorage, new_storage_key

logger = logging.getLogger(__name__)

_COPY_CHUNK_BYTES = 1024 * 1024

# HTTP status for each upload validation failure.
_UPLOAD_ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNSUPPORTED_FILE_TYPE: 415,
    ErrorCode.CONTENT_TYPE_MISMATCH: 415,
    ErrorCode.FILE_TOO_LARGE: 413,
}


@dataclass(frozen=True)
class UploadOutcome:
    document: Document
    duplicate: bool


def _upload_error(error: IngestionError) -> ApiError:
    return ApiError(_UPLOAD_ERROR_STATUS.get(error.code, 422), error.code.value, error.message)


async def _spool_upload(upload: UploadFile, directory: Path, max_bytes: int) -> tuple[Path, str]:
    """Copy the upload to a temporary file, enforcing the size limit and hashing as it goes."""
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix=".upload-", delete=False)  # noqa: SIM115
    path = Path(handle.name)
    digest = hashlib.sha256()
    size = 0
    try:
        with handle:
            while chunk := await upload.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise IngestionError(
                        ErrorCode.FILE_TOO_LARGE,
                        f"The file is larger than the {max_bytes // (1024 * 1024)} MB limit.",
                    )
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, digest.hexdigest()


async def upload_document(
    session: AsyncSession,
    storage: DocumentStorage,
    upload: UploadFile,
    *,
    staging_dir: Path,
    max_bytes: int,
    max_attempts: int,
    request_id: str | None,
) -> UploadOutcome:
    filename = sanitize_filename(upload.filename)
    if not filename:
        raise ApiError(422, "invalid_filename", "The uploaded file has no usable filename.")
    try:
        file_type_from_extension(filename)
        spooled, sha256 = await _spool_upload(upload, staging_dir, max_bytes)
    except IngestionError as exc:
        raise _upload_error(exc) from exc

    storage_key: str | None = None
    try:
        try:
            validated = await asyncio.to_thread(validate_file, spooled, filename)
        except IngestionError as exc:
            raise _upload_error(exc) from exc

        existing = await session.scalar(select(Document).where(Document.sha256 == sha256))
        if existing is not None:
            return UploadOutcome(existing, duplicate=True)

        storage_key = new_storage_key()
        await asyncio.to_thread(storage.put_file, storage_key, spooled)
        document = Document(
            id=uuid.uuid4(),
            filename=filename,
            file_type=validated.file_type.value,
            mime_type=validated.mime_type,
            size_bytes=spooled.stat().st_size,
            sha256=sha256,
            storage_key=storage_key,
            page_count=validated.page_count,
            status=DocumentStatus.PENDING.value,
            chunk_count=0,
        )
        session.add(document)
        try:
            await session.flush()
            await jobs.enqueue_job(
                session,
                document,
                JobKind.INGEST,
                max_attempts=max_attempts,
                request_id=request_id,
            )
            await session.commit()
        except IntegrityError:
            # A concurrent upload of the same content won the race on uq_documents_sha256.
            await session.rollback()
            await asyncio.to_thread(storage.delete, storage_key)
            storage_key = None
            existing = await session.scalar(select(Document).where(Document.sha256 == sha256))
            if existing is None:
                raise
            return UploadOutcome(existing, duplicate=True)
        await session.refresh(document)
        logger.info(
            "document_uploaded",
            extra={
                "document_id": str(document.id),
                "file_type": document.file_type,
                "size_bytes": document.size_bytes,
            },
        )
        storage_key = None  # owned by the committed document now
        return UploadOutcome(document, duplicate=False)
    finally:
        spooled.unlink(missing_ok=True)
        if storage_key is not None:
            await asyncio.to_thread(storage.delete, storage_key)


async def list_documents(
    session: AsyncSession, *, status: DocumentStatus | None, limit: int, offset: int
) -> tuple[list[Document], int]:
    query = select(Document)
    count = select(func.count()).select_from(Document)
    if status is not None:
        query = query.where(Document.status == status.value)
        count = count.where(Document.status == status.value)
    total = await session.scalar(count) or 0
    rows = await session.scalars(
        query.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit).offset(offset)
    )
    return list(rows.all()), total


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await session.get(Document, document_id)
    if document is None:
        raise ApiError(404, "document_not_found", "Document not found.")
    return document


async def latest_job(session: AsyncSession, document_id: uuid.UUID) -> IngestionJob | None:
    return await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
        .limit(1)
    )


async def list_chunks(
    session: AsyncSession, document_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Chunk], int]:
    await get_document(session, document_id)
    total = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )
    # Load only preview columns; never the embedding vector.
    rows = await session.execute(
        select(
            Chunk.id,
            Chunk.ordinal,
            Chunk.text,
            Chunk.token_count,
            Chunk.language,
            Chunk.page_start,
            Chunk.page_end,
            Chunk.section,
        )
        .where(Chunk.document_id == document_id)
        .order_by(Chunk.ordinal)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.all()), total or 0  # type: ignore[arg-type]


async def requeue_document(
    session: AsyncSession,
    document_id: uuid.UUID,
    *,
    reindex: bool,
    max_attempts: int,
    request_id: str | None,
) -> Document:
    """Queue a retry (failed documents only) or a reindex (completed or failed documents)."""
    document = await session.get(Document, document_id, with_for_update=True)
    if document is None:
        raise ApiError(404, "document_not_found", "Document not found.")

    allowed = (
        {DocumentStatus.COMPLETED.value, DocumentStatus.FAILED.value}
        if reindex
        else {DocumentStatus.FAILED.value}
    )
    if document.status not in allowed:
        action = "reindexed" if reindex else "retried"
        reason = (
            "it is already queued or being processed"
            if document.status in (DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value)
            else "only failed documents can be retried; use reindex instead"
        )
        await session.rollback()
        raise ApiError(409, "invalid_document_state", f"Document cannot be {action}: {reason}.")

    kind = JobKind.REINDEX if reindex and document.chunk_count > 0 else JobKind.INGEST
    try:
        await jobs.enqueue_job(
            session, document, kind, max_attempts=max_attempts, request_id=request_id
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            409, "invalid_document_state", "Document already has a queued or running job."
        ) from exc
    await session.refresh(document)
    logger.info(
        "document_requeued", extra={"document_id": str(document.id), "job_kind": kind.value}
    )
    return document


async def delete_document(
    sessions: async_sessionmaker[AsyncSession], storage: DocumentStorage, document_id: uuid.UUID
) -> None:
    """Delete the document row (chunks and jobs cascade), then its stored file.

    The database delete commits first, so the document disappears atomically. If removing the
    file then fails, the file is an orphan that the worker's startup sweep removes later. A
    worker still processing the document loses its lease and discards its work.
    """
    async with sessions() as session:
        document = await session.get(Document, document_id, with_for_update=True)
        if document is None:
            raise ApiError(404, "document_not_found", "Document not found.")
        storage_key = document.storage_key
        await session.delete(document)
        await session.commit()
    try:
        await asyncio.to_thread(storage.delete, storage_key)
    except OSError:
        logger.exception(
            "stored_file_delete_failed",
            extra={"document_id": str(document_id), "storage_key": storage_key},
        )
    logger.info("document_deleted", extra={"document_id": str(document_id)})


def staging_directory(storage_dir: Path) -> Path:
    """Uploads are spooled next to the store so moving them in is cheap."""
    return storage_dir / ".staging"


def clean_staging_directory(directory: Path, min_age_seconds: float = 3600) -> None:
    """Remove spool files left by a crashed API process (old ones only, so uploads in
    progress in another API process are not affected)."""
    if not directory.is_dir():
        return
    cutoff = time.time() - min_age_seconds
    for entry in directory.iterdir():
        if entry.name.startswith(".upload-") and entry.stat().st_mtime < cutoff:
            try:
                os.remove(entry)
            except OSError:
                logger.warning("staging_cleanup_failed", extra={"file": entry.name})
