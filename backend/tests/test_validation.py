from pathlib import Path

import pytest

from app.db.models import FileType
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.validation import file_type_from_extension, sanitize_filename, validate_file
from tests.samples import ENGLISH_TEXT, JAPANESE_TEXT, make_encrypted_pdf, make_pdf


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "upload.bin"
    path.write_bytes(data)
    return path


def _code(exc: pytest.ExceptionInfo[IngestionError]) -> ErrorCode:
    return exc.value.code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\windows\\system.ini.md", "system.ini.md"),
        ("/absolute/path/notes.md", "notes.md"),
        ("報告書　2026年.pdf", "報告書　2026年.pdf"),
        ('bad"name<>|?*.txt', "badname.txt"),
        ("line\nbreak\x00.txt", "linebreak.txt"),
        ("..", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_normalizes_to_nfc() -> None:
    decomposed = "が.txt"  # か + combining dakuten
    assert sanitize_filename(decomposed) == "が.txt"


def test_sanitize_filename_limits_length_and_keeps_extension() -> None:
    name = sanitize_filename("あ" * 400 + ".pdf")
    assert len(name) == 255
    assert name.endswith(".pdf")


@pytest.mark.parametrize(
    ("name", "file_type"),
    [
        ("a.pdf", FileType.PDF),
        ("A.PDF", FileType.PDF),
        ("a.md", FileType.MARKDOWN),
        ("a.markdown", FileType.MARKDOWN),
        ("a.txt", FileType.TEXT),
    ],
)
def test_file_type_from_extension(name: str, file_type: FileType) -> None:
    assert file_type_from_extension(name) is file_type


@pytest.mark.parametrize("name", ["a.docx", "a.html", "a.exe", "noextension", "a.pdf.exe"])
def test_unsupported_extensions_are_rejected(name: str) -> None:
    with pytest.raises(IngestionError) as exc:
        file_type_from_extension(name)
    assert _code(exc) is ErrorCode.UNSUPPORTED_FILE_TYPE


def test_valid_pdf(tmp_path: Path) -> None:
    path = _write(tmp_path, "a.pdf", make_pdf([ENGLISH_TEXT, JAPANESE_TEXT]))
    result = validate_file(path, "a.pdf")
    assert result.file_type is FileType.PDF
    assert result.mime_type == "application/pdf"
    assert result.page_count == 2


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("a.txt", ENGLISH_TEXT.encode()),
        ("a.md", ("# 見出し\n\n" + JAPANESE_TEXT).encode()),
        ("bom.txt", b"\xef\xbb\xbf" + JAPANESE_TEXT.encode()),
    ],
)
def test_valid_utf8_text(tmp_path: Path, name: str, data: bytes) -> None:
    result = validate_file(_write(tmp_path, name, data), name)
    assert result.page_count is None


def test_text_named_pdf_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.pdf", b"just text"), "a.pdf")
    assert _code(exc) is ErrorCode.CONTENT_TYPE_MISMATCH


def test_pdf_named_txt_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.txt", make_pdf([ENGLISH_TEXT])), "a.txt")
    assert _code(exc) is ErrorCode.CONTENT_TYPE_MISMATCH


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.txt", b""), "a.txt")
    assert _code(exc) is ErrorCode.EMPTY_FILE


def test_whitespace_only_text_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.md", " \n\t\n　".encode()), "a.md")
    assert _code(exc) is ErrorCode.EMPTY_FILE


@pytest.mark.parametrize(
    "data",
    [
        "日本語".encode("shift_jis"),
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        b"\xff\xfe\x00h\x00i",
    ],
)
def test_non_utf8_or_binary_text_is_rejected(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.txt", data), "a.txt")
    assert _code(exc) is ErrorCode.INVALID_ENCODING


def test_malformed_pdf_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50), "a.pdf")
    assert _code(exc) in {ErrorCode.MALFORMED_PDF, ErrorCode.EMPTY_FILE}


def test_password_protected_pdf_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.pdf", make_encrypted_pdf()), "a.pdf")
    assert _code(exc) is ErrorCode.ENCRYPTED_PDF


def test_owner_password_only_pdf_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError) as exc:
        validate_file(_write(tmp_path, "a.pdf", make_encrypted_pdf(user_password="")), "a.pdf")
    assert _code(exc) is ErrorCode.ENCRYPTED_PDF
