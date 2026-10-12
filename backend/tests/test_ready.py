import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import check_database
from app.main import create_app


def test_ready_returns_503_when_database_is_unreachable(client: TestClient) -> None:
    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["status"] == "error"
    assert "pgvector_version" not in body["checks"]["database"]


def test_ready_response_does_not_leak_connection_details(client: TestClient) -> None:
    response = client.get("/api/v1/ready")

    assert "unused" not in response.text  # password from the test DATABASE_URL
    assert "127.0.0.1" not in response.text


def test_check_database_times_out() -> None:
    class HangingEngine:
        @asynccontextmanager
        async def connect(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            yield

    status = asyncio.run(check_database(HangingEngine(), timeout_seconds=0.05))  # type: ignore[arg-type]

    assert status.ready is False
    assert status.error == "timed out"


@pytest.mark.integration
def test_ready_returns_200_when_database_is_migrated(
    make_settings: Callable[..., Settings], database_url: str
) -> None:
    # Assumes `alembic upgrade head` has been run against TEST_DATABASE_URL (CI does this).
    with TestClient(create_app(make_settings(database_url=database_url))) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["database"]["pgvector_version"]


@pytest.mark.integration
def test_ready_returns_503_when_migrations_are_missing(
    make_settings: Callable[..., Settings], empty_database_url: str
) -> None:
    with TestClient(create_app(make_settings(database_url=empty_database_url))) as client:
        response = client.get("/api/v1/ready")

    assert response.status_code == 503
    database = response.json()["checks"]["database"]
    assert database["status"] == "error"
    assert "alembic upgrade head" in database["detail"]
