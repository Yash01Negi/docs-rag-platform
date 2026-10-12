"""FastAPI application factory."""

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers, unhandled_exception_response
from app.core.logging import configure_logging, request_id_var
from app.db.session import create_engine

logger = logging.getLogger(__name__)

API_V1_PREFIX = "/api/v1"
REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = create_engine(settings)
        logger.info(
            "application_startup",
            extra={"environment": settings.environment.value, "version": __version__},
        )
        try:
            yield
        finally:
            await app.state.engine.dispose()
            logger.info("application_shutdown")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=f"{API_V1_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_V1_PREFIX}/openapi.json",
    )
    app.state.settings = settings

    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Handled here rather than by an exception handler so the request ID is still set.
            response = unhandled_exception_response(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        request_id_var.reset(token)
        return response

    # Added last so it is the outermost middleware and also covers error responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )

    app.include_router(health_router, prefix=API_V1_PREFIX)
    return app
