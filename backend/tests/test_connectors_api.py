"""Connector API: validation, allowlist, duplicates, updates, sync requests, history,
cancellation, deletion and credential safety (real PostgreSQL; the API never calls S3)."""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # noqa: S105 - a fake key to detect leaks
ENDPOINT = "http://minio-internal.example:9000"


@pytest.fixture
def settings(
    make_settings: Callable[..., Settings], clean_database: str, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLETESTING")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    return make_settings(
        database_url=clean_database,
        s3_allowed_sources="docs-bucket,shared/team/",
        s3_endpoint_url=ENDPOINT,
    )


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def create(api: TestClient, **fields: object) -> Any:
    body = {"name": "Team docs", "bucket": "docs-bucket", "prefix": "", **fields}
    return api.post("/api/v1/connectors", json=body)


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


def sql(settings: Settings, statement: str, **params: object) -> list[Any]:
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            result = connection.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        engine.dispose()


def test_create_and_read_a_connector(api: TestClient) -> None:
    response = create(api, prefix="handbook", sync_interval_minutes=30)
    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["location"] == f"/api/v1/connectors/{body['id']}"
    assert body["name"] == "Team docs"
    assert body["provider"] == "s3"
    assert (body["bucket"], body["prefix"]) == ("docs-bucket", "handbook/")
    assert body["uri"] == "s3://docs-bucket/handbook/"
    assert body["enabled"] is True and body["sync_interval_minutes"] == 30
    assert body["next_sync_at"] is not None  # the first sync is due at once
    assert body["document_count"] == 0
    assert body["last_sync_status"] is None and body["latest_run"] is None
    assert api.get(f"/api/v1/connectors/{body['id']}").json() == body

    listing = api.get("/api/v1/connectors").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == body["id"]
    assert "active_run" not in listing["items"][0]


def test_defaults_and_disabled_connectors(api: TestClient) -> None:
    body = create(api, enabled=False).json()
    assert body["sync_interval_minutes"] == 60
    assert body["enabled"] is False and body["next_sync_at"] is None


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"bucket": "Bad_Bucket"}, "invalid_bucket"),
        ({"bucket": "192.168.0.1"}, "invalid_bucket"),
        ({"bucket": "docs..bucket"}, "invalid_bucket"),
        ({"prefix": "/absolute"}, "invalid_prefix"),
        ({"prefix": "a/../b"}, "invalid_prefix"),
        ({"prefix": "a\\b"}, "invalid_prefix"),
        ({"name": "   "}, "invalid_name"),
        ({"bucket": "other-bucket"}, "source_not_allowed"),
        ({"bucket": "shared", "prefix": ""}, "source_not_allowed"),
        ({"bucket": "shared", "prefix": "teamwork/"}, "source_not_allowed"),
    ],
)
def test_invalid_or_disallowed_sources(api: TestClient, fields: dict, code: str) -> None:
    response = create(api, **fields)
    assert response.status_code == 422
    assert error_code(response) == code


@pytest.mark.parametrize(
    "fields",
    [
        {"sync_interval_minutes": 4},
        {"sync_interval_minutes": 10081},
        {"name": "x" * 101},
        {"provider": "gcs"},
        {"bucket": "ab"},
        {"prefix": "p" * 513},
        # Credentials and endpoints are never accepted from clients.
        {"aws_secret_access_key": SECRET},
        {"aws_access_key_id": "AKIA"},
        {"endpoint_url": "http://evil.example"},
        {"region": "us-east-1"},
    ],
)
def test_request_validation_is_strict(api: TestClient, fields: dict) -> None:
    response = create(api, **fields)
    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert SECRET not in response.text


def test_scoped_allowlist_entry_allows_subfolders(api: TestClient) -> None:
    assert create(api, bucket="shared", prefix="team").status_code == 201
    assert create(api, name="Sub", bucket="shared", prefix="team/sub/").status_code == 409


def test_duplicates_and_overlaps_are_rejected(api: TestClient) -> None:
    assert create(api, prefix="team/").status_code == 201
    duplicate = create(api, name="Other", prefix="team")
    assert duplicate.status_code == 409 and error_code(duplicate) == "connector_exists"
    for prefix in ("", "team/sub/"):
        overlap = create(api, name=f"Overlap {prefix}", prefix=prefix)
        assert overlap.status_code == 409 and error_code(overlap) == "connector_overlap"
    name = create(api, name="TEAM DOCS", prefix="other/")
    assert name.status_code == 409 and error_code(name) == "connector_name_taken"
    assert create(api, name="Other", prefix="other/").status_code == 201
    assert api.get("/api/v1/connectors").json()["total"] == 2


def test_connectors_need_an_allowlist(
    make_settings: Callable[..., Settings], clean_database: str
) -> None:
    with TestClient(create_app(make_settings(database_url=clean_database))) as api:
        response = create(api)
        assert response.status_code == 503
        assert error_code(response) == "connectors_not_configured"
        options = api.get("/api/v1/connectors/options").json()
        assert options["configured"] is False and options["allowed_sources"] == []


def test_options_explain_the_server_configuration_without_secrets(api: TestClient) -> None:
    response = api.get("/api/v1/connectors/options")
    assert response.json() == {
        "configured": True,
        "allowed_sources": ["docs-bucket", "shared/team/"],
        "endpoint_override": True,
        "supported_extensions": [".pdf", ".md", ".markdown", ".txt"],
        "max_object_bytes": 20 * 1024 * 1024,
        "min_interval_minutes": 5,
        "max_interval_minutes": 10080,
    }
    assert ENDPOINT not in response.text and "minio-internal" not in response.text


def test_update_name_interval_and_scheduling(api: TestClient, settings: Settings) -> None:
    connector = create(api).json()
    url = f"/api/v1/connectors/{connector['id']}"

    disabled = api.patch(url, json={"enabled": False}).json()
    assert disabled["enabled"] is False and disabled["next_sync_at"] is None

    renamed = api.patch(url, json={"name": "  Handbook   folder ", "sync_interval_minutes": 15})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Handbook folder"
    assert renamed.json()["sync_interval_minutes"] == 15
    assert renamed.json()["next_sync_at"] is None  # still disabled

    # Re-enabling a connector that synced before schedules one interval after that sync.
    sql(
        settings,
        "UPDATE connectors SET last_sync_finished_at = now() - interval '1 minute'",
    )
    enabled = api.patch(url, json={"enabled": True}).json()
    gap = sql(
        settings, "SELECT extract(epoch FROM next_sync_at - last_sync_finished_at) FROM connectors"
    )[0][0]
    assert enabled["enabled"] is True and float(gap) == 15 * 60


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"bucket": "docs-bucket"}, "immutable_field"),
        ({"prefix": "other/"}, "immutable_field"),
        ({"name": None}, "validation_error"),
        ({"enabled": None}, "validation_error"),
        ({"sync_interval_minutes": 1}, "validation_error"),
        ({"endpoint_url": "http://evil.example"}, "validation_error"),
    ],
)
def test_invalid_updates(api: TestClient, body: dict, code: str) -> None:
    connector = create(api).json()
    response = api.patch(f"/api/v1/connectors/{connector['id']}", json=body)
    assert response.status_code == 422 and error_code(response) == code


def test_rename_to_an_existing_name_conflicts(api: TestClient) -> None:
    create(api, prefix="a/")
    other = create(api, name="Other", prefix="b/").json()
    response = api.patch(f"/api/v1/connectors/{other['id']}", json={"name": "team docs"})
    assert response.status_code == 409 and error_code(response) == "connector_name_taken"


def test_unknown_connectors_and_runs_are_404(api: TestClient) -> None:
    missing = uuid.uuid4()
    for method, path in [
        ("get", f"/api/v1/connectors/{missing}"),
        ("patch", f"/api/v1/connectors/{missing}"),
        ("delete", f"/api/v1/connectors/{missing}"),
        ("post", f"/api/v1/connectors/{missing}/sync"),
        ("get", f"/api/v1/connectors/{missing}/sync-runs"),
        ("get", f"/api/v1/connectors/{missing}/sync-runs/{missing}"),
    ]:
        kwargs = {"json": {"name": "x"}} if method == "patch" else {}
        response = getattr(api, method)(path, **kwargs)
        assert response.status_code == 404, path
        assert error_code(response) == "connector_not_found"
    connector = create(api).json()
    response = api.get(f"/api/v1/connectors/{connector['id']}/sync-runs/{missing}")
    assert response.status_code == 404 and error_code(response) == "sync_run_not_found"
    assert api.get("/api/v1/connectors/not-a-uuid").status_code == 422


def test_manual_sync_is_queued_once(api: TestClient) -> None:
    connector = create(api, enabled=False).json()
    url = f"/api/v1/connectors/{connector['id']}"

    first = api.post(f"{url}/sync")
    assert first.status_code == 202
    run = first.json()["run"]
    assert first.json()["created"] is True
    assert (run["status"], run["trigger"], run["attempts"]) == ("pending", "manual", 0)
    assert first.headers["location"] == f"{url}/sync-runs/{run['id']}"

    second = api.post(f"{url}/sync")  # already queued: same run, not a duplicate
    assert second.status_code == 200
    assert second.json() == {"created": False, "run": run}

    detail = api.get(url).json()
    assert detail["active_run"]["id"] == run["id"]
    assert detail["latest_run"]["id"] == run["id"]
    history = api.get(f"{url}/sync-runs").json()
    assert history["total"] == 1 and history["items"][0]["id"] == run["id"]
    one = api.get(f"{url}/sync-runs/{run['id']}").json()
    assert one["failures"] == [] and one["id"] == run["id"]


def test_sync_history_is_paginated_newest_first(api: TestClient, settings: Settings) -> None:
    connector = create(api, enabled=False).json()
    url = f"/api/v1/connectors/{connector['id']}"
    ids = []
    for _ in range(3):
        ids.append(api.post(f"{url}/sync").json()["run"]["id"])
        sql(settings, "UPDATE connector_sync_runs SET status = 'succeeded'")
    page = api.get(f"{url}/sync-runs", params={"limit": 2}).json()
    assert page["total"] == 3 and [r["id"] for r in page["items"]] == ids[::-1][:2]
    rest = api.get(f"{url}/sync-runs", params={"limit": 2, "offset": 2}).json()
    assert [r["id"] for r in rest["items"]] == [ids[0]]
    assert api.get(f"{url}/sync-runs", params={"limit": 101}).status_code == 422


def test_cancel_a_queued_sync(api: TestClient) -> None:
    connector = create(api).json()
    url = f"/api/v1/connectors/{connector['id']}"
    run = api.post(f"{url}/sync").json()["run"]

    cancelled = api.post(f"{url}/sync-runs/{run['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    detail = api.get(url).json()
    assert detail["active_run"] is None and detail["last_sync_status"] == "cancelled"
    assert detail["next_sync_at"] is not None  # rescheduled one interval later

    again = api.post(f"{url}/sync-runs/{run['id']}/cancel")
    assert again.status_code == 409 and error_code(again) == "sync_not_active"
    # A new sync can be queued after the cancelled one.
    assert api.post(f"{url}/sync").status_code == 202


def test_delete_a_connector(api: TestClient, settings: Settings) -> None:
    connector = create(api).json()
    api.post(f"/api/v1/connectors/{connector['id']}/sync")
    assert api.delete(f"/api/v1/connectors/{connector['id']}").status_code == 204
    assert api.get(f"/api/v1/connectors/{connector['id']}").status_code == 404
    assert sql(settings, "SELECT count(*) FROM connector_sync_runs")[0][0] == 0
    # The bucket and prefix can be used again.
    assert create(api).status_code == 201


def test_no_credentials_or_endpoint_in_any_response(api: TestClient) -> None:
    connector = create(api).json()
    url = f"/api/v1/connectors/{connector['id']}"
    run = api.post(f"{url}/sync").json()["run"]
    bodies = [
        api.get("/api/v1/connectors").text,
        api.get(url).text,
        api.get(f"{url}/sync-runs").text,
        api.get(f"{url}/sync-runs/{run['id']}").text,
        api.get("/api/v1/connectors/options").text,
        api.get("/api/v1/openapi.json").text,
    ]
    for body in bodies:
        for leaked in (SECRET, "AKIAEXAMPLETESTING", "minio-internal"):
            assert leaked not in body
    schema = api.get("/api/v1/openapi.json").json()["components"]["schemas"]
    for name in ("ConnectorCreate", "ConnectorUpdate"):
        fields = set(schema[name]["properties"])
        assert not {f for f in fields if "secret" in f or "key" in f or "endpoint" in f}
