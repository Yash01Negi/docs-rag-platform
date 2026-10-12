from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def app_with_test_routes(make_settings: Callable[..., Settings]) -> FastAPI:
    app = create_app(make_settings())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    return app


@pytest.fixture
def error_client(app_with_test_routes: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_with_test_routes, raise_server_exceptions=False) as test_client:
        yield test_client


def test_not_found_uses_error_envelope(error_client: TestClient) -> None:
    response = error_client.get("/api/v1/does-not-exist", headers={"X-Request-ID": "req-1"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Not Found", "request_id": "req-1"}
    }


def test_method_not_allowed_uses_error_envelope(error_client: TestClient) -> None:
    response = error_client.post("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_validation_error_lists_details(error_client: TestClient) -> None:
    response = error_client.get("/items/not-a-number")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["location"] == ["path", "item_id"]


def test_unhandled_exception_returns_generic_500(error_client: TestClient) -> None:
    response = error_client.get("/boom", headers={"X-Request-ID": "req-500"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "An unexpected error occurred",
            "request_id": "req-500",
        }
    }
    assert "hunter2" not in response.text
