"""Object key checks applied before an S3 object is imported.

An object key is never used as a filesystem path (stored files get random keys), but keys
that look like path traversal or contain control characters are refused anyway, so a
hostile bucket cannot produce misleading file names.
"""

from pathlib import PurePosixPath

from app.connectors.sources import (
    AllowedSource,
    ConnectorValidationError,
    has_control_characters,
    parse_allowed_source,
    validate_bucket,
    validate_prefix,
)
from app.ingestion.validation import SUPPORTED_EXTENSIONS

__all__ = [
    "AllowedSource",
    "ConnectorValidationError",
    "display_filename",
    "is_folder_marker",
    "parse_allowed_source",
    "supported_extension",
    "unsafe_key_reason",
    "validate_bucket",
    "validate_prefix",
]

MAX_KEY_BYTES = 1024


def is_folder_marker(key: str) -> bool:
    return key.endswith("/")


def supported_extension(key: str) -> bool:
    return PurePosixPath(key).suffix.lower() in SUPPORTED_EXTENSIONS


def unsafe_key_reason(key: str) -> str | None:
    """Why an object key is refused, or None when it can be imported."""
    if not key or len(key.encode("utf-8")) > MAX_KEY_BYTES:
        return "The object key is empty or too long."
    if has_control_characters(key):
        return "The object key contains control characters."
    if key.startswith("/") or "\\" in key:
        return "The object key looks like an absolute or Windows path."
    if any(segment in (".", "..") for segment in key.split("/")):
        return "The object key contains '.' or '..' path segments."
    return None


def display_filename(key: str) -> str:
    """The last path segment of the key, used as the document's file name."""
    return key.rsplit("/", 1)[-1]
