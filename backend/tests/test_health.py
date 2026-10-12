from fastapi.testclient import TestClient

from app import __version__


def test_health_returns_ok_without_database(client: TestClient) -> None:
    # The configured database is unreachable; liveness must not depend on it.
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "YashRAG API",
        "version": __version__,
        "environment": "test",
    }


def test_responses_carry_a_generated_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32


def test_valid_incoming_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "trace-abc_123"})

    assert response.headers["X-Request-ID"] == "trace-abc_123"


def test_unsafe_incoming_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\nwith newline"})

    assert response.headers["X-Request-ID"] != "bad id\nwith newline"
    assert len(response.headers["X-Request-ID"]) == 32


def test_cors_allows_configured_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    preflight = client.options(
        "/api/v1/health",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    simple = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in simple.headers
