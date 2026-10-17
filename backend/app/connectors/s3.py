"""Amazon S3 access for connectors, through the official AWS SDK (boto3).

Credentials are resolved by boto3's standard chain in the worker's environment: environment
variables, ``AWS_PROFILE`` and the shared config files, container credentials, or an EC2 /
ECS / EKS IAM role. The application never reads, stores, logs or returns them. The only
endpoint override is the server-side ``S3_ENDPOINT_URL`` (for MinIO or other emulators).

boto3 is synchronous; callers run these methods in a thread.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
)

from app.connectors.errors import ConnectorError, ConnectorErrorCode
from app.core.config import Settings

logger = logging.getLogger(__name__)

# botocore's DEBUG logs include request headers; never let them reach the application log.
for _name in ("boto3", "botocore", "s3transfer", "urllib3"):
    logging.getLogger(_name).setLevel(max(logging.getLogger(_name).level, logging.WARNING))

_READ_CHUNK_BYTES = 1024 * 1024

_CREDENTIAL_CODES = {
    "AccessDenied",
    "AllAccessDisabled",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "ExpiredToken",
    "InvalidToken",
    "TokenRefreshRequired",
    "AccountProblem",
}
_TRANSIENT_CODES = {
    "SlowDown",
    "Throttling",
    "ThrottlingException",
    "RequestTimeout",
    "RequestTimeTooSkewed",
    "InternalError",
    "ServiceUnavailable",
    "503",
    "500",
}
_REGION_CODES = {"PermanentRedirect", "AuthorizationHeaderMalformed", "IllegalLocationConstraint"}


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    etag: str
    size: int
    last_modified: datetime


@dataclass(frozen=True)
class ObjectPage:
    objects: list[ObjectInfo]
    next_token: str | None  # None when this was the last page


@dataclass(frozen=True)
class DownloadedObject:
    size: int
    etag: str
    version_id: str | None


class ObjectStore(Protocol):
    """What the sync engine needs from S3 (a seam for tests and other providers)."""

    def list_page(
        self, bucket: str, prefix: str, token: str | None, page_size: int
    ) -> ObjectPage: ...

    def download(
        self, bucket: str, key: str, etag: str, target: Path, max_bytes: int
    ) -> DownloadedObject: ...


def normalize_etag(value: str | None) -> str:
    """ETags are opaque version tokens. They are compared as strings, never as MD5 sums
    (multipart uploads and SSE-KMS objects do not have MD5 ETags)."""
    return (value or "").strip().strip('"')


def map_error(exc: BaseException) -> ConnectorError:
    """A ConnectorError with a message that is safe to show and store (no credentials,
    request IDs, signatures or endpoint details)."""
    if isinstance(exc, ConnectorError):
        return exc
    if isinstance(
        exc,
        NoCredentialsError | PartialCredentialsError | ProfileNotFound | CredentialRetrievalError,
    ):
        return ConnectorError(
            ConnectorErrorCode.CREDENTIALS_MISSING,
            "No usable AWS credentials are configured for the worker.",
        )
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == "NoSuchBucket":
            return ConnectorError(ConnectorErrorCode.BUCKET_NOT_FOUND, "The bucket does not exist.")
        if code in ("NoSuchKey", "404") or status == 404:
            return ConnectorError(
                ConnectorErrorCode.OBJECT_NOT_FOUND,
                "The object was deleted while the sync was running.",
                retryable=True,
            )
        if code in ("PreconditionFailed", "412") or status == 412:
            return ConnectorError(
                ConnectorErrorCode.OBJECT_CHANGED,
                "The object changed while it was being downloaded; the next sync imports it.",
                retryable=True,
            )
        if code in _REGION_CODES or status == 301:
            return ConnectorError(
                ConnectorErrorCode.WRONG_REGION,
                "The bucket is in a different AWS region than the one configured.",
            )
        if code in _CREDENTIAL_CODES or status == 403:
            return ConnectorError(
                ConnectorErrorCode.ACCESS_DENIED,
                "Access to the bucket was denied. Check the worker's AWS credentials and IAM "
                "permissions (s3:ListBucket and s3:GetObject).",
            )
        if code in _TRANSIENT_CODES or (isinstance(status, int) and status >= 500):
            return ConnectorError(
                ConnectorErrorCode.S3_UNAVAILABLE,
                "Amazon S3 is throttling or temporarily unavailable.",
                retryable=True,
            )
        return ConnectorError(ConnectorErrorCode.S3_ERROR, "Amazon S3 rejected the request.")
    if isinstance(
        exc,
        EndpointConnectionError | ConnectTimeoutError | ReadTimeoutError | ConnectionClosedError,
    ):
        return ConnectorError(
            ConnectorErrorCode.S3_UNREACHABLE,
            "Amazon S3 could not be reached (network error or timeout).",
            retryable=True,
        )
    if isinstance(exc, BotoCoreError):
        return ConnectorError(
            ConnectorErrorCode.S3_ERROR, "The request to Amazon S3 failed.", retryable=True
        )
    return ConnectorError(
        ConnectorErrorCode.INTERNAL_ERROR, "An unexpected error occurred.", retryable=True
    )


def create_client(settings: Settings) -> Any:
    style = settings.s3_addressing_style
    if style == "auto" and settings.s3_endpoint_url:
        style = "path"  # what MinIO and most emulators expect
    config = Config(
        connect_timeout=settings.s3_connect_timeout_seconds,
        read_timeout=settings.s3_read_timeout_seconds,
        retries={"max_attempts": settings.s3_max_attempts, "mode": "standard"},
        s3={"addressing_style": style},
        user_agent_extra="yashrag-connector",
    )
    session = boto3.session.Session(region_name=settings.s3_region)
    return session.client("s3", endpoint_url=settings.s3_endpoint_url, config=config)


class S3ObjectStore:
    def __init__(self, client: Any) -> None:
        self.client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3ObjectStore":
        return cls(create_client(settings))

    def list_page(self, bucket: str, prefix: str, token: str | None, page_size: int) -> ObjectPage:
        params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": page_size}
        if token:
            params["ContinuationToken"] = token
        try:
            response = self.client.list_objects_v2(**params)
        except Exception as exc:
            raise map_error(exc) from exc
        objects = [
            ObjectInfo(
                key=item["Key"],
                etag=normalize_etag(item.get("ETag")),
                size=int(item.get("Size", 0)),
                last_modified=item["LastModified"],
            )
            for item in response.get("Contents", [])
        ]
        truncated = bool(response.get("IsTruncated"))
        next_token = response.get("NextContinuationToken") if truncated else None
        if truncated and not next_token:
            raise ConnectorError(
                ConnectorErrorCode.INVALID_LISTING,
                "The bucket listing was truncated without a continuation token.",
                retryable=True,
            )
        return ObjectPage(objects, next_token)

    def download(
        self, bucket: str, key: str, etag: str, target: Path, max_bytes: int
    ) -> DownloadedObject:
        """Download exactly the listed version (If-Match on its ETag), up to max_bytes."""
        try:
            response = self.client.get_object(Bucket=bucket, Key=key, IfMatch=f'"{etag}"')
            length = int(response.get("ContentLength") or 0)
            if length > max_bytes:
                response["Body"].close()
                raise too_large_error(max_bytes)
            size = 0
            with target.open("wb") as handle:
                for chunk in response["Body"].iter_chunks(_READ_CHUNK_BYTES):
                    size += len(chunk)
                    if size > max_bytes:
                        raise too_large_error(max_bytes)
                    handle.write(chunk)
        except ConnectorError:
            raise
        except Exception as exc:
            raise map_error(exc) from exc
        return DownloadedObject(
            size=size,
            etag=normalize_etag(response.get("ETag")) or etag,
            version_id=response.get("VersionId"),
        )


def size_limit_text(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024:
        return f"{max_bytes // (1024 * 1024)} MB"
    return f"{max_bytes} byte"


def too_large_error(max_bytes: int) -> ConnectorError:
    return ConnectorError(
        ConnectorErrorCode.OBJECT_TOO_LARGE,
        f"The object is larger than the {size_limit_text(max_bytes)} limit (S3_MAX_OBJECT_BYTES).",
    )


def iter_listing(
    store: ObjectStore, bucket: str, prefix: str, page_size: int, max_objects: int
) -> Iterator[ObjectInfo]:
    """Every object under the prefix, page by page. Raises if the listing cannot complete."""
    token: str | None = None
    seen = 0
    while True:
        page = store.list_page(bucket, prefix, token, page_size)
        for item in page.objects:
            seen += 1
            if seen > max_objects:
                raise ConnectorError(
                    ConnectorErrorCode.TOO_MANY_OBJECTS,
                    f"The prefix holds more than {max_objects} objects (S3_MAX_OBJECTS_PER_SYNC).",
                )
            yield item
        if page.next_token is None:
            return
        token = page.next_token
