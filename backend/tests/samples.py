"""Small documents generated on the fly for tests (no binary fixtures in the repository).

PDFs are written by a tiny PDF writer below rather than by a PDF library, so the test suite
does not depend on PyMuPDF (AGPL). Text uses one non-embedded Type 0 font with a ToUnicode
map, which is enough for any PDF text extractor to recover English and Japanese exactly.
Encryption is applied with pypdf (BSD-3-Clause), a dev-only dependency.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass

ENGLISH_TEXT = (
    "YashRAG stores documents in PostgreSQL. Each document is split into chunks. "
    "Every chunk receives a dense embedding vector with 1024 dimensions. "
    "The worker processes uploads in the background so the API stays responsive."
)
JAPANESE_TEXT = (
    "YashRAGは文書をPostgreSQLに保存します。各文書はチャンクに分割されます。"
    "それぞれのチャンクには1024次元の埋め込みベクトルが付与されます。"
    "ワーカーがバックグラウンドで処理するため、APIは応答性を保ちます。"
)

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
_MARGIN = 72
_LINE_WIDTH_UNITS = (PAGE_WIDTH - 2 * _MARGIN) * 1000  # in glyph units x font size


@dataclass(frozen=True)
class Paragraph:
    """A paragraph laid out by :func:`make_pdf_document`."""

    text: str
    size: float = 10.0


def _is_wide(char: str) -> bool:
    return ord(char) >= 0x2E80


def _advance(char: str) -> int:
    # Glyph widths declared in the font's /W array: CJK full width, everything else half.
    return 1000 if _is_wide(char) else 500


def wrap(text: str, size: float) -> list[str]:
    """Greedy line wrapping by declared glyph widths; breaks at spaces when possible."""
    limit = _LINE_WIDTH_UNITS / size
    lines: list[str] = []
    line = ""
    width = 0
    last_space = -1
    for char in text:
        line += char
        width += _advance(char)
        if char == " ":
            last_space = len(line) - 1
        if width > limit:
            if last_space > 0 and not _is_wide(char):
                lines.append(line[:last_space])
                line = line[last_space + 1 :]
            else:
                lines.append(line[:-1])
                line = char
            width = sum(_advance(c) for c in line)
            last_space = line.rfind(" ")
    if line.strip():
        lines.append(line)
    return [item.strip() for item in lines]


def _hex_text(text: str) -> str:
    # Identity-H: each character is written as its 2-byte code, and the CID equals the code.
    return "".join(f"{ord(char):04X}" for char in text)


def _pdf_string(text: str) -> str:
    return "<FEFF" + "".join(f"{unit:04X}" for unit in _utf16_units(text)) + ">"


def _utf16_units(text: str) -> list[int]:
    data = text.encode("utf-16-be")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def _to_unicode_cmap(chars: set[str]) -> bytes:
    blocks = sorted({ord(char) >> 8 for char in chars})
    ranges = "\n".join(f"<{b:02X}00> <{b:02X}FF> <{b:02X}00>" for b in blocks)
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(blocks)} beginbfrange\n{ranges}\nendbfrange\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")


class _Writer:
    def __init__(self) -> None:
        self.objects: list[bytes | None] = []

    def reserve(self) -> int:
        self.objects.append(None)
        return len(self.objects)

    def set(self, number: int, body: str | bytes) -> None:
        self.objects[number - 1] = body.encode("latin-1") if isinstance(body, str) else body

    def add(self, body: str | bytes) -> int:
        number = self.reserve()
        self.set(number, body)
        return number

    def stream(self, data: bytes, extra: str = "") -> int:
        return self.add(
            f"<< /Length {len(data)} {extra}>>\nstream\n".encode("latin-1") + data + b"\nendstream"
        )

    def render(self, root: int) -> bytes:
        out = io.BytesIO()
        out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(self.objects, start=1):
            assert body is not None, f"object {number} was reserved but never written"
            offsets.append(out.tell())
            out.write(f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n")
        xref = out.tell()
        out.write(f"xref\n0 {len(self.objects) + 1}\n0000000000 65535 f \n".encode("latin-1"))
        for offset in offsets:
            out.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        out.write(
            f"trailer\n<< /Size {len(self.objects) + 1} /Root {root} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n".encode("latin-1")
        )
        return out.getvalue()


def make_pdf_document(
    pages: Sequence[Sequence[Paragraph | str] | None],
    *,
    toc: Sequence[tuple[int, str, int]] | None = None,
    image_only_pages: set[int] | None = None,
    paragraph_gap: float = 1.0,
    offpage_text: str | None = None,
) -> bytes:
    """Build a PDF where each page is a list of paragraphs (``None`` = blank page).

    Lines inside a paragraph use 1.2 x font size leading; consecutive paragraphs are
    separated by an extra ``paragraph_gap`` x font size. ``toc`` entries are
    ``(level, title, page_number)`` like a PDF outline. ``offpage_text`` is drawn on every
    page outside the media box, where no viewer shows it.
    """
    writer = _Writer()
    catalog = writer.reserve()
    pages_root = writer.reserve()

    all_chars: set[str] = {" "}
    for page in pages:
        for paragraph in page or []:
            all_chars.update(paragraph.text if isinstance(paragraph, Paragraph) else paragraph)
    all_chars.update(offpage_text or "")
    to_unicode = writer.stream(_to_unicode_cmap(all_chars))
    descriptor = writer.add(
        "<< /Type /FontDescriptor /FontName /TestSans /Flags 4 /FontBBox [0 -200 1000 900] "
        "/ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 >>"
    )
    cid_font = writer.add(
        "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /TestSans "
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {descriptor} 0 R /DW 1000 /W [0 11903 500] /CIDToGIDMap /Identity >>"
    )
    font = writer.add(
        "<< /Type /Font /Subtype /Type0 /BaseFont /TestSans /Encoding /Identity-H "
        f"/DescendantFonts [{cid_font} 0 R] /ToUnicode {to_unicode} 0 R >>"
    )

    page_numbers: list[int] = []
    for index, page in enumerate(pages):
        commands: list[str] = []
        if image_only_pages and index in image_only_pages:
            commands.append("0.5 g 72 72 228 228 re f")
        else:
            y = PAGE_HEIGHT - _MARGIN
            for item in page or []:
                paragraph = item if isinstance(item, Paragraph) else Paragraph(item)
                for line in wrap(paragraph.text, paragraph.size):
                    y -= paragraph.size * 1.2
                    commands.append(
                        f"BT /F1 {paragraph.size:g} Tf {_MARGIN} {y:.2f} Td "
                        f"<{_hex_text(line)}> Tj ET"
                    )
                y -= paragraph.size * paragraph_gap
        if offpage_text:
            commands.append(f"BT /F1 10 Tf -2000 -2000 Td <{_hex_text(offpage_text)}> Tj ET")
        content = writer.stream("\n".join(commands).encode("latin-1"))
        page_numbers.append(
            writer.add(
                f"<< /Type /Page /Parent {pages_root} 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
            )
        )
    writer.set(
        pages_root,
        f"<< /Type /Pages /Count {len(page_numbers)} "
        f"/Kids [{' '.join(f'{n} 0 R' for n in page_numbers)}] >>",
    )

    outlines = _write_outline(writer, toc, page_numbers) if toc else None
    writer.set(
        catalog,
        f"<< /Type /Catalog /Pages {pages_root} 0 R"
        + (f" /Outlines {outlines} 0 R /PageMode /UseOutlines" if outlines else "")
        + " >>",
    )
    return writer.render(catalog)


def _write_outline(
    writer: _Writer, toc: Sequence[tuple[int, str, int]], page_objects: list[int]
) -> int:
    root = writer.reserve()
    # Build a tree from (level, title, page) entries.
    nodes: list[dict] = []
    stack: list[dict] = [{"children": nodes, "level": 0}]
    for level, title, page in toc:
        node = {"title": title, "page": page, "children": [], "level": level}
        while stack[-1]["level"] >= level:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)

    def write_level(children: list[dict], parent: int) -> tuple[int, int, int]:
        numbers = [writer.reserve() for _ in children]
        total = 0
        for i, (node, number) in enumerate(zip(children, numbers, strict=True)):
            links = f"/Parent {parent} 0 R"
            if i > 0:
                links += f" /Prev {numbers[i - 1]} 0 R"
            if i + 1 < len(numbers):
                links += f" /Next {numbers[i + 1]} 0 R"
            sub = ""
            if node["children"]:
                first, last, count = write_level(node["children"], number)
                sub = f" /First {first} 0 R /Last {last} 0 R /Count {count}"
                total += count
            target = page_objects[node["page"] - 1]
            writer.set(
                number,
                f"<< /Title {_pdf_string(node['title'])} {links}{sub} "
                f"/Dest [{target} 0 R /XYZ 0 {PAGE_HEIGHT} 0] >>",
            )
            total += 1
        return numbers[0], numbers[-1], total

    first, last, count = write_level(nodes, root)
    writer.set(root, f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {count} >>")
    return root


def make_pdf(
    pages: list[str],
    *,
    toc: list[tuple[int, str, int]] | None = None,
    image_only_pages: set[int] | None = None,
) -> bytes:
    """Build a PDF with one text string (one paragraph) per page; ``""`` is a blank page.

    Page indexes in ``image_only_pages`` get a filled rectangle instead of text, like a scan.
    """
    return make_pdf_document(
        [[text] if text else None for text in pages],
        toc=toc,
        image_only_pages=image_only_pages,
    )


def make_encrypted_pdf(*, user_password: str = "secret") -> bytes:  # noqa: S107 - test data
    """An encrypted PDF. With ``user_password=""`` it opens without a password but is still
    encrypted (owner password only)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf(["Confidential"]))))
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")  # noqa: S106
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
