"""Dropzone file reader — resolves base_filename to text content.

Checks my_profile/ for a file matching base_filename with three extensions,
in priority order: .docx > .md > .pdf. Returns the extracted text or None
if no matching file is found.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DROPZONE_DIR = Path("my_profile")
_PRIORITY = [".docx", ".md", ".pdf"]


def read_dropzone_file(base_filename: str) -> str | None:
    """Find and read a file from the my_profile/ dropzone.

    Searches for ``base_filename`` with extensions .docx, .md, .pdf (in that
    priority order). Returns the extracted text content, or None if no file is
    found with any supported extension.

    Args:
        base_filename: Filename without extension (e.g. "base_resume",
            "cover_letter").

    Returns:
        Extracted text content, or None if no matching file exists.
    """
    for ext in _PRIORITY:
        path = DROPZONE_DIR / f"{base_filename}{ext}"
        if path.exists():
            logger.debug("Reading dropzone file: %s", path)
            return _read_file(path)
    return None


def _read_file(path: Path) -> str:
    """Dispatch to the appropriate reader based on file extension."""
    ext = path.suffix.lower()
    if ext == ".md":
        return path.read_text(encoding="utf-8")
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".pdf":
        return _read_pdf(path)
    raise ValueError(f"Unsupported file extension: {ext}")


def _read_docx(path: Path) -> str:
    """Extract text from a .docx file using python-docx."""
    from docx import Document  # type: ignore[import-untyped]

    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def _read_pdf(path: Path) -> str:
    """Extract text from a .pdf file using pdfplumber."""
    import pdfplumber  # type: ignore[import-untyped]

    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n".join(pages)
