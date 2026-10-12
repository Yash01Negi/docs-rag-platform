"""Regression tests for PDF extraction with PDFium (pypdfium2).

They pin down the behaviour PyMuPDF used to provide: page numbers, reading order,
Japanese and English text, paragraph boundaries, outlines, clean failures, and chunks that
are fit for retrieval.
"""

import threading
from pathlib import Path

import pytest

from app.db.models import FileType, Language
from app.embeddings.fake import fake_token_count
from app.ingestion.chunking import ChunkingConfig, chunk_blocks
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.extraction import extract
from app.ingestion.validation import validate_file
from tests.samples import (
    ENGLISH_TEXT,
    JAPANESE_TEXT,
    Paragraph,
    make_encrypted_pdf,
    make_pdf,
    make_pdf_document,
)

MIXED_TEXT = "本システムはBGE-M3を使い、English and Japanese を同じベクトル空間に埋め込みます。"


def _write(tmp_path: Path, data: bytes, name: str = "doc.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _squash(text: str) -> str:
    return "".join(text.split())


# --- multilingual text -------------------------------------------------------------------


def test_english_japanese_and_mixed_text_is_extracted_exactly(tmp_path: Path) -> None:
    data = make_pdf_document([[ENGLISH_TEXT], [JAPANESE_TEXT], [MIXED_TEXT]])
    document = extract(_write(tmp_path, data), FileType.PDF)

    by_page = {page: [b.text for b in document.blocks if b.page == page] for page in (1, 2, 3)}
    assert by_page[1] == [ENGLISH_TEXT]
    assert by_page[2] == [JAPANESE_TEXT]
    assert by_page[3] == [MIXED_TEXT]


def test_wrapped_japanese_lines_are_joined_without_spaces(tmp_path: Path) -> None:
    text = JAPANESE_TEXT * 4  # wraps over several lines
    document = extract(_write(tmp_path, make_pdf([text])), FileType.PDF)

    assert [b.text for b in document.blocks] == [text]


def test_wrapped_english_lines_are_joined_with_single_spaces(tmp_path: Path) -> None:
    text = " ".join([ENGLISH_TEXT] * 3)
    document = extract(_write(tmp_path, make_pdf([text])), FileType.PDF)

    assert [b.text for b in document.blocks] == [text]


def test_word_hyphenated_across_lines_is_rejoined(tmp_path: Path) -> None:
    # Pad so that "inter-" ends the first line and "national" starts the second.
    filler = "x" * 85
    data = make_pdf_document([[f"{filler} inter- national standards apply."]])
    lines_text = extract(_write(tmp_path, data), FileType.PDF).text

    assert "international standards apply." in lines_text


# --- structure ---------------------------------------------------------------------------


def test_paragraph_boundaries_follow_vertical_spacing(tmp_path: Path) -> None:
    paragraphs = [
        "First paragraph about ingestion. " * 4,
        "第二段落です。文書は段落ごとに分けられます。" * 3,
        "Third paragraph, short.",
    ]
    document = extract(_write(tmp_path, make_pdf_document([paragraphs])), FileType.PDF)

    assert [b.text for b in document.blocks] == [p.strip() for p in paragraphs]


def test_lines_without_extra_spacing_stay_in_one_paragraph(tmp_path: Path) -> None:
    data = make_pdf_document([["Line one.", "Line two."]], paragraph_gap=0.0)
    document = extract(_write(tmp_path, data), FileType.PDF)

    assert [b.text for b in document.blocks] == ["Line one. Line two."]


def test_font_size_change_separates_a_heading(tmp_path: Path) -> None:
    data = make_pdf_document(
        [[Paragraph("Chapter 1 概要", 18), Paragraph(ENGLISH_TEXT, 10)]], paragraph_gap=0.0
    )
    document = extract(_write(tmp_path, data), FileType.PDF)

    assert [b.text for b in document.blocks] == ["Chapter 1 概要", ENGLISH_TEXT]


def test_text_order_follows_the_page(tmp_path: Path) -> None:
    paragraphs = [f"Paragraph number {n}." for n in range(1, 9)]
    document = extract(_write(tmp_path, make_pdf_document([paragraphs])), FileType.PDF)

    assert [b.text for b in document.blocks] == paragraphs


def test_nested_outline_becomes_section_path(tmp_path: Path) -> None:
    data = make_pdf(
        [ENGLISH_TEXT, JAPANESE_TEXT, ENGLISH_TEXT],
        toc=[(1, "Guide", 1), (2, "インストール", 2), (1, "Appendix", 3)],
    )
    document = extract(_write(tmp_path, data), FileType.PDF)

    sections = {b.page: b.section for b in document.blocks}
    assert sections == {1: "Guide", 2: "Guide > インストール", 3: "Appendix"}


def test_text_outside_the_media_box_is_ignored(tmp_path: Path) -> None:
    data = make_pdf_document([[ENGLISH_TEXT]], offpage_text="HIDDEN-WATERMARK")
    document = extract(_write(tmp_path, data), FileType.PDF)

    assert "HIDDEN" not in document.text
    assert document.text == ENGLISH_TEXT


def test_page_count_includes_blank_and_image_only_pages(tmp_path: Path) -> None:
    data = make_pdf([ENGLISH_TEXT, "", "", JAPANESE_TEXT], image_only_pages={2})
    document = extract(_write(tmp_path, data), FileType.PDF)

    assert document.page_count == 4
    assert {b.page for b in document.blocks} == {1, 4}


# --- failures ----------------------------------------------------------------------------


@pytest.mark.parametrize("user_password", ["secret", ""])
def test_encrypted_pdf_is_rejected_by_extraction(tmp_path: Path, user_password: str) -> None:
    path = _write(tmp_path, make_encrypted_pdf(user_password=user_password))
    with pytest.raises(IngestionError) as exc:
        extract(path, FileType.PDF)
    assert exc.value.code is ErrorCode.ENCRYPTED_PDF
    assert exc.value.retryable is False


def test_corrupt_pdf_fails_validation_cleanly(tmp_path: Path) -> None:
    path = _write(tmp_path, b"%PDF-1.7\n" + b"\x00\xffgarbage" * 50)
    with pytest.raises(IngestionError) as exc:
        validate_file(path, "doc.pdf")
    assert exc.value.code is ErrorCode.MALFORMED_PDF


def test_corrupt_pdf_fails_extraction_cleanly(tmp_path: Path) -> None:
    path = _write(tmp_path, b"%PDF-1.7\n" + b"\x00\xffgarbage" * 50)
    with pytest.raises(IngestionError) as exc:
        extract(path, FileType.PDF)
    assert exc.value.code is ErrorCode.EXTRACTION_FAILED
    assert "damaged" in exc.value.message


def test_truncated_pdf_never_raises_an_unexpected_error(tmp_path: Path) -> None:
    data = make_pdf([ENGLISH_TEXT, JAPANESE_TEXT])
    path = _write(tmp_path, data[: len(data) // 2])
    # PDFium may repair a truncated file; either outcome must be a clean one.
    try:
        document = extract(path, FileType.PDF)
    except IngestionError as exc:
        assert exc.code in {ErrorCode.EXTRACTION_FAILED, ErrorCode.NO_EXTRACTABLE_TEXT}
    else:
        assert all(block.text for block in document.blocks)


def test_text_less_pdf_fails_with_no_extractable_text(tmp_path: Path) -> None:
    path = _write(tmp_path, make_pdf(["", ""], image_only_pages={0}))
    with pytest.raises(IngestionError) as exc:
        extract(path, FileType.PDF)
    assert exc.value.code is ErrorCode.NO_EXTRACTABLE_TEXT


def test_concurrent_extraction_is_safe(tmp_path: Path) -> None:
    """PDFium is not thread-safe; open_pdf serialises access, so threads must not crash."""
    paths = [
        _write(tmp_path, make_pdf([ENGLISH_TEXT * 3, JAPANESE_TEXT * 3]), f"d{i}.pdf")
        for i in range(6)
    ]
    results: list[str] = []
    errors: list[BaseException] = []

    def run(path: Path) -> None:
        try:
            for _ in range(5):
                results.append(extract(path, FileType.PDF).text)
                validate_file(path, path.name)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 30
    assert len(set(results)) == 1


# --- chunk quality -----------------------------------------------------------------------


def test_chunks_from_a_bilingual_pdf_cover_the_text_without_damage(tmp_path: Path) -> None:
    pages = [
        [ENGLISH_TEXT * 2, ENGLISH_TEXT],
        [JAPANESE_TEXT * 2, JAPANESE_TEXT],
        [MIXED_TEXT * 3],
    ]
    data = make_pdf_document(pages, toc=[(1, "English", 1), (1, "日本語", 2)])
    document = extract(_write(tmp_path, data), FileType.PDF)
    config = ChunkingConfig(target_tokens=60, overlap_tokens=10, min_tokens=10)
    chunks = chunk_blocks(document.blocks, fake_token_count, config)

    # Every word of the source appears, in order, with nothing lost between chunks.
    source = _squash("".join(text for page in pages for text in page))
    assert all(_squash(chunk.text) in source for chunk in chunks)
    covered = "".join(_squash(c.text) for c in chunks)
    for sentence in (
        "Thequickbrownfox",
        "チャンクに分割されます。",
        "同じベクトル空間に埋め込みます。",
    ):
        assert (sentence in source) == (sentence in covered)

    # Sizes respect the configured bounds (no truncation, no tiny tail).
    assert all(0 < c.token_count <= config.target_tokens + config.min_tokens for c in chunks)
    # Japanese is never broken up by inserted spaces.
    japanese_chunks = [c for c in chunks if c.language is Language.JAPANESE]
    assert japanese_chunks
    assert all("。 " not in c.text and " 。" not in c.text for c in japanese_chunks)
    # Pages and sections are carried through.
    assert {c.page_start for c in chunks} == {1, 2, 3}
    assert {c.section for c in chunks if c.page_start == 2} == {"日本語"}
    # Chunks end on sentence boundaries where the text allows it.
    assert all(c.text.rstrip()[-1] in ".。" for c in chunks)
