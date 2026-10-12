"""Text extraction from PDF (via PDFium), Markdown and plain-text files.

Each extractor returns an ordered list of :class:`Block` objects: paragraphs and headings
with their source location (PDF page, section heading). No OCR is attempted; a PDF whose
pages contain no extractable text fails with ``no_extractable_text``.
"""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from app.db.models import FileType
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.language import is_cjk
from app.ingestion.pdf import PdfOpenError, PdfPasswordError, is_encrypted, open_pdf

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

    English lines are joined with a space. A line break next to a CJK character is joined
    without one, because Japanese does not separate words with spaces: a PDF line break
    inside a Japanese sentence (including before a number or a Latin word, as in
    ``それぞれのチャンクには`` / ``1024次元``) is not a word boundary.
    """
    result = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if result and not (is_cjk(result[-1]) or is_cjk(line[0])):
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
    """Extract text blocks from a PDF with PDFium, page by page, in content order.

    PDFium reports text as lines; lines are grouped into paragraphs by their vertical
    spacing and font size (see :func:`_paragraphs`). Text outside the page's media box is
    ignored, and words hyphenated across a line break are re-joined.
    """
    blocks: list[Block] = []
    try:
        with open_pdf(path) as document:
            if is_encrypted(document):
                raise IngestionError(
                    ErrorCode.ENCRYPTED_PDF,
                    "Encrypted or password-protected PDFs are not supported.",
                )
            page_count = len(document)
            sections = _pdf_sections_by_page(document, page_count)
            for index in range(page_count):
                page_number = index + 1
                try:
                    lines = _page_lines(document, index)
                except Exception as exc:
                    raise IngestionError(
                        ErrorCode.EXTRACTION_FAILED,
                        f"Text could not be extracted from page {page_number}.",
                    ) from exc
                for paragraph in _paragraphs(lines):
                    text = join_lines([normalize_text(line) for line in paragraph])
                    if text:
                        blocks.append(Block(text, page=page_number, section=sections[index]))
    except PdfPasswordError as exc:
        raise IngestionError(
            ErrorCode.ENCRYPTED_PDF, "Encrypted or password-protected PDFs are not supported."
        ) from exc
    except PdfOpenError as exc:
        raise IngestionError(
            ErrorCode.EXTRACTION_FAILED, "The PDF could not be opened; it may be damaged."
        ) from exc
    return ExtractedDocument(blocks, page_count=page_count)


@dataclass
class _Line:
    text: str
    top: float
    bottom: float
    size: float


def _page_lines(document: pdfium.PdfDocument, index: int) -> list[_Line]:
    """Return the page's text lines in content order, with their vertical extent and the
    largest font size on each line."""
    page = document[index]
    textpage = page.get_textpage()
    try:
        left, bottom, right, top = page.get_mediabox()
        lines: list[_Line] = []
        chars: list[str] = []
        extent: list[float] = []  # [top, bottom, size] of the current line
        high_surrogate = 0
        pending_hyphen = False

        def flush() -> None:
            text = "".join(chars)
            if extent and text.strip():
                lines.append(_Line(text, extent[0], extent[1], extent[2]))
            chars.clear()
            extent.clear()

        for i in range(textpage.count_chars()):
            code = pdfium_c.FPDFText_GetUnicode(textpage, i)
            if 0xD800 <= code <= 0xDBFF:
                high_surrogate = code
                continue
            if 0xDC00 <= code <= 0xDFFF and high_surrogate:
                code = 0x10000 + ((high_surrogate - 0xD800) << 10) + (code - 0xDC00)
            high_surrogate = 0
            if code in (0x0D, 0x0A):
                flush()
                continue
            if code == 0:
                continue
            if code == 0x02:
                # PDFium joins a line ending in a hyphen with the next line and reports the
                # hyphen as 0x02. It is a word break when the next line starts in lower case
                # ("inter-" + "national"), and a real hyphen otherwise.
                pending_hyphen = True
                continue
            char = chr(code)
            if pending_hyphen and not char.isspace():
                if not char.islower():
                    chars.append("-")
                pending_hyphen = False
            if not char.isspace():
                x0, y0, x1, y1 = textpage.get_charbox(i, loose=True)
                if x1 < left or x0 > right or y1 < bottom or y0 > top:
                    continue  # outside the media box: not visible on the page
                size = pdfium_c.FPDFText_GetFontSize(textpage, i) or (y1 - y0)
                if extent:
                    extent[0] = max(extent[0], y1)
                    extent[1] = min(extent[1], y0)
                    extent[2] = max(extent[2], size)
                else:
                    extent.extend([y1, y0, size])
            chars.append(char)
        flush()
        return lines
    finally:
        textpage.close()
        page.close()


def _paragraphs(lines: list[_Line]) -> list[list[str]]:
    """Group consecutive lines into paragraphs.

    A new paragraph starts when the vertical gap to the previous line is more than half a
    line, when the text jumps upwards (a new column or region), or when the font size changes
    by more than 20% (typically a heading).
    """
    paragraphs: list[list[str]] = []
    previous: _Line | None = None
    for line in lines:
        if previous is None or _starts_paragraph(previous, line):
            paragraphs.append([])
        paragraphs[-1].append(line.text)
        previous = line
    return paragraphs


def _starts_paragraph(previous: _Line, line: _Line) -> bool:
    size = max(previous.size, line.size, 1.0)
    if abs(previous.size - line.size) > 0.2 * size:
        return True
    if line.top > previous.top + 0.5 * size:
        return True
    return previous.bottom - line.top > 0.5 * size


def _pdf_sections_by_page(document: pdfium.PdfDocument, page_count: int) -> list[str | None]:
    """Map each page to the most recent outline (bookmark) entry, if the PDF has one."""
    sections: list[str | None] = [None] * page_count
    try:
        toc = [
            (bookmark.level + 1, bookmark.get_title(), bookmark.get_dest())
            for bookmark in document.get_toc()
        ]
    except Exception:
        return sections
    path: list[str] = []
    starts: list[tuple[int, str]] = []
    for level, title, destination in toc:
        title = normalize_text(str(title)).strip()
        if not title:
            continue
        path = [*path[: level - 1], title]
        page_index = destination.get_index() if destination is not None else None
        if page_index is None or page_index < 0:
            continue
        starts.append((page_index, _SECTION_SEPARATOR.join(path)[:_MAX_SECTION_LENGTH]))
    for start_index, section in sorted(starts, key=lambda item: item[0]):
        for index in range(start_index, page_count):
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
