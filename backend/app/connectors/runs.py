"""The connector sync queue: scheduling, claiming, leases, retries and recovery.

Sync runs live in ``connector_sync_runs`` and follow the same lease protocol as ingestion
jobs (``app/ingestion/jobs.py``): a worker claims a run with ``SELECT ... FOR UPDATE SKIP
LOCKED``, receives a fresh ``lease_token``, renews ``lease_expires_at`` while it works, and
every later write is conditional on still holding that token.

The partial unique index ``uq_connector_sync_runs_one_active`` allows at most one pending or
running run per connector. Both the scheduler (in any number of workers) and the API insert
with ``ON CONFLICT DO NOTHING`` against that index, so duplicate runs cannot be created and
two runs of one connector can never overlap.

States::

    pending -> running -> succeeded | partial | failed | cancelled
               running -> pending              (retry scheduled, worker lost, or released)
    pending -> cancelled                       (cancelled before a worker claimed it)

Scheduling uses database time only: an enabled connector is due when ``next_sync_at <=
now()``. Finishing a run (in any final state) sets ``next_sync_at = now() + interval``.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.errors import ConnectorError, ConnectorErrorCode
from app.db.models import (
    ACTIVE_SYNC_STATUSES,
    Connector,
    ConnectorSyncRun,
    SyncRunStatus,
    SyncTrigger,
)
from app.ingestion.jobs import LeaseLostError, sanitize_error_message

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY_SECONDS = 900.0
FINAL_STATUSES = frozenset(
    {
        SyncRunStatus.SUCCEEDED,
        SyncRunStatus.PARTIAL,
        SyncRunStatus.FAILED,
        SyncRunStatus.CANCELLED,
    }
)

_COUNTERS = (
    "objects_scanned",
    "documents_imported",
    "documents_updated",
    "documents_unchanged",
    "documents_removed",
    "objects_skipped",
    "objects_failed",
)

# One statement for every due connector, safe under any number of concurrent schedulers.
_SCHEDULE_DUE = text(
    """
    INSERT INTO connector_sync_runs (
        id, connector_id, trigger, status, attempts, max_attempts, run_after,
        created_at, updated_at
    )
    SELECT gen_random_uuid(), c.id, 'scheduled', 'pending', 0, :max_attempts, now(),
           now(), now()
    FROM connectors AS c
    WHERE c.enabled AND c.next_sync_at IS NOT NULL AND c.next_sync_at <= now()
    ON CONFLICT (connector_id) WHERE status IN ('pending', 'running') DO NOTHING
    RETURNING connector_id
    """
)

_ENQUEUE = text(
    """
    INSERT INTO connector_sync_runs (
        id, connector_id, trigger, status, attempts, max_attempts, run_after, request_id,
        created_at, updated_at
    )
    VALUES (:id, :connector_id, :trigger, 'pending', 0, :max_attempts, now(), :request_id,
            now(), now())
    ON CONFLICT (connector_id) WHERE status IN ('pending', 'running') DO NOTHING
    RETURNING id
    """
)


def sync_retry_delay(base_seconds: float, attempt: int) -> timedelta:
    """Exponential backoff: base, 2 x base, 4 x base, ... capped at 15 minutes."""
    return timedelta(seconds=min(base_seconds * 2 ** max(attempt - 1, 0), MAX_RETRY_DELAY_SECONDS))


@dataclass(frozen=True)
class ClaimedRun:
    id: uuid.UUID
    connector_id: uuid.UUID
    trigger: str
    attempt: int
    max_attempts: int
    lease_token: uuid.UUID
    request_id: str | None
    bucket: str
    prefix: str


async def schedule_due_syncs(session: AsyncSession, max_attempts: int) -> int:
    """Queue a scheduled run for every enabled connector that is due. Commits."""
    result = await session.execute(_SCHEDULE_DUE, {"max_attempts": max_attempts})
    queued = [str(row[0]) for row in result.all()]
    await session.commit()
    for connector_id in queued:
        logger.info("sync_scheduled", extra={"connector_id": connector_id})
    return len(queued)


async def enqueue_sync(
    session: AsyncSession,
    connector_id: uuid.UUID,
    *,
    trigger: SyncTrigger,
    max_attempts: int,
    request_id: str | None,
) -> tuple[ConnectorSyncRun, bool]:
    """Queue a run, or return the connector's active run. Returns (run, created). Commits."""
    created_id = await session.scalar(
        _ENQUEUE,
        {
            "id": uuid.uuid4(),
            "connector_id": connector_id,
            "trigger": trigger.value,
            "max_attempts": max_attempts,
            "request_id": request_id,
        },
    )
    await session.commit()
    if created_id is not None:
        run = await session.get(ConnectorSyncRun, created_id)
        if run is None:  # the connector was deleted in between
            raise LeaseLostError(str(created_id))
        logger.info(
            "sync_enqueued",
            extra={"connector_id": str(connector_id), "run_id": str(run.id), "trigger": trigger},
        )
        return run, True
    active = await session.scalar(
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.connector_id == connector_id)
        .where(ConnectorSyncRun.status.in_(ACTIVE_SYNC_STATUSES))
    )
    if active is None:  # finished between the insert and this read; queue again
        return await enqueue_sync(
            session,
            connector_id,
            trigger=trigger,
            max_attempts=max_attempts,
            request_id=request_id,
        )
    return active, False


async def claim_next_run(
    session: AsyncSession, worker_id: str, lease_seconds: float
) -> ClaimedRun | None:
    """Claim the oldest runnable sync, or return None. Commits on success."""
    run = await session.scalar(
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.status == SyncRunStatus.PENDING.value)
        .where(ConnectorSyncRun.run_after <= func.now())
        .order_by(ConnectorSyncRun.run_after, ConnectorSyncRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is None:
        await session.rollback()
        return None
    connector = await session.get(Connector, run.connector_id)
    if connector is None:  # deleted between enqueue and claim; cascade removes the run
        await session.rollback()
        return None
    token = uuid.uuid4()
    run.status = SyncRunStatus.RUNNING.value
    run.attempts += 1
    run.lease_token = token
    run.locked_by = worker_id
    run.lease_expires_at = func.now() + timedelta(seconds=lease_seconds)
    run.heartbeat_at = func.now()
    run.started_at = func.coalesce(ConnectorSyncRun.started_at, func.now())
    run.updated_at = func.now()
    # Counts describe the latest attempt; a retried run starts them again.
    for counter in _COUNTERS:
        setattr(run, counter, 0)
    run.failures = []
    run.error_code = None
    run.error_message = None
    connector.last_sync_started_at = func.now()
    connector.last_sync_status = SyncRunStatus.RUNNING.value
    claimed = ClaimedRun(
        id=run.id,
        connector_id=run.connector_id,
        trigger=run.trigger,
        attempt=run.attempts,
        max_attempts=run.max_attempts,
        lease_token=token,
        request_id=run.request_id,
        bucket=connector.bucket,
        prefix=connector.prefix,
    )
    await session.commit()
    return claimed


def _held(run: ClaimedRun) -> Select[tuple[ConnectorSyncRun]]:
    return (
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.id == run.id)
        .where(ConnectorSyncRun.lease_token == run.lease_token)
        .where(ConnectorSyncRun.status == SyncRunStatus.RUNNING.value)
    )


async def renew_lease(session: AsyncSession, run: ClaimedRun, lease_seconds: float) -> bool:
    """Extend the lease. Returns False if this worker no longer holds the run."""
    result = await session.execute(
        update(ConnectorSyncRun)
        .where(ConnectorSyncRun.id == run.id)
        .where(ConnectorSyncRun.lease_token == run.lease_token)
        .where(ConnectorSyncRun.status == SyncRunStatus.RUNNING.value)
        .values(
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            heartbeat_at=func.now(),
            updated_at=func.now(),
        )
    )
    await session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


async def cancel_requested(session: AsyncSession, run: ClaimedRun) -> bool:
    """Whether cancellation was requested. Raises LeaseLostError if the run is not held."""
    row = await session.scalar(_held(run).with_only_columns(ConnectorSyncRun.cancel_requested))
    await session.rollback()
    if row is None:
        raise LeaseLostError(str(run.id))
    return bool(row)


async def lock_held_run(session: AsyncSession, run: ClaimedRun) -> ConnectorSyncRun:
    """Lock the run row in the caller's transaction, fencing out a worker that lost it."""
    row = await session.scalar(_held(run).with_for_update())
    if row is None:
        raise LeaseLostError(str(run.id))
    return row


def add_counts(row: ConnectorSyncRun, **counts: int) -> None:
    for name, value in counts.items():
        if name not in _COUNTERS:
            raise ValueError(f"unknown counter {name}")
        setattr(row, name, getattr(row, name) + value)
    row.updated_at = func.now()


def _release_lease(row: ConnectorSyncRun) -> None:
    row.lease_token = None
    row.lease_expires_at = None
    row.locked_by = None
    row.updated_at = func.now()


async def _finish_connector(
    session: AsyncSession, row: ConnectorSyncRun, status: SyncRunStatus
) -> None:
    connector = await session.get(Connector, row.connector_id, with_for_update=True)
    if connector is None:
        return
    connector.last_sync_status = status.value
    if status in FINAL_STATUSES:
        connector.last_sync_finished_at = func.now()
        if status in (SyncRunStatus.SUCCEEDED, SyncRunStatus.PARTIAL):
            # The listing completed: the documents match the bucket as of this run.
            connector.last_success_at = func.now()
        connector.last_error_code = row.error_code
        connector.last_error_message = row.error_message
        connector.next_sync_at = (
            func.now() + timedelta(minutes=connector.sync_interval_minutes)
            if connector.enabled
            else None
        )
    else:
        connector.last_error_code = row.error_code
        connector.last_error_message = row.error_message


def _set_error(row: ConnectorSyncRun, error: ConnectorError | None) -> None:
    row.error_code = error.code.value if error else None
    row.error_message = sanitize_error_message(error.message) if error else None


async def finish_run(
    session: AsyncSession,
    run: ClaimedRun,
    status: SyncRunStatus,
    *,
    failures: list[dict[str, str]] | None = None,
    error: ConnectorError | None = None,
) -> None:
    """Record the final state of a run this worker holds. Commits."""
    if status not in FINAL_STATUSES:
        raise ValueError(f"{status} is not a final status")
    try:
        row = await lock_held_run(session, run)
        row.status = status.value
        row.finished_at = func.now()
        if failures is not None:
            row.failures = failures
        _set_error(row, error)
        _release_lease(row)
        await _finish_connector(session, row, status)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


def _apply_failure(
    row: ConnectorSyncRun, error: ConnectorError, backoff_seconds: float
) -> SyncRunStatus:
    _set_error(row, error)
    _release_lease(row)
    if error.retryable and row.attempts < row.max_attempts:
        row.status = SyncRunStatus.PENDING.value
        row.run_after = func.now() + sync_retry_delay(backoff_seconds, row.attempts)
        return SyncRunStatus.PENDING
    row.status = SyncRunStatus.FAILED.value
    row.finished_at = func.now()
    return SyncRunStatus.FAILED


async def fail_run(
    session: AsyncSession,
    run: ClaimedRun,
    error: ConnectorError,
    backoff_seconds: float,
    *,
    failures: list[dict[str, str]] | None = None,
) -> SyncRunStatus:
    """Record a failed attempt: schedule a retry with backoff, or fail the run. Commits."""
    try:
        row = await lock_held_run(session, run)
        if failures is not None:
            row.failures = failures
        status = _apply_failure(row, error, backoff_seconds)
        await _finish_connector(session, row, status)
        await session.commit()
        return status
    except BaseException:
        await session.rollback()
        raise


async def release_run(session: AsyncSession, run: ClaimedRun) -> bool:
    """Return a run to the queue without counting the attempt (graceful shutdown)."""
    try:
        row = await lock_held_run(session, run)
    except LeaseLostError:
        await session.rollback()
        return False
    _release_lease(row)
    row.status = SyncRunStatus.PENDING.value
    row.attempts = max(row.attempts - 1, 0)
    row.run_after = func.now()
    await _finish_connector(session, row, SyncRunStatus.PENDING)
    await session.commit()
    return True


async def recover_expired_runs(session: AsyncSession, backoff_seconds: float) -> int:
    """Requeue (or fail) runs whose worker stopped renewing its lease. Commits.

    The interrupted attempt counts towards ``max_attempts``. A recovered run lists the
    bucket again from the start; objects imported before the crash are then unchanged.
    """
    rows = (
        await session.scalars(
            select(ConnectorSyncRun)
            .where(ConnectorSyncRun.status == SyncRunStatus.RUNNING.value)
            .where(ConnectorSyncRun.lease_expires_at < func.now())
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        previous_worker = row.locked_by
        if row.cancel_requested:
            _set_error(row, None)
            _release_lease(row)
            row.status = SyncRunStatus.CANCELLED.value
            row.finished_at = func.now()
            status = SyncRunStatus.CANCELLED
        else:
            error = ConnectorError(
                ConnectorErrorCode.WORKER_LOST,
                "The worker running this sync stopped before finishing.",
                retryable=True,
            )
            status = _apply_failure(row, error, backoff_seconds)
        await _finish_connector(session, row, status)
        logger.warning(
            "sync_lease_expired",
            extra={
                "run_id": str(row.id),
                "connector_id": str(row.connector_id),
                "previous_worker": previous_worker,
                "new_status": status.value,
                "attempt": row.attempts,
            },
        )
    await session.commit()
    return len(rows)


async def cancel_run(session: AsyncSession, row: ConnectorSyncRun) -> ConnectorSyncRun:
    """Cancel a locked, active run. A pending run is cancelled now; a running one stops at
    the next object boundary (the worker checks between objects). Commits."""
    if row.status == SyncRunStatus.PENDING.value:
        row.status = SyncRunStatus.CANCELLED.value
        row.cancel_requested = True
        row.finished_at = func.now()
        _set_error(row, None)
        await _finish_connector(session, row, SyncRunStatus.CANCELLED)
    elif row.status == SyncRunStatus.RUNNING.value:
        row.cancel_requested = True
        row.updated_at = func.now()
    else:
        raise ValueError("only pending or running syncs can be cancelled")
    await session.commit()
    await session.refresh(row)
    logger.info(
        "sync_cancel_requested",
        extra={"run_id": str(row.id), "connector_id": str(row.connector_id), "status": row.status},
    )
    return row
