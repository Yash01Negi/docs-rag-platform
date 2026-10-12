"""Text extraction from PDF, Markdown and plain-text files.

Each extractor returns an ordered list of :class:`Block` objects: paragraphs and headings
with their source location (PDF page, section heading). No OCR is attempted; a PDF whose
pages contain no extractable text fails with ``no_extractable_text``.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.db.models import FileType
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.language import is_cjk

# Control characters other than tab and newline (form feeds, NULs, C1 controls, ...).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_SETEXT_UNDERLINE = re.compile(r"^(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^(```|~~~)")
_SECTION_SEPARATOR = " > "
_MAX_SECTION_LENGTH = 500
# Plain-text heading heuristic: a short single-line paragraph without closing punctuation.
_TEXT_HEADING_MAX_CHARS = 60
_SENTENCE_END_CHARS = ".。!?！？:：;；,、)）」』"


@dataclass(frozen=True)
class Block:
    text: str
    page: int | None = None
    section: str | None = None
    is_heading: bool = False


@dataclass(frozen=True)
class ExtractedDocument:
    blocks: list[Block]
    page_count: int | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks)


def normalize_text(text: str) -> str:
    """NFC-normalise, unify newlines and strip control characters.

    NFC (not NFKC) is used deliberately: it does not rewrite full-width characters, circled
    numbers or half-width katakana, so the stored chunk text matches the source.
    """
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _CONTROL_CHARS.sub("", text)


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines of one paragraph.

    English lines are joined with a space. Lines that meet between two CJK characters are
    joined without one, because Japanese does not separate words with spaces and a PDF line
    break inside a sentence is not a word boundary.
    """
    result = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if result and not (is_cjk(result[-1]) and is_cjk(line[0])):
            result += " "
        result += line
    return re.sub(r"[ \t]+", " ", result)


def extract(path: Path, file_type: FileType) -> ExtractedDocument:
    if file_type is FileType.PDF:
        document = extract_pdf(path)
    else:
        try:
            raw = path.read_bytes().decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise IngestionError(
                ErrorCode.EXTRACTION_FAILED, "The file could not be read as UTF-8 text."
            ) from exc
        text = normalize_text(raw)
        blocks = extract_markdown(text) if file_type is FileType.MARKDOWN else extract_text(text)
        document = ExtractedDocument(blocks)

    if not any(block.text.strip() for block in document.blocks):
        raise IngestionError(
            ErrorCode.NO_EXTRACTABLE_TEXT,
            "No extractable text was found. Scanned or image-only documents need OCR, "
            "which is not supported.",
        )
    return document


def extract_pdf(path: Path) -> ExtractedDocument:
    try:
        document = pymupdf.open(path, filetype="pdf")
    except Exception as exc:
        raise IngestionError(
            ErrorCode.EXTRACTION_FAILED, "The PDF could not be opened; it may be damaged."
        ) from exc

    blocks: list[Block] = []
    with document:
        if document.needs_pass:
            raise IngestionError(
                ErrorCode.ENCRYPTED_PDF, "Encrypted or password-protected PDFs are not supported."
            )
        sections = _pdf_sections_by_page(document)
        flags = pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_MEDIABOX_CLIP
        for index, page in enumerate(document):
            page_number = index + 1
            try:
                raw_blocks = page.get_text("blocks", flags=flags, sort=True)
            except Exception as exc:
                raise IngestionError(
                    ErrorCode.EXTRACTION_FAILED,
                    f"Text could not be extracted from page {page_number}.",
                ) from exc
            for raw in raw_blocks:
                # (x0, y0, x1, y1, text, block_no, block_type); type 0 is text, 1 is image.
                if raw[6] != 0:
                    continue
                text = join_lines(normalize_text(raw[4]).split("\n"))
                if text:
                    blocks.append(Block(text, page=page_number, section=sections[index]))
        page_count = int(document.page_count)
    return ExtractedDocument(blocks, page_count=page_count)


def _pdf_sections_by_page(document: pymupdf.Document) -> list[str | None]:
    """Map each page to the most recent outline (bookmark) entry, if the PDF has one."""
    sections: list[str | None] = [None] * document.page_count
    try:
        toc = document.get_toc(simple=True)
    except Exception:
        return sections
    path: list[str] = []
    starts: list[tuple[int, str]] = []
    for level, title, page in toc:
        title = normalize_text(str(title)).strip()
        if not title or level < 1:
            continue
        path = [*path[: level - 1], title]
        starts.append((int(page), _SECTION_SEPARATOR.join(path)[:_MAX_SECTION_LENGTH]))
    for start_page, section in sorted(starts, key=lambda item: item[0]):
        for index in range(max(start_page - 1, 0), document.page_count):
            sections[index] = section
    return sections


def extract_markdown(text: str) -> list[Block]:
    """Split Markdown into heading and paragraph blocks, tracking the heading path."""
    blocks: list[Block] = []
    headings: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def section() -> str | None:
        return _SECTION_SEPARATOR.join(headings)[:_MAX_SECTION_LENGTH] if headings else None

    def flush() -> None:
        if paragraph:
            content = "\n".join(line.rstrip() for line in paragraph).strip("\n")
            if content.strip():
                blocks.append(Block(content, section=section()))
            paragraph.clear()

    def add_heading(level: int, title: str) -> None:
        nonlocal headings
        headings = [*headings[: level - 1], title]
        blocks.append(Block(title, section=section(), is_heading=True))

    for line in text.split("\n"):
        if _FENCE.match(line.strip()):
            in_fence = not in_fence
            paragraph.append(line)
            continue
        if in_fence:
            paragraph.append(line)
            continue
        atx = _ATX_HEADING.match(line)
        if atx:
            flush()
            add_heading(len(atx.group(1)), atx.group(2).strip())
            continue
        if _SETEXT_UNDERLINE.match(line) and len(paragraph) == 1 and paragraph[0].strip():
            title = paragraph.pop().strip()
            add_heading(1 if line.lstrip().startswith("=") else 2, title)
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line)
    flush()
    return blocks


def extract_text(text: str) -> list[Block]:
    """Split plain text into paragraphs at blank lines.

    A short single-line paragraph without closing punctuation that is followed by more text
    is treated as a heading (for example "第1章 概要" or "Introduction"). This is a heuristic:
    plain text has no markup to say what a heading is.
    """
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    blocks: list[Block] = []
    current_section: str | None = None
    for index, paragraph in enumerate(paragraphs):
        lines = [line for line in paragraph.split("\n") if line.strip()]
        single = lines[0].strip() if len(lines) == 1 else ""
        if (
            single
            and len(single) <= _TEXT_HEADING_MAX_CHARS
            and single[-1] not in _SENTENCE_END_CHARS
            and index + 1 < len(paragraphs)
        ):
            current_section = single
            blocks.append(Block(single, section=current_section, is_heading=True))
            continue
        content = "\n".join(line.rstrip() for line in paragraph.strip("\n").split("\n"))
        blocks.append(Block(content.strip(), section=current_section))
    return blocks
