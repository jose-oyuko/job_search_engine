"""Validating and reading uploaded CV files (spec §20 uploads, §15.4 step 1).

Files are untrusted. The extension AND the leading bytes must agree, the size is capped, DOCX
archives are checked for decompression bombs, and PDF reading is bounded in pages.
"""

import io
import logging
import re
import zipfile
from pathlib import PurePath
from typing import Any

from django.conf import settings
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from apps.core.logging import log_event

from .exceptions import InvalidCVFile, UnreadableCV

logger = logging.getLogger(__name__)

MAX_PDF_PAGES = 20
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MIN_TEXT_CHARS = 80  # fewer visible characters than this means scanned, image-only or empty
PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx"}

_BLANK_LINES = re.compile(r"\n{3,}")


def validate_upload(name: str, content: bytes) -> str:
    """Return the file kind (`"pdf"` or `"docx"`), or raise `InvalidCVFile`."""
    kind = ALLOWED_EXTENSIONS.get(PurePath(name).suffix.lower())
    if kind is None:
        raise InvalidCVFile("Please upload a PDF or DOCX file.")
    if not content:
        raise InvalidCVFile("That file is empty.")
    if len(content) > settings.CV_MAX_UPLOAD_BYTES:
        limit_mb = settings.CV_MAX_UPLOAD_BYTES // (1024 * 1024)
        raise InvalidCVFile(f"That file is too large. The limit is {limit_mb} MB.")
    if kind == "pdf" and not content.startswith(PDF_MAGIC):
        raise InvalidCVFile("That file does not look like a real PDF.")
    if kind == "docx" and not _looks_like_docx(content):
        raise InvalidCVFile("That file does not look like a real DOCX document.")
    return kind


def _looks_like_docx(content: bytes) -> bool:
    if not content.startswith(ZIP_MAGIC):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        return False
    return "word/document.xml" in names and total <= MAX_DOCX_UNCOMPRESSED_BYTES


def extract_text(content: bytes, kind: str) -> str:
    """Plain text from a validated file. Raises `UnreadableCV` if there is no usable text."""
    text = _extract_pdf(content) if kind == "pdf" else _extract_docx(content)
    text = text.replace("\x00", "")
    text = _BLANK_LINES.sub("\n\n", text).strip()
    if len(re.sub(r"\s+", "", text)) < MIN_TEXT_CHARS:
        if kind == "pdf":
            raise UnreadableCV(
                "This PDF has no readable text; it looks like a scan or an image. "
                "Please upload a text-based PDF or a DOCX, or fill in your profile by hand."
            )
        raise UnreadableCV("This document has almost no text. Please check the file and try again.")
    return text


def _extract_pdf(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnreadableCV("This PDF is password-protected. Please remove the password first.")
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n\n".join((page.extract_text() or "") for page in pages)
    except UnreadableCV:
        raise
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError, OSError):
        log_event(logger, logging.WARNING, "cv_pdf_unreadable", "PDF could not be parsed")
        raise UnreadableCV(
            "We could not read this PDF. Try saving it again or use a DOCX."
        ) from None


def _extract_docx(content: bytes) -> str:
    try:
        document = Document(io.BytesIO(content))
        lines: list[str] = []
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                lines.append(Paragraph(child, document).text)
            elif tag == "tbl":
                lines.extend(_table_lines(Table(child, document)))
        return "\n".join(lines)
    except (KeyError, ValueError, TypeError, zipfile.BadZipFile, OSError):
        log_event(logger, logging.WARNING, "cv_docx_unreadable", "DOCX could not be parsed")
        raise UnreadableCV("We could not read this document. Try saving it again.") from None


def _table_lines(table: Table) -> list[str]:
    lines: list[str] = []
    for row in table.rows:
        seen: list[Any] = []
        cells: list[str] = []
        for cell in row.cells:
            if any(cell._tc is other for other in seen):  # merged cells repeat
                continue
            seen.append(cell._tc)
            if cell.text.strip():
                cells.append(cell.text.strip())
        if cells:
            lines.append(" | ".join(cells))
    return lines
