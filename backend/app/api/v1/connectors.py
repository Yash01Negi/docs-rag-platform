"""Connector endpoints (Phase 5): Amazon S3 sources and their sync runs.

No endpoint accepts or returns AWS credentials or endpoint URLs. Credentials come from the
worker's environment (environment variables, AWS_PROFILE or an IAM role) and the only
endpoint override is the server-side S3_ENDPOINT_URL. See docs/connectors.md.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.v1.documents import Limit, Offset, Session
from app.api.v1.schemas import ErrorResponse
from app.connectors import service
from app.connectors.service import MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES
from app.core.errors import ApiError
from app.core.logging import request_id_var
from app.db.models import Connector, ConnectorSyncRun, SyncRunStatus, SyncTrigger
from app.ingestion.validation import SUPPORTED_EXTENSIONS

router = APIRouter(prefix="/connectors", tags=["connectors"])

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Connector not found"}}
_CONFLICT = {409: {"model": ErrorResponse, "description": "Duplicate name or source"}}
_NOT_CONFIGURED = {
    503: {"model": ErrorResponse, "description": "S3_ALLOWED_SOURCES is not set on the server"}
}

Interval = Annotated[
    int,
    Field(
        ge=MIN_INTERVAL_MINUTES,
        le=MAX_INTERVAL_MINUTES,
        description="Minutes between automatic syncs (5 minutes to 7 days).",
    ),
]
Name = Annotated[str, Field(min_length=1, max_length=100)]


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    provider: Literal["s3"] = "s3"
    bucket: str = Field(min_length=3, max_length=63, description="S3 bucket name.")
    prefix: str = Field(
        default="",
        max_length=512,
        description="Folder inside the bucket, e.g. 'handbook/'. Empty imports the whole bucket.",
    )
    sync_interval_minutes: Interval = 60
    enabled: bool = Field(default=True, description="Whether automatic syncs are scheduled.")


class ConnectorUpdate(BaseModel):
    """Bucket and prefix cannot be changed: create a new connector instead (documents are
    tied to the objects they came from)."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    sync_interval_minutes: Interval | None = None
    enabled: bool | None = None
    bucket: str | None = Field(default=None, description="Not changeable; rejected if set.")
    prefix: str | None = Field(default=None, description="Not changeable; rejected if set.")

    @field_validator("name", "sync_interval_minutes", "enabled", mode="before")
    @classmethod
    def _no_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("must not be null")
        return value


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connector_id: uuid.UUID
    trigger: SyncTrigger
    status: SyncRunStatus
    attempts: int
    max_attempts: int
    cancel_requested: bool
    run_after: datetime = Field(description="Earliest time a queued run starts.")
    objects_scanned: int
    documents_imported: int
    documents_updated: int
    documents_unchanged: int
    documents_removed: int
    objects_skipped: int
    objects_failed: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SyncFailureOut(BaseModel):
    key: str
    code: str
    message: str
    kind: Literal["failed", "skipped"]


class SyncRunDetail(SyncRunOut):
    failures: list[SyncFailureOut] = Field(
        description="Up to 50 objects that failed or were skipped in this run."
    )


class SyncRunList(BaseModel):
    items: list[SyncRunOut]
    total: int
    limit: int
    offset: int


class ConnectorOut(BaseModel):
    id: uuid.UUID
    name: str
    provider: Literal["s3"]
    bucket: str
    prefix: str
    uri: str = Field(description="s3://bucket/prefix")
    enabled: bool
    sync_interval_minutes: int
    next_sync_at: datetime | None
    last_sync_started_at: datetime | None
    last_sync_finished_at: datetime | None
    last_success_at: datetime | None
    last_sync_status: SyncRunStatus | None
    last_error_code: str | None
    last_error_message: str | None
    document_count: int
    created_at: datetime
    updated_at: datetime


class ConnectorDetail(ConnectorOut):
    failed_object_count: int = Field(description="Objects whose current version was rejected.")
    deleted_object_count: int = Field(description="Objects removed from the bucket (audit).")
    active_run: SyncRunOut | None
    latest_run: SyncRunOut | None


class ConnectorList(BaseModel):
    items: list[ConnectorOut]
    total: int
    limit: int
    offset: int


class SyncRequestResult(BaseModel):
    created: bool = Field(
        description="False if a sync was already queued or running; that run is returned."
    )
    run: SyncRunOut


class ConnectorOptions(BaseModel):
    """What the server allows, so the form can explain and validate before submitting."""

    configured: bool
    allowed_sources: list[str] = Field(description="Buckets or bucket/prefix scopes.")
    endpoint_override: bool = Field(
        description="True when the server uses a custom S3 endpoint (e.g. MinIO)."
    )
    supported_extensions: list[str]
    max_object_bytes: int
    min_interval_minutes: int
    max_interval_minutes: int


def _out(connector: Connector, document_count: int) -> dict[str, object]:
    return {
        "id": connector.id,
        "name": connector.name,
        "provider": connector.provider,
        "bucket": connector.bucket,
        "prefix": connector.prefix,
        "uri": f"s3://{connector.bucket}/{connector.prefix}",
        "enabled": connector.enabled,
        "sync_interval_minutes": connector.sync_interval_minutes,
        "next_sync_at": connector.next_sync_at,
        "last_sync_started_at": connector.last_sync_started_at,
        "last_sync_finished_at": connector.last_sync_finished_at,
        "last_success_at": connector.last_success_at,
        "last_sync_status": connector.last_sync_status,
        "last_error_code": connector.last_error_code,
        "last_error_message": connector.last_error_message,
        "document_count": document_count,
        "created_at": connector.created_at,
        "updated_at": connector.updated_at,
    }


async def _detail(session: Session, connector: Connector) -> ConnectorDetail:
    stats = await service.connector_stats(session, connector.id)
    latest, _ = await service.list_runs(session, connector.id, limit=1, offset=0)
    return ConnectorDetail.model_validate(
        {
            **_out(connector, stats.documents),
            "failed_object_count": stats.failed_objects,
            "deleted_object_count": stats.deleted_objects,
            "active_run": SyncRunOut.model_validate(stats.active_run) if stats.active_run else None,
            "latest_run": SyncRunOut.model_validate(latest[0]) if latest else None,
        }
    )


@router.get("/options", response_model=ConnectorOptions)
async def connector_options(request: Request) -> ConnectorOptions:
    settings = request.app.state.settings
    return ConnectorOptions(
        configured=bool(settings.s3_allowed_sources),
        allowed_sources=[str(source) for source in settings.s3_allowed_sources],
        endpoint_override=settings.s3_endpoint_url is not None,
        supported_extensions=list(SUPPORTED_EXTENSIONS),
        max_object_bytes=settings.s3_max_object_bytes,
        min_interval_minutes=MIN_INTERVAL_MINUTES,
        max_interval_minutes=MAX_INTERVAL_MINUTES,
    )


@router.get("", response_model=ConnectorList)
async def list_connectors(session: Session, limit: Limit = 20, offset: Offset = 0) -> ConnectorList:
    """Connectors, by name."""
    rows, total = await service.list_connectors(session, limit=limit, offset=offset)
    return ConnectorList(
        items=[ConnectorOut.model_validate(_out(c, count)) for c, count in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    status_code=201,
    response_model=ConnectorDetail,
    responses={
        **_CONFLICT,
        422: {"model": ErrorResponse, "description": "Invalid or disallowed bucket/prefix"},
        **_NOT_CONFIGURED,
    },
)
async def create_connector(
    request: Request, response: Response, session: Session, body: ConnectorCreate
) -> ConnectorDetail:
    """Create an S3 connector. When enabled, its first sync starts on the worker's next poll."""
    connector = await service.create_connector(
        session,
        request.app.state.settings,
        name=body.name,
        bucket=body.bucket,
        prefix=body.prefix,
        sync_interval_minutes=body.sync_interval_minutes,
        enabled=body.enabled,
    )
    response.headers["Location"] = f"{request.url.path}/{connector.id}"
    return await _detail(session, connector)


@router.get("/{connector_id}", response_model=ConnectorDetail, responses=_NOT_FOUND)
async def get_connector(connector_id: uuid.UUID, session: Session) -> ConnectorDetail:
    return await _detail(session, await service.get_connector(session, connector_id))


@router.patch(
    "/{connector_id}",
    response_model=ConnectorDetail,
    responses={**_NOT_FOUND, **_CONFLICT},
)
async def update_connector(
    connector_id: uuid.UUID, session: Session, body: ConnectorUpdate
) -> ConnectorDetail:
    """Rename, enable or disable automatic syncs, or change the interval."""
    if body.bucket is not None or body.prefix is not None:
        raise ApiError(
            422,
            "immutable_field",
            "The bucket and prefix of a connector cannot be changed; create a new connector.",
        )
    connector = await service.update_connector(
        session,
        connector_id,
        name=body.name,
        enabled=body.enabled,
        sync_interval_minutes=body.sync_interval_minutes,
    )
    return await _detail(session, connector)


@router.delete("/{connector_id}", status_code=204, responses=_NOT_FOUND)
async def delete_connector(connector_id: uuid.UUID, request: Request) -> Response:
    """Delete the connector, its sync history and **all documents it imported** (their
    chunks leave search at once). Manually uploaded documents are not affected. Objects in
    S3 are never modified."""
    await service.delete_connector(
        request.app.state.sessions, request.app.state.storage, connector_id
    )
    return Response(status_code=204)


@router.post(
    "/{connector_id}/sync",
    status_code=202,
    response_model=SyncRequestResult,
    responses={
        200: {"model": SyncRequestResult, "description": "A sync is already queued or running"},
        **_NOT_FOUND,
    },
)
async def request_sync(
    connector_id: uuid.UUID, request: Request, response: Response, session: Session
) -> SyncRequestResult:
    """Queue a sync now and return at once (the worker runs it). Works for disabled
    connectors too; `enabled` only controls automatic scheduling."""
    run, created = await service.request_sync(
        session, request.app.state.settings, connector_id, request_id_var.get()
    )
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"{request.url.path}-runs/{run.id}"
    return SyncRequestResult(created=created, run=SyncRunOut.model_validate(run))


@router.get("/{connector_id}/sync-runs", response_model=SyncRunList, responses=_NOT_FOUND)
async def list_sync_runs(
    connector_id: uuid.UUID,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Offset = 0,
) -> SyncRunList:
    """Sync history, newest first."""
    items, total = await service.list_runs(session, connector_id, limit=limit, offset=offset)
    return SyncRunList(
        items=[SyncRunOut.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def _run_detail(run: ConnectorSyncRun) -> SyncRunDetail:
    return SyncRunDetail.model_validate(
        {**SyncRunOut.model_validate(run).model_dump(), "failures": run.failures or []}
    )


@router.get(
    "/{connector_id}/sync-runs/{run_id}",
    response_model=SyncRunDetail,
    responses={404: {"model": ErrorResponse, "description": "Connector or run not found"}},
)
async def get_sync_run(
    connector_id: uuid.UUID, run_id: uuid.UUID, session: Session
) -> SyncRunDetail:
    return _run_detail(await service.get_run(session, connector_id, run_id))


@router.post(
    "/{connector_id}/sync-runs/{run_id}/cancel",
    response_model=SyncRunDetail,
    responses={
        404: {"model": ErrorResponse, "description": "Connector or run not found"},
        409: {"model": ErrorResponse, "description": "The run already finished"},
    },
)
async def cancel_sync_run(
    connector_id: uuid.UUID, run_id: uuid.UUID, session: Session
) -> SyncRunDetail:
    """Cancel a queued run at once, or ask a running one to stop after its current object.
    A cancelled run never deletes documents."""
    return _run_detail(await service.cancel_sync(session, connector_id, run_id))
