import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.main import create_app

# Nothing listens on port 1, so connections are refused immediately.
UNREACHABLE_DATABASE_URL = "postgresql+psycopg://yashrag:unused@127.0.0.1:1/yashrag"


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Build Settings without reading the developer's .env file or environment."""

    def _make(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "environment": "test",
            "database_url": UNREACHABLE_DATABASE_URL,
            "cors_allowed_origins": "http://localhost:5173",
            "database_connect_timeout_seconds": 1,
            "readiness_timeout_seconds": 3,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def client(make_settings: Callable[..., Settings]) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings())) as test_client:
        yield test_client


@pytest.fixture
def database_url() -> str:
    """URL of a real PostgreSQL + pgvector server, for tests marked ``integration``."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@pytest.fixture
def empty_database_url(database_url: str) -> Iterator[str]:
    """A freshly created database on the test server with no migrations applied."""
    name = f"yashrag_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(database_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
