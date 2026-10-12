from pathlib import Path

import pytest

from app.db.models import FileType
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.extraction import (
    extract,
    extract_markdown,
    extract_text,
    join_lines,
    normalize_text,
)
from tests.samples import ENGLISH_TEXT, JAPANESE_TEXT, make_pdf


def _pdf(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(data)
    return path


def test_multi_page_pdf_preserves_page_numbers(tmp_path: Path) -> None:
    document = extract(_pdf(tmp_path, make_pdf([ENGLISH_TEXT, JAPANESE_TEXT])), FileType.PDF)

    assert document.page_count == 2
    pages = {block.page for block in document.blocks}
    assert pages == {1, 2}
    english = " ".join(b.text for b in document.blocks if b.page == 1)
    japanese = "".join(b.text for b in document.blocks if b.page == 2)
    assert "PostgreSQL" in english
    assert "チャンクに分割されます。" in japanese


def test_japanese_pdf_lines_are_joined_without_spaces(tmp_path: Path) -> None:
    document = extract(_pdf(tmp_path, make_pdf([JAPANESE_TEXT * 3])), FileType.PDF)
    text = document.text
    # The text box wraps lines; the join must not insert spaces between Japanese characters.
    assert "埋め込みベクトル" in text
    assert " 。" not in text


def test_empty_pdf_pages_are_skipped(tmp_path: Path) -> None:
    document = extract(_pdf(tmp_path, make_pdf([ENGLISH_TEXT, "", JAPANESE_TEXT])), FileType.PDF)
    assert document.page_count == 3
    assert {block.page for block in document.blocks} == {1, 3}


def test_pdf_outline_becomes_section_metadata(tmp_path: Path) -> None:
    data = make_pdf(
        [ENGLISH_TEXT, ENGLISH_TEXT, JAPANESE_TEXT],
        toc=[(1, "Introduction", 1), (1, "第2章 詳細", 3)],
    )
    document = extract(_pdf(tmp_path, data), FileType.PDF)
    sections = {block.page: block.section for block in document.blocks}
    assert sections == {1: "Introduction", 2: "Introduction", 3: "第2章 詳細"}


def test_scanned_pdf_without_text_fails_with_no_extractable_text(tmp_path: Path) -> None:
    data = make_pdf(["", ""], image_only_pages={0, 1})
    with pytest.raises(IngestionError) as exc:
        extract(_pdf(tmp_path, data), FileType.PDF)
    assert exc.value.code is ErrorCode.NO_EXTRACTABLE_TEXT
    assert "OCR" in exc.value.message
    assert exc.value.retryable is False


def test_markdown_headings_and_paragraphs() -> None:
    blocks = extract_markdown(
        "# 製品ガイド\n\n導入です。\n\n## Install\n\nRun the installer.\nThen restart.\n\n"
        "### 注意\n\n- 項目1\n- 項目2\n\n## Usage\n\nUse it.\n"
    )
    summary = [(b.is_heading, b.section, b.text) for b in blocks]
    assert summary == [
        (True, "製品ガイド", "製品ガイド"),
        (False, "製品ガイド", "導入です。"),
        (True, "製品ガイド > Install", "Install"),
        (False, "製品ガイド > Install", "Run the installer.\nThen restart."),
        (True, "製品ガイド > Install > 注意", "注意"),
        (False, "製品ガイド > Install > 注意", "- 項目1\n- 項目2"),
        (True, "製品ガイド > Usage", "Usage"),
        (False, "製品ガイド > Usage", "Use it."),
    ]


def test_markdown_setext_headings() -> None:
    blocks = extract_markdown("Title\n=====\n\nBody text.\n\nSub\n---\n\nMore.\n")
    assert [(b.is_heading, b.section) for b in blocks] == [
        (True, "Title"),
        (False, "Title"),
        (True, "Title > Sub"),
        (False, "Title > Sub"),
    ]


def test_markdown_code_fences_are_not_headings() -> None:
    blocks = extract_markdown("# Real\n\n```bash\n# not a heading\necho hi\n```\n")
    assert [b.text for b in blocks if b.is_heading] == ["Real"]
    assert "# not a heading" in blocks[1].text


def test_plain_text_paragraphs_and_heading_heuristic() -> None:
    blocks = extract_text(
        "第1章 概要\n\nこれは本文です。二文目です。\n\nIntroduction\n\n"
        "This paragraph\nwraps lines.\n\nA final sentence."
    )
    assert [(b.is_heading, b.section, b.text) for b in blocks] == [
        (True, "第1章 概要", "第1章 概要"),
        (False, "第1章 概要", "これは本文です。二文目です。"),
        (True, "Introduction", "Introduction"),
        (False, "Introduction", "This paragraph\nwraps lines."),
        (False, "Introduction", "A final sentence."),
    ]


def test_trailing_short_line_is_not_a_heading() -> None:
    blocks = extract_text("Body text here.\n\nThe end")
    assert not any(block.is_heading for block in blocks)


def test_text_extraction_reads_utf8_with_bom(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"\xef\xbb\xbf" + "こんにちは。\r\nHello.".encode())
    document = extract(path, FileType.TEXT)
    assert document.text == "こんにちは。\nHello."


def test_normalize_text_removes_control_characters_and_keeps_width() -> None:
    assert normalize_text("Ａ１\x0cｶﾀｶﾅ\x00\r\nok") == "Ａ１ｶﾀｶﾅ\nok"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["東京は日本の", "首都です。"], "東京は日本の首都です。"),
        (["The quick", "brown fox"], "The quick brown fox"),
        # A break next to a Japanese character is never a word boundary.
        (["BGE-M3は", "multilingual"], "BGE-M3はmultilingual"),
        (["それぞれのチャンクには", "1024次元"], "それぞれのチャンクには1024次元"),
        (["stored in", "PostgreSQLに保存"], "stored in PostgreSQLに保存"),
        (["  spaced   out ", "", "line"], "spaced out line"),
    ],
)
def test_join_lines(lines: list[str], expected: str) -> None:
    assert join_lines(lines) == expected
