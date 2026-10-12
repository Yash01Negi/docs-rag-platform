"""PDF access through pypdfium2 (Google PDFium; BSD-3-Clause / Apache-2.0).

PDFium is not thread-safe, and both the API (upload validation) and the worker (extraction)
call it from worker threads. Every use therefore goes through :func:`open_pdf`, which holds a
process-wide lock for as long as the document is open.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

_PDFIUM_LOCK = threading.Lock()


class PdfOpenError(Exception):
    """The file is not a PDF PDFium can read (damaged, truncated or not a PDF at all)."""


class PdfPasswordError(Exception):
    """The PDF needs a password to open."""


@contextmanager
def open_pdf(path: Path) -> Iterator[pdfium.PdfDocument]:
    with _PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as exc:
            if exc.err_code == pdfium_c.FPDF_ERR_PASSWORD:
                raise PdfPasswordError from exc
            raise PdfOpenError from exc
        try:
            yield document
        finally:
            document.close()


def is_encrypted(document: pdfium.PdfDocument) -> bool:
    """True for any encrypted PDF, including one with only an owner password, which opens
    without a password but still restricts use."""
    return pdfium_c.FPDF_GetSecurityHandlerRevision(document) != -1
