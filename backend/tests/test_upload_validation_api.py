"""Upload rejections that happen before the database is touched (no database needed)."""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.samples import ENGLISH_TEXT, make_encrypted_pdf, make_pdf


@pytest.fixture
def small_limit_client(make_settings: Callable[..., Settings]) -> Iterator[TestClient]:
    app = create_app(make_settings(max_upload_bytes=1024 * 1024))
    with TestClient(app) as client:
        yield client


def _upload(client: TestClient, name: str, data: bytes, content_type: str = "text/plain"):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/documents",
        files={"file": (name, data, content_type)},
        headers={"X-Request-ID": "upload-test"},
    )


def test_limits_endpoint(small_limit_client: TestClient) -> None:
    body = small_limit_client.get("/api/v1/documents/limits").json()
    assert body == {
        "max_upload_bytes": 1024 * 1024,
        "accepted_extensions": [".pdf", ".md", ".markdown", ".txt"],
        "accepted_mime_types": ["application/pdf", "text/markdown", "text/plain"],
    }


@pytest.mark.parametrize(
    ("name", "data", "status", "code"),
    [
        ("evil.exe", b"MZ\x90\x00", 415, "unsupported_file_type"),
        ("page.html", b"<script>alert(1)</script>", 415, "unsupported_file_type"),
        ("fake.pdf", b"not a pdf at all", 415, "content_type_mismatch"),
        ("real-pdf.txt", make_pdf([ENGLISH_TEXT]), 415, "content_type_mismatch"),
        ("empty.txt", b"", 422, "empty_file"),
        ("sjis.txt", "日本語".encode("shift_jis"), 422, "invalid_encoding"),
        ("locked.pdf", make_encrypted_pdf(), 422, "encrypted_pdf"),
        ("..", b"text", 422, "invalid_filename"),
    ],
)
def test_invalid_uploads_are_rejected_with_error_envelope(
    small_limit_client: TestClient, name: str, data: bytes, status: int, code: str
) -> None:
    response = _upload(small_limit_client, name, data)

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["request_id"] == "upload-test"
    assert "/" not in error["message"]  # no filesystem paths in messages


def test_client_mime_type_is_not_trusted(small_limit_client: TestClient) -> None:
    response = _upload(small_limit_client, "fake.pdf", b"plain text", "application/pdf")
    assert response.json()["error"]["code"] == "content_type_mismatch"


def test_oversized_upload_is_rejected_from_content_length(small_limit_client: TestClient) -> None:
    response = _upload(small_limit_client, "big.txt", b"a" * (2 * 1024 * 1024))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert response.headers["X-Request-ID"] == "upload-test"


def test_file_just_over_the_limit_is_rejected(small_limit_client: TestClient) -> None:
    # Within the multipart overhead allowance, so the endpoint's exact check must catch it.
    response = _upload(small_limit_client, "big.txt", b"a" * (1024 * 1024 + 1))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_chunked_upload_without_content_length_is_limited(
    small_limit_client: TestClient,
) -> None:
    boundary = "testboundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()

    def body() -> Iterator[bytes]:
        yield head
        for _ in range(40):
            yield b"a" * 65536
        yield f"\r\n--{boundary}--\r\n".encode()

    response = small_limit_client.post(
        "/api/v1/documents",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_missing_file_field_is_a_validation_error(small_limit_client: TestClient) -> None:
    response = small_limit_client.post("/api/v1/documents", data={"other": "x"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_invalid_document_id_is_a_validation_error(small_limit_client: TestClient) -> None:
    response = small_limit_client.get("/api/v1/documents/not-a-uuid")
    assert response.status_code == 422


def test_openapi_documents_the_document_endpoints(small_limit_client: TestClient) -> None:
    paths = small_limit_client.get("/api/v1/openapi.json").json()["paths"]
    assert set(paths) >= {
        "/api/v1/documents",
        "/api/v1/documents/limits",
        "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/chunks",
        "/api/v1/documents/{document_id}/file",
        "/api/v1/documents/{document_id}/retry",
        "/api/v1/documents/{document_id}/reindex",
    }
    upload = paths["/api/v1/documents"]["post"]["responses"]
    assert {"200", "202", "413", "415", "422"} <= set(upload)
    chunk_schema = small_limit_client.get("/api/v1/openapi.json").json()["components"]["schemas"][
        "ChunkPreview"
    ]
    assert "embedding" not in chunk_schema["properties"]
