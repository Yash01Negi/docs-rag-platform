"""The sync engine: brings a connector's documents in line with its S3 prefix.

One run, in order:

1. List every object under the prefix (all pages). If the listing fails, times out or is
   cut short, the run fails or retries and *nothing is deleted*.
2. Compare each listed object with ``connector_objects``. An object is unchanged when its
   ETag and size match the version last imported (ETags are compared as opaque strings,
   never as MD5 sums); unchanged objects are not downloaded.
3. Import new and changed objects one at a time. Each object is downloaded with
   ``If-Match`` on its listed ETag (so the bytes are exactly the listed version), validated
   like an upload, stored, and committed in its own transaction together with its ingestion
   job. A changed object's old chunks are deleted in that same transaction, so search and
   chat never see the old and new revisions mixed; the document is searchable again once
   its new index is built.
4. Only when the listing completed and the run was not cancelled: documents whose objects
   are no longer listed are deleted through the normal document deletion path. Their
   ``connector_objects`` rows are kept with ``status = 'deleted'`` as the audit record.

Only documents this connector imported are touched; manual uploads never are.
"""

import asyncio
import hashlib
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors import runs
from app.connectors.errors import ConnectorError, ConnectorErrorCode
from app.connectors.s3 import (
    DownloadedObject,
    ObjectInfo,
    ObjectStore,
    iter_listing,
    too_large_error,
)
from app.connectors.sources import AllowedSource
from app.connectors.validation import (
    display_filename,
    is_folder_marker,
    supported_extension,
    unsafe_key_reason,
)
from app.core.config import Settings
from app.db.models import (
    Chunk,
    ConnectorObject,
    ConnectorObjectStatus,
    Document,
    DocumentSource,
    DocumentStatus,
    JobKind,
    SyncRunStatus,
)
from app.ingestion import jobs
from app.ingestion.errors import IngestionError
from app.ingestion.validation import ValidatedFile, sanitize_filename, validate_file
from app.services.documents import remove_document_rows, staging_directory
from app.storage import DocumentStorage, new_storage_key

logger = logging.getLogger(__name__)

MAX_RECORDED_FAILURES = 50
_HASH_CHUNK_BYTES = 1024 * 1024
_BUSY_STATUSES = (DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value)


class SyncCancelled(Exception):
    pass


@dataclass
class SyncReport:
    status: SyncRunStatus = SyncRunStatus.SUCCEEDED
    scanned: int = 0
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def record(self, key: str, code: str, message: str, *, kind: str) -> None:
        if len(self.failures) < MAX_RECORDED_FAILURES:
            self.failures.append({"key": key, "code": code, "message": message, "kind": kind})

    def as_log(self) -> dict[str, int | str]:
        return {
            "status": self.status.value,
            "scanned": self.scanned,
            "imported": self.imported,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "skipped": self.skipped,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class _Fetched:
    path: Path
    filename: str
    sha256: str
    size: int
    validated: ValidatedFile
    downloaded: DownloadedObject


class SyncEngine:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        storage: DocumentStorage,
        store: ObjectStore,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.storage = storage
        self.store = store
        self.staging_dir = staging_directory(settings.storage_dir)

    # -- the run ---------------------------------------------------------------------------

    async def run(self, run: runs.ClaimedRun) -> SyncReport:
        """Sync one claimed run. Raises ConnectorError for run-level failures (the caller
        records them), LeaseLostError if the run was taken away, SyncCancelled is handled."""
        report = SyncReport()
        if not any(source.allows(run.bucket, run.prefix) for source in self._allowed()):
            raise ConnectorError(
                ConnectorErrorCode.SOURCE_NOT_ALLOWED,
                "This bucket and prefix are no longer in S3_ALLOWED_SOURCES; nothing was "
                "imported or removed.",
            )
        listing = await asyncio.to_thread(self._list, run)
        report.scanned = len(listing)
        async with self.sessions() as session:
            row = await runs.lock_held_run(session, run)
            runs.add_counts(row, objects_scanned=report.scanned)
            await session.commit()

        try:
            await self._import_changes(run, listing, report)
        except SyncCancelled:
            report.status = SyncRunStatus.CANCELLED
            return report

        # Deletions only after a complete listing, and never for a cancelled run.
        if await self._cancelled(run):
            report.status = SyncRunStatus.CANCELLED
            return report
        await self._remove_deleted(run, {item.key for item in listing}, report)
        report.status = SyncRunStatus.PARTIAL if report.failed else SyncRunStatus.SUCCEEDED
        return report

    def _allowed(self) -> list[AllowedSource]:
        return list(self.settings.s3_allowed_sources)

    def _list(self, run: runs.ClaimedRun) -> list[ObjectInfo]:
        return [
            item
            for item in iter_listing(
                self.store,
                run.bucket,
                run.prefix,
                self.settings.s3_list_page_size,
                self.settings.s3_max_objects_per_sync,
            )
            # "Folder" placeholder objects (keys ending in '/') are not files.
            if not is_folder_marker(item.key)
        ]

    async def _cancelled(self, run: runs.ClaimedRun) -> bool:
        async with self.sessions() as session:
            return await runs.cancel_requested(session, run)

    # -- planning --------------------------------------------------------------------------

    async def _import_changes(
        self, run: runs.ClaimedRun, listing: list[ObjectInfo], report: SyncReport
    ) -> None:
        async with self.sessions() as session:
            known = {
                row.object_key: row
                for row in (
                    await session.scalars(
                        select(ConnectorObject).where(
                            ConnectorObject.connector_id == run.connector_id
                        )
                    )
                ).all()
            }
        unchanged: list[uuid.UUID] = []
        work: list[tuple[ObjectInfo, ConnectorObject | None]] = []
        for item in listing:
            reason = self._skip_reason(item)
            if reason is not None:
                code, message = reason
                report.skipped += 1
                report.record(item.key, code.value, message, kind="skipped")
                continue
            mapping = known.get(item.key)
            if mapping is not None and _is_unchanged(mapping, item):
                unchanged.append(mapping.id)
                continue
            work.append((item, mapping))

        report.unchanged = len(unchanged)
        async with self.sessions() as session:
            row = await runs.lock_held_run(session, run)
            runs.add_counts(
                row, documents_unchanged=report.unchanged, objects_skipped=report.skipped
            )
            if unchanged:
                await session.execute(
                    update(ConnectorObject)
                    .where(ConnectorObject.id.in_(unchanged))
                    .values(last_seen_at=func.now())
                )
            await session.commit()

        for item, mapping in work:
            if await self._cancelled(run):
                raise SyncCancelled
            await self._sync_object(run, item, mapping, report)

    def _skip_reason(self, item: ObjectInfo) -> tuple[ConnectorErrorCode, str] | None:
        reason = unsafe_key_reason(item.key)
        if reason is None and not sanitize_filename(display_filename(item.key)):
            reason = "The object key has no usable file name."
        if reason is not None:
            return ConnectorErrorCode.UNSAFE_OBJECT_KEY, reason
        if not supported_extension(item.key):
            return (
                ConnectorErrorCode.UNSUPPORTED_FILE_TYPE,
                "Only .pdf, .md, .markdown and .txt objects are imported.",
            )
        return None

    # -- one object ------------------------------------------------------------------------

    async def _sync_object(
        self,
        run: runs.ClaimedRun,
        item: ObjectInfo,
        mapping: ConnectorObject | None,
        report: SyncReport,
    ) -> None:
        is_update = mapping is not None and mapping.document_id is not None
        started = time.perf_counter()
        context = {"connector_id": str(run.connector_id), "run_id": str(run.id)}
        fetched: _Fetched | None = None
        try:
            if item.size > self.settings.s3_max_object_bytes:
                raise too_large_error(self.settings.s3_max_object_bytes)
            fetched = await self._fetch(run, item)
            outcome = await self._commit_object(run, item, fetched)
            if outcome == "imported":
                report.imported += 1
            else:
                report.updated += 1
            logger.info(
                "sync_object_" + outcome,
                extra={
                    **context,
                    "size_bytes": fetched.size,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                },
            )
        except jobs.LeaseLostError:
            raise
        except (ConnectorError, IngestionError) as exc:
            code, message, retryable = _describe(exc)
            report.failed += 1
            report.record(item.key, code, message, kind="failed")
            logger.warning(
                "sync_object_failed",
                extra={**context, "error_code": code, "retryable": retryable, "update": is_update},
            )
            await self._record_object_failure(run, item, code, message, retryable=retryable)
        except Exception:
            report.failed += 1
            report.record(
                item.key,
                ConnectorErrorCode.INTERNAL_ERROR.value,
                "An unexpected error occurred while importing the object.",
                kind="failed",
            )
            logger.exception("sync_object_error", extra=context)
            await self._record_object_failure(
                run,
                item,
                ConnectorErrorCode.INTERNAL_ERROR.value,
                "An unexpected error occurred while importing the object.",
                retryable=True,
            )
        finally:
            if fetched is not None:
                fetched.path.unlink(missing_ok=True)

    async def _fetch(self, run: runs.ClaimedRun, item: ObjectInfo) -> _Fetched:
        filename = sanitize_filename(display_filename(item.key))
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed at once, unlinked by caller
            dir=self.staging_dir, prefix=".upload-s3-", delete=False
        )
        handle.close()
        path = Path(handle.name)
        try:
            downloaded = await asyncio.to_thread(
                self.store.download,
                run.bucket,
                item.key,
                item.etag,
                path,
                self.settings.s3_max_object_bytes,
            )
            validated = await asyncio.to_thread(validate_file, path, filename)
            sha256 = await asyncio.to_thread(_sha256, path)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return _Fetched(path, filename, sha256, path.stat().st_size, validated, downloaded)

    async def _commit_object(
        self, run: runs.ClaimedRun, item: ObjectInfo, fetched: _Fetched
    ) -> str:
        """Store the file and switch the document to it in one transaction."""
        storage_key = new_storage_key()
        await asyncio.to_thread(self.storage.put_file, storage_key, fetched.path)
        old_storage_key: str | None = None
        try:
            async with self.sessions() as session:
                row = await runs.lock_held_run(session, run)
                mapping = await self._lock_mapping(session, run, item.key)
                document = (
                    await session.get(Document, mapping.document_id, with_for_update=True)
                    if mapping is not None and mapping.document_id is not None
                    else None
                )
                if document is None:
                    document = Document(
                        id=uuid.uuid4(),
                        filename=fetched.filename,
                        file_type=fetched.validated.file_type.value,
                        mime_type=fetched.validated.mime_type,
                        size_bytes=fetched.size,
                        sha256=fetched.sha256,
                        storage_key=storage_key,
                        page_count=fetched.validated.page_count,
                        status=DocumentStatus.PENDING.value,
                        chunk_count=0,
                        source_type=DocumentSource.S3.value,
                    )
                    session.add(document)
                    await session.flush()
                    outcome = "imported"
                else:
                    if document.status in _BUSY_STATUSES:
                        raise _busy()
                    # Remove the old revision from search in the same transaction that
                    # switches the document to the new file.
                    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
                    old_storage_key = document.storage_key
                    document.filename = fetched.filename
                    document.file_type = fetched.validated.file_type.value
                    document.mime_type = fetched.validated.mime_type
                    document.size_bytes = fetched.size
                    document.sha256 = fetched.sha256
                    document.storage_key = storage_key
                    document.page_count = fetched.validated.page_count
                    document.chunk_count = 0
                    document.language = None
                    outcome = "updated"
                await jobs.enqueue_job(
                    session,
                    document,
                    JobKind.INGEST,
                    max_attempts=self.settings.worker_max_attempts,
                    request_id=run.request_id,
                )
                mapping = mapping or ConnectorObject(
                    id=uuid.uuid4(), connector_id=run.connector_id, object_key=item.key
                )
                _set_version(mapping, item, fetched.downloaded)
                mapping.document_id = document.id
                mapping.status = ConnectorObjectStatus.ACTIVE.value
                mapping.last_error_code = None
                mapping.last_error_message = None
                mapping.last_synced_at = func.now()
                mapping.deleted_at = None
                session.add(mapping)
                if outcome == "imported":
                    runs.add_counts(row, documents_imported=1)
                else:
                    runs.add_counts(row, documents_updated=1)
                await session.commit()
        except BaseException:
            await asyncio.to_thread(self.storage.delete, storage_key)
            raise
        if old_storage_key is not None:
            await self._delete_file(old_storage_key)
        return outcome

    async def _lock_mapping(
        self, session: AsyncSession, run: runs.ClaimedRun, key: str
    ) -> ConnectorObject | None:
        return await session.scalar(
            select(ConnectorObject)
            .where(ConnectorObject.connector_id == run.connector_id)
            .where(ConnectorObject.object_key == key)
            .with_for_update()
        )

    async def _record_object_failure(
        self, run: runs.ClaimedRun, item: ObjectInfo, code: str, message: str, *, retryable: bool
    ) -> None:
        """Count the failure. A permanent failure (the object is too large or not a valid
        document) is remembered with the object's version so it is not downloaded again
        until it changes; a previous revision of its document is removed, because it no
        longer matches the bucket."""
        removed_key: str | None = None
        async with self.sessions() as session:
            row = await runs.lock_held_run(session, run)
            runs.add_counts(row, objects_failed=1)
            mapping = await self._lock_mapping(session, run, item.key)
            if not retryable:
                if mapping is not None and mapping.document_id is not None:
                    document = await session.get(
                        Document, mapping.document_id, with_for_update=True
                    )
                    if document is not None:
                        removed_key = await remove_document_rows(session, document)
                mapping = mapping or ConnectorObject(
                    id=uuid.uuid4(), connector_id=run.connector_id, object_key=item.key
                )
                _set_version(mapping, item, None)
                mapping.document_id = None
                mapping.status = ConnectorObjectStatus.FAILED.value
                mapping.deleted_at = None
                mapping.last_synced_at = func.now()
                session.add(mapping)
            if mapping is not None:
                mapping.last_error_code = code
                mapping.last_error_message = message
                mapping.last_seen_at = func.now()
                mapping.updated_at = func.now()
            await session.commit()
        if removed_key is not None:
            await self._delete_file(removed_key)

    # -- deletions -------------------------------------------------------------------------

    async def _remove_deleted(
        self, run: runs.ClaimedRun, listed: set[str], report: SyncReport
    ) -> None:
        async with self.sessions() as session:
            candidates = [
                (mapping_id, key)
                for mapping_id, key in (
                    await session.execute(
                        select(ConnectorObject.id, ConnectorObject.object_key)
                        .where(ConnectorObject.connector_id == run.connector_id)
                        .where(ConnectorObject.status != ConnectorObjectStatus.DELETED.value)
                    )
                ).all()
                if key not in listed
            ]
        for mapping_id, _key in candidates:
            if await self._cancelled(run):
                return  # the rest are removed by the next complete run
            removed_key: str | None = None
            async with self.sessions() as session:
                row = await runs.lock_held_run(session, run)
                mapping = await session.get(ConnectorObject, mapping_id, with_for_update=True)
                if mapping is None or mapping.status == ConnectorObjectStatus.DELETED.value:
                    await session.rollback()
                    continue
                had_document = False
                if mapping.document_id is not None:
                    document = await session.get(
                        Document, mapping.document_id, with_for_update=True
                    )
                    if document is not None:
                        removed_key = await remove_document_rows(session, document)
                        had_document = True
                mapping.status = ConnectorObjectStatus.DELETED.value
                mapping.document_id = None
                mapping.deleted_at = func.now()
                mapping.updated_at = func.now()
                if had_document:
                    runs.add_counts(row, documents_removed=1)
                    report.removed += 1
                await session.commit()
            if removed_key is not None:
                await self._delete_file(removed_key)
                logger.info(
                    "sync_object_removed",
                    extra={"connector_id": str(run.connector_id), "run_id": str(run.id)},
                )

    async def _delete_file(self, key: str) -> None:
        try:
            await asyncio.to_thread(self.storage.delete, key)
        except OSError:
            # Left for the worker's orphaned-file sweep.
            logger.warning("stored_file_delete_failed", extra={"storage_key": key})


def _is_unchanged(mapping: ConnectorObject, item: ObjectInfo) -> bool:
    if mapping.status == ConnectorObjectStatus.DELETED.value:
        return False
    if mapping.status == ConnectorObjectStatus.ACTIVE.value and mapping.document_id is None:
        return False  # its document was deleted by hand; import it again
    return mapping.etag == item.etag and mapping.size_bytes == item.size


def _set_version(
    mapping: ConnectorObject, item: ObjectInfo, downloaded: DownloadedObject | None
) -> None:
    mapping.etag = item.etag[:128]
    mapping.size_bytes = item.size
    mapping.last_modified = item.last_modified
    mapping.version_id = (
        downloaded.version_id[:1024] if downloaded and downloaded.version_id else None
    )
    mapping.last_seen_at = func.now()
    mapping.updated_at = func.now()


def _busy() -> ConnectorError:
    return ConnectorError(
        ConnectorErrorCode.DOCUMENT_BUSY,
        "The previous version is still being indexed; the next sync imports this change.",
        retryable=True,
    )


def _describe(exc: ConnectorError | IngestionError) -> tuple[str, str, bool]:
    return exc.code.value, exc.message, exc.retryable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
