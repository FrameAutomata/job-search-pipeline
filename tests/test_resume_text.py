"""Tests for pipeline.resume_text — format-dispatching resume extraction.

The module is the single source of truth for turning a resume file (PDF / DOCX /
ODT / TXT) into plain text, used by both the keyword-scoring filter and the UI
onboarding upload. PDF behavior is unchanged from the old filter.extract_resume_text;
these tests pin the new DOCX/ODT/TXT branches and the dispatch contract.
"""

from pathlib import Path

import pytest

from pipeline import resume_text


def _make_docx(path: Path, paragraphs, table=None) -> Path:
    from docx import Document
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=1, cols=len(table))
        for i, cell in enumerate(table):
            t.rows[0].cells[i].text = cell
    d.save(str(path))
    return path


def _make_odt(path: Path, headings_and_paras) -> Path:
    """headings_and_paras: list of ("h", text) | ("p", text) in document order."""
    from odf.opendocument import OpenDocumentText
    from odf.text import H, P
    od = OpenDocumentText()
    for kind, text in headings_and_paras:
        if kind == "h":
            od.text.addElement(H(outlinelevel=1, text=text))
        else:
            od.text.addElement(P(text=text))
    od.save(str(path))
    return path


class TestExtractResumeText:
    def test_docx_paragraphs_and_tables(self, tmp_path):
        """DOCX extraction returns paragraph text AND table-cell text (skills are
        frequently laid out in a table)."""
        f = _make_docx(
            tmp_path / "resume.docx",
            ["Jane Dev", "jane@example.com | Dallas, TX", "Python, AWS, Go"],
            table=["React", "Kubernetes"],
        )
        text = resume_text.extract_resume_text(f)
        assert "Jane Dev" in text
        assert "Python, AWS, Go" in text
        assert "React" in text and "Kubernetes" in text

    def test_odt_preserves_heading_and_paragraph_order(self, tmp_path):
        """ODT extraction walks the body in document order so the name (a heading)
        leads — the contact-info parser keys off the first line."""
        f = _make_odt(
            tmp_path / "resume.odt",
            [("h", "Jane Dev"), ("p", "jane@example.com | Dallas, TX"),
             ("p", "Python, AWS, Go")],
        )
        text = resume_text.extract_resume_text(f)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines[0] == "Jane Dev"
        assert "Python, AWS, Go" in text

    def test_txt_passthrough(self, tmp_path):
        f = tmp_path / "resume.txt"
        f.write_text("Jane Dev\nPython", encoding="utf-8")
        assert resume_text.extract_resume_text(f) == "Jane Dev\nPython"

    def test_suffix_is_case_insensitive(self, tmp_path):
        f = _make_docx(tmp_path / "resume.DOCX", ["Hello world"])
        assert "Hello world" in resume_text.extract_resume_text(f)

    def test_unsupported_suffix_raises_valueerror(self, tmp_path):
        f = tmp_path / "resume.rtf"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            resume_text.extract_resume_text(f)

    def test_pdf_routes_to_pdf_extractor(self, tmp_path, monkeypatch):
        """A .pdf suffix dispatches to the pdfplumber branch (patched here so the
        test needs no real PDF). Pins the dispatch, not pdfplumber itself."""
        called = {}

        def fake_pdf(p):
            called["p"] = p
            return "PDF TEXT"

        monkeypatch.setattr(resume_text, "_from_pdf", fake_pdf)
        f = tmp_path / "resume.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        assert resume_text.extract_resume_text(f) == "PDF TEXT"
        assert called["p"] == f


class TestExtractResumeBytes:
    def test_dispatches_on_filename_suffix(self, tmp_path):
        """extract_resume_bytes routes by the *filename's* suffix (the upload has
        bytes + a client filename, not a path on disk)."""
        f = _make_docx(tmp_path / "src.docx", ["Jane Dev", "Python, AWS"])
        data = f.read_bytes()
        text = resume_text.extract_resume_bytes(data, "whatever-the-user-named-it.docx")
        assert "Jane Dev" in text and "Python, AWS" in text

    def test_unsupported_filename_raises(self):
        with pytest.raises(ValueError):
            resume_text.extract_resume_bytes(b"x", "resume.rtf")


class TestSupportedSuffixes:
    def test_advertises_the_three_import_formats_plus_txt(self):
        s = set(resume_text.SUPPORTED_SUFFIXES)
        assert {".pdf", ".docx", ".odt", ".txt"} <= s
