"""Typed connector failures with messages that are safe to show and store."""

from enum import StrEnum


class ConnectorErrorCode(StrEnum):
    # Run-level: the listing could not be completed, so nothing was deleted.
    CREDENTIALS_MISSING = "credentials_missing"
    ACCESS_DENIED = "access_denied"
    BUCKET_NOT_FOUND = "bucket_not_found"
    WRONG_REGION = "wrong_region"
    S3_UNAVAILABLE = "s3_unavailable"
    S3_UNREACHABLE = "s3_unreachable"
    S3_ERROR = "s3_error"
    INVALID_LISTING = "invalid_listing"
    TOO_MANY_OBJECTS = "too_many_objects"
    SOURCE_NOT_ALLOWED = "source_not_allowed"
    WORKER_LOST = "worker_lost"
    INTERNAL_ERROR = "internal_error"
    # Object-level: recorded on the run and the object, the rest of the sync continues.
    OBJECT_NOT_FOUND = "object_not_found"
    OBJECT_CHANGED = "object_changed"
    OBJECT_TOO_LARGE = "object_too_large"
    UNSAFE_OBJECT_KEY = "unsafe_object_key"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    DOCUMENT_BUSY = "document_busy"


class ConnectorError(Exception):
    def __init__(self, code: ConnectorErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def __repr__(self) -> str:
        return f"ConnectorError({self.code.value!r}, {self.message!r}, retryable={self.retryable})"
