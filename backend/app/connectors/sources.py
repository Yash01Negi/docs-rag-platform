"""Bucket, prefix and allowlist validation, shared by the settings and the API.

This module imports nothing from the application, so ``app.core.config`` can use it.
Bucket names follow the S3 general-purpose bucket naming rules; prefixes are folders.
"""

import ipaddress
import re
import unicodedata
from dataclasses import dataclass

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_BUCKET_RESERVED_PREFIXES = ("xn--", "sthree-", "amzn-s3-demo-")
_BUCKET_RESERVED_SUFFIXES = ("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3")
MAX_PREFIX_LENGTH = 512


class ConnectorValidationError(ValueError):
    """A configuration value is invalid. The message is safe to show to users."""


def validate_bucket(value: str) -> str:
    bucket = value.strip()
    if not _BUCKET.match(bucket):
        raise ConnectorValidationError(
            "Bucket names are 3 to 63 characters: lowercase letters, digits, dots and "
            "hyphens, starting and ending with a letter or digit."
        )
    if ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ConnectorValidationError("Bucket names cannot contain '..', '.-' or '-.'.")
    if bucket.startswith(_BUCKET_RESERVED_PREFIXES) or bucket.endswith(_BUCKET_RESERVED_SUFFIXES):
        raise ConnectorValidationError("This bucket name uses a prefix or suffix reserved by S3.")
    try:
        ipaddress.IPv4Address(bucket)
    except ValueError:
        return bucket
    raise ConnectorValidationError("Bucket names cannot be formatted as IP addresses.")


def has_control_characters(value: str) -> bool:
    return any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value)


def validate_prefix(value: str | None) -> str:
    """Normalize a prefix to '' or a folder path ending in '/'."""
    prefix = (value or "").strip()
    if not prefix:
        return ""
    if prefix.startswith("/"):
        raise ConnectorValidationError("The prefix must not start with '/'.")
    if has_control_characters(prefix) or "\\" in prefix:
        raise ConnectorValidationError("The prefix contains characters that are not allowed.")
    if not prefix.endswith("/"):
        prefix += "/"
    segments = prefix[:-1].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ConnectorValidationError("The prefix must not contain empty, '.' or '..' segments.")
    if len(prefix) > MAX_PREFIX_LENGTH:
        raise ConnectorValidationError(f"The prefix is longer than {MAX_PREFIX_LENGTH} characters.")
    return prefix


@dataclass(frozen=True)
class AllowedSource:
    bucket: str
    prefix: str  # '' allows the whole bucket

    def allows(self, bucket: str, prefix: str) -> bool:
        return bucket == self.bucket and prefix.startswith(self.prefix)

    def __str__(self) -> str:
        return f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket


def parse_allowed_source(entry: object) -> AllowedSource:
    if isinstance(entry, AllowedSource):
        return entry
    if not isinstance(entry, str):
        raise ValueError(f"invalid S3_ALLOWED_SOURCES entry: {entry!r}")
    text = entry.strip().removeprefix("s3://")
    bucket, _, prefix = text.partition("/")
    try:
        return AllowedSource(validate_bucket(bucket), validate_prefix(prefix))
    except ConnectorValidationError as exc:
        raise ValueError(f"invalid S3_ALLOWED_SOURCES entry {entry!r}: {exc}") from exc
