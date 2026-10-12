"""Typed ingestion failures.

Every failure carries a stable machine-readable ``code`` and a short message that is safe to
show to users (no file paths, stack traces or library internals). ``retryable`` decides
whether the worker schedules another attempt or fails the job immediately.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    # Rejected at upload time (HTTP 4xx, no document is created).
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    CONTENT_TYPE_MISMATCH = "content_type_mismatch"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_ENCODING = "invalid_encoding"
    ENCRYPTED_PDF = "encrypted_pdf"
    MALFORMED_PDF = "malformed_pdf"
    # Recorded on a document by the worker.
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    EXTRACTION_FAILED = "extraction_failed"
    STORED_FILE_MISSING = "stored_file_missing"
    EMBEDDING_MODEL_UNAVAILABLE = "embedding_model_unavailable"
    EMBEDDING_FAILED = "embedding_failed"
    INVALID_EMBEDDING = "invalid_embedding"
    INPUT_TOO_LONG = "input_too_long"
    WORKER_LOST = "worker_lost"
    INTERNAL_ERROR = "internal_error"


class IngestionError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def __repr__(self) -> str:
        return f"IngestionError({self.code.value!r}, {self.message!r}, retryable={self.retryable})"
