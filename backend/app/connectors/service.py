"""Connector operations used by the API: create, list, update, delete and request syncs.

The API never contacts AWS. It validates configuration, stores it and queues sync runs;
the worker does all S3 access with its own credentials.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors import runs
from app.connectors.sources import ConnectorValidationError, validate_bucket, validate_prefix
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import (
    ACTIVE_SYNC_STATUSES,
    Connector,
    ConnectorObject,
    ConnectorObjectStatus,
    ConnectorProvider,
    ConnectorSyncRun,
    Document,
    MessageCitation,
    SyncTrigger,
)
from app.storage import DocumentStorage

logger = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 10080  # one week


@dataclass(frozen=True)
class ConnectorStats:
    documents: int
    failed_objects: int
    deleted_objects: int
    active_run: ConnectorSyncRun | None


def require_configured(settings: Settings) -> None:
    if not settings.s3_allowed_sources:
        raise ApiError(
            503,
            "connectors_not_configured",
            "S3 connectors are not configured on the server. Set S3_ALLOWED_SOURCES "
            "(see docs/s3-setup.md).",
        )


def _validated_source(settings: Settings, bucket: str, prefix: str | None) -> tuple[str, str]:
    try:
        bucket = validate_bucket(bucket)
    except ConnectorValidationError as exc:
        raise ApiError(422, "invalid_bucket", str(exc)) from exc
    try:
        prefix = validate_prefix(prefix)
    except ConnectorValidationError as exc:
        raise ApiError(422, "invalid_prefix", str(exc)) from exc
    if not any(source.allows(bucket, prefix) for source in settings.s3_allowed_sources):
        raise ApiError(
            422,
            "source_not_allowed",
            "This bucket and prefix are not in the server's S3_ALLOWED_SOURCES.",
        )
    return bucket, prefix


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ApiError(422, "invalid_name", "The connector name must not be empty.")
    return cleaned


async def _check_name_free(
    session: AsyncSession, name: str, exclude: uuid.UUID | None = None
) -> None:
    query = select(Connector.id).where(func.lower(Connector.name) == name.lower())
    if exclude is not None:
        query = query.where(Connector.id != exclude)
    if await session.scalar(query) is not None:
        raise ApiError(409, "connector_name_taken", "A connector with this name already exists.")


async def create_connector(
    session: AsyncSession,
    settings: Settings,
    *,
    name: str,
    bucket: str,
    prefix: str | None,
    sync_interval_minutes: int,
    enabled: bool,
) -> Connector:
    require_configured(settings)
    name = _clean_name(name)
    bucket, prefix = _validated_source(settings, bucket, prefix)
    # Serialize creations for the same bucket, so two overlapping connectors cannot both
    # pass the overlap check.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('yashrag-connector:' || :bucket))"),
        {"bucket": bucket},
    )
    await _check_name_free(session, name)
    prefixes = (
        await session.scalars(select(Connector.prefix).where(Connector.bucket == bucket))
    ).all()
    for existing in prefixes:
        if existing == prefix:
            raise ApiError(
                409, "connector_exists", "A connector for this bucket and prefix already exists."
            )
        if existing.startswith(prefix) or prefix.startswith(existing):
            raise ApiError(
                409,
                "connector_overlap",
                "Another connector already covers part of this bucket and prefix; a document "
                "can only be imported by one connector.",
            )
    connector = Connector(
        id=uuid.uuid4(),
        name=name,
        provider=ConnectorProvider.S3.value,
        bucket=bucket,
        prefix=prefix,
        enabled=enabled,
        sync_interval_minutes=sync_interval_minutes,
        # An enabled connector runs its first sync on the worker's next poll.
        next_sync_at=func.now() if enabled else None,
    )
    session.add(connector)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            409, "connector_exists", "A connector with this name or source already exists."
        ) from exc
    await session.refresh(connector)
    logger.info(
        "connector_created",
        extra={"connector_id": str(connector.id), "enabled": enabled},
    )
    return connector


async def get_connector(session: AsyncSession, connector_id: uuid.UUID) -> Connector:
    connector = await session.get(Connector, connector_id)
    if connector is None:
        raise ApiError(404, "connector_not_found", "Connector not found.")
    return connector


async def list_connectors(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[list[tuple[Connector, int]], int]:
    total = await session.scalar(select(func.count()).select_from(Connector)) or 0
    documents = (
        select(func.count())
        .select_from(ConnectorObject)
        .where(ConnectorObject.connector_id == Connector.id)
        .where(ConnectorObject.document_id.is_not(None))
        .scalar_subquery()
    )
    rows = await session.execute(
        select(Connector, documents)
        .order_by(func.lower(Connector.name), Connector.id)
        .limit(limit)
        .offset(offset)
    )
    return [(connector, count) for connector, count in rows.all()], total


async def connector_stats(session: AsyncSession, connector_id: uuid.UUID) -> ConnectorStats:
    counts = dict(
        (
            await session.execute(
                select(ConnectorObject.status, func.count())
                .where(ConnectorObject.connector_id == connector_id)
                .group_by(ConnectorObject.status)
            )
        ).all()
    )
    documents = await session.scalar(
        select(func.count())
        .select_from(ConnectorObject)
        .where(ConnectorObject.connector_id == connector_id)
        .where(ConnectorObject.document_id.is_not(None))
    )
    active = await session.scalar(
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.connector_id == connector_id)
        .where(ConnectorSyncRun.status.in_(ACTIVE_SYNC_STATUSES))
    )
    return ConnectorStats(
        documents=documents or 0,
        failed_objects=counts.get(ConnectorObjectStatus.FAILED.value, 0),
        deleted_objects=counts.get(ConnectorObjectStatus.DELETED.value, 0),
        active_run=active,
    )


async def update_connector(
    session: AsyncSession,
    connector_id: uuid.UUID,
    *,
    name: str | None,
    enabled: bool | None,
    sync_interval_minutes: int | None,
) -> Connector:
    connector = await session.get(Connector, connector_id, with_for_update=True)
    if connector is None:
        raise ApiError(404, "connector_not_found", "Connector not found.")
    if name is not None:
        name = _clean_name(name)
        await _check_name_free(session, name, exclude=connector.id)
        connector.name = name
    reschedule = False
    if sync_interval_minutes is not None and sync_interval_minutes != (
        connector.sync_interval_minutes
    ):
        connector.sync_interval_minutes = sync_interval_minutes
        reschedule = True
    if enabled is not None and enabled != connector.enabled:
        connector.enabled = enabled
        reschedule = True
    if reschedule:
        if connector.enabled:
            # The next sync is one interval after the last one finished (now if it never ran
            # or that time has passed; the scheduler then queues it on its next poll).
            connector.next_sync_at = (
                Connector.last_sync_finished_at + timedelta(minutes=connector.sync_interval_minutes)
                if connector.last_sync_finished_at is not None
                else func.now()
            )
        else:
            connector.next_sync_at = None
    connector.updated_at = func.now()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            409, "connector_name_taken", "A connector with this name already exists."
        ) from exc
    await session.refresh(connector)
    logger.info("connector_updated", extra={"connector_id": str(connector.id)})
    return connector


async def delete_connector(
    sessions: async_sessionmaker[AsyncSession],
    storage: DocumentStorage,
    connector_id: uuid.UUID,
) -> int:
    """Delete the connector, its sync history and every document it imported, in one
    transaction. Manual uploads are never affected. Returns the number of documents removed.

    A sync that is running loses its lease (its run row is deleted) and stops at its next
    write; anything it was writing is rolled back.
    """
    async with sessions() as session:
        # Lock order matches the sync worker (run row, then connector), avoiding deadlocks.
        await session.execute(
            select(ConnectorSyncRun.id)
            .where(ConnectorSyncRun.connector_id == connector_id)
            .where(ConnectorSyncRun.status.in_(ACTIVE_SYNC_STATUSES))
            .with_for_update()
        )
        connector = await session.get(Connector, connector_id, with_for_update=True)
        if connector is None:
            raise ApiError(404, "connector_not_found", "Connector not found.")
        rows = (
            await session.execute(
                select(Document.id, Document.storage_key)
                .join(ConnectorObject, ConnectorObject.document_id == Document.id)
                .where(ConnectorObject.connector_id == connector_id)
                .with_for_update(of=Document)
            )
        ).all()
        document_ids = [row[0] for row in rows]
        if document_ids:
            await session.execute(
                update(MessageCitation)
                .where(MessageCitation.document_id.in_(document_ids))
                .values(excerpt=None)
            )
            await session.execute(delete(Document).where(Document.id.in_(document_ids)))
        await session.delete(connector)
        await session.commit()
    for _, storage_key in rows:
        try:
            await asyncio.to_thread(storage.delete, storage_key)
        except OSError:
            logger.warning("stored_file_delete_failed", extra={"storage_key": storage_key})
    logger.info(
        "connector_deleted",
        extra={"connector_id": str(connector_id), "documents_removed": len(document_ids)},
    )
    return len(document_ids)


async def request_sync(
    session: AsyncSession,
    settings: Settings,
    connector_id: uuid.UUID,
    request_id: str | None,
) -> tuple[ConnectorSyncRun, bool]:
    """Queue a manual sync (allowed whether or not scheduling is enabled)."""
    await get_connector(session, connector_id)
    try:
        return await runs.enqueue_sync(
            session,
            connector_id,
            trigger=SyncTrigger.MANUAL,
            max_attempts=settings.connector_sync_max_attempts,
            request_id=request_id,
        )
    except (IntegrityError, runs.LeaseLostError) as exc:  # the connector was just deleted
        await session.rollback()
        raise ApiError(404, "connector_not_found", "Connector not found.") from exc


async def list_runs(
    session: AsyncSession, connector_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[ConnectorSyncRun], int]:
    await get_connector(session, connector_id)
    total = await session.scalar(
        select(func.count())
        .select_from(ConnectorSyncRun)
        .where(ConnectorSyncRun.connector_id == connector_id)
    )
    rows = await session.scalars(
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.connector_id == connector_id)
        .order_by(ConnectorSyncRun.created_at.desc(), ConnectorSyncRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.all()), total or 0


async def get_run(
    session: AsyncSession, connector_id: uuid.UUID, run_id: uuid.UUID, *, lock: bool = False
) -> ConnectorSyncRun:
    await get_connector(session, connector_id)
    query = (
        select(ConnectorSyncRun)
        .where(ConnectorSyncRun.id == run_id)
        .where(ConnectorSyncRun.connector_id == connector_id)
    )
    if lock:
        query = query.with_for_update()
    run = await session.scalar(query)
    if run is None:
        raise ApiError(404, "sync_run_not_found", "Sync run not found.")
    return run


async def cancel_sync(
    session: AsyncSession, connector_id: uuid.UUID, run_id: uuid.UUID
) -> ConnectorSyncRun:
    run = await get_run(session, connector_id, run_id, lock=True)
    if run.status not in ACTIVE_SYNC_STATUSES:
        await session.rollback()
        raise ApiError(409, "sync_not_active", "Only queued or running syncs can be cancelled.")
    return await runs.cancel_run(session, run)


async def document_sources(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[ConnectorObject, Connector]]:
    """The S3 object and connector behind each imported document."""
    if not document_ids:
        return {}
    rows = await session.execute(
        select(ConnectorObject, Connector)
        .join(Connector, Connector.id == ConnectorObject.connector_id)
        .where(ConnectorObject.document_id.in_(document_ids))
    )
    return {mapping.document_id: (mapping, connector) for mapping, connector in rows.all()}
