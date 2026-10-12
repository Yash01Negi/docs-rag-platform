"""Upload validation: filename sanitising and content-based file type detection.

The file type is decided from the bytes, not from the client's ``Content-Type``. The
extension only chooses between Markdown and plain text (which are byte-identical formats)
and must agree with the content: a ``.txt`` that is really a PDF is rejected.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.db.models import FileType
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.pdf import PdfOpenError, PdfPasswordError, is_encrypted, open_pdf

MIME_TYPES: dict[FileType, str] = {
    FileType.PDF: "application/pdf",
    FileType.MARKDOWN: "text/markdown",
    FileType.TEXT: "text/plain",
}

_EXTENSIONS: dict[str, FileType] = {
    ".pdf": FileType.PDF,
    ".md": FileType.MARKDOWN,
    ".markdown": FileType.MARKDOWN,
    ".txt": FileType.TEXT,
}

SUPPORTED_EXTENSIONS = tuple(_EXTENSIONS)

_MAX_FILENAME_LENGTH = 255
# C0/C1 control characters, plus characters that are unsafe in Content-Disposition headers or
# on common filesystems.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f"<>:|?*\\/]')
# The PDF specification allows the header anywhere in the first 1024 bytes.
_PDF_HEADER_WINDOW = 1024


@dataclass(frozen=True)
class ValidatedFile:
    file_type: FileType
    mime_type: str
    page_count: int | None


def sanitize_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to a safe display name.

    Directory components are dropped (both ``/`` and ``\\``), control and reserved
    characters are removed, and the result is NFC-normalised so Japanese names compare
    consistently. The name is only ever used for display and Content-Disposition, never as a
    filesystem path.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = _UNSAFE_FILENAME_CHARS.sub("", name).strip().strip(".")
    if not name:
        return ""
    if len(name) > _MAX_FILENAME_LENGTH:
        stem, suffix = Path(name).stem, Path(name).suffix[:16]
        name = stem[: _MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name


def file_type_from_extension(filename: str) -> FileType:
    suffix = Path(filename).suffix.lower()
    file_type = _EXTENSIONS.get(suffix)
    if file_type is None:
        raise IngestionError(
            ErrorCode.UNSUPPORTED_FILE_TYPE,
            f"Unsupported file type. Accepted: {', '.join(SUPPORTED_EXTENSIONS)}",
        )
    return file_type


def _looks_like_pdf(head: bytes) -> bool:
    return b"%PDF-" in head[:_PDF_HEADER_WINDOW]


def validate_file(path: Path, filename: str) -> ValidatedFile:
    """Check that the stored upload really is the supported type its name claims."""
    declared = file_type_from_extension(filename)
    size = path.stat().st_size
    if size == 0:
        raise IngestionError(ErrorCode.EMPTY_FILE, "The file is empty.")

    with path.open("rb") as handle:
        head = handle.read(_PDF_HEADER_WINDOW)

    if declared is FileType.PDF:
        if not _looks_like_pdf(head):
            raise IngestionError(
                ErrorCode.CONTENT_TYPE_MISMATCH, "The file has a .pdf name but is not a PDF."
            )
        page_count = _validate_pdf(path)
        return ValidatedFile(declared, MIME_TYPES[declared], page_count)

    if _looks_like_pdf(head):
        raise IngestionError(
            ErrorCode.CONTENT_TYPE_MISMATCH,
            "The file content is a PDF; upload it with a .pdf extension.",
        )
    _validate_utf8_text(path)
    return ValidatedFile(declared, MIME_TYPES[declared], None)


def _validate_pdf(path: Path) -> int:
    try:
        with open_pdf(path) as document:
            # is_encrypted also catches PDFs with only an owner password, which open without
            # one but are still encrypted files.
            if is_encrypted(document):
                raise _encrypted()
            page_count = len(document)
    except PdfPasswordError as exc:
        raise _encrypted() from exc
    except PdfOpenError as exc:
        raise IngestionError(
            ErrorCode.MALFORMED_PDF, "The PDF could not be opened; it may be damaged."
        ) from exc
    if page_count == 0:
        raise IngestionError(ErrorCode.EMPTY_FILE, "The PDF has no pages.")
    return page_count


def _encrypted() -> IngestionError:
    return IngestionError(
        ErrorCode.ENCRYPTED_PDF, "Encrypted or password-protected PDFs are not supported."
    )


def _validate_utf8_text(path: Path) -> None:
    data = path.read_bytes()
    if b"\x00" in data:
        raise IngestionError(
            ErrorCode.INVALID_ENCODING, "The file appears to be binary, not UTF-8 text."
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            ErrorCode.INVALID_ENCODING, "Text files must be UTF-8 encoded."
        ) from exc
    if not text.strip():
        raise IngestionError(ErrorCode.EMPTY_FILE, "The file contains no text.")
