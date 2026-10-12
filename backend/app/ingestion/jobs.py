"""Database-backed ingestion job queue and document state machine.

Jobs live in ``ingestion_jobs``. A worker claims one with ``SELECT ... FOR UPDATE SKIP
LOCKED`` (so concurrent workers never claim the same job), receives a fresh ``lease_token``
and must renew ``lease_expires_at`` while it works. Every write a worker makes afterwards
is conditional on still holding that token, so a worker whose lease expired and was
recovered by another worker cannot complete or fail the job a second time.

State transitions (anything else raises :class:`InvalidTransitionError`)::

    job:       pending -> processing -> completed
                          processing -> failed
                          processing -> pending      (retry scheduled, or released on shutdown)

    document:  pending -> processing -> completed
                          processing -> failed       (no previous index)
                          processing -> completed    (reindex failed; previous index kept)
                          processing -> pending      (retry scheduled, or released)
               completed -> pending                  (reindex requested)
               failed    -> pending                  (retry or reindex requested)

The final write of a successful job replaces the document's chunks, updates the document
and completes the job in one transaction, so readers see either the old index or the new
one, never a mixture, and a document can only become ``completed`` together with its chunks.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, DocumentStatus, IngestionJob, JobKind, JobStatus
from app.embeddings.base import EmbeddingModelInfo
from app.ingestion.chunking import TextChunk
from app.ingestion.errors import ErrorCode, IngestionError

logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE = 1000

DOCUMENT_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING: frozenset({DocumentStatus.PROCESSING}),
    DocumentStatus.PROCESSING: frozenset(
        {DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.PENDING}
    ),
    DocumentStatus.COMPLETED: frozenset({DocumentStatus.PENDING}),
    DocumentStatus.FAILED: frozenset({DocumentStatus.PENDING}),
}

JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.PROCESSING}),
    JobStatus.PROCESSING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PENDING}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class InvalidTransitionError(Exception):
    pass


class LeaseLostError(Exception):
    """The job is no longer held by this worker (lease expired, or the document was deleted)."""


def check_document_transition(current: str, target: DocumentStatus) -> None:
    if target not in DOCUMENT_TRANSITIONS[DocumentStatus(current)]:
        raise InvalidTransitionError(f"document cannot move from {current} to {target.value}")


def check_job_transition(current: str, target: JobStatus) -> None:
    if target not in JOB_TRANSITIONS[JobStatus(current)]:
        raise InvalidTransitionError(f"job cannot move from {current} to {target.value}")


def retry_delay(base_seconds: float, attempt: int) -> timedelta:
    """Exponential backoff: base, 2 x base, 4 x base, ... capped at one hour."""
    return timedelta(seconds=min(base_seconds * 2 ** max(attempt - 1, 0), 3600.0))


def sanitize_error_message(message: str) -> str:
    return " ".join(message.split())[:_MAX_ERROR_MESSAGE]


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    document_id: uuid.UUID
    kind: str
    attempt: int
    max_attempts: int
    lease_token: uuid.UUID
    request_id: str | None


async def enqueue_job(
    session: AsyncSession,
    document: Document,
    kind: JobKind,
    *,
    max_attempts: int,
    request_id: str | None,
) -> IngestionJob:
    """Queue a job and move the document to ``pending``. The caller commits.

    The partial unique index on active jobs makes a second concurrent request for the same
    document fail with an IntegrityError instead of queueing duplicate work.
    """
    if document.status is not None and document.status != DocumentStatus.PENDING.value:
        check_document_transition(document.status, DocumentStatus.PENDING)
    job = IngestionJob(
        id=uuid.uuid4(),
        document_id=document.id,
        kind=kind.value,
        status=JobStatus.PENDING.value,
        attempts=0,
        max_attempts=max_attempts,
        request_id=request_id,
    )
    session.add(job)
    document.status = DocumentStatus.PENDING.value
    document.updated_at = func.now()
    document.error_code = None
    document.error_message = None
    await session.flush()
    return job


async def claim_next_job(
    session: AsyncSession, worker_id: str, lease_seconds: float
) -> ClaimedJob | None:
    """Claim the oldest runnable job, or return None. Commits on success."""
    job = await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.status == JobStatus.PENDING.value)
        .where(IngestionJob.run_after <= func.now())
        .order_by(IngestionJob.run_after, IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        await session.rollback()
        return None
    document = await session.get(Document, job.document_id, with_for_update=True)
    if document is None:  # deleted between enqueue and claim; cascade removes the job
        await session.rollback()
        return None

    check_job_transition(job.status, JobStatus.PROCESSING)
    check_document_transition(document.status, DocumentStatus.PROCESSING)
    token = uuid.uuid4()
    job.status = JobStatus.PROCESSING.value
    job.attempts += 1
    job.lease_token = token
    job.locked_by = worker_id
    job.lease_expires_at = func.now() + timedelta(seconds=lease_seconds)
    job.heartbeat_at = func.now()
    job.started_at = func.coalesce(IngestionJob.started_at, func.now())
    job.updated_at = func.now()
    document.status = DocumentStatus.PROCESSING.value
    document.updated_at = func.now()
    claimed = ClaimedJob(
        id=job.id,
        document_id=job.document_id,
        kind=job.kind,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
        lease_token=token,
        request_id=job.request_id,
    )
    await session.commit()
    return claimed


async def renew_lease(session: AsyncSession, job: ClaimedJob, lease_seconds: float) -> bool:
    """Extend the lease. Returns False if this worker no longer holds the job."""
    result = await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job.id)
        .where(IngestionJob.lease_token == job.lease_token)
        .where(IngestionJob.status == JobStatus.PROCESSING.value)
        .values(
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            heartbeat_at=func.now(),
            updated_at=func.now(),
        )
    )
    await session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _lock_held_job(session: AsyncSession, job: ClaimedJob) -> tuple[IngestionJob, Document]:
    row = await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.id == job.id)
        .where(IngestionJob.lease_token == job.lease_token)
        .where(IngestionJob.status == JobStatus.PROCESSING.value)
        .with_for_update()
    )
    if row is None:
        raise LeaseLostError(str(job.id))
    document = await session.get(Document, job.document_id, with_for_update=True)
    if document is None:
        raise LeaseLostError(str(job.id))
    return row, document


@dataclass(frozen=True)
class IndexResult:
    chunks: Sequence[TextChunk]
    vectors: Sequence[Sequence[float]]
    language: str
    page_count: int | None
    model: EmbeddingModelInfo


async def complete_job(session: AsyncSession, job: ClaimedJob, result: IndexResult) -> None:
    """Atomically replace the document's index and mark the job completed."""
    if not result.chunks or len(result.chunks) != len(result.vectors):
        raise ValueError("an index needs one vector per chunk and at least one chunk")
    try:
        row, document = await _lock_held_job(session, job)
        check_job_transition(row.status, JobStatus.COMPLETED)
        check_document_transition(document.status, DocumentStatus.COMPLETED)

        # Deleting first makes the write idempotent: a retried job replaces, never appends.
        await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
        await session.execute(
            insert(Chunk),
            [
                {
                    "id": uuid.uuid4(),
                    "document_id": document.id,
                    "ordinal": chunk.ordinal,
                    "text": chunk.text,
                    "token_count": chunk.token_count,
                    "language": chunk.language.value,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section": chunk.section,
                    "embedding": list(vector),
                }
                for chunk, vector in zip(result.chunks, result.vectors, strict=True)
            ],
        )
        document.status = DocumentStatus.COMPLETED.value
        document.chunk_count = len(result.chunks)
        document.language = result.language
        document.page_count = result.page_count
        document.embedding_model = result.model.name
        document.embedding_model_revision = result.model.revision
        document.embedding_dimension = result.model.dimension
        document.error_code = None
        document.error_message = None
        document.processed_at = func.now()
        document.updated_at = func.now()
        _finish(row, JobStatus.COMPLETED)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


def _finish(row: IngestionJob, status: JobStatus) -> None:
    row.status = status.value
    row.lease_token = None
    row.lease_expires_at = None
    row.locked_by = None
    row.updated_at = func.now()
    if status in (JobStatus.COMPLETED, JobStatus.FAILED):
        row.completed_at = func.now()


def _apply_failure(
    row: IngestionJob, document: Document, error: IngestionError, backoff_seconds: float
) -> JobStatus:
    message = sanitize_error_message(error.message)
    row.last_error_code = error.code.value
    row.last_error_message = message
    if error.retryable and row.attempts < row.max_attempts:
        check_job_transition(row.status, JobStatus.PENDING)
        check_document_transition(document.status, DocumentStatus.PENDING)
        _finish(row, JobStatus.PENDING)
        row.run_after = func.now() + retry_delay(backoff_seconds, row.attempts)
        document.status = DocumentStatus.PENDING.value
        document.updated_at = func.now()
        return JobStatus.PENDING

    check_job_transition(row.status, JobStatus.FAILED)
    _finish(row, JobStatus.FAILED)
    document.updated_at = func.now()
    if document.chunk_count > 0:
        # A reindex failed. The previous index is complete and still valid, so keep it.
        check_document_transition(document.status, DocumentStatus.COMPLETED)
        document.status = DocumentStatus.COMPLETED.value
        document.error_code = error.code.value
        document.error_message = sanitize_error_message(
            f"Reindexing failed: {message} The previous index is still in use."
        )
    else:
        check_document_transition(document.status, DocumentStatus.FAILED)
        document.status = DocumentStatus.FAILED.value
        document.error_code = error.code.value
        document.error_message = message
    return JobStatus.FAILED


async def fail_job(
    session: AsyncSession, job: ClaimedJob, error: IngestionError, backoff_seconds: float
) -> JobStatus:
    """Record a failed attempt; schedules a retry or fails the job. Returns the new status."""
    try:
        row, document = await _lock_held_job(session, job)
        status = _apply_failure(row, document, error, backoff_seconds)
        await session.commit()
        return status
    except BaseException:
        await session.rollback()
        raise


async def release_job(session: AsyncSession, job: ClaimedJob) -> bool:
    """Return a job to the queue without counting the attempt (graceful shutdown)."""
    try:
        row, document = await _lock_held_job(session, job)
    except LeaseLostError:
        await session.rollback()
        return False
    _finish(row, JobStatus.PENDING)
    row.attempts = max(row.attempts - 1, 0)
    row.run_after = func.now()
    document.status = DocumentStatus.PENDING.value
    document.updated_at = func.now()
    await session.commit()
    return True


async def recover_expired_jobs(session: AsyncSession, backoff_seconds: float) -> int:
    """Requeue (or fail) jobs whose worker stopped renewing its lease.

    The interrupted attempt counts towards ``max_attempts``, so a document that crashes the
    worker every time cannot loop forever.
    """
    rows = (
        await session.scalars(
            select(IngestionJob)
            .where(IngestionJob.status == JobStatus.PROCESSING.value)
            .where(IngestionJob.lease_expires_at < func.now())
            .with_for_update(skip_locked=True)
        )
    ).all()
    recovered = 0
    for row in rows:
        document = await session.get(Document, row.document_id, with_for_update=True)
        if document is None:
            continue
        previous_worker = row.locked_by
        error = IngestionError(
            ErrorCode.WORKER_LOST,
            "The worker processing this document stopped before finishing.",
            retryable=True,
        )
        status = _apply_failure(row, document, error, backoff_seconds=0)
        logger.warning(
            "job_lease_expired",
            extra={
                "job_id": str(row.id),
                "document_id": str(row.document_id),
                "previous_worker": previous_worker,
                "new_status": status.value,
            },
        )
        recovered += 1
    await session.commit()
    return recovered
