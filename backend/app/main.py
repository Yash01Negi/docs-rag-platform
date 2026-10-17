"""FastAPI application factory."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1.conversations import router as conversations_router
from app.api.v1.conversations import wait_for_answers
from app.api.v1.documents import router as documents_router
from app.api.v1.health import router as health_router
from app.api.v1.search import router as search_router
from app.chat.service import ChatService
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers, unhandled_exception_response
from app.core.limits import MULTIPART_OVERHEAD_BYTES, BodySizeLimitMiddleware
from app.core.logging import configure_logging, request_id_var
from app.db.session import create_engine, create_session_factory
from app.embeddings import create_embedding_provider
from app.llm import LLMProvider, LLMRegistry, create_llm_registry
from app.search.service import SearchService
from app.services.documents import clean_staging_directory, staging_directory
from app.storage import LocalFileStorage

logger = logging.getLogger(__name__)

API_V1_PREFIX = "/api/v1"
REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app(
    settings: Settings | None = None,
    llm_provider: LLMProvider | None = None,
    llm_registry: LLMRegistry | None = None,
) -> FastAPI:
    """Build the application.

    ``llm_provider`` (a single provider) or ``llm_registry`` replaces the configured
    providers (tests).
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = create_engine(settings)
        app.state.sessions = create_session_factory(app.state.engine)
        app.state.storage = LocalFileStorage(settings.storage_dir)
        clean_staging_directory(staging_directory(settings.storage_dir))
        # The provider does not load the model until the first semantic search (or the
        # warm-up below); the model libraries are imported lazily.
        app.state.search = SearchService(settings, create_embedding_provider(settings))
        if llm_registry is not None:
            app.state.llm = llm_registry
        elif llm_provider is not None:
            app.state.llm = LLMRegistry.single(llm_provider)
        else:
            app.state.llm = create_llm_registry(settings)
        app.state.chat = ChatService(settings, app.state.sessions, app.state.search, app.state.llm)
        # Answers being generated; each runs in its own task (see conversations.ask).
        app.state.chat_tasks = set()
        warm_up = (
            asyncio.create_task(app.state.search.warm_up())
            if settings.search_preload_model
            else None
        )
        logger.info(
            "application_startup",
            extra={
                "environment": settings.environment.value,
                "version": __version__,
                "llm_configured": app.state.llm.any_configured,
                "llm_providers": [
                    name for name, b in app.state.llm.backends.items() if b.configured
                ],
                "llm_default": (
                    f"{app.state.llm.default.provider}:{app.state.llm.default.model}"
                    if app.state.llm.default
                    else None
                ),
            },
        )
        try:
            yield
        finally:
            if warm_up is not None:
                warm_up.cancel()
            await wait_for_answers(app.state.chat_tasks, timeout=10)
            await app.state.llm.aclose()
            app.state.search.close()
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

    # Inside the request-context middleware, so its 413 responses carry the request ID.
    app.add_middleware(
        BodySizeLimitMiddleware,
        path=f"{API_V1_PREFIX}/documents",
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
    )

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
    app.include_router(documents_router, prefix=API_V1_PREFIX)
    app.include_router(search_router, prefix=API_V1_PREFIX)
    app.include_router(conversations_router, prefix=API_V1_PREFIX)
    return app
