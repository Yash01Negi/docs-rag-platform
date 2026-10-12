"""Ingestion worker and job queue against a real PostgreSQL + pgvector database."""

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Chunk, Document, DocumentStatus, IngestionJob, JobKind, JobStatus
from app.db.session import create_engine, create_session_factory
from app.embeddings.fake import FakeEmbeddingProvider
from app.ingestion import jobs
from app.ingestion.errors import ErrorCode, IngestionError
from app.storage import LocalFileStorage, new_storage_key
from app.worker.runner import Worker
from tests.samples import ENGLISH_TEXT, JAPANESE_TEXT

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class FailingProvider(FakeEmbeddingProvider):
    """Fails the first ``failures`` embed calls with the given error, then works."""

    def __init__(self, error: IngestionError, failures: int = 10**6) -> None:
        super().__init__(1024)
        self.error = error
        self.failures = failures

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return super()._embed_batch(texts)


RETRYABLE = IngestionError(ErrorCode.EMBEDDING_FAILED, "temporary failure", retryable=True)
PERMANENT = IngestionError(ErrorCode.INVALID_EMBEDDING, "bad vectors", retryable=False)


@pytest.fixture
async def engine(db_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(db_settings)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def storage(db_settings: Settings) -> LocalFileStorage:
    return LocalFileStorage(db_settings.storage_dir)


def make_worker(
    settings: Settings,
    engine: AsyncEngine,
    storage: LocalFileStorage,
    provider: FakeEmbeddingProvider | None = None,
    worker_id: str = "worker-a",
) -> Worker:
    return Worker(settings, engine, provider or FakeEmbeddingProvider(1024), storage, worker_id)


async def create_document(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    tmp_path: Path,
    content: str = ENGLISH_TEXT * 3,
    max_attempts: int = 3,
) -> uuid.UUID:
    source = tmp_path / f"{uuid.uuid4().hex}.txt"
    source.write_text(content, encoding="utf-8")
    key = new_storage_key()
    storage.put_file(key, source)
    async with sessions() as session:
        document = Document(
            id=uuid.uuid4(),
            filename="doc.txt",
            file_type="text",
            mime_type="text/plain",
            size_bytes=source.stat().st_size,
            sha256=uuid.uuid4().hex * 2,
            storage_key=key,
            status=DocumentStatus.PENDING.value,
            chunk_count=0,
        )
        session.add(document)
        await session.flush()
        await jobs.enqueue_job(
            session, document, JobKind.INGEST, max_attempts=max_attempts, request_id="req-test"
        )
        await session.commit()
        return document.id


async def drain(worker: Worker) -> None:
    while await worker.run_once():
        await worker.wait_idle()


async def state(sessions: async_sessionmaker[AsyncSession], document_id: uuid.UUID):  # type: ignore[no-untyped-def]
    async with sessions() as session:
        document = await session.get(Document, document_id)
        job_rows = (
            await session.scalars(
                select(IngestionJob)
                .where(IngestionJob.document_id == document_id)
                .order_by(IngestionJob.created_at)
            )
        ).all()
        chunk_ids = (
            await session.scalars(
                select(Chunk.id).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)
            )
        ).all()
        return document, list(job_rows), list(chunk_ids)


async def make_runnable(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Skip retry backoff by making every pending job runnable now."""
    async with sessions() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.status == "pending")
            .values(run_after=text("now() - interval '1 second'"))
        )
        await session.commit()


async def test_successful_job_writes_chunks_and_completes(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path, JAPANESE_TEXT * 5)
    await drain(make_worker(db_settings, engine, storage))

    document, job_rows, chunk_ids = await state(sessions, document_id)
    assert document.status == "completed"
    assert document.chunk_count == len(chunk_ids) > 0
    assert document.embedding_model == "fake-deterministic"
    assert document.error_code is None
    assert [j.status for j in job_rows] == ["completed"]
    assert job_rows[0].lease_token is None and job_rows[0].completed_at is not None


async def test_retryable_failure_is_retried_then_succeeds(
    make_settings, clean_database: str, storage, tmp_path: Path
) -> None:
    settings = make_settings(database_url=clean_database, worker_retry_backoff_seconds=600)
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        document_id = await create_document(sessions, storage, tmp_path)
        worker = make_worker(settings, engine, storage, FailingProvider(RETRYABLE, failures=1))

        await drain(worker)
        document, job_rows, _ = await state(sessions, document_id)
        assert document.status == "pending"
        assert document.error_code is None  # only terminal failures are shown as errors
        assert job_rows[0].status == "pending"
        assert job_rows[0].attempts == 1
        assert job_rows[0].last_error_code == "embedding_failed"
        assert job_rows[0].run_after > job_rows[0].created_at

        await make_runnable(sessions)
        await drain(worker)
        document, job_rows, chunk_ids = await state(sessions, document_id)
        assert document.status == "completed"
        assert job_rows[0].attempts == 2
        assert len(chunk_ids) == document.chunk_count
    finally:
        await engine.dispose()


async def test_retry_backoff_delays_the_next_attempt(
    make_settings, clean_database: str, storage, tmp_path: Path
) -> None:
    settings = make_settings(database_url=clean_database, worker_retry_backoff_seconds=3600)
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        await create_document(sessions, storage, tmp_path)
        worker = make_worker(settings, engine, storage, FailingProvider(RETRYABLE))
        await drain(worker)
        assert await worker.run_once() is False  # the retry is not due yet
    finally:
        await engine.dispose()


async def test_job_fails_after_max_attempts(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path, max_attempts=3)
    worker = make_worker(db_settings, engine, storage, FailingProvider(RETRYABLE))

    for _ in range(5):
        await make_runnable(sessions)
        await drain(worker)

    document, job_rows, chunk_ids = await state(sessions, document_id)
    assert document.status == "failed"
    assert document.error_code == "embedding_failed"
    assert document.chunk_count == 0 and chunk_ids == []
    assert job_rows[0].status == "failed"
    assert job_rows[0].attempts == 3


async def test_permanent_failure_is_not_retried(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    await drain(make_worker(db_settings, engine, storage, FailingProvider(PERMANENT)))

    document, job_rows, _ = await state(sessions, document_id)
    assert document.status == "failed"
    assert document.error_code == "invalid_embedding"
    assert job_rows[0].attempts == 1


async def test_unexpected_exceptions_are_recorded_without_internals(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    class Broken(FakeEmbeddingProvider):
        def count_tokens(self, text: str) -> int:
            raise KeyError("/secret/internal/path")

    document_id = await create_document(sessions, storage, tmp_path, max_attempts=1)
    await drain(make_worker(db_settings, engine, storage, Broken(1024)))

    document, _, _ = await state(sessions, document_id)
    assert document.status == "failed"
    assert document.error_code == "internal_error"
    assert "secret" not in (document.error_message or "")


async def test_missing_stored_file_fails_the_document(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    document, _, _ = await state(sessions, document_id)
    storage.delete(document.storage_key)

    await drain(make_worker(db_settings, engine, storage))
    document, _, _ = await state(sessions, document_id)
    assert document.status == "failed"
    assert document.error_code == "stored_file_missing"


async def test_concurrent_claims_never_return_the_same_job(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    for _ in range(3):
        await create_document(sessions, storage, tmp_path)

    async def claim(worker_id: str) -> jobs.ClaimedJob | None:
        async with sessions() as session:
            return await jobs.claim_next_job(session, worker_id, 60)

    claimed = await asyncio.gather(*(claim(f"w{i}") for i in range(6)))
    ids = [job.id for job in claimed if job is not None]
    assert len(ids) == 3
    assert len(set(ids)) == 3


async def test_interrupted_worker_job_is_recovered_by_another_worker(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)

    # Worker A claims the job and then "crashes": it never renews its lease.
    async with sessions() as session:
        crashed = await jobs.claim_next_job(session, "worker-a", lease_seconds=60)
    assert crashed is not None
    document, _, _ = await state(sessions, document_id)
    assert document.status == "processing"

    # While the lease is valid, nobody else can take the job.
    worker_b = make_worker(db_settings, engine, storage, worker_id="worker-b")
    assert await worker_b.run_once() is False

    async with sessions() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == crashed.id)
            .values(lease_expires_at=text("now() - interval '1 second'"))
        )
        await session.commit()

    await drain(worker_b)
    document, job_rows, chunk_ids = await state(sessions, document_id)
    assert document.status == "completed"
    assert job_rows[0].attempts == 2  # the interrupted attempt counts
    assert job_rows[0].last_error_code == "worker_lost"

    # Worker A comes back and tries to finish: it is fenced out, nothing is duplicated.
    result = await worker_b._build(crashed)
    async with sessions() as session:
        with pytest.raises(jobs.LeaseLostError):
            await jobs.complete_job(session, crashed, result)
    _, _, chunk_ids_after = await state(sessions, document_id)
    assert chunk_ids_after == chunk_ids


async def test_expired_job_at_max_attempts_fails(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path, max_attempts=1)
    async with sessions() as session:
        await jobs.claim_next_job(session, "worker-a", lease_seconds=60)
        await session.execute(
            update(IngestionJob).values(lease_expires_at=text("now() - interval '1 second'"))
        )
        await session.commit()
    async with sessions() as session:
        assert await jobs.recover_expired_jobs(session, 0) == 1

    document, job_rows, _ = await state(sessions, document_id)
    assert document.status == "failed"
    assert document.error_code == "worker_lost"
    assert job_rows[0].status == "failed"


async def test_completing_twice_does_not_duplicate_chunks(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    async with sessions() as session:
        claimed = await jobs.claim_next_job(session, "worker-a", 60)
    assert claimed is not None
    worker = make_worker(db_settings, engine, storage)
    result = await worker._build(claimed)

    async with sessions() as session:
        await jobs.complete_job(session, claimed, result)
    async with sessions() as session:
        with pytest.raises(jobs.LeaseLostError):
            await jobs.complete_job(session, claimed, result)

    document, _, chunk_ids = await state(sessions, document_id)
    assert len(chunk_ids) == document.chunk_count == len(result.chunks)


async def test_reindex_keeps_old_index_visible_until_the_new_one_commits(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path, ENGLISH_TEXT * 6)
    await drain(make_worker(db_settings, engine, storage))
    _, _, original_chunks = await state(sessions, document_id)

    async with sessions() as session:
        document = await session.get(Document, document_id, with_for_update=True)
        await jobs.enqueue_job(session, document, JobKind.REINDEX, max_attempts=3, request_id=None)
        await session.commit()
    async with sessions() as session:
        claimed = await jobs.claim_next_job(session, "worker-a", 60)
    assert claimed is not None and claimed.kind == "reindex"

    document, _, during = await state(sessions, document_id)
    assert document.status == "processing"
    assert during == original_chunks  # old index fully intact while the reindex runs
    assert document.chunk_count == len(original_chunks)

    result = await make_worker(db_settings, engine, storage)._build(claimed)
    async with sessions() as session:
        await jobs.complete_job(session, claimed, result)
    document, _, after = await state(sessions, document_id)
    assert document.status == "completed"
    assert len(after) == len(result.chunks)
    assert not set(after) & set(original_chunks)


async def test_failed_reindex_preserves_previous_index(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path, ENGLISH_TEXT * 6)
    await drain(make_worker(db_settings, engine, storage))
    _, _, original_chunks = await state(sessions, document_id)

    async with sessions() as session:
        document = await session.get(Document, document_id, with_for_update=True)
        await jobs.enqueue_job(session, document, JobKind.REINDEX, max_attempts=1, request_id=None)
        await session.commit()
    await drain(make_worker(db_settings, engine, storage, FailingProvider(PERMANENT)))

    document, job_rows, chunk_ids = await state(sessions, document_id)
    assert document.status == "completed"  # still searchable with the previous index
    assert chunk_ids == original_chunks
    assert document.error_code == "invalid_embedding"
    assert "previous index is still in use" in document.error_message
    assert [j.status for j in job_rows] == ["completed", "failed"]


async def test_graceful_release_does_not_consume_an_attempt(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    async with sessions() as session:
        claimed = await jobs.claim_next_job(session, "worker-a", 60)
    async with sessions() as session:
        assert await jobs.release_job(session, claimed) is True

    document, job_rows, _ = await state(sessions, document_id)
    assert document.status == "pending"
    assert job_rows[0].status == "pending"
    assert job_rows[0].attempts == 0
    assert job_rows[0].lease_token is None


async def test_deleting_a_document_during_processing_discards_the_work(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    async with sessions() as session:
        claimed = await jobs.claim_next_job(session, "worker-a", 60)
    result = await make_worker(db_settings, engine, storage)._build(claimed)

    async with sessions() as session:  # what DELETE /documents/{id} does in the database
        await session.delete(await session.get(Document, document_id))
        await session.commit()
    async with sessions() as session:
        with pytest.raises(jobs.LeaseLostError):
            await jobs.complete_job(session, claimed, result)
    async with sessions() as session:
        assert await session.scalar(select(text("count(*)")).select_from(Chunk)) == 0


async def test_database_rejects_completed_document_without_chunks(
    sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    async with sessions() as session:
        with pytest.raises(IntegrityError, match="ck_documents_completed_has_index"):
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(status="completed", processed_at=text("now()"), embedding_model="x")
            )
            await session.commit()


async def test_database_rejects_two_active_jobs_for_one_document(
    sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    async with sessions() as session:
        session.add(
            IngestionJob(
                id=uuid.uuid4(),
                document_id=document_id,
                kind="ingest",
                status=JobStatus.PENDING.value,
                attempts=0,
                max_attempts=3,
            )
        )
        with pytest.raises(IntegrityError, match="uq_ingestion_jobs_one_active_per_document"):
            await session.commit()


async def test_worker_run_loop_stops_gracefully_and_reports_liveness(
    make_settings, clean_database: str, storage, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "worker.alive"
    settings = make_settings(database_url=clean_database, worker_heartbeat_file=heartbeat)
    engine = create_engine(settings)
    try:
        sessions = create_session_factory(engine)
        document_id = await create_document(sessions, storage, tmp_path)
        worker = make_worker(settings, engine, storage)
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            document, _, _ = await state(sessions, document_id)
            if document.status == "completed":
                break
            await asyncio.sleep(0.05)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)
        assert document.status == "completed"
        assert heartbeat.read_text().split()[1] == "worker-a"
    finally:
        await engine.dispose()


async def test_orphaned_files_are_swept(
    db_settings: Settings, engine: AsyncEngine, sessions, storage, tmp_path: Path
) -> None:
    document_id = await create_document(sessions, storage, tmp_path)
    document, _, _ = await state(sessions, document_id)
    orphan = new_storage_key()
    source = tmp_path / "orphan.txt"
    source.write_text("orphan")
    storage.put_file(orphan, source)
    old = time.time() - 7200
    for key in (orphan, document.storage_key):
        os.utime(storage.local_path(key), (old, old))

    removed = await make_worker(db_settings, engine, storage).sweep_orphaned_files()

    assert removed == 1
    assert not storage.exists(orphan)
    assert storage.exists(document.storage_key)
