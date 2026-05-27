"""Tests for pipeline/app/data.py — applications.md parsing + report lookup."""

from pathlib import Path

import pytest

from pipeline.app import data


SAMPLE_APPLICATIONS = """# Applications Tracker

| # | Date | Company | Role | Score | Status | PDF | Report | Notes |
|---|------|---------|------|-------|--------|-----|--------|-------|
| 1 | 2026-05-27 | Acme | Backend Engineer | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme-2026-05-27.md) | APPLY strong match |
| 2 | 2026-05-27 | Globex | Frontend Engineer | 2.8/5 | SKIP | ❌ | [002](reports/002-globex-2026-05-27.md) | SKIP weak fit |
| 3 | 2026-05-27 | Initech | Full Stack Engineer | 3.5/5 | Applied | ✅ | [003](reports/003-initech-2026-05-27.md) | CONSIDER ok |
"""


class TestParseApplications:
    def test_missing_file_returns_empty(self, tmp_path):
        assert data.parse_applications(tmp_path / "nope.md") == []

    def test_header_only_returns_empty(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "# Applications Tracker\n\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n",
            encoding="utf-8",
        )
        assert data.parse_applications(f) == []

    def test_parses_rows(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert len(rows) == 3
        first = rows[0]
        assert first["company"] == "Acme"
        assert first["role"] == "Backend Engineer"
        assert first["status"] == "Evaluated"
        assert first["num"] == "1"

    def test_extracts_report_num_and_path(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert rows[0]["report_num"] == "001"
        assert rows[0]["report_path"] == "reports/001-acme-2026-05-27.md"

    def test_parses_score_value(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert rows[0]["score_value"] == 4.2
        assert rows[1]["score_value"] == 2.8
        assert rows[2]["score_value"] == 3.5

    def test_score_value_none_when_unparseable(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "# Applications Tracker\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-05-27 | Acme | Eng | n/a | Evaluated | ❌ | [001](reports/001-x.md) | note |\n",
            encoding="utf-8",
        )
        rows = data.parse_applications(f)
        assert rows[0]["score_value"] is None

    def test_skips_separator_and_header(self, tmp_path):
        # No data row should ever be the header or separator.
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert all(r["num"] not in ("#", "num") for r in rows)
        assert all("---" not in r["date"] for r in rows)

    def test_row_missing_columns_skipped(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-05-27 | Acme |\n"  # too few columns
            "| 2 | 2026-05-27 | Globex | Eng | 3.0/5 | Evaluated | ❌ | [002](reports/002-x.md) | ok |\n",
            encoding="utf-8",
        )
        rows = data.parse_applications(f)
        assert len(rows) == 1
        assert rows[0]["company"] == "Globex"


class TestFindReportFile:
    def _make_reports(self, tmp_path):
        d = tmp_path / "reports"
        d.mkdir()
        (d / "001-acme-2026-05-27.md").write_text("# Acme report", encoding="utf-8")
        (d / "042-globex-2026-05-27.md").write_text("# Globex report", encoding="utf-8")
        return d

    def test_finds_by_padded_number(self, tmp_path):
        d = self._make_reports(tmp_path)
        f = data.find_report_file(d, "001")
        assert f is not None and f.name == "001-acme-2026-05-27.md"

    def test_finds_with_padding_mismatch(self, tmp_path):
        # Tracker may store "42" while the file is "042-...".
        d = self._make_reports(tmp_path)
        f = data.find_report_file(d, "42")
        assert f is not None and f.name == "042-globex-2026-05-27.md"

    def test_missing_number_returns_none(self, tmp_path):
        d = self._make_reports(tmp_path)
        assert data.find_report_file(d, "999") is None

    def test_non_numeric_returns_none(self, tmp_path):
        d = self._make_reports(tmp_path)
        assert data.find_report_file(d, "abc") is None

    def test_missing_dir_returns_none(self, tmp_path):
        assert data.find_report_file(tmp_path / "nope", "001") is None


class TestRenderReportHtml:
    def test_renders_markdown_or_falls_back(self, tmp_path):
        f = tmp_path / "r.md"
        f.write_text("# Title\n\nSome **bold** text.", encoding="utf-8")
        html = data.render_report_html(f)
        # Either real markdown (markdown installed) or a <pre> fallback — both
        # must contain the source text.
        assert "Title" in html
        assert "bold" in html

    def test_markdown_renders_headings_when_available(self, tmp_path):
        pytest.importorskip("markdown")
        f = tmp_path / "r.md"
        f.write_text("# Heading One\n\nbody", encoding="utf-8")
        html = data.render_report_html(f)
        assert "<h1>" in html.lower()
