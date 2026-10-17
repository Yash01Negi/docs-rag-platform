"""Opt-in smoke test against a real S3 API: MinIO (or another emulator) or Amazon S3.

Skipped unless RUN_S3_SMOKE_TESTS=1. It runs the real S3 client over HTTP, the worker and
the database end to end, then checks the documents through the API.

    RUN_S3_SMOKE_TESTS=1 S3_SMOKE_BUCKET=yashrag-local S3_SMOKE_WRITE=1 \\
    S3_ENDPOINT_URL=http://127.0.0.1:9000 AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
    TEST_DATABASE_URL=... uv run pytest tests/test_s3_smoke.py -m s3_smoke

S3_SMOKE_WRITE=1 lets the test create, change and delete objects under a fresh prefix (use
an emulator or a scratch bucket). Without it the test is read-only: it imports what is
already under S3_SMOKE_PREFIX, which needs only the IAM policy in docs/s3-setup.md.
See docs/s3-smoke-tests.md.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

from app.connectors.s3 import S3ObjectStore, create_client
from app.core.config import Settings
from app.main import create_app
from tests.test_connector_sync import create_connector, documents, run_worker, search, sync

pytestmark = [
    pytest.mark.integration,
    pytest.mark.s3_smoke,
    pytest.mark.skipif(
        os.environ.get("RUN_S3_SMOKE_TESTS") != "1",
        reason="set RUN_S3_SMOKE_TESTS=1 to run the S3 smoke test",
    ),
]

WRITE = os.environ.get("S3_SMOKE_WRITE") == "1"
BUCKET = os.environ.get("S3_SMOKE_BUCKET", "")
ENGLISH = "Smoke test: the cafeteria opens at eight and serves breakfast until ten."
ENGLISH_V2 = "Smoke test: the cafeteria now opens at seven and serves brunch until noon."
JAPANESE = "スモークテスト：食堂は午前八時に開き、十時まで朝食を提供します。"


@pytest.fixture
def prefix() -> str:
    if WRITE:
        return f"yashrag-smoke/{uuid.uuid4().hex[:12]}/"
    value = os.environ.get("S3_SMOKE_PREFIX", "")
    if not value:
        pytest.skip("read-only mode needs S3_SMOKE_PREFIX with existing documents")
    return value if value.endswith("/") else f"{value}/"


@pytest.fixture
def settings(make_settings: Callable[..., Settings], clean_database: str, prefix: str) -> Settings:
    if not BUCKET:
        pytest.skip("S3_SMOKE_BUCKET is not set")
    return make_settings(
        database_url=clean_database,
        s3_allowed_sources=f"{BUCKET}/{prefix}",
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", ""),
        s3_region=os.environ.get("S3_REGION", ""),
        s3_list_page_size=1,  # exercise pagination over HTTP
        connector_sync_retry_backoff_seconds=0,
    )


@pytest.fixture
def writer(settings: Settings, prefix: str) -> Iterator[Any]:
    """A separate client for creating test objects (the connector itself only reads)."""
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        config=Config(s3={"addressing_style": "path" if settings.s3_endpoint_url else "auto"}),
    )
    yield client
    if WRITE:
        listed = client.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
        for item in listed:
            client.delete_object(Bucket=BUCKET, Key=item["Key"])


def test_sync_over_a_real_s3_api(settings: Settings, writer: Any, prefix: str) -> None:
    store = S3ObjectStore(create_client(settings))
    if WRITE:
        writer.put_object(Bucket=BUCKET, Key=f"{prefix}cafeteria.md", Body=ENGLISH.encode())
        writer.put_object(Bucket=BUCKET, Key=f"{prefix}食堂.txt", Body=JAPANESE.encode())
        writer.put_object(Bucket=BUCKET, Key=f"{prefix}logo.png", Body=b"\x89PNG not imported")

    with TestClient(create_app(settings)) as api:
        connector = create_connector(api, bucket=BUCKET, prefix=prefix, enabled=False)
        run = sync(api, settings, store, connector["id"])
        assert run["status"] == "succeeded", run
        assert run["documents_imported"] >= 1
        imported = documents(api)
        assert imported and all(d["status"] == "completed" for d in imported.values())
        detail = api.get(f"/api/v1/documents/{next(iter(imported.values()))['id']}").json()
        assert detail["source"]["uri"].startswith(f"s3://{BUCKET}/{prefix}")
        assert detail["source"]["etag"]

        # A second sync downloads nothing.
        again = sync(api, settings, store, connector["id"])
        assert again["documents_unchanged"] == len(imported)
        assert again["documents_imported"] == again["documents_updated"] == 0

        if not WRITE:
            return
        assert run["objects_skipped"] == 1 and set(imported) == {"cafeteria.md", "食堂.txt"}
        assert search(api, "cafeteria breakfast")[0]["filename"] == "cafeteria.md"
        assert search(api, "食堂")[0]["filename"] == "食堂.txt"

        writer.put_object(Bucket=BUCKET, Key=f"{prefix}cafeteria.md", Body=ENGLISH_V2.encode())
        writer.delete_object(Bucket=BUCKET, Key=f"{prefix}食堂.txt")
        changed = sync(api, settings, store, connector["id"])
        assert (changed["documents_updated"], changed["documents_removed"]) == (1, 1), changed
        assert set(documents(api)) == {"cafeteria.md"}
        assert search(api, "brunch noon")[0]["filename"] == "cafeteria.md"
        assert not [r for r in search(api, "breakfast") if r["filename"] == "cafeteria.md"]
        assert search(api, "食堂") == []

        assert api.delete(f"/api/v1/connectors/{connector['id']}").status_code == 204
        assert documents(api) == {}
    run_worker(settings, store)  # nothing left to do
