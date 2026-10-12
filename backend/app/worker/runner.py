"""The ingestion worker: claims jobs from PostgreSQL and builds document indexes.

One process runs up to ``WORKER_CONCURRENCY`` jobs at a time. CPU-bound work (extraction,
chunking, embedding) runs in a thread so the event loop keeps renewing leases. Workers are
stateless apart from the loaded model: any number can run, and any of them can be killed
and restarted. A job whose worker disappears is recovered when its lease expires.
"""

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import request_id_var
from app.db.models import Document, FileType
from app.db.session import create_session_factory
from app.embeddings.base import EmbeddingProvider
from app.ingestion import jobs
from app.ingestion.chunking import ChunkingConfig
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.pipeline import build_index
from app.storage import DocumentStorage, StoredFileNotFoundError

logger = logging.getLogger(__name__)

# Files in storage that no document references are deleted after this long. The delay
# covers the window between storing an upload and committing its database row.
ORPHAN_FILE_MIN_AGE_SECONDS = 3600


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        provider: EmbeddingProvider,
        storage: DocumentStorage,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.sessions: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        self.provider = provider
        self.storage = storage
        self.worker_id = worker_id or default_worker_id()
        self.chunking = ChunkingConfig(
            target_tokens=settings.chunk_target_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
            min_tokens=settings.chunk_min_tokens,
        )
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        """Stop claiming new jobs; in-flight jobs are given a grace period to finish."""
        self._stopping.set()

    # -- main loop -------------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "worker_started",
            extra={
                "worker_id": self.worker_id,
                "concurrency": self.settings.worker_concurrency,
                "embedding_provider": self.settings.embedding_provider,
            },
        )
        liveness = asyncio.create_task(self._liveness_loop())
        try:
            await self.sweep_orphaned_files()
            if self.settings.worker_preload_model:
                await asyncio.to_thread(self.provider.load)
            while not self._stopping.is_set():
                claimed_any = await self.run_once()
                if not claimed_any:
                    await self._sleep(self.settings.worker_poll_interval_seconds)
        finally:
            await self._drain()
            liveness.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await liveness
            logger.info("worker_stopped", extra={"worker_id": self.worker_id})

    async def run_once(self) -> bool:
        """Recover expired leases and fill free slots. Returns True if a job was claimed."""
        try:
            async with self.sessions() as session:
                recovered = await jobs.recover_expired_jobs(
                    session, self.settings.worker_retry_backoff_seconds
                )
            if recovered:
                logger.info("jobs_recovered", extra={"count": recovered})
            claimed_any = False
            while len(self._tasks) < self.settings.worker_concurrency:
                if self._stopping.is_set():
                    break
                async with self.sessions() as session:
                    claimed = await jobs.claim_next_job(
                        session, self.worker_id, self.settings.worker_lease_seconds
                    )
                if claimed is None:
                    break
                claimed_any = True
                task = asyncio.create_task(self.process(claimed))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return claimed_any
        except Exception:
            # Database outages must not kill the worker; it retries on the next poll.
            logger.exception("worker_poll_failed")
            return False

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def wait_idle(self) -> None:
        """Wait until all in-flight jobs have finished (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _drain(self) -> None:
        if not self._tasks:
            return
        logger.info("worker_draining", extra={"in_flight": len(self._tasks)})
        _, pending = await asyncio.wait(
            set(self._tasks), timeout=self.settings.worker_shutdown_grace_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _liveness_loop(self) -> None:
        path = self.settings.worker_heartbeat_file
        while True:
            if path is not None:
                with contextlib.suppress(OSError):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"{time.time():.0f} {self.worker_id}\n")
            await asyncio.sleep(min(10.0, self.settings.worker_heartbeat_seconds))

    # -- one job ---------------------------------------------------------------------------

    async def process(self, job: jobs.ClaimedJob) -> None:
        token = request_id_var.set(job.request_id or f"job-{job.id.hex[:12]}")
        context = {
            "job_id": str(job.id),
            "document_id": str(job.document_id),
            "job_kind": job.kind,
            "attempt": job.attempt,
        }
        started = time.perf_counter()
        logger.info("job_started", extra=context)
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            result = await self._build(job)
            async with self.sessions() as session:
                await jobs.complete_job(session, job, result)
            logger.info(
                "job_completed",
                extra={
                    **context,
                    "chunks": len(result.chunks),
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
        except asyncio.CancelledError:
            # Shutdown grace period exceeded: hand the job back without using an attempt.
            # The embedding thread cannot be interrupted; the process exit ends it.
            with contextlib.suppress(Exception):
                async with self.sessions() as session:
                    released = await jobs.release_job(session, job)
                logger.warning("job_released", extra={**context, "released": released})
            raise
        except jobs.LeaseLostError:
            logger.warning("job_lease_lost", extra=context)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, IngestionError)
                else IngestionError(
                    ErrorCode.INTERNAL_ERROR,
                    "An unexpected error occurred while processing the document.",
                    retryable=True,
                )
            )
            log = logger.warning if isinstance(exc, IngestionError) else logger.exception
            log(
                "job_attempt_failed",
                extra={
                    **context,
                    "error_code": error.code.value,
                    "retryable": error.retryable,
                    "error": error.message,
                },
            )
            try:
                async with self.sessions() as session:
                    status = await jobs.fail_job(
                        session, job, error, self.settings.worker_retry_backoff_seconds
                    )
                logger.info("job_failure_recorded", extra={**context, "status": status.value})
            except jobs.LeaseLostError:
                logger.warning("job_lease_lost", extra=context)
            except Exception:
                # The lease will expire and recovery will requeue the job.
                logger.exception("job_failure_not_recorded", extra=context)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            request_id_var.reset(token)

    async def _build(self, job: jobs.ClaimedJob) -> jobs.IndexResult:
        async with self.sessions() as session:
            document = await session.scalar(select(Document).where(Document.id == job.document_id))
        if document is None:
            raise jobs.LeaseLostError(str(job.id))
        try:
            path = self.storage.local_path(document.storage_key)
        except StoredFileNotFoundError as exc:
            raise IngestionError(
                ErrorCode.STORED_FILE_MISSING, "The original file is missing from storage."
            ) from exc
        return await asyncio.to_thread(
            build_index, Path(path), FileType(document.file_type), self.provider, self.chunking
        )

    async def _heartbeat(self, job: jobs.ClaimedJob) -> None:
        while True:
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)
            try:
                async with self.sessions() as session:
                    held = await jobs.renew_lease(session, job, self.settings.worker_lease_seconds)
            except Exception:
                logger.warning("lease_renewal_failed", extra={"job_id": str(job.id)})
                continue
            if not held:
                logger.warning("lease_lost_during_processing", extra={"job_id": str(job.id)})
                return

    # -- maintenance -----------------------------------------------------------------------

    async def sweep_orphaned_files(self) -> int:
        """Delete stored files that no document references (left by a crash mid-upload or
        mid-delete). Only files older than an hour are considered."""
        try:
            candidates = await asyncio.to_thread(
                lambda: list(self.storage.iter_keys_older_than(ORPHAN_FILE_MIN_AGE_SECONDS))
            )
            if not candidates:
                return 0
            async with self.sessions() as session:
                referenced = set(
                    (
                        await session.scalars(
                            select(Document.storage_key).where(Document.storage_key.in_(candidates))
                        )
                    ).all()
                )
            removed = 0
            for key in candidates:
                if key not in referenced and self.storage.delete(key):
                    removed += 1
            if removed:
                logger.info("orphaned_files_removed", extra={"count": removed})
            return removed
        except Exception:
            logger.exception("orphaned_file_sweep_failed")
            return 0
