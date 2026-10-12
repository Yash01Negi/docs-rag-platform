"""Document API against a real PostgreSQL + pgvector database, with the fake embedder."""

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from starlette.datastructures import Headers, UploadFile

from app.core.config import Settings
from app.db.session import create_engine as create_async_engine
from app.db.session import create_session_factory
from app.embeddings.fake import FAKE_MODEL_NAME
from app.services.documents import staging_directory, upload_document
from app.storage import LocalFileStorage
from tests.samples import ENGLISH_TEXT, JAPANESE_TEXT, make_pdf

pytestmark = pytest.mark.integration


def run_worker_until_idle(settings: Settings, provider: object | None = None) -> int:
    """Process every runnable job with a real Worker; returns the number of jobs claimed."""
    from app.db.session import create_engine as create_async_engine
    from app.embeddings import create_embedding_provider
    from app.storage import LocalFileStorage
    from app.worker.runner import Worker

    async def _run() -> int:
        engine = create_async_engine(settings)
        worker = Worker(
            settings,
            engine,
            provider or create_embedding_provider(settings),  # type: ignore[arg-type]
            LocalFileStorage(settings.storage_dir),
            worker_id="test-worker",
        )
        claimed = 0
        try:
            while await worker.run_once():
                claimed += 1
                await worker.wait_idle()
        finally:
            await engine.dispose()
        return claimed

    return asyncio.run(_run())


def _upload(client: TestClient, name: str, data: bytes):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/documents", files={"file": (name, data, "application/octet-stream")}
    )


def _sql(settings: Settings, statement: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            result = connection.execute(text(statement), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def _stored_files(settings: Settings) -> list[Path]:
    return [
        p for p in settings.storage_dir.rglob("*") if p.is_file() and not p.name.startswith(".")
    ]


def test_upload_queues_document_and_worker_indexes_it(
    db_client: TestClient, db_settings: Settings
) -> None:
    response = _upload(db_client, "english.pdf", make_pdf([ENGLISH_TEXT, ENGLISH_TEXT]))

    assert response.status_code == 202
    body = response.json()
    assert body["duplicate"] is False
    document = body["document"]
    assert document["status"] == "pending"
    assert document["file_type"] == "pdf"
    assert document["mime_type"] == "application/pdf"
    assert document["chunk_count"] == 0
    assert response.headers["Location"] == f"/api/v1/documents/{document['id']}"
    assert len(_stored_files(db_settings)) == 1

    assert run_worker_until_idle(db_settings) == 1

    detail = db_client.get(f"/api/v1/documents/{document['id']}").json()
    assert detail["status"] == "completed"
    assert detail["chunk_count"] >= 1
    assert detail["language"] == "en"
    assert detail["page_count"] == 2
    assert detail["embedding_model"] == FAKE_MODEL_NAME
    assert detail["embedding_dimension"] == 1024
    assert detail["processed_at"] is not None
    assert detail["latest_job"]["status"] == "completed"
    assert detail["latest_job"]["attempts"] == 1

    chunks = db_client.get(f"/api/v1/documents/{document['id']}/chunks").json()
    assert chunks["total"] == detail["chunk_count"]
    first = chunks["items"][0]
    assert first["page_start"] == 1
    assert first["language"] == "en"
    assert "embedding" not in first
    assert "PostgreSQL" in first["text"]

    dims = _sql(
        db_settings,
        "SELECT DISTINCT vector_dims(embedding) FROM chunks WHERE document_id = :id",
        id=document["id"],
    )
    assert dims == [(1024,)]


def test_japanese_pdf_is_indexed_with_page_references(
    db_client: TestClient, db_settings: Settings
) -> None:
    pure_japanese = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。" * 4
    data = make_pdf([pure_japanese, "", pure_japanese])
    document = _upload(db_client, "日本語の文書.pdf", data).json()["document"]
    assert document["filename"] == "日本語の文書.pdf"

    run_worker_until_idle(db_settings)

    detail = db_client.get(f"/api/v1/documents/{document['id']}").json()
    assert detail["status"] == "completed"
    assert detail["language"] == "ja"
    chunks = db_client.get(f"/api/v1/documents/{document['id']}/chunks").json()["items"]
    assert {c["language"] for c in chunks} == {"ja"}
    pages = {(c["page_start"], c["page_end"]) for c in chunks}
    assert all(start in (1, 3) for start, _ in pages)


def test_markdown_sections_are_exposed_on_chunks(
    db_client: TestClient, db_settings: Settings
) -> None:
    markdown = f"# ガイド\n\n{JAPANESE_TEXT}\n\n## Setup\n\n{ENGLISH_TEXT}\n".encode()
    document = _upload(db_client, "guide.md", markdown).json()["document"]
    run_worker_until_idle(db_settings)

    chunks = db_client.get(f"/api/v1/documents/{document['id']}/chunks").json()["items"]
    assert chunks[0]["section"] == "ガイド"
    assert chunks[0]["page_start"] is None
    detail = db_client.get(f"/api/v1/documents/{document['id']}").json()
    assert detail["file_type"] == "markdown"
    assert detail["language"] == "mixed"


def test_duplicate_content_returns_existing_document(
    db_client: TestClient, db_settings: Settings
) -> None:
    first = _upload(db_client, "a.txt", ENGLISH_TEXT.encode())
    second = _upload(db_client, "renamed-copy.txt", ENGLISH_TEXT.encode())

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["document"]["filename"] == "a.txt"
    assert db_client.get("/api/v1/documents").json()["total"] == 1
    assert len(_stored_files(db_settings)) == 1


def test_concurrent_duplicate_uploads_create_one_document(db_settings: Settings) -> None:
    async def scenario() -> list[tuple[uuid.UUID, bool]]:
        engine = create_async_engine(db_settings)
        sessions = create_session_factory(engine)
        storage = LocalFileStorage(db_settings.storage_dir)
        content = make_pdf([JAPANESE_TEXT])

        async def one(index: int) -> tuple[uuid.UUID, bool]:
            upload = UploadFile(
                io.BytesIO(content),
                filename=f"copy-{index}.pdf",
                headers=Headers({"content-type": "application/pdf"}),
            )
            async with sessions() as session:
                outcome = await upload_document(
                    session,
                    storage,
                    upload,
                    staging_dir=staging_directory(db_settings.storage_dir),
                    max_bytes=db_settings.max_upload_bytes,
                    max_attempts=3,
                    request_id=None,
                )
            return outcome.document.id, outcome.duplicate

        try:
            return await asyncio.gather(*(one(i) for i in range(8)))
        finally:
            await engine.dispose()

    results = asyncio.run(scenario())

    assert len({document_id for document_id, _ in results}) == 1
    assert sum(1 for _, duplicate in results if not duplicate) == 1
    assert _sql(db_settings, "SELECT count(*) FROM documents") == [(1,)]
    assert _sql(db_settings, "SELECT count(*) FROM ingestion_jobs") == [(1,)]
    # Losing uploads removed the files they had stored.
    assert len(_stored_files(db_settings)) == 1
    assert not any(staging_directory(db_settings.storage_dir).glob(".upload-*"))


def test_list_pagination_and_status_filter(db_client: TestClient, db_settings: Settings) -> None:
    ids = [
        _upload(db_client, f"doc-{i}.txt", f"Document number {i}.".encode()).json()["document"][
            "id"
        ]
        for i in range(5)
    ]

    page1 = db_client.get("/api/v1/documents", params={"limit": 2}).json()
    page3 = db_client.get("/api/v1/documents", params={"limit": 2, "offset": 4}).json()
    assert page1["total"] == 5
    assert [d["id"] for d in page1["items"]] == ids[::-1][:2]  # newest first
    assert [d["id"] for d in page3["items"]] == [ids[0]]

    run_worker_until_idle(db_settings)
    assert db_client.get("/api/v1/documents", params={"status": "completed"}).json()["total"] == 5
    assert db_client.get("/api/v1/documents", params={"status": "pending"}).json()["total"] == 0
    bad = db_client.get("/api/v1/documents", params={"status": "bogus"})
    assert bad.status_code == 422
    assert db_client.get("/api/v1/documents", params={"limit": 101}).status_code == 422


def test_chunk_pagination(db_client: TestClient, db_settings: Settings) -> None:
    long_text = "\n\n".join(f"Paragraph {i}. " + ENGLISH_TEXT * 3 for i in range(20))
    document = _upload(db_client, "long.txt", long_text.encode()).json()["document"]
    run_worker_until_idle(db_settings)

    first = db_client.get(f"/api/v1/documents/{document['id']}/chunks", params={"limit": 3})
    body = first.json()
    assert body["total"] > 3
    assert [c["ordinal"] for c in body["items"]] == [0, 1, 2]
    rest = db_client.get(
        f"/api/v1/documents/{document['id']}/chunks", params={"limit": 100, "offset": 3}
    ).json()
    assert [c["ordinal"] for c in rest["items"]] == list(range(3, body["total"]))


def test_download_original_file(db_client: TestClient) -> None:
    content = "# 見出し\n\n本文です。\n".encode()
    document = _upload(db_client, "メモ.md", content).json()["document"]

    inline = db_client.get(f"/api/v1/documents/{document['id']}/file")
    assert inline.status_code == 200
    assert inline.content == content
    assert inline.headers["content-type"] == "text/markdown; charset=utf-8"
    assert inline.headers["content-disposition"].startswith("inline;")
    assert "filename*=UTF-8''%E3%83%A1%E3%83%A2.md" in inline.headers["content-disposition"]
    assert inline.headers["x-content-type-options"] == "nosniff"

    attachment = db_client.get(
        f"/api/v1/documents/{document['id']}/file", params={"download": "true"}
    )
    assert attachment.headers["content-disposition"].startswith("attachment;")


def test_unknown_document_returns_404(db_client: TestClient) -> None:
    missing = uuid.uuid4()
    for method, path in [
        ("get", f"/api/v1/documents/{missing}"),
        ("get", f"/api/v1/documents/{missing}/chunks"),
        ("get", f"/api/v1/documents/{missing}/file"),
        ("post", f"/api/v1/documents/{missing}/retry"),
        ("post", f"/api/v1/documents/{missing}/reindex"),
        ("delete", f"/api/v1/documents/{missing}"),
    ]:
        response = getattr(db_client, method)(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "document_not_found"


def test_scanned_pdf_fails_and_retry_is_eligible(
    db_client: TestClient, db_settings: Settings
) -> None:
    data = make_pdf(["", ""], image_only_pages={0, 1})
    document = _upload(db_client, "scan.pdf", data).json()["document"]
    run_worker_until_idle(db_settings)

    detail = db_client.get(f"/api/v1/documents/{document['id']}").json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "no_extractable_text"
    assert detail["chunk_count"] == 0
    assert detail["latest_job"]["attempts"] == 1  # not retryable: no automatic retries
    assert db_client.get("/api/v1/documents", params={"status": "failed"}).json()["total"] == 1

    retry = db_client.post(f"/api/v1/documents/{document['id']}/retry")
    assert retry.status_code == 202
    assert retry.json()["status"] == "pending"
    assert retry.json()["error_code"] is None

    second = db_client.post(f"/api/v1/documents/{document['id']}/retry")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_document_state"

    run_worker_until_idle(db_settings)
    jobs = _sql(
        db_settings,
        "SELECT status FROM ingestion_jobs WHERE document_id = :id ORDER BY created_at",
        id=document["id"],
    )
    assert jobs == [("failed",), ("failed",)]


def test_retry_is_rejected_for_completed_documents(
    db_client: TestClient, db_settings: Settings
) -> None:
    document = _upload(db_client, "a.txt", b"Some text.").json()["document"]
    run_worker_until_idle(db_settings)

    response = db_client.post(f"/api/v1/documents/{document['id']}/retry")
    assert response.status_code == 409
    assert "reindex" in response.json()["error"]["message"]


def test_reindex_replaces_chunks_and_rejects_double_requests(
    db_client: TestClient, db_settings: Settings
) -> None:
    document = _upload(db_client, "a.txt", (ENGLISH_TEXT * 4).encode()).json()["document"]
    run_worker_until_idle(db_settings)
    before = _sql(db_settings, "SELECT id FROM chunks WHERE document_id = :id", id=document["id"])

    response = db_client.post(f"/api/v1/documents/{document['id']}/reindex")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["chunk_count"] == len(before)  # old index still active while queued

    duplicate = db_client.post(f"/api/v1/documents/{document['id']}/reindex")
    assert duplicate.status_code == 409

    run_worker_until_idle(db_settings)
    after = _sql(db_settings, "SELECT id FROM chunks WHERE document_id = :id", id=document["id"])
    assert len(after) == len(before)
    assert not set(after) & set(before)  # every chunk row was replaced
    detail = db_client.get(f"/api/v1/documents/{document['id']}").json()
    assert detail["status"] == "completed"
    assert detail["latest_job"]["kind"] == "reindex"


def test_delete_removes_rows_and_file(db_client: TestClient, db_settings: Settings) -> None:
    document = _upload(db_client, "a.pdf", make_pdf([ENGLISH_TEXT])).json()["document"]
    run_worker_until_idle(db_settings)
    assert len(_stored_files(db_settings)) == 1

    response = db_client.delete(f"/api/v1/documents/{document['id']}")

    assert response.status_code == 204
    assert db_client.get(f"/api/v1/documents/{document['id']}").status_code == 404
    for table in ("documents", "chunks", "ingestion_jobs"):
        assert _sql(db_settings, f"SELECT count(*) FROM {table}") == [(0,)]  # noqa: S608
    assert _stored_files(db_settings) == []

    # The same content can be uploaded again after deletion.
    assert _upload(db_client, "a.pdf", make_pdf([ENGLISH_TEXT])).status_code == 202
