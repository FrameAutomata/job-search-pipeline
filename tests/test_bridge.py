"""Tests for pipeline/bridge.py"""

from datetime import date
from pathlib import Path

import pytest

from pipeline import bridge as bridge_mod


class TestLoadSeenUrls:
    """Test bridge.load_seen_urls function."""

    def test_load_seen_urls_from_scan_history(self, career_ops_dir):
        """Load URLs from scan-history.tsv."""
        hist = career_ops_dir / "data" / "scan-history.tsv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            "https://job1.com\t2026-05-01\tjobspy\tengineer\tacme\tadded\n"
            "https://job2.com\t2026-05-02\tjobspy\tdeveloper\tglobex\tadded\n"
        )

        result = bridge_mod.load_seen_urls(career_ops_dir)
        assert "https://job1.com" in result
        assert "https://job2.com" in result

    def test_load_seen_urls_from_pipeline_md(self, career_ops_dir):
        """Load URLs from pipeline.md."""
        pipeline = career_ops_dir / "data" / "pipeline.md"
        pipeline.parent.mkdir(parents=True, exist_ok=True)
        pipeline.write_text(
            "# Pipeline\n\n"
            "## Pendientes\n"
            "- [ ] https://job1.com | acme | engineer\n"
            "- [x] https://job2.com | globex | developer\n"
        )

        result = bridge_mod.load_seen_urls(career_ops_dir)
        assert "https://job1.com" in result
        assert "https://job2.com" in result

    def test_load_seen_urls_from_applications_md(self, career_ops_dir):
        """Load URLs from applications.md."""
        applications = career_ops_dir / "data" / "applications.md"
        applications.parent.mkdir(parents=True, exist_ok=True)
        applications.write_text(
            "# Applications\n\n"
            "| # | Date | Company | Role | URL | Status |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 1 | 2026-05-01 | acme | engineer | https://job1.com | applied |\n"
        )

        result = bridge_mod.load_seen_urls(career_ops_dir)
        assert "https://job1.com" in result

    def test_load_seen_urls_all_three_sources_merged(self, career_ops_dir):
        """Load from all three files and merge."""
        hist = career_ops_dir / "data" / "scan-history.tsv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text("url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
                        "https://from-history.com\t2026-05-01\tjobspy\tengineer\tacme\tadded\n")

        pipeline = career_ops_dir / "data" / "pipeline.md"
        pipeline.write_text("# Pipeline\n## Pendientes\n- [ ] https://from-pipeline.com | a | b\n")

        applications = career_ops_dir / "data" / "applications.md"
        applications.write_text("# Apps\n| # | Date | Company | Role | URL | Status |\n"
                                "| 1 | 2026-05-01 | a | b | https://from-apps.com | yes |\n")

        result = bridge_mod.load_seen_urls(career_ops_dir)
        assert len(result) >= 3
        assert "https://from-history.com" in result
        assert "https://from-pipeline.com" in result
        assert "https://from-apps.com" in result

    def test_load_seen_urls_no_files_returns_empty(self, career_ops_dir):
        """All files missing returns empty set."""
        result = bridge_mod.load_seen_urls(career_ops_dir)
        assert result == set()

    def test_load_seen_urls_malformed_tsv_line_skipped(self, career_ops_dir):
        """Malformed TSV line (no tab) is skipped gracefully."""
        hist = career_ops_dir / "data" / "scan-history.tsv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            "https://good.com\t2026-05-01\tjobspy\tengineer\tacme\tadded\n"
            "no-tab-in-this-line\n"
            "https://also-good.com\t2026-05-02\tjobspy\tdev\tglobex\tadded\n"
        )

        result = bridge_mod.load_seen_urls(career_ops_dir)
        # Should have parsed the valid lines, skipped the bad one
        assert "https://good.com" in result
        assert "https://also-good.com" in result


class TestLoadSeenCompanyRoles:
    """Test bridge.load_seen_company_roles function."""

    def test_load_seen_company_roles_parses_table(self, career_ops_dir):
        """Parse company::role from Markdown table."""
        applications = career_ops_dir / "data" / "applications.md"
        applications.parent.mkdir(parents=True, exist_ok=True)
        # Markdown table format expected by the regex: | col1 | col2 | company | role |
        applications.write_text(
            "# Applications\n\n"
            "| Date | ID | acme | engineer |\n"
            "| 2026-05-01 | 1 | globex | developer |\n"
        )

        result = bridge_mod.load_seen_company_roles(career_ops_dir)
        # The regex matches tables with the pattern | any | any | company | role |
        assert len(result) >= 1
        # At least one of the company::role pairs should be found
        assert any("::" in pair for pair in result)

    def test_load_seen_company_roles_case_insensitive(self, career_ops_dir):
        """Company::role keys are lowercased."""
        applications = career_ops_dir / "data" / "applications.md"
        applications.parent.mkdir(parents=True, exist_ok=True)
        applications.write_text(
            "| Date | ID | ACME | Engineer |\n"
        )

        result = bridge_mod.load_seen_company_roles(career_ops_dir)
        assert "acme::engineer" in result

    def test_load_seen_company_roles_skips_header_row(self, career_ops_dir):
        """Header row (company='company') is skipped."""
        applications = career_ops_dir / "data" / "applications.md"
        applications.parent.mkdir(parents=True, exist_ok=True)
        applications.write_text(
            "| # | Date | Company | Role | URL | Status |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
        )

        result = bridge_mod.load_seen_company_roles(career_ops_dir)
        assert "company::role" not in result

    def test_load_seen_company_roles_no_file_returns_empty(self, career_ops_dir):
        """Missing file returns empty set."""
        result = bridge_mod.load_seen_company_roles(career_ops_dir)
        assert result == set()


class TestAppendToPipeline:
    """Test bridge.append_to_pipeline function."""

    def test_append_creates_file_if_not_exists(self, career_ops_dir):
        """Creates pipeline.md with correct structure."""
        offers = [
            {"url": "https://job1.com", "company": "acme", "title": "engineer"}
        ]

        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        assert pipeline.exists()

        content = pipeline.read_text()
        assert "# Pipeline" in content
        assert "## Pendientes" in content
        assert "- [ ] https://job1.com | acme | engineer" in content

    def test_append_inserts_under_existing_pendientes(self, career_ops_dir):
        """Insert under existing ## Pendientes section."""
        pipeline = career_ops_dir / "data" / "pipeline.md"
        pipeline.parent.mkdir(parents=True, exist_ok=True)
        pipeline.write_text(
            "# Pipeline\n\n"
            "## Pendientes\n"
            "- [ ] https://old.com | old_co | old_role\n\n"
            "## Procesadas\n"
            "- [x] https://prev.com | prev_co | prev\n"
        )

        offers = [
            {"url": "https://new.com", "company": "new_co", "title": "new_role"}
        ]
        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        content = pipeline.read_text()
        # New offer should be inserted before Procesadas
        assert "- [ ] https://new.com | new_co | new_role" in content
        lines = content.split("\n")
        old_idx = next(i for i, l in enumerate(lines) if "https://new.com" in l)
        procesadas_idx = next(i for i, l in enumerate(lines) if "## Procesadas" in l)
        assert old_idx < procesadas_idx

    def test_append_creates_pendientes_before_procesadas(self, career_ops_dir):
        """Create ## Pendientes before ## Procesadas when missing."""
        pipeline = career_ops_dir / "data" / "pipeline.md"
        pipeline.parent.mkdir(parents=True, exist_ok=True)
        pipeline.write_text("# Pipeline\n\n## Procesadas\nSome processed items\n")

        offers = [
            {"url": "https://new.com", "company": "new_co", "title": "new_role"}
        ]
        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        content = pipeline.read_text()
        assert "## Pendientes" in content
        lines = content.split("\n")
        pendientes_idx = next(i for i, l in enumerate(lines) if "## Pendientes" in l)
        procesadas_idx = next(i for i, l in enumerate(lines) if "## Procesadas" in l)
        assert pendientes_idx < procesadas_idx

    def test_append_creates_pendientes_at_end_if_no_sections(self, career_ops_dir):
        """Create ## Pendientes at end when no sections exist."""
        pipeline = career_ops_dir / "data" / "pipeline.md"
        pipeline.parent.mkdir(parents=True, exist_ok=True)
        pipeline.write_text("# Pipeline\n\nSome intro text\n")

        offers = [
            {"url": "https://new.com", "company": "new_co", "title": "new_role"}
        ]
        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        content = pipeline.read_text()
        assert "## Pendientes" in content
        assert "- [ ] https://new.com | new_co | new_role" in content

    def test_append_multiple_offers_all_written(self, career_ops_dir):
        """Multiple offers are all written."""
        offers = [
            {"url": "https://job1.com", "company": "a", "title": "x"},
            {"url": "https://job2.com", "company": "b", "title": "y"},
            {"url": "https://job3.com", "company": "c", "title": "z"},
        ]

        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        content = pipeline.read_text()

        for offer in offers:
            expected = f"- [ ] {offer['url']} | {offer['company']} | {offer['title']}"
            assert expected in content

    def test_append_format_is_correct(self, career_ops_dir):
        """Format matches exactly."""
        offers = [
            {"url": "http://example.com", "company": "Acme", "title": "Dev", "description": ""}
        ]

        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        content = pipeline.read_text()
        assert "- [ ] http://example.com | Acme | Dev" in content

    def test_append_includes_description_as_collapsible(self, career_ops_dir):
        """Job description is included as a <details> section."""
        offers = [
            {
                "url": "http://example.com",
                "company": "Acme",
                "title": "Dev",
                "description": "Build backend services using Python and Django.",
            }
        ]

        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        content = pipeline.read_text()
        assert "<details><summary>Description</summary>" in content
        assert "Build backend services using Python and Django." in content
        assert "</details>" in content

    def test_append_escapes_html_in_description(self, career_ops_dir):
        """HTML special characters in description are escaped."""
        offers = [
            {
                "url": "http://example.com",
                "company": "Acme",
                "title": "Dev",
                "description": "Work with <script> and & symbols",
            }
        ]

        bridge_mod.append_to_pipeline(career_ops_dir, offers)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        content = pipeline.read_text()
        # Should be escaped
        assert "&lt;script&gt;" in content
        assert "&amp;" in content
        # Raw versions should not appear
        assert "<script>" not in content


class TestAppendToScanHistory:
    """Test bridge.append_to_scan_history function."""

    def test_append_scan_history_creates_file_with_header(self, career_ops_dir):
        """Creates file with correct header."""
        offers = [
            {"url": "https://job1.com", "company": "acme", "title": "engineer"}
        ]

        bridge_mod.append_to_scan_history(career_ops_dir, offers, "2026-05-12")

        hist = career_ops_dir / "data" / "scan-history.tsv"
        assert hist.exists()

        lines = hist.read_text().strip().split("\n")
        assert len(lines) >= 2  # Header + at least one row
        assert "url\tfirst_seen" in lines[0]

    def test_append_scan_history_appends_to_existing(self, career_ops_dir):
        """Appends to existing file."""
        hist = career_ops_dir / "data" / "scan-history.tsv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text("url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
                        "https://old.com\t2026-05-01\tjobspy\tengineer\tacme\tadded\n")

        offers = [
            {"url": "https://new.com", "company": "globex", "title": "developer"}
        ]
        bridge_mod.append_to_scan_history(career_ops_dir, offers, "2026-05-12")

        content = hist.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 3  # Header + old + new
        assert "https://new.com" in content

    def test_append_scan_history_row_format(self, career_ops_dir):
        """Row format is correct."""
        offers = [
            {"url": "https://job1.com", "company": "acme", "title": "engineer"}
        ]
        today = "2026-05-12"

        bridge_mod.append_to_scan_history(career_ops_dir, offers, today)

        hist = career_ops_dir / "data" / "scan-history.tsv"
        lines = hist.read_text().strip().split("\n")
        # lines[1] is the data row (after header)
        assert "https://job1.com" in lines[1]
        assert "2026-05-12" in lines[1]
        assert "jobspy" in lines[1]
        assert "engineer" in lines[1]
        assert "acme" in lines[1]
        assert "added" in lines[1]

    def test_append_scan_history_creates_data_dir(self, tmp_path):
        """Creates data/ directory if missing."""
        career_ops = tmp_path / "career-ops"
        # Don't create data/ dir

        offers = [
            {"url": "https://job1.com", "company": "acme", "title": "engineer"}
        ]
        bridge_mod.append_to_scan_history(career_ops, offers, "2026-05-12")

        hist = career_ops / "data" / "scan-history.tsv"
        assert hist.exists()


class TestRun:
    """Test bridge.run function."""

    def test_run_missing_filtered_csv_returns_zero(self, career_ops_dir, monkeypatch, tmp_path):
        """Missing filtered_jobs.csv returns 0."""
        # Patch FILTERED_PATH to a nonexistent file
        nonexistent = tmp_path / "nonexistent.csv"
        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", nonexistent)

        result = bridge_mod.run(career_ops_dir)
        assert result == 0

    def test_run_empty_filtered_csv_returns_zero(self, career_ops_dir, monkeypatch, tmp_path):
        """Empty filtered_jobs.csv returns 0."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text("")

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        assert result == 0

    def test_run_missing_career_ops_raises(self, tmp_path, monkeypatch):
        """Missing career_ops_path raises FileNotFoundError."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text("title,company,job_url\nengineer,acme,https://job1.com\n")

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        nonexistent = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError):
            bridge_mod.run(nonexistent)

    def test_run_returns_count_of_new_offers(self, career_ops_dir, monkeypatch, tmp_path):
        """Returns count of added offers."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,acme,https://job1.com\n"
            "developer,globex,https://job2.com\n"
            "dev,initech,https://job3.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        assert result == 3

    def test_run_dedupes_by_url(self, career_ops_dir, monkeypatch, tmp_path):
        """Deduplicate by URL within a single run."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,acme,https://job1.com\n"
            "developer,acme,https://job1.com\n"  # Same URL
            "dev,globex,https://job2.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        # Only 2 unique URLs
        assert result == 2

    def test_run_dedupes_by_company_role(self, career_ops_dir, monkeypatch, tmp_path):
        """Deduplicate by company::role (case-insensitive)."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,ACME,https://job1.com\n"
            "engineer,acme,https://job2.com\n"  # Different URL, same company::title (case-insensitive)
            "developer,globex,https://job3.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        # Only 2 unique company::title combos
        assert result == 2

    def test_run_skips_urls_already_in_scan_history(self, career_ops_dir, monkeypatch, tmp_path):
        """Skip URLs already in scan-history.tsv."""
        hist = career_ops_dir / "data" / "scan-history.tsv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            "https://job1.com\t2026-05-01\tjobspy\tengineer\tacme\tadded\n"
        )

        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,acme,https://job1.com\n"  # Already seen
            "developer,globex,https://job2.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        # Only job2 is new
        assert result == 1

    def test_run_skips_urls_from_pipeline_md(self, career_ops_dir, monkeypatch, tmp_path):
        """Skip URLs already in pipeline.md."""
        pipeline = career_ops_dir / "data" / "pipeline.md"
        pipeline.parent.mkdir(parents=True, exist_ok=True)
        pipeline.write_text(
            "# Pipeline\n"
            "## Pendientes\n"
            "- [ ] https://job1.com | acme | engineer\n"
        )

        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,acme,https://job1.com\n"  # Already in pipeline
            "developer,globex,https://job2.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        # Only job2 is new
        assert result == 1

    def test_run_skips_rows_with_missing_url(self, career_ops_dir, monkeypatch, tmp_path):
        """Skip rows with blank job_url."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,acme,\n"  # Blank URL
            "developer,globex,https://job2.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        # Only job2
        assert result == 1

    def test_run_writes_to_both_pipeline_and_history(self, career_ops_dir, monkeypatch, tmp_path):
        """Both pipeline.md and scan-history.tsv are written."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url,description,date_posted\n"
            "engineer,acme,https://job1.com,build apis,2026-05-12\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        bridge_mod.run(career_ops_dir)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        hist = career_ops_dir / "data" / "scan-history.tsv"

        assert pipeline.exists()
        assert hist.exists()
        assert "https://job1.com" in pipeline.read_text()
        assert "https://job1.com" in hist.read_text()

    def test_run_sorts_by_date_posted_newest_first(self, career_ops_dir, monkeypatch, tmp_path):
        """Offers are sorted by date_posted descending (newest first)."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url,description,date_posted\n"
            "engineer,acme,https://job1.com,old job,2026-05-10\n"
            "developer,globex,https://job2.com,recent job,2026-05-12\n"
            "analyst,initech,https://job3.com,mid job,2026-05-11\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        bridge_mod.run(career_ops_dir)

        pipeline = career_ops_dir / "data" / "pipeline.md"
        content = pipeline.read_text()

        # Find positions of each URL in the content
        job1_idx = content.find("https://job1.com")
        job2_idx = content.find("https://job2.com")
        job3_idx = content.find("https://job3.com")

        # Newest (job2) should come first, oldest (job1) last
        assert job2_idx < job3_idx < job1_idx
