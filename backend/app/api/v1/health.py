"""Liveness and readiness endpoints."""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.db.session import check_database

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str


class DatabaseCheck(BaseModel):
    status: Literal["ok", "error"]
    pgvector_version: str | None = None
    detail: str | None = None


class ReadinessChecks(BaseModel):
    database: DatabaseCheck


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness: the process is up and serving requests. Does not touch the database."""
    settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment.value,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is not ready"}},
)
async def ready(request: Request) -> JSONResponse:
    """Readiness: the database is reachable and migrations (pgvector) have been applied."""
    settings = request.app.state.settings
    db = await check_database(request.app.state.engine, settings.readiness_timeout_seconds)

    body = ReadinessResponse(
        status="ready" if db.ready else "not_ready",
        checks=ReadinessChecks(
            database=DatabaseCheck(
                status="ok" if db.ready else "error",
                pgvector_version=db.pgvector_version,
                detail=db.error,
            )
        ),
    )
    return JSONResponse(
        status_code=200 if db.ready else 503,
        content=body.model_dump(exclude_none=True),
    )
