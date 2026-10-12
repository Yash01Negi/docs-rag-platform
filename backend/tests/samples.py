"""Small documents generated on the fly for tests (no binary fixtures in the repository)."""

import pymupdf

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


def make_pdf(
    pages: list[str],
    *,
    toc: list[tuple[int, str, int]] | None = None,
    image_only_pages: set[int] | None = None,
) -> bytes:
    """Build a PDF with one text string per page (CJK text uses PyMuPDF's built-in font).

    Page indexes in ``image_only_pages`` get a drawn rectangle instead of text, like a scan.
    """
    document = pymupdf.open()
    for index, text in enumerate(pages):
        page = document.new_page()
        if image_only_pages and index in image_only_pages:
            page.draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
            continue
        if text:
            box = pymupdf.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
            fontname = "japan" if any(ord(c) > 0x3000 for c in text) else "helv"
            page.insert_textbox(box, text, fontname=fontname, fontsize=10)
    if toc:
        document.set_toc([list(entry) for entry in toc])
    data = document.tobytes()
    document.close()
    return data


def make_encrypted_pdf(*, user_password: str = "secret") -> bytes:  # noqa: S107 - test data
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "Confidential", fontsize=12)
    data = document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw=user_password
    )
    document.close()
    return data
