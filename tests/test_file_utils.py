"""Tests for the dropzone file reader (src/file_utils.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.file_utils import read_dropzone_file


class TestReadDropzoneFile:
    """Priority resolver: .docx > .md > .pdf, returns None when nothing found."""

    def _mock_exists(self, present: set[str]):
        """Return a side_effect that makes only listed suffixes 'exist'."""
        def exists_for(path: Path) -> bool:
            return path.suffix in present
        return exists_for

    def test_returns_none_when_no_file(self):
        with patch.object(Path, "exists", return_value=False):
            assert read_dropzone_file("base_resume") is None

    def test_reads_md_when_only_md_present(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile_dir = tmp_path / "my_profile"
        profile_dir.mkdir()
        (profile_dir / "base_resume.md").write_text("# My Resume\nPython dev.", encoding="utf-8")

        result = read_dropzone_file("base_resume")
        assert result == "# My Resume\nPython dev."

    def test_docx_preferred_over_md(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile_dir = tmp_path / "my_profile"
        profile_dir.mkdir()
        # Write an .md file so we can verify it's NOT the one returned
        (profile_dir / "base_resume.md").write_text("md content", encoding="utf-8")

        # Mock .docx reading
        with patch("src.file_utils._read_docx", return_value="docx content") as mock_docx:
            (profile_dir / "base_resume.docx").touch()
            result = read_dropzone_file("base_resume")

        assert result == "docx content"
        mock_docx.assert_called_once()

    def test_md_preferred_over_pdf(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile_dir = tmp_path / "my_profile"
        profile_dir.mkdir()
        (profile_dir / "base_resume.md").write_text("md content", encoding="utf-8")
        # Also create a PDF — should not be chosen
        (profile_dir / "base_resume.pdf").touch()

        result = read_dropzone_file("base_resume")
        assert result == "md content"

    def test_pdf_used_as_fallback(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile_dir = tmp_path / "my_profile"
        profile_dir.mkdir()
        (profile_dir / "base_resume.pdf").touch()

        with patch("src.file_utils._read_pdf", return_value="pdf content"):
            result = read_dropzone_file("base_resume")

        assert result == "pdf content"

    def test_cover_letter_filename(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile_dir = tmp_path / "my_profile"
        profile_dir.mkdir()
        (profile_dir / "cover_letter.md").write_text("Dear Hiring Manager...", encoding="utf-8")

        result = read_dropzone_file("cover_letter")
        assert result == "Dear Hiring Manager..."


class TestDocxReader:
    def test_extracts_paragraph_text(self):
        from src.file_utils import _read_docx

        p1 = MagicMock()
        p1.text = "First paragraph"
        p2 = MagicMock()
        p2.text = "   "  # whitespace-only, should be skipped
        p3 = MagicMock()
        p3.text = "Third paragraph"

        mock_doc = MagicMock()
        mock_doc.paragraphs = [p1, p2, p3]

        # Lazy import inside _read_docx — patch at the source module
        with patch("docx.Document", return_value=mock_doc):
            result = _read_docx(Path("fake.docx"))

        assert result == "First paragraph\nThird paragraph"


class TestPdfReader:
    def test_extracts_text_from_pages(self):
        import sys

        from src.file_utils import _read_pdf

        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "Page one text"
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "Page two text"
        mock_page3 = MagicMock()
        mock_page3.extract_text.return_value = None  # blank page — skipped

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page1, mock_page2, mock_page3]
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)

        # pdfplumber may not be installed in the test env — inject a mock module
        mock_pdfplumber = MagicMock()
        mock_pdfplumber.open.return_value = mock_pdf
        with patch.dict(sys.modules, {"pdfplumber": mock_pdfplumber}):
            result = _read_pdf(Path("fake.pdf"))

        assert result == "Page one text\nPage two text"
