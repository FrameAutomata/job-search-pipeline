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

    @pytest.mark.parametrize(
        "seed",
        [None, "", "title,company,job_url,relevance_score\n"],
        ids=["missing", "zero-byte", "header-only"],
    )
    def test_run_with_no_rows_returns_empty(
        self, career_ops_dir, monkeypatch, tmp_path, seed
    ):
        """All three "produced nothing" shapes are one condition to bridge.

        Header-only is the case that used to differ: it is the shape screen
        wrote on its all-seen path, and bridge's old `st_size == 0` test read it
        as a file with content — so bridge announced it was bridging and then
        found no rows. read_rows collapses the three, so they are parametrized
        rather than written out as three tests asserting one line.
        """
        filtered = tmp_path / "filtered_jobs.csv"
        if seed is not None:
            filtered.write_text(seed, encoding="utf-8")
        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        assert bridge_mod.run(career_ops_dir) == []

    def test_missing_career_ops_is_reported_even_with_no_rows(
        self, tmp_path, monkeypatch
    ):
        """A bad CAREER_OPS_PATH is the user's .env, not their scrape.

        read_rows treats one more shape as empty than the old size test did, so
        testing rows first would answer "run filter first" to someone whose
        actual fault is a path that doesn't exist.
        """
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text("title,company,job_url\n", encoding="utf-8")
        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        with pytest.raises(FileNotFoundError, match="career-ops not found"):
            bridge_mod.run(tmp_path / "nope")

    def test_run_missing_career_ops_raises(self, tmp_path, monkeypatch):
        """Missing career_ops_path raises FileNotFoundError."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text("title,company,job_url\nengineer,acme,https://job1.com\n")

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        nonexistent = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError):
            bridge_mod.run(nonexistent)

    def test_run_returns_count_of_new_offers(self, career_ops_dir, monkeypatch, tmp_path):
        """Returns list of added offers."""
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url\n"
            "engineer,acme,https://job1.com\n"
            "developer,globex,https://job2.com\n"
            "dev,initech,https://job3.com\n"
        )

        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)

        result = bridge_mod.run(career_ops_dir)
        assert len(result) == 3

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
        assert len(result) == 2

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
        assert len(result) == 2

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
        assert len(result) == 1

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
        assert len(result) == 1

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
        assert len(result) == 1

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


class TestEasyApplyUrls:
    """bridge persists the URLs that came from an easy_apply pass to a deduped,
    append-only side channel (career-ops/data/easy-apply-urls.txt) so the UI can
    gate the Indeed SmartApply apply button — JobSpy returns no per-job flag."""

    def test_append_and_load_roundtrip(self, career_ops_dir):
        bridge_mod.append_easy_apply_urls(career_ops_dir, ["https://a", "https://b"])
        assert bridge_mod.load_easy_apply_urls(career_ops_dir) == {"https://a", "https://b"}

    def test_append_dedupes_within_and_across_calls(self, career_ops_dir):
        bridge_mod.append_easy_apply_urls(career_ops_dir, ["https://a", "https://a"])
        bridge_mod.append_easy_apply_urls(career_ops_dir, ["https://a", "https://c"])
        assert bridge_mod.load_easy_apply_urls(career_ops_dir) == {"https://a", "https://c"}
        text = (career_ops_dir / "data" / "easy-apply-urls.txt").read_text(encoding="utf-8")
        assert text.count("https://a\n") == 1

    def test_append_ignores_blank(self, career_ops_dir):
        bridge_mod.append_easy_apply_urls(career_ops_dir, ["", "   ", "https://a"])
        assert bridge_mod.load_easy_apply_urls(career_ops_dir) == {"https://a"}

    def test_load_missing_file_returns_empty(self, career_ops_dir):
        assert bridge_mod.load_easy_apply_urls(career_ops_dir) == set()

    def test_run_records_only_easy_apply_urls(self, career_ops_dir, monkeypatch, tmp_path):
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url,easy_apply\n"
            "engineer,acme,https://job1.com,True\n"
            "developer,globex,https://job2.com,False\n"
            "dev,initech,https://job3.com,True\n"
        )
        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)
        bridge_mod.run(career_ops_dir)
        assert bridge_mod.load_easy_apply_urls(career_ops_dir) == {
            "https://job1.com",
            "https://job3.com",
        }

    def test_run_records_easy_apply_url_even_when_already_seen(self, career_ops_dir, monkeypatch, tmp_path):
        # A URL already in scan-history is skipped for the tracker (a dup) but
        # must still be recorded in the easy-apply set, so the write happens
        # before the "no new offers" early return.
        hist = career_ops_dir / "data" / "scan-history.tsv"
        hist.write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            "https://job1.com\t2026-01-01\tjobspy\teng\tacme\tadded\n",
            encoding="utf-8",
        )
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text(
            "title,company,job_url,easy_apply\n"
            "engineer,acme,https://job1.com,True\n"
        )
        monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered)
        result = bridge_mod.run(career_ops_dir)
        assert result == []
        assert "https://job1.com" in bridge_mod.load_easy_apply_urls(career_ops_dir)


class TestPipelineMdHeadings:
    """career-ops writes the pending/processed headings in English now and reads
    both spellings (scan.mjs PENDING_MARKERS, reconcile-pipeline.mjs PENDING_RE).
    Matching only our Spanish one against a pipeline.md that scan.mjs created
    finds nothing and appends a SECOND pending section, splitting the queue."""

    OFFERS = [{"url": "https://x/j/9", "company": "Acme", "title": "SWE", "description": ""}]

    def _headings(self, career_ops_dir, initial):
        pipe = career_ops_dir / "data" / "pipeline.md"
        pipe.parent.mkdir(parents=True, exist_ok=True)
        if initial is not None:
            pipe.write_text(initial, encoding="utf-8")
        bridge_mod.append_to_pipeline(career_ops_dir, self.OFFERS)
        text = pipe.read_text(encoding="utf-8")
        return [l for l in text.splitlines() if l.startswith("## ")], text

    def test_appends_into_an_english_pending_section(self, career_ops_dir):
        heads, text = self._headings(
            career_ops_dir,
            "# Pipeline — Pending URLs\n\n## Pending\n\n"
            "- [ ] https://x/j/1 | Old | Role\n\n## Processed\n")
        assert heads == ["## Pending", "## Processed"], "must not add a 2nd pending section"
        assert "j/9" in text

    def test_appends_into_our_own_spanish_section(self, career_ops_dir):
        heads, text = self._headings(
            career_ops_dir, "# Pipeline\n\n## Pendientes\n\n- [ ] https://x/j/1 | Old | Role\n")
        assert heads == ["## Pendientes"]
        assert "j/9" in text

    def test_creates_a_section_when_the_file_has_none(self, career_ops_dir):
        heads, text = self._headings(career_ops_dir, "# Pipeline\n")
        assert heads == ["## Pendientes"]
        assert "j/9" in text

    def test_creates_the_file_from_scratch(self, career_ops_dir):
        heads, text = self._headings(career_ops_dir, None)
        assert heads == ["## Pendientes"]
        assert "j/9" in text


class TestDedupAcrossTrackerLayouts:
    """company::role dedup must key on the role, whichever tracker layout the
    file uses — a Via column shifts Role right by one."""

    ROW = "| 1 | 2026-08-25 | Acme | {} 4/5 | Applied | null | [1](reports/1.md) | n |\n"

    def test_via_layout_keys_on_the_role(self):
        md = ("| # | Date | Company | Via | Role | Score | Status | PDF | Report | Notes |\n"
              "|---|---|---|---|---|---|---|---|---|---|\n"
              + self.ROW.format("Robert Half | SWE |"))
        assert bridge_mod._parse_applications_md(md)[1] == {"acme::swe"}

    def test_canonical_layout_unchanged(self):
        md = ("| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
              "|---|---|---|---|---|---|---|---|---|\n"
              + self.ROW.format("SWE |"))
        assert bridge_mod._parse_applications_md(md)[1] == {"acme::swe"}

    def test_headerless_table_falls_back_to_positions(self):
        assert bridge_mod._parse_applications_md(
            self.ROW.format("SWE |"))[1] == {"acme::swe"}
