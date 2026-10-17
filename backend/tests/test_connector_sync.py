"""Connector syncs end to end: moto (an in-process S3 emulator), the real worker and a real
PostgreSQL database. No AWS account or network access is used.

Each test drives the same code the worker runs: the scheduler, the lease-based sync queue,
the sync engine, and the ingestion pipeline (with the fake embedding provider), then checks
the results through the HTTP API.
"""

import asyncio
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors import runs
from app.connectors.errors import ConnectorError, ConnectorErrorCode
from app.connectors.s3 import DownloadedObject, ObjectPage, S3ObjectStore, create_client
from app.connectors.sync import SyncEngine
from app.core.config import Settings
from app.db.models import (
    Chunk,
    Connector,
    ConnectorObject,
    ConnectorSyncRun,
    Document,
    SyncRunStatus,
    SyncTrigger,
)
from app.db.session import create_engine, create_session_factory
from app.embeddings.fake import FakeEmbeddingProvider
from app.main import create_app
from app.storage import LocalFileStorage
from app.worker.runner import Worker
from tests.chat_fakes import ScriptedProvider, cite_first_source
from tests.chat_helpers import ask_events, create_conversation, one
from tests.samples import make_pdf

pytestmark = pytest.mark.integration

BUCKET = "docs-bucket"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # noqa: S105 - a fake key to detect leaks

HANDBOOK = "Remote work is allowed three days per week with approval from a manager."
HANDBOOK_V2 = "Remote work is now allowed four days per week; approval is no longer required."
SECURITY = "Laptops must use full disk encryption and lock after five idle minutes."
JAPANESE = "経費精算は月末までに申請してください。領収書の原本が必要です。"
PDF_TEXT = "The quarterly travel budget is reviewed by the finance committee in March."


class Crash(BaseException):
    """Simulates the worker process dying (not an Exception, so nothing catches it)."""


class RecordingStore:
    """Wraps the real S3 client: records downloads and can inject failures."""

    def __init__(self, inner: S3ObjectStore) -> None:
        self.inner = inner
        self.downloads: list[str] = []
        self.list_calls = 0
        self.fail_list: Callable[[int], ConnectorError | None] = lambda call: None
        self.on_download: Callable[[str], None] = lambda key: None

    def list_page(self, bucket: str, prefix: str, token: str | None, page_size: int) -> ObjectPage:
        self.list_calls += 1
        error = self.fail_list(self.list_calls)
        if error is not None:
            raise error
        return self.inner.list_page(bucket, prefix, token, page_size)

    def download(
        self, bucket: str, key: str, etag: str, target: Path, max_bytes: int
    ) -> DownloadedObject:
        self.downloads.append(key)
        self.on_download(key)
        return self.inner.download(bucket, key, etag, target, max_bytes)


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    # Fake credentials for moto; nothing ever reaches AWS.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLETESTING")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        yield client


@pytest.fixture
def settings(make_settings: Callable[..., Settings], clean_database: str) -> Settings:
    return make_settings(
        database_url=clean_database,
        s3_allowed_sources=f"{BUCKET},shared/team/",
        s3_region="us-east-1",
        s3_list_page_size=2,  # every test also exercises pagination
        connector_sync_retry_backoff_seconds=0,
        connector_sync_max_attempts=3,
    )


@pytest.fixture
def store(aws: Any, settings: Settings) -> RecordingStore:
    return RecordingStore(S3ObjectStore(create_client(settings)))


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


def put(client: Any, key: str, body: bytes | str) -> None:
    client.put_object(Bucket=BUCKET, Key=key, Body=body.encode() if isinstance(body, str) else body)


def make_worker(settings: Settings, engine: Any, store: Any, worker_id: str = "w1") -> Worker:
    return Worker(
        settings,
        engine,
        FakeEmbeddingProvider(1024),
        LocalFileStorage(settings.storage_dir),
        worker_id=worker_id,
        object_store=store,
    )


def run_worker(settings: Settings, store: Any, worker_id: str = "w1") -> None:
    """Run the worker until nothing is left to do (syncs, then the ingestion they queue)."""

    async def _run() -> None:
        engine = create_engine(settings)
        worker = make_worker(settings, engine, store, worker_id)
        try:
            while await worker.run_once():
                await worker.wait_idle()
        finally:
            await engine.dispose()

    asyncio.run(_run())


def sql(settings: Settings, statement: str, **params: object) -> list[Any]:
    from sqlalchemy import create_engine as create_sync_engine

    engine = create_sync_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            result = connection.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        engine.dispose()


def create_connector(api: TestClient, **fields: object) -> dict[str, Any]:
    body = {"name": "Team docs", "bucket": BUCKET, "prefix": "team/", **fields}
    response = api.post("/api/v1/connectors", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def sync(api: TestClient, settings: Settings, store: Any, connector_id: str) -> dict[str, Any]:
    """Request a manual sync, run the worker, and return the finished run (with failures)."""
    response = api.post(f"/api/v1/connectors/{connector_id}/sync")
    assert response.status_code in (200, 202), response.text
    run_id = response.json()["run"]["id"]
    run_worker(settings, store)
    return api.get(f"/api/v1/connectors/{connector_id}/sync-runs/{run_id}").json()


def latest_run(api: TestClient, connector_id: str) -> dict[str, Any]:
    runs_page = api.get(f"/api/v1/connectors/{connector_id}/sync-runs").json()
    return api.get(
        f"/api/v1/connectors/{connector_id}/sync-runs/{runs_page['items'][0]['id']}"
    ).json()


def documents(api: TestClient) -> dict[str, dict[str, Any]]:
    items = api.get("/api/v1/documents", params={"limit": 100}).json()["items"]
    return {item["filename"]: item for item in items}


def search(api: TestClient, query: str) -> list[dict[str, Any]]:
    response = api.post("/api/v1/search", json={"query": query, "mode": "keyword"})
    assert response.status_code == 200, response.text
    return response.json()["results"]


def seed_team_folder(client: Any) -> None:
    put(client, "team/handbook.md", f"# Handbook\n\n{HANDBOOK}\n")
    put(client, "team/security/policy.txt", SECURITY)
    put(client, "team/経費.txt", JAPANESE)
    put(client, "team/travel.pdf", make_pdf([PDF_TEXT]))
    put(client, "team/diagram.png", b"\x89PNG\r\n\x1a\n not imported")
    put(client, "team/empty-folder/", b"")  # a console "folder" marker
    put(client, "teamwork/other.txt", "Outside the prefix (a different folder).")
    put(client, "root.txt", "Outside the prefix.")


# -- initial import and incremental sync ---------------------------------------------------


def test_initial_import_makes_documents_searchable(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)

    run = sync(api, settings, store, connector["id"])

    assert run["status"] == "succeeded", run
    assert run["trigger"] == "manual"
    assert (run["objects_scanned"], run["documents_imported"], run["objects_skipped"]) == (5, 4, 1)
    assert run["objects_failed"] == 0 and run["documents_removed"] == 0
    assert run["failures"] == [
        {
            "key": "team/diagram.png",
            "code": "unsupported_file_type",
            "message": "Only .pdf, .md, .markdown and .txt objects are imported.",
            "kind": "skipped",
        }
    ]
    assert store.list_calls == 3  # five objects, two per page
    docs = documents(api)
    assert set(docs) == {"handbook.md", "policy.txt", "経費.txt", "travel.pdf"}
    assert all(d["status"] == "completed" and d["source_type"] == "s3" for d in docs.values())
    assert docs["travel.pdf"]["file_type"] == "pdf"

    detail = api.get(f"/api/v1/documents/{docs['policy.txt']['id']}").json()
    source = detail["source"]
    assert source["connector_id"] == connector["id"]
    assert source["connector_name"] == "Team docs"
    assert source["bucket"] == BUCKET
    assert source["object_key"] == "team/security/policy.txt"
    assert source["uri"] == f"s3://{BUCKET}/team/security/policy.txt"
    assert source["etag"] and source["size_bytes"] == len(SECURITY)
    assert source["version_id"]  # the bucket is versioned
    assert source["last_modified"] and source["last_synced_at"]

    assert search(api, "full disk encryption")[0]["document_id"] == docs["policy.txt"]["id"]
    assert search(api, "経費精算")[0]["filename"] == "経費.txt"
    assert search(api, "finance committee travel budget")[0]["filename"] == "travel.pdf"

    after = api.get(f"/api/v1/connectors/{connector['id']}").json()
    assert after["document_count"] == 4
    assert after["last_sync_status"] == "succeeded"
    assert after["last_success_at"] is not None
    assert after["next_sync_at"] is None  # disabled: no automatic syncs


def test_unchanged_objects_are_not_downloaded_again(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    chunks_before = sql(settings, "SELECT id FROM chunks ORDER BY id")
    store.downloads.clear()

    run = sync(api, settings, store, connector["id"])

    assert run["status"] == "succeeded"
    assert run["documents_unchanged"] == 4
    assert run["documents_imported"] == run["documents_updated"] == 0
    assert store.downloads == []
    assert sql(settings, "SELECT id FROM chunks ORDER BY id") == chunks_before


def test_modified_object_updates_only_its_document(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    before = documents(api)
    other_chunks = sql(
        settings,
        "SELECT id FROM chunks WHERE document_id <> :d ORDER BY id",
        d=before["handbook.md"]["id"],
    )
    store.downloads.clear()

    put(aws, "team/handbook.md", f"# Handbook\n\n{HANDBOOK_V2}\n")
    run = sync(api, settings, store, connector["id"])

    assert run["status"] == "succeeded"
    assert (run["documents_updated"], run["documents_unchanged"]) == (1, 3)
    assert store.downloads == ["team/handbook.md"]
    after = documents(api)
    assert after["handbook.md"]["id"] == before["handbook.md"]["id"]  # same document
    assert after["handbook.md"]["status"] == "completed"
    assert search(api, "four days approval no longer required")[0]["filename"] == "handbook.md"
    assert not [r for r in search(api, "manager") if r["filename"] == "handbook.md"]
    texts = sql(
        settings, "SELECT text FROM chunks WHERE document_id = :d", d=after["handbook.md"]["id"]
    )
    assert all(HANDBOOK not in row[0] for row in texts)
    assert (
        sql(
            settings,
            "SELECT id FROM chunks WHERE document_id <> :d ORDER BY id",
            d=before["handbook.md"]["id"],
        )
        == other_chunks
    )
    # Only one file is stored per document: the old revision's file was removed.
    stored = [
        p for p in settings.storage_dir.rglob("*") if p.is_file() and ".staging" not in p.parts
    ]
    assert len(stored) == 4


def test_deleted_object_removes_its_document_and_keeps_an_audit_record(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    removed_id = documents(api)["policy.txt"]["id"]

    aws.delete_object(Bucket=BUCKET, Key="team/security/policy.txt")
    run = sync(api, settings, store, connector["id"])

    assert run["status"] == "succeeded"
    assert (run["documents_removed"], run["documents_unchanged"]) == (1, 3)
    assert api.get(f"/api/v1/documents/{removed_id}").status_code == 404
    assert search(api, "full disk encryption") == []
    audit = sql(
        settings,
        "SELECT status, document_id, deleted_at IS NOT NULL, etag FROM connector_objects "
        "WHERE object_key = 'team/security/policy.txt'",
    )
    assert audit[0][0] == "deleted" and audit[0][1] is None and audit[0][2] and audit[0][3]
    detail = api.get(f"/api/v1/connectors/{connector['id']}").json()
    assert (detail["document_count"], detail["deleted_object_count"]) == (3, 1)

    # Re-creating the object imports it again as a new document.
    put(aws, "team/security/policy.txt", SECURITY)
    run = sync(api, settings, store, connector["id"])
    assert run["documents_imported"] == 1
    assert documents(api)["policy.txt"]["id"] != removed_id


def test_scheduled_sync_runs_without_a_request(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    connector = create_connector(api, sync_interval_minutes=5)  # enabled: due at once
    assert connector["next_sync_at"] is not None

    run_worker(settings, store)

    run = latest_run(api, connector["id"])
    assert run["trigger"] == "scheduled" and run["status"] == "succeeded"
    assert documents(api)["handbook.md"]["status"] == "completed"
    detail = api.get(f"/api/v1/connectors/{connector['id']}").json()
    # The next sync is one interval after this one finished (database time).
    gap = sql(
        settings,
        "SELECT extract(epoch FROM next_sync_at - last_sync_finished_at) FROM connectors",
    )[0][0]
    assert float(gap) == 300
    assert detail["next_sync_at"] > detail["last_sync_finished_at"]
    # Not due again yet: running the worker does nothing.
    run_worker(settings, store)
    assert api.get(f"/api/v1/connectors/{connector['id']}/sync-runs").json()["total"] == 1


# -- failures, retries and safety ----------------------------------------------------------


def test_partial_listing_failure_never_deletes(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    last_success = api.get(f"/api/v1/connectors/{connector['id']}").json()["last_success_at"]

    # Two objects disappear, then every listing fails on its second page.
    aws.delete_object(Bucket=BUCKET, Key="team/handbook.md")
    aws.delete_object(Bucket=BUCKET, Key="team/経費.txt")
    store.list_calls = 0
    store.fail_list = lambda call: (
        ConnectorError(ConnectorErrorCode.S3_UNAVAILABLE, "throttled", retryable=True)
        if call % 2 == 0
        else None
    )
    run = sync(api, settings, store, connector["id"])

    assert run["status"] == "failed"
    assert run["attempts"] == 3  # retried with backoff, then gave up
    assert run["error_code"] == "s3_unavailable"
    assert run["documents_removed"] == 0
    assert len(documents(api)) == 4  # nothing deleted on an incomplete listing
    detail = api.get(f"/api/v1/connectors/{connector['id']}").json()
    assert detail["last_sync_status"] == "failed"
    assert detail["last_error_code"] == "s3_unavailable"
    assert detail["last_success_at"] == last_success

    # Once listing works again, the deletions are applied.
    store.fail_list = lambda call: None
    run = sync(api, settings, store, connector["id"])
    assert run["status"] == "succeeded" and run["documents_removed"] == 2
    assert len(documents(api)) == 2


def test_too_many_objects_fails_the_run_without_deleting(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    aws.delete_object(Bucket=BUCKET, Key="team/handbook.md")
    limited = settings.model_copy(update={"s3_max_objects_per_sync": 2})

    response = api.post(f"/api/v1/connectors/{connector['id']}/sync")
    run_worker(limited, store)
    run = api.get(
        f"/api/v1/connectors/{connector['id']}/sync-runs/{response.json()['run']['id']}"
    ).json()

    assert run["status"] == "failed" and run["attempts"] == 1  # not retryable
    assert run["error_code"] == "too_many_objects"
    assert "handbook.md" in documents(api)


def test_transient_failure_is_retried_and_recovers(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    connector = create_connector(api, enabled=False)
    store.fail_list = lambda call: (
        ConnectorError(ConnectorErrorCode.S3_UNREACHABLE, "timeout", retryable=True)
        if call == 1
        else None
    )
    run = sync(api, settings, store, connector["id"])
    assert run["status"] == "succeeded" and run["attempts"] == 2
    assert run["error_code"] is None
    assert documents(api)["handbook.md"]["status"] == "completed"


def test_credential_errors_fail_at_once_with_a_safe_message(
    api: TestClient, settings: Settings, aws: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    connector = create_connector(api, enabled=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    no_credentials = S3ObjectStore(create_client(settings))

    run = sync(api, settings, no_credentials, connector["id"])

    assert run["status"] == "failed" and run["attempts"] == 1
    assert run["error_code"] == "credentials_missing"
    assert run["error_message"] == "No usable AWS credentials are configured for the worker."
    assert documents(api) == {}


def test_access_denied_is_reported_without_details(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    connector = create_connector(api, enabled=False)
    from botocore.exceptions import ClientError

    from app.connectors.s3 import map_error

    denied = map_error(
        ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": f"User arn:aws:iam::1:user/x {SECRET}",
                },
                "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "RID"},
            },
            "ListObjectsV2",
        )
    )
    store.fail_list = lambda call: denied
    run = sync(api, settings, store, connector["id"])
    assert run["status"] == "failed" and run["error_code"] == "access_denied"
    body = api.get(f"/api/v1/connectors/{connector['id']}").text + str(run)
    for leaked in (SECRET, "arn:aws", "RID", "AKIAEXAMPLETESTING"):
        assert leaked not in body


def test_invalid_objects_make_the_run_partial_and_are_not_retried_until_changed(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    put(aws, "team/fake.pdf", "This is plain text with a .pdf name.")
    put(aws, "team/big.txt", "x" * 5000)
    put(aws, "team/empty.txt", b"")
    put(aws, "team/../escape.txt", "path traversal attempt")
    limited = settings.model_copy(update={"s3_max_object_bytes": 4096})
    connector = create_connector(api, enabled=False)

    api.post(f"/api/v1/connectors/{connector['id']}/sync")
    run_worker(limited, store)
    run = latest_run(api, connector["id"])

    assert run["status"] == "partial"
    assert (run["documents_imported"], run["objects_failed"], run["objects_skipped"]) == (1, 3, 1)
    by_key = {f["key"]: f for f in run["failures"]}
    assert by_key["team/fake.pdf"]["code"] == "content_type_mismatch"
    assert by_key["team/big.txt"]["code"] == "object_too_large"
    assert by_key["team/empty.txt"]["code"] == "empty_file"
    assert by_key["team/../escape.txt"] == {
        "key": "team/../escape.txt",
        "code": "unsafe_object_key",
        "message": "The object key contains '.' or '..' path segments.",
        "kind": "skipped",
    }
    assert "team/big.txt" not in store.downloads  # rejected from the listing's size
    assert set(documents(api)) == {"handbook.md"}
    detail = api.get(f"/api/v1/connectors/{connector['id']}").json()
    assert detail["failed_object_count"] == 3
    assert detail["last_success_at"] is not None  # the listing completed

    # The rejected versions are not downloaded again ...
    store.downloads.clear()
    api.post(f"/api/v1/connectors/{connector['id']}/sync")
    run_worker(limited, store)
    again = latest_run(api, connector["id"])
    assert again["status"] == "succeeded" and store.downloads == []
    assert again["documents_unchanged"] == 4
    # ... until they change.
    put(aws, "team/fake.pdf", make_pdf(["Now a real PDF about parking permits."]))
    api.post(f"/api/v1/connectors/{connector['id']}/sync")
    run_worker(limited, store)
    assert latest_run(api, connector["id"])["documents_imported"] == 1
    assert documents(api)["fake.pdf"]["status"] == "completed"


def test_changed_object_that_becomes_invalid_leaves_search(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    assert search(api, "remote work manager approval")

    put(aws, "team/handbook.md", b"")
    run = sync(api, settings, store, connector["id"])

    assert run["status"] == "partial" and run["failures"][0]["code"] == "empty_file"
    assert documents(api) == {}
    assert search(api, "remote work manager approval") == []  # no stale revision


def test_manual_uploads_are_never_touched(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    upload = api.post(
        "/api/v1/documents", files={"file": ("handbook.md", f"# Handbook\n\n{HANDBOOK}\n")}
    )
    assert upload.status_code == 202
    put(aws, "team/handbook.md", f"# Handbook\n\n{HANDBOOK}\n")  # identical bytes
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    rows = sql(settings, "SELECT source_type FROM documents ORDER BY source_type")
    assert [r[0] for r in rows] == ["s3", "upload"]  # imports are not de-duplicated

    aws.delete_object(Bucket=BUCKET, Key="team/handbook.md")
    sync(api, settings, store, connector["id"])
    assert api.get(f"/api/v1/documents/{upload.json()['document']['id']}").status_code == 200

    assert api.delete(f"/api/v1/connectors/{connector['id']}").status_code == 204
    assert [r[0] for r in sql(settings, "SELECT source_type FROM documents")] == ["upload"]
    # A second identical upload is still de-duplicated.
    again = api.post(
        "/api/v1/documents", files={"file": ("copy.md", f"# Handbook\n\n{HANDBOOK}\n")}
    )
    assert again.status_code == 200 and again.json()["duplicate"] is True


def test_manually_deleted_import_is_imported_again(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    first = documents(api)["handbook.md"]["id"]
    assert api.delete(f"/api/v1/documents/{first}").status_code == 204

    run = sync(api, settings, store, connector["id"])
    assert run["documents_imported"] == 1
    assert documents(api)["handbook.md"]["id"] != first


def test_connector_deletion_removes_its_documents_from_search(
    api: TestClient, settings: Settings, store: RecordingStore, aws: Any
) -> None:
    seed_team_folder(aws)
    connector = create_connector(api, enabled=False)
    sync(api, settings, store, connector["id"])
    assert search(api, "full disk encryption")

    assert api.delete(f"/api/v1/connectors/{connector['id']}").status_code == 204

    assert documents(api) == {}
    assert search(api, "full disk encryption") == []
    assert sql(settings, "SELECT count(*) FROM connector_sync_runs")[0][0] == 0
    stored = [
        p for p in settings.storage_dir.rglob("*") if p.is_file() and ".staging" not in p.parts
    ]
    assert stored == []
    # Nothing in the bucket was modified.
    assert len(aws.list_objects_v2(Bucket=BUCKET)["Contents"]) == 8


def test_chat_citations_follow_object_changes(
    settings: Settings, store: RecordingStore, aws: Any
) -> None:
    put(aws, "team/handbook.md", f"# Handbook\n\n{HANDBOOK}\n")
    put(aws, "team/security.txt", SECURITY)
    with TestClient(create_app(settings, llm_provider=ScriptedProvider(cite_first_source))) as api:
        connector = create_connector(api, enabled=False)
        sync(api, settings, store, connector["id"])
        conversation = create_conversation(api)
        events = ask_events(
            api,
            conversation,
            "How many remote work days are allowed?",
            retrieval={"mode": "keyword"},
        )
        cited = [c for c in one(events, "citations")["citations"] if c["supporting"]][0]
        assert cited["filename"] == "handbook.md"

        def citation() -> dict[str, Any]:
            detail = api.get(f"/api/v1/conversations/{conversation}").json()
            answer = detail["messages"][1]
            return [c for c in answer["citations"] if c["supporting"]][0]

        assert citation()["source_status"] == "available"
        put(aws, "team/handbook.md", f"# Handbook\n\n{HANDBOOK_V2}\n")
        sync(api, settings, store, connector["id"])
        assert citation()["source_status"] == "changed"  # the cited text is gone

        aws.delete_object(Bucket=BUCKET, Key="team/handbook.md")
        sync(api, settings, store, connector["id"])
        gone = citation()
        assert gone["source_status"] == "deleted"
        assert gone["excerpt"] is None  # deleted text is not kept
        assert gone["filename"] == "handbook.md"  # the citation record survives


# -- the queue: concurrency, restarts, cancellation ----------------------------------------


@pytest.fixture
async def sessions(settings: Settings) -> Any:
    engine = create_engine(settings)
    yield create_session_factory(engine)
    await engine.dispose()


async def _connector(
    sessions: async_sessionmaker[AsyncSession], *, enabled: bool = True, due: bool = True
) -> uuid.UUID:
    async with sessions() as session:
        connector = Connector(
            id=uuid.uuid4(),
            name=f"c-{uuid.uuid4().hex[:6]}",
            bucket=BUCKET,
            prefix=f"{uuid.uuid4().hex[:6]}/",
            enabled=enabled,
            sync_interval_minutes=60,
            next_sync_at=text("now() - interval '1 second'") if enabled and due else None,
        )
        session.add(connector)
        await session.commit()
        return connector.id


async def _runs(sessions: async_sessionmaker[AsyncSession]) -> list[ConnectorSyncRun]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(ConnectorSyncRun).order_by(ConnectorSyncRun.created_at)
                )
            ).all()
        )


@pytest.mark.anyio
async def test_scheduler_queues_each_due_connector_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    due = await _connector(sessions)
    await _connector(sessions, due=False)
    disabled = await _connector(sessions, enabled=False)

    async def schedule() -> int:
        async with sessions() as session:
            return await runs.schedule_due_syncs(session, 3)

    # Ten schedulers (as in ten worker processes) at the same moment.
    queued = await asyncio.gather(*(schedule() for _ in range(10)))
    assert sum(queued) == 1
    rows = await _runs(sessions)
    assert [(r.connector_id, r.trigger, r.status) for r in rows] == [(due, "scheduled", "pending")]

    # Manual syncs are allowed for disabled connectors, and are idempotent while active.
    async def manual() -> tuple[ConnectorSyncRun, bool]:
        async with sessions() as session:
            return await runs.enqueue_sync(
                session, disabled, trigger=SyncTrigger.MANUAL, max_attempts=3, request_id=None
            )

    results = await asyncio.gather(*(manual() for _ in range(10)))
    assert sum(created for _, created in results) == 1
    assert len({run.id for run, _ in results}) == 1


@pytest.mark.anyio
async def test_two_workers_never_run_the_same_sync(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: RecordingStore, aws: Any
) -> None:
    connector_id = await _connector(sessions)
    async with sessions() as session:
        connector = await session.get(Connector, connector_id)
        prefix = connector.prefix  # type: ignore[union-attr]
    for i in range(3):
        put(aws, f"{prefix}doc-{i}.txt", f"Document number {i} about parking permits.")

    engine_a, engine_b = create_engine(settings), create_engine(settings)
    worker_a = make_worker(settings, engine_a, store, "worker-a")
    worker_b = make_worker(settings, engine_b, store, "worker-b")
    try:
        claimed = await asyncio.gather(worker_a.sync_once(), worker_b.sync_once())
        assert sorted(claimed) == [False, True]
        await asyncio.gather(worker_a.wait_idle(), worker_b.wait_idle())
        for _ in range(5):
            await asyncio.gather(worker_a.run_once(), worker_b.run_once())
            await asyncio.gather(worker_a.wait_idle(), worker_b.wait_idle())
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    rows = await _runs(sessions)
    assert [(r.status, r.documents_imported) for r in rows] == [("succeeded", 3)]
    assert sorted(store.downloads) == [f"{prefix}doc-{i}.txt" for i in range(3)]
    async with sessions() as session:
        statuses = (await session.scalars(select(Document.status))).all()
    assert sorted(statuses) == ["completed"] * 3


@pytest.mark.anyio
async def test_worker_crash_mid_sync_is_recovered_without_duplicates(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: RecordingStore, aws: Any
) -> None:
    connector_id = await _connector(sessions)
    async with sessions() as session:
        prefix = (await session.get(Connector, connector_id)).prefix  # type: ignore[union-attr]
    for i in range(4):
        put(aws, f"{prefix}doc-{i}.txt", f"Document number {i} about parking permits.")

    def crash_on_third(key: str) -> None:
        if len(store.downloads) == 3:
            raise Crash

    store.on_download = crash_on_third
    engine = create_engine(settings)
    worker = make_worker(settings, engine, store, "doomed")
    try:
        assert await worker.sync_once()
        with pytest.raises(Crash):
            await asyncio.gather(*worker._sync_tasks)  # noqa: SLF001
    finally:
        await engine.dispose()
    rows = await _runs(sessions)
    assert rows[0].status == "running" and rows[0].documents_imported == 2

    # The lease expires; another worker recovers the run and finishes it.
    async with sessions() as session:
        await session.execute(
            update(ConnectorSyncRun).values(lease_expires_at=text("now() - interval '1 second'"))
        )
        await session.commit()
    store.on_download = lambda key: None
    store.downloads.clear()
    engine = create_engine(settings)
    rescuer = make_worker(settings, engine, store, "rescuer")
    try:
        while await rescuer.run_once():
            await rescuer.wait_idle()
    finally:
        await engine.dispose()

    rows = await _runs(sessions)
    assert len(rows) == 1
    run = rows[0]
    assert (run.status, run.attempts) == ("succeeded", 2)
    assert (run.documents_imported, run.documents_unchanged) == (2, 2)
    assert len(store.downloads) == 2  # the two imported before the crash were not redone
    async with sessions() as session:
        objects = (await session.scalars(select(ConnectorObject))).all()
        documents_ = (await session.scalars(select(Document))).all()
    assert len(objects) == len(documents_) == 4
    assert {d.status for d in documents_} == {"completed"}


@pytest.mark.anyio
async def test_graceful_shutdown_releases_a_running_sync(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: RecordingStore, aws: Any
) -> None:
    connector_id = await _connector(sessions)
    async with sessions() as session:
        prefix = (await session.get(Connector, connector_id)).prefix  # type: ignore[union-attr]
    put(aws, f"{prefix}a.txt", "A document about parking permits.")
    release = threading.Event()
    store.on_download = lambda key: release.wait(5) and None
    engine = create_engine(settings)
    worker = make_worker(settings, engine, store, "stopping")
    try:
        assert await worker.sync_once()
        while not store.downloads:
            await asyncio.sleep(0.01)
        # What the worker does when the shutdown grace period runs out.
        tasks = list(worker._sync_tasks)  # noqa: SLF001
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        release.set()
        await engine.dispose()
    rows = await _runs(sessions)
    assert (rows[0].status, rows[0].attempts, rows[0].lease_token) == ("pending", 0, None)


@pytest.mark.anyio
async def test_cancellation(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: RecordingStore, aws: Any
) -> None:
    connector_id = await _connector(sessions, enabled=False)
    async with sessions() as session:
        prefix = (await session.get(Connector, connector_id)).prefix  # type: ignore[union-attr]
    for i in range(3):
        put(aws, f"{prefix}doc-{i}.txt", f"Document number {i} about parking permits.")

    async def enqueue() -> ConnectorSyncRun:
        async with sessions() as session:
            run, _ = await runs.enqueue_sync(
                session, connector_id, trigger=SyncTrigger.MANUAL, max_attempts=3, request_id=None
            )
            return run

    # A queued run is cancelled at once and never starts.
    queued = await enqueue()
    async with sessions() as session:
        row = await session.get(ConnectorSyncRun, queued.id, with_for_update=True)
        await runs.cancel_run(session, row)  # type: ignore[arg-type]
    engine = create_engine(settings)
    worker = make_worker(settings, engine, store)
    try:
        assert not await worker.sync_once()
        # A running run stops at the next object and deletes nothing.
        running = await enqueue()

        def cancel_after_first(key: str) -> None:
            if len(store.downloads) == 1:
                sql(
                    settings,
                    "UPDATE connector_sync_runs SET cancel_requested = true WHERE id = :i",
                    i=running.id,
                )

        store.on_download = cancel_after_first
        assert await worker.sync_once()
        await worker.wait_idle()
    finally:
        await engine.dispose()
    rows = {r.id: r for r in await _runs(sessions)}
    assert rows[queued.id].status == "cancelled" and rows[queued.id].started_at is None
    assert rows[running.id].status == "cancelled"
    assert rows[running.id].documents_imported == 1
    assert store.downloads == [f"{prefix}doc-0.txt"]
    async with sessions() as session:
        connector = await session.get(Connector, connector_id)
    assert connector.last_sync_status == "cancelled"  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_lost_lease_stops_the_engine(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: RecordingStore, aws: Any
) -> None:
    connector_id = await _connector(sessions)
    async with sessions() as session:
        prefix = (await session.get(Connector, connector_id)).prefix  # type: ignore[union-attr]
        await runs.schedule_due_syncs(session, 3)
    for i in range(2):
        put(aws, f"{prefix}doc-{i}.txt", f"Document number {i}.")
    async with sessions() as session:
        claimed = await runs.claim_next_run(session, "w", 60)
    assert claimed is not None
    # Another worker recovered the run (new token): every write of this worker is fenced.
    async with sessions() as session:
        await session.execute(update(ConnectorSyncRun).values(lease_token=uuid.uuid4()))
        await session.commit()
    engine = SyncEngine(settings, sessions, LocalFileStorage(settings.storage_dir), store)
    from app.ingestion.jobs import LeaseLostError

    with pytest.raises(LeaseLostError):
        await engine.run(claimed)
    async with sessions() as session:
        assert (await session.scalars(select(Document))).all() == []


@pytest.mark.anyio
async def test_changed_object_never_exposes_mixed_revisions(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], store: RecordingStore, aws: Any
) -> None:
    """Between the sync and the new index, the document has no chunks (never old ones)."""
    connector_id = await _connector(sessions, enabled=False)
    async with sessions() as session:
        prefix = (await session.get(Connector, connector_id)).prefix  # type: ignore[union-attr]
    put(aws, f"{prefix}a.md", HANDBOOK)
    storage = LocalFileStorage(settings.storage_dir)

    async def sync_only() -> SyncRunStatus:
        async with sessions() as session:
            await runs.enqueue_sync(
                session, connector_id, trigger=SyncTrigger.MANUAL, max_attempts=3, request_id=None
            )
        async with sessions() as session:
            claimed = await runs.claim_next_run(session, "w", 60)
        assert claimed is not None
        report = await SyncEngine(settings, sessions, storage, store).run(claimed)
        async with sessions() as session:
            await runs.finish_run(session, claimed, report.status, failures=report.failures)
        return report.status

    async def index() -> None:
        engine = create_engine(settings)
        worker = make_worker(settings, engine, store)
        try:
            while await worker.run_once():
                await worker.wait_idle()
        finally:
            await engine.dispose()

    assert await sync_only() == SyncRunStatus.SUCCEEDED
    await index()
    put(aws, f"{prefix}a.md", HANDBOOK_V2)
    assert await sync_only() == SyncRunStatus.SUCCEEDED
    async with sessions() as session:
        document = (await session.scalars(select(Document))).one()
        chunks = (await session.scalars(select(Chunk))).all()
    assert (document.status, document.chunk_count, chunks) == ("pending", 0, [])

    # While the new revision waits to be indexed, a further change is deferred, not mixed in.
    put(aws, f"{prefix}a.md", HANDBOOK + " (third revision)")
    assert await sync_only() == SyncRunStatus.PARTIAL
    rows = await _runs(sessions)
    assert rows[-1].failures[0]["code"] == "document_busy"
    await index()
    assert await sync_only() == SyncRunStatus.SUCCEEDED
    await index()
    async with sessions() as session:
        texts = (await session.scalars(select(Chunk.text))).all()
    assert texts and all("third revision" in t for t in texts)


def test_logs_carry_ids_and_counts_but_no_contents_or_credentials(
    api: TestClient,
    settings: Settings,
    store: RecordingStore,
    aws: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    put(aws, "team/handbook.md", HANDBOOK)
    put(aws, "team/broken.pdf", "not a pdf")
    connector = create_connector(api, enabled=False)
    caplog.set_level("DEBUG")

    sync(api, settings, store, connector["id"])

    finished = [r for r in caplog.records if r.getMessage() == "sync_finished"]
    assert len(finished) == 1
    record = finished[0].__dict__
    assert record["connector_id"] == connector["id"] and record["run_id"]
    assert (record["imported"], record["failed"], record["status"]) == (1, 1, "partial")
    assert record["duration_ms"] >= 0 and record["attempt"] == 1
    everything = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for leaked in (SECRET, "AKIAEXAMPLETESTING", HANDBOOK, "not a pdf"):
        assert leaked not in everything
    # botocore never logs below WARNING (its debug output includes request headers).
    assert not [r for r in caplog.records if r.name.startswith(("botocore", "boto3"))]
