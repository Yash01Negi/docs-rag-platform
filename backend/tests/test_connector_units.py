"""Connector validation, settings, S3 error mapping and the S3 client (moto, no AWS)."""

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from moto import mock_aws

from app.connectors.errors import ConnectorError, ConnectorErrorCode
from app.connectors.runs import sync_retry_delay
from app.connectors.s3 import (
    ObjectPage,
    S3ObjectStore,
    create_client,
    iter_listing,
    map_error,
    normalize_etag,
)
from app.connectors.sources import (
    AllowedSource,
    ConnectorValidationError,
    parse_allowed_source,
    validate_bucket,
    validate_prefix,
)
from app.connectors.validation import (
    display_filename,
    is_folder_marker,
    supported_extension,
    unsafe_key_reason,
)
from app.core.config import Settings

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # noqa: S105 - a fake key to detect leaks


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield


# -- bucket, prefix and allowlist ----------------------------------------------------------


@pytest.mark.parametrize("bucket", ["docs", "my-team.docs-2026", "a1b", "x" * 63])
def test_valid_bucket_names(bucket: str) -> None:
    assert validate_bucket(f" {bucket} ") == bucket


@pytest.mark.parametrize(
    "bucket",
    [
        "ab",
        "x" * 64,
        "Docs",
        "docs_bucket",
        "-docs",
        "docs-",
        "my..docs",
        "my.-docs",
        "192.168.1.10",
        "xn--docs",
        "sthree-docs",
        "docs-s3alias",
        "docs--ol-s3",
        "s3://docs",
        "docs/prefix",
        "",
    ],
)
def test_invalid_bucket_names(bucket: str) -> None:
    with pytest.raises(ConnectorValidationError):
        validate_bucket(bucket)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (None, ""),
        ("  ", ""),
        ("handbook", "handbook/"),
        ("handbook/", "handbook/"),
        ("team/policies", "team/policies/"),
        ("日本語/資料", "日本語/資料/"),
    ],
)
def test_prefix_is_normalized_to_a_folder(raw: str | None, expected: str) -> None:
    assert validate_prefix(raw) == expected


@pytest.mark.parametrize(
    "prefix",
    ["/abs", "a//b", "a/../b", "../a", "./a", "a/./b", "a\\b", "a\x00b", "a\nb", "x" * 600],
)
def test_unsafe_prefixes_are_rejected(prefix: str) -> None:
    with pytest.raises(ConnectorValidationError):
        validate_prefix(prefix)


def test_allowed_source_matches_bucket_and_prefix_scope() -> None:
    whole = parse_allowed_source("docs")
    scoped = parse_allowed_source("s3://shared/team/")
    assert whole == AllowedSource("docs", "")
    assert scoped == AllowedSource("shared", "team/")
    assert whole.allows("docs", "") and whole.allows("docs", "any/")
    assert not whole.allows("docs2", "")
    assert scoped.allows("shared", "team/") and scoped.allows("shared", "team/sub/")
    assert not scoped.allows("shared", "")
    assert not scoped.allows("shared", "teamwork/")  # prefix scopes are folder-aligned
    assert str(scoped) == "shared/team/" and str(whole) == "docs"


def test_settings_parse_allowed_sources_and_reject_bad_entries(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(s3_allowed_sources="docs, shared/team ,")
    assert settings.s3_allowed_sources == [
        AllowedSource("docs", ""),
        AllowedSource("shared", "team/"),
    ]
    assert make_settings().s3_allowed_sources == []
    with pytest.raises(ValueError, match="S3_ALLOWED_SOURCES"):
        make_settings(s3_allowed_sources="Bad_Bucket")
    with pytest.raises(ValueError, match="S3_ALLOWED_SOURCES"):
        make_settings(s3_allowed_sources="docs/../etc")


def test_settings_validate_the_endpoint_without_echoing_it(
    make_settings: Callable[..., Settings],
) -> None:
    assert make_settings(s3_endpoint_url="http://minio:9000/").s3_endpoint_url == (
        "http://minio:9000"
    )
    assert make_settings(s3_endpoint_url="").s3_endpoint_url is None
    for bad in (
        "ftp://minio",
        "minio:9000",
        f"http://key:{SECRET}@minio",
        "http://m/?x=1",
        "http://m/path",
        "http://m:99999",
    ):
        with pytest.raises(ValueError) as caught:
            make_settings(s3_endpoint_url=bad)
        assert SECRET not in str(caught.value)
    assert make_settings(s3_region="ap-northeast-1").s3_region == "ap-northeast-1"
    with pytest.raises(ValueError):
        make_settings(s3_region="not a region")


# -- object keys ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "supported"),
    [
        ("a/report.PDF", True),
        ("notes.md", True),
        ("notes.markdown", True),
        ("readme.txt", True),
        ("image.png", False),
        ("archive.pdf.zip", False),
        ("noextension", False),
        ("folder/", False),
    ],
)
def test_supported_extensions(key: str, supported: bool) -> None:
    assert supported_extension(key) is supported


@pytest.mark.parametrize(
    "key",
    ["", "/etc/passwd", "a/../b.txt", "../b.txt", "./b.txt", "a\\b.txt", "a\x07.txt", "x" * 1100],
)
def test_unsafe_object_keys(key: str) -> None:
    assert unsafe_key_reason(key) is not None


def test_safe_object_keys_and_display_names() -> None:
    assert unsafe_key_reason("team/日本語の資料.pdf") is None
    assert unsafe_key_reason("a/b..c.txt") is None  # dots inside a name are fine
    assert display_filename("team/sub/guide.md") == "guide.md"
    assert is_folder_marker("team/") and not is_folder_marker("team/a.md")


def test_etags_are_opaque_strings() -> None:
    assert (
        normalize_etag('"d41d8cd98f00b204e9800998ecf8427e"') == "d41d8cd98f00b204e9800998ecf8427e"
    )
    # Multipart and SSE-KMS ETags are not MD5 sums; they are kept as they are.
    assert normalize_etag('"9b2cf535f27731c974343645a3985328-3"') == (
        "9b2cf535f27731c974343645a3985328-3"
    )
    assert normalize_etag(None) == ""


def test_sync_retry_backoff_is_bounded() -> None:
    assert sync_retry_delay(30, 1) == timedelta(seconds=30)
    assert sync_retry_delay(30, 2) == timedelta(seconds=60)
    assert sync_retry_delay(30, 3) == timedelta(seconds=120)
    assert sync_retry_delay(30, 20) == timedelta(minutes=15)


# -- error mapping: safe messages, no credentials or infrastructure details ----------------


def _client_error(code: str, status: int, message: str = "") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message or f"secret {SECRET} host s3.internal"},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": "REQ123",
                "HostId": "host-id-xyz",
            },
        },
        "ListObjectsV2",
    )


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (NoCredentialsError(), ConnectorErrorCode.CREDENTIALS_MISSING, False),
        (_client_error("AccessDenied", 403), ConnectorErrorCode.ACCESS_DENIED, False),
        (_client_error("InvalidAccessKeyId", 403), ConnectorErrorCode.ACCESS_DENIED, False),
        (_client_error("SignatureDoesNotMatch", 403), ConnectorErrorCode.ACCESS_DENIED, False),
        (_client_error("ExpiredToken", 400), ConnectorErrorCode.ACCESS_DENIED, False),
        (_client_error("NoSuchBucket", 404), ConnectorErrorCode.BUCKET_NOT_FOUND, False),
        (_client_error("NoSuchKey", 404), ConnectorErrorCode.OBJECT_NOT_FOUND, True),
        (_client_error("PreconditionFailed", 412), ConnectorErrorCode.OBJECT_CHANGED, True),
        (_client_error("PermanentRedirect", 301), ConnectorErrorCode.WRONG_REGION, False),
        (_client_error("SlowDown", 503), ConnectorErrorCode.S3_UNAVAILABLE, True),
        (_client_error("InternalError", 500), ConnectorErrorCode.S3_UNAVAILABLE, True),
        (_client_error("Weird", 400), ConnectorErrorCode.S3_ERROR, False),
        (
            EndpointConnectionError(endpoint_url=f"https://{SECRET}.internal"),
            ConnectorErrorCode.S3_UNREACHABLE,
            True,
        ),
        (
            ReadTimeoutError(endpoint_url="https://s3.internal"),
            ConnectorErrorCode.S3_UNREACHABLE,
            True,
        ),
        (RuntimeError(SECRET), ConnectorErrorCode.INTERNAL_ERROR, True),
    ],
)
def test_errors_map_to_safe_codes(
    exc: Exception, code: ConnectorErrorCode, retryable: bool
) -> None:
    error = map_error(exc)
    assert error.code is code
    assert error.retryable is retryable
    for leaked in (SECRET, "REQ123", "host-id-xyz", "s3.internal", "testing"):
        assert leaked not in error.message
        assert leaked not in repr(error)


# -- the S3 client against moto ------------------------------------------------------------


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    return make_settings(s3_region="us-east-1", **overrides)


def test_client_uses_timeouts_retries_and_no_stored_credentials(
    make_settings: Callable[..., Settings], aws: None
) -> None:
    client = create_client(
        _settings(make_settings, s3_connect_timeout_seconds=3, s3_read_timeout_seconds=7)
    )
    config = client.meta.config
    assert config.connect_timeout == 3 and config.read_timeout == 7
    assert config.retries["mode"] == "standard"
    assert config.retries["total_max_attempts"] == 4  # botocore: max_attempts + the first try
    assert "yashrag-connector" in config.user_agent_extra
    # A server-side endpoint override switches to path-style addressing (MinIO).
    minio = create_client(_settings(make_settings, s3_endpoint_url="http://minio:9000"))
    assert minio.meta.endpoint_url == "http://minio:9000"
    assert minio.meta.config.s3["addressing_style"] == "path"


def test_listing_paginates_and_filters_by_prefix(
    make_settings: Callable[..., Settings], aws: None
) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="docs")
    for i in range(7):
        client.put_object(Bucket="docs", Key=f"team/{i}.txt", Body=f"text {i}".encode())
    client.put_object(Bucket="docs", Key="teamwork/x.txt", Body=b"other")
    client.put_object(Bucket="docs", Key="root.txt", Body=b"root")
    store = S3ObjectStore(create_client(_settings(make_settings)))

    pages: list[ObjectPage] = []
    token: str | None = None
    while True:
        page = store.list_page("docs", "team/", token, 3)
        pages.append(page)
        if page.next_token is None:
            break
        token = page.next_token
    assert [len(p.objects) for p in pages] == [3, 3, 1]
    keys = [o.key for o in iter_listing(store, "docs", "team/", 2, 100)]
    assert keys == [f"team/{i}.txt" for i in range(7)]
    with pytest.raises(ConnectorError) as caught:
        list(iter_listing(store, "docs", "team/", 2, 5))
    assert caught.value.code is ConnectorErrorCode.TOO_MANY_OBJECTS


def test_download_fetches_exactly_the_listed_version(
    make_settings: Callable[..., Settings], aws: None, tmp_path: Path
) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="docs")
    client.put_bucket_versioning(Bucket="docs", VersioningConfiguration={"Status": "Enabled"})
    client.put_object(Bucket="docs", Key="a.txt", Body=b"version one")
    store = S3ObjectStore(create_client(_settings(make_settings)))
    listed = next(iter_listing(store, "docs", "", 10, 10))

    target = tmp_path / "a.bin"
    downloaded = store.download("docs", "a.txt", listed.etag, target, 1024)
    assert target.read_bytes() == b"version one"
    assert downloaded.size == 11 and downloaded.etag == listed.etag
    assert downloaded.version_id  # recorded for audit on versioned buckets

    client.put_object(Bucket="docs", Key="a.txt", Body=b"version two")
    with pytest.raises(ConnectorError) as changed:
        store.download("docs", "a.txt", listed.etag, target, 1024)
    assert changed.value.code is ConnectorErrorCode.OBJECT_CHANGED and changed.value.retryable

    current = next(iter_listing(store, "docs", "", 10, 10))
    with pytest.raises(ConnectorError) as limited:
        store.download("docs", "a.txt", current.etag, target, 4)
    assert limited.value.code is ConnectorErrorCode.OBJECT_TOO_LARGE
    assert "4 byte limit" in limited.value.message


def test_missing_bucket_and_missing_credentials(
    make_settings: Callable[..., Settings], aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = S3ObjectStore(create_client(_settings(make_settings)))
    with pytest.raises(ConnectorError) as missing:
        list(iter_listing(store, "no-such-bucket", "", 10, 10))
    assert missing.value.code is ConnectorErrorCode.BUCKET_NOT_FOUND

    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    no_credentials = S3ObjectStore(create_client(_settings(make_settings)))
    with pytest.raises(ConnectorError) as caught:
        no_credentials.list_page("docs", "", None, 10)
    assert caught.value.code is ConnectorErrorCode.CREDENTIALS_MISSING
