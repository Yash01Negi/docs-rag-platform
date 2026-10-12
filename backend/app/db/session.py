"""Database engine lifecycle and readiness checks."""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

logger = logging.getLogger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine. No connection is opened until the engine is first used."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": max(1, round(settings.database_connect_timeout_seconds))},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass(frozen=True)
class DatabaseStatus:
    reachable: bool
    pgvector_version: str | None
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.reachable and self.pgvector_version is not None


async def check_database(engine: AsyncEngine, timeout_seconds: float) -> DatabaseStatus:
    """Verify the database accepts queries and that the pgvector extension is installed.

    The extension is created by the initial Alembic migration, so a missing extension
    means migrations have not been applied yet.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                version = await connection.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
    except TimeoutError:
        logger.warning("database_check_timeout", extra={"timeout_seconds": timeout_seconds})
        return DatabaseStatus(reachable=False, pgvector_version=None, error="timed out")
    except Exception as exc:  # any driver/network error means "not ready"
        logger.warning("database_check_failed", extra={"error_type": type(exc).__name__})
        return DatabaseStatus(
            reachable=False,
            pgvector_version=None,
            error=f"database unreachable ({type(exc).__name__})",
        )

    if version is None:
        return DatabaseStatus(
            reachable=True,
            pgvector_version=None,
            error="pgvector extension not installed; run 'alembic upgrade head'",
        )
    return DatabaseStatus(reachable=True, pgvector_version=str(version))
