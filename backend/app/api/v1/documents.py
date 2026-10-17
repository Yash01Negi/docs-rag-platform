"""Document management endpoints: upload, list, inspect, retry, reindex and delete.

Documents imported by connectors (Phase 5) are listed here too, with their S3 source.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import (
    ChunkList,
    ChunkPreview,
    DocumentDetail,
    DocumentList,
    DocumentSourceInfo,
    DocumentSummary,
    ErrorResponse,
    JobSummary,
    UploadLimits,
    UploadResult,
)
from app.connectors.service import document_sources
from app.core.errors import ApiError
from app.core.logging import request_id_var
from app.db.models import Connector, ConnectorObject, DocumentStatus, FileType
from app.ingestion.validation import MIME_TYPES, SUPPORTED_EXTENSIONS
from app.services import documents as service
from app.storage import StoredFileNotFoundError

router = APIRouter(prefix="/documents", tags=["documents"])

_ERRORS = {
    404: {"model": ErrorResponse, "description": "Document not found"},
}
_STATE_ERRORS = {
    **_ERRORS,
    409: {"model": ErrorResponse, "description": "The document is not in an eligible state"},
}
_DOWNLOAD_CHUNK_BYTES = 64 * 1024


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.get("/limits", response_model=UploadLimits)
async def upload_limits(request: Request) -> UploadLimits:
    """Upload constraints, so clients can validate before sending a file."""
    return UploadLimits(
        max_upload_bytes=request.app.state.settings.max_upload_bytes,
        accepted_extensions=list(SUPPORTED_EXTENSIONS),
        accepted_mime_types=sorted(set(MIME_TYPES.values())),
    )


@router.post(
    "",
    status_code=202,
    response_model=UploadResult,
    responses={
        200: {"model": UploadResult, "description": "Identical content already uploaded"},
        413: {"model": ErrorResponse, "description": "File exceeds the upload limit"},
        415: {"model": ErrorResponse, "description": "Unsupported or mismatched file type"},
        422: {"model": ErrorResponse, "description": "Empty, encrypted or unreadable file"},
    },
)
async def upload_document(
    request: Request,
    response: Response,
    session: Session,
    file: Annotated[UploadFile, File(description="A .pdf, .md or .txt file")],
) -> UploadResult:
    """Upload a document and queue it for ingestion.

    Returns **202** with the new document (status `pending`). If a document with
    byte-identical content already exists, nothing new is stored and the existing document is
    returned with **200** and `duplicate: true`, whatever its status.
    """
    settings = request.app.state.settings
    outcome = await service.upload_document(
        session,
        request.app.state.storage,
        file,
        staging_dir=service.staging_directory(settings.storage_dir),
        max_bytes=settings.max_upload_bytes,
        max_attempts=settings.worker_max_attempts,
        request_id=request_id_var.get(),
    )
    response.status_code = 200 if outcome.duplicate else 202
    response.headers["Location"] = f"{request.url.path}/{outcome.document.id}"
    return UploadResult(
        duplicate=outcome.duplicate,
        document=DocumentSummary.model_validate(outcome.document),
    )


@router.get("", response_model=DocumentList)
async def list_documents(
    session: Session,
    status: Annotated[DocumentStatus | None, Query(description="Filter by status")] = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> DocumentList:
    """Documents, newest first."""
    items, total = await service.list_documents(session, status=status, limit=limit, offset=offset)
    return DocumentList(
        items=[DocumentSummary.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{document_id}", response_model=DocumentDetail, responses=_ERRORS)
async def get_document(document_id: uuid.UUID, session: Session) -> DocumentDetail:
    document = await service.get_document(session, document_id)
    job = await service.latest_job(session, document_id)
    source = (await document_sources(session, [document_id])).get(document_id)
    detail = DocumentDetail.model_validate(
        {
            **DocumentSummary.model_validate(document).model_dump(),
            "sha256": document.sha256,
            "page_count": document.page_count,
            "embedding_model": document.embedding_model,
            "embedding_model_revision": document.embedding_model_revision,
            "embedding_dimension": document.embedding_dimension,
            "processed_at": document.processed_at,
            "latest_job": JobSummary.model_validate(job) if job else None,
            "source": _source_info(*source) if source else None,
        }
    )
    return detail


def _source_info(mapping: ConnectorObject, connector: Connector) -> DocumentSourceInfo:
    return DocumentSourceInfo(
        connector_id=connector.id,
        connector_name=connector.name,
        provider=connector.provider,
        bucket=connector.bucket,
        object_key=mapping.object_key,
        uri=f"s3://{connector.bucket}/{mapping.object_key}",
        etag=mapping.etag,
        version_id=mapping.version_id,
        size_bytes=mapping.size_bytes,
        last_modified=mapping.last_modified,
        last_synced_at=mapping.last_synced_at,
    )


@router.get("/{document_id}/chunks", response_model=ChunkList, responses=_ERRORS)
async def list_chunks(
    document_id: uuid.UUID, session: Session, limit: Limit = 20, offset: Offset = 0
) -> ChunkList:
    """Chunk text and source metadata in document order. Embedding vectors are not returned."""
    items, total = await service.list_chunks(session, document_id, limit=limit, offset=offset)
    return ChunkList(
        items=[ChunkPreview.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


def _content_disposition(disposition: str, filename: str) -> str:
    ascii_name = "".join(c if c.isascii() and c.isprintable() else "_" for c in filename)
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get(
    "/{document_id}/file",
    response_class=StreamingResponse,
    responses={
        **_ERRORS,
        200: {
            "description": "The original file",
            "content": {mime: {} for mime in sorted(set(MIME_TYPES.values()))},
        },
    },
)
async def download_file(
    request: Request,
    document_id: uuid.UUID,
    session: Session,
    download: Annotated[
        bool, Query(description="Force a download instead of opening in the browser")
    ] = False,
) -> StreamingResponse:
    document = await service.get_document(session, document_id)
    try:
        handle = request.app.state.storage.open(document.storage_key)
    except StoredFileNotFoundError as exc:
        raise ApiError(
            404, "stored_file_missing", "The original file is missing from storage."
        ) from exc

    def stream() -> Iterator[bytes]:
        with handle:
            while chunk := handle.read(_DOWNLOAD_CHUNK_BYTES):
                yield chunk

    media_type = MIME_TYPES[FileType(document.file_type)]
    if media_type.startswith("text/"):
        media_type += "; charset=utf-8"
    return StreamingResponse(
        stream(),
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition(
                "attachment" if download else "inline", document.filename
            ),
            "Content-Length": str(document.size_bytes),
            "X-Content-Type-Options": "nosniff",
            # Uploaded content is never allowed to run scripts in the API's origin.
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
        },
    )


@router.post(
    "/{document_id}/retry",
    status_code=202,
    response_model=DocumentSummary,
    responses=_STATE_ERRORS,
)
async def retry_document(document_id: uuid.UUID, request: Request, session: Session) -> object:
    """Queue a new ingestion attempt for a **failed** document."""
    document = await service.requeue_document(
        session,
        document_id,
        reindex=False,
        max_attempts=request.app.state.settings.worker_max_attempts,
        request_id=request_id_var.get(),
    )
    return DocumentSummary.model_validate(document)


@router.post(
    "/{document_id}/reindex",
    status_code=202,
    response_model=DocumentSummary,
    responses=_STATE_ERRORS,
)
async def reindex_document(document_id: uuid.UUID, request: Request, session: Session) -> object:
    """Rebuild the index of a completed or failed document.

    The current chunks stay in place and searchable until the new index is committed in a
    single transaction. If the reindex fails, the document returns to `completed` with the
    previous index and an error message.
    """
    document = await service.requeue_document(
        session,
        document_id,
        reindex=True,
        max_attempts=request.app.state.settings.worker_max_attempts,
        request_id=request_id_var.get(),
    )
    return DocumentSummary.model_validate(document)


@router.delete("/{document_id}", status_code=204, responses=_ERRORS)
async def delete_document(document_id: uuid.UUID, request: Request) -> Response:
    """Delete the document, its chunks, its jobs and the stored original file."""
    await service.delete_document(
        request.app.state.sessions, request.app.state.storage, document_id
    )
    return Response(status_code=204)
