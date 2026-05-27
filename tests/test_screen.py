"""Tests for pipeline/screen.py"""

import csv
from pathlib import Path

import pytest

from pipeline import screen as screen_mod
from pipeline.screen import (
    classify_liveness,
    extract_description,
    linkedin_guest_jd_url,
    run,
)


class TestLinkedInGuestUrl:
    """Test the LinkedIn /jobs/view/ → guest job-posting endpoint mapping.
    The guest endpoint serves the full JD without the login wall that the
    regular page hits from datacenter IPs."""

    GUEST = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4419521927"

    def test_bare_id(self):
        assert linkedin_guest_jd_url(
            "https://www.linkedin.com/jobs/view/4419521927"
        ) == self.GUEST

    def test_trailing_slash(self):
        assert linkedin_guest_jd_url(
            "https://www.linkedin.com/jobs/view/4419521927/"
        ) == self.GUEST

    def test_query_string(self):
        assert linkedin_guest_jd_url(
            "https://www.linkedin.com/jobs/view/4419521927?refId=abc&trk=xyz"
        ) == self.GUEST

    def test_slug_prefix(self):
        # LinkedIn sometimes includes a title slug before the numeric ID.
        assert linkedin_guest_jd_url(
            "https://www.linkedin.com/jobs/view/software-engineer-at-acme-4419521927"
        ) == self.GUEST

    def test_no_www(self):
        assert linkedin_guest_jd_url(
            "https://linkedin.com/jobs/view/4419521927"
        ) == self.GUEST

    def test_non_linkedin_returns_none(self):
        assert linkedin_guest_jd_url("https://indeed.com/viewjob?jk=abc123") is None
        assert linkedin_guest_jd_url("https://www.glassdoor.com/job-listing/12345") is None

    def test_linkedin_non_view_url_returns_none(self):
        # A LinkedIn URL that isn't a job-view URL shouldn't map to a guest JD.
        assert linkedin_guest_jd_url("https://www.linkedin.com/company/acme") is None

    def test_empty_or_none_input(self):
        assert linkedin_guest_jd_url("") is None
        assert linkedin_guest_jd_url(None) is None


class TestClassifyLiveness:
    def test_http_404_expired(self):
        result, reason = classify_liveness(404, "https://example.com", "page content")
        assert result == "expired"
        assert "404" in reason

    def test_http_410_expired(self):
        result, reason = classify_liveness(410, "https://example.com", "page content")
        assert result == "expired"
        assert "410" in reason

    def test_error_redirect_url_expired(self):
        result, reason = classify_liveness(200, "https://example.com?error=true", "some content " * 50)
        assert result == "expired"
        assert "error redirect" in reason

    def test_hard_expired_body_no_longer_available(self):
        result, reason = classify_liveness(200, "https://example.com", "This job is no longer available.")
        assert result == "expired"

    def test_hard_expired_position_filled(self):
        result, reason = classify_liveness(200, "https://example.com", "This position has been filled.")
        assert result == "expired"

    def test_hard_expired_job_expired(self):
        result, reason = classify_liveness(200, "https://example.com", "This job has expired.")
        assert result == "expired"

    def test_hard_expired_no_longer_accepting(self):
        result, reason = classify_liveness(200, "https://example.com", "We are no longer accepting applications.")
        assert result == "expired"

    def test_hard_expired_applications_closed(self):
        result, reason = classify_liveness(200, "https://example.com", "Applications are closed.")
        assert result == "expired"

    def test_apply_button_active(self):
        body = "Senior Software Engineer at Acme Corp. " * 20 + "Apply now to join our team."
        result, reason = classify_liveness(200, "https://example.com", body)
        assert result == "active"
        assert "apply" in reason.lower()

    def test_easy_apply_active(self):
        body = "Senior Software Engineer. " * 20 + "Easy Apply"
        result, reason = classify_liveness(200, "https://example.com", body)
        assert result == "active"

    def test_short_body_expired(self):
        result, reason = classify_liveness(200, "https://example.com", "Short page")
        assert result == "expired"
        assert "insufficient content" in reason

    def test_content_without_apply_uncertain(self):
        long_body = "This is a job listing for a software engineer. " * 20
        result, reason = classify_liveness(200, "https://example.com", long_body)
        assert result == "uncertain"
        assert "no apply control" in reason

    def test_listing_page_expired(self):
        body = "Search for jobs page is loaded. " * 20
        result, reason = classify_liveness(200, "https://example.com", body)
        assert result == "expired"
        assert "listing page" in reason

    def test_jobs_found_listing_expired(self):
        body = "125 jobs found matching your search. " * 20
        result, reason = classify_liveness(200, "https://example.com", body)
        assert result == "expired"

    def test_hard_expired_case_insensitive(self):
        result, reason = classify_liveness(200, "https://example.com", "JOB IS NO LONGER AVAILABLE.")
        assert result == "expired"


class TestRunScreen:
    def _write_filtered_csv(self, path: Path, rows: list[dict]) -> None:
        fieldnames = ["title", "company", "job_url", "relevance_score"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _write_config(self, path: Path, liveness: bool = False, timeout: int = 8) -> None:
        path.write_text(
            f"screen:\n  liveness: {'true' if liveness else 'false'}\n  liveness_timeout: {timeout}\n",
            encoding="utf-8",
        )

    def test_liveness_disabled_is_noop(self, tmp_path, monkeypatch):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg, liveness=False)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_csv(filtered, [
            {"title": "Eng", "company": "Acme", "job_url": "https://job.com", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        result = run(cfg)
        assert result == 0
        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1

    def test_missing_csv_returns_zero(self, tmp_path, monkeypatch):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg, liveness=True)
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", tmp_path / "nonexistent.csv")
        result = run(cfg)
        assert result == 0

    def test_drops_expired_keeps_active(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_csv(filtered, [
            {"title": "Active Job", "company": "Acme", "job_url": "https://active.com", "relevance_score": 8},
            {"title": "Expired Job", "company": "Globex", "job_url": "https://expired.com", "relevance_score": 6},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        def mock_fetch(url, timeout=8):
            if "active" in url:
                return "active", "apply control visible", ""
            return "expired", "HTTP 404", ""

        mocker.patch.object(screen_mod, "fetch_and_classify", side_effect=mock_fetch)
        result = run(cfg)
        assert result == 1
        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["title"] == "Active Job"

    def test_keeps_uncertain_jobs(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_csv(filtered, [
            {"title": "Uncertain Job", "company": "Initech", "job_url": "https://uncertain.com", "relevance_score": 7},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("uncertain", "content present, no apply control", ""),
        )
        result = run(cfg)
        assert result == 0
        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1


class TestExtractDescription:
    """Test screen.extract_description — site-specific selectors + body fallback."""

    def test_empty_returns_empty(self):
        assert extract_description("") == ""

    def test_linkedin_show_more_less_markup(self):
        html_body = (
            "<html><body>"
            "<div>chrome</div>"
            '<div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">'
            "Software Engineer at Acme. Build distributed systems in Go and Python."
            "</div>"
            "<div>footer</div>"
            "</body></html>"
        )
        result = extract_description(html_body)
        assert "Software Engineer at Acme" in result
        assert "Build distributed systems in Go and Python" in result
        # Chrome/footer text from outside the JD container should be excluded.
        assert "chrome" not in result
        assert "footer" not in result

    def test_indeed_job_description_text_container(self):
        html_body = (
            "<html><body>"
            '<div id="jobDescriptionText">Backend Engineer — Spring Boot, PostgreSQL, AWS</div>'
            "</body></html>"
        )
        result = extract_description(html_body)
        assert "Backend Engineer" in result
        assert "PostgreSQL" in result

    def test_glassdoor_jd_content(self):
        html_body = (
            '<div class="jobDescriptionContent some-other-class">'
            "Senior Software Engineer working on developer tools."
            "</div>"
        )
        result = extract_description(html_body)
        assert "Senior Software Engineer" in result

    def test_strips_html_tags_and_decodes_entities(self):
        html_body = (
            '<div class="show-more-less-html__markup">'
            "<p>Work with <strong>Python</strong> &amp; Rust.</p>"
            "<ul><li>5+ years exp</li></ul>"
            "</div>"
        )
        result = extract_description(html_body)
        assert "Python" in result
        assert "Rust" in result
        assert "5+ years exp" in result
        assert "<strong>" not in result
        # Entity was decoded.
        assert "&amp;" not in result
        assert "&" in result

    def test_strips_script_and_style_blocks(self):
        # Scripts/styles in the JD container should not bleed into the description.
        html_body = (
            '<div class="show-more-less-html__markup">'
            "<script>var tracking = true;</script>"
            "<style>.x{color:red}</style>"
            "Actual job description here"
            "</div>"
        )
        result = extract_description(html_body)
        assert "Actual job description here" in result
        assert "tracking" not in result
        assert "color:red" not in result

    def test_body_fallback_when_no_known_selector(self):
        html_body = (
            "<html><head><title>x</title></head>"
            "<body><h1>Random Job Site</h1><p>Generic JD content goes here.</p></body></html>"
        )
        result = extract_description(html_body)
        assert "Random Job Site" in result
        assert "Generic JD content" in result

    def test_truncates_to_max_chars(self):
        long = "x" * 20000
        html_body = f'<div class="show-more-less-html__markup">{long}</div>'
        result = extract_description(html_body)
        assert len(result) <= screen_mod._MAX_DESCRIPTION_CHARS


class TestBackfillDescription:
    """Test that screen.run populates missing description fields from the
    fetched page body — the whole point of skipping linkedin_fetch_description."""

    def _write_filtered_with_desc(self, path: Path, rows: list[dict]) -> None:
        fieldnames = ["title", "company", "job_url", "description", "relevance_score"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _write_config(self, path: Path) -> None:
        path.write_text("screen:\n  liveness: true\n  liveness_timeout: 8\n", encoding="utf-8")

    def test_backfills_empty_description(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_with_desc(filtered, [
            {"title": "Eng", "company": "Acme", "job_url": "https://x.com",
             "description": "", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        page_html = (
            '<html><body>Apply now! '
            '<div class="show-more-less-html__markup">'
            'Backfilled job description content with enough chars to look real. ' * 10
            + '</div></body></html>'
        )
        mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "apply control visible", page_html),
        )

        run(cfg)

        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert "Backfilled job description content" in rows[0]["description"]

    def test_linkedin_job_fetched_via_guest_endpoint(self, tmp_path, monkeypatch, mocker):
        # The whole point of this fix: a LinkedIn job_url should be fetched
        # through the guest job-posting API, not the login-walled /jobs/view/
        # page. job_url in the CSV stays unchanged; only the fetch target swaps.
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        view_url = "https://www.linkedin.com/jobs/view/4419521927"
        self._write_filtered_with_desc(filtered, [
            {"title": "Eng", "company": "Acme", "job_url": view_url,
             "description": "", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        guest_html = (
            '<div class="show-more-less-html__markup">'
            'Full LinkedIn JD from the guest endpoint. ' * 10
            + '</div>'
        )
        fetch_spy = mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "content present", guest_html),
        )

        run(cfg)

        # fetch_and_classify must have been called with the guest URL, not the
        # original /jobs/view/ URL.
        called_url = fetch_spy.call_args.args[0]
        assert called_url == (
            "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4419521927"
        )

        # And the description got backfilled from the guest fragment.
        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert "Full LinkedIn JD from the guest endpoint" in rows[0]["description"]
        # job_url in the CSV is unchanged — user still clicks the human page.
        assert rows[0]["job_url"] == view_url

    def test_non_linkedin_job_fetched_via_original_url(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        indeed_url = "https://www.indeed.com/viewjob?jk=abc123"
        self._write_filtered_with_desc(filtered, [
            {"title": "Eng", "company": "Acme", "job_url": indeed_url,
             "description": "", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        page = '<div id="jobDescriptionText">Indeed JD body here. ' * 20 + '</div>'
        fetch_spy = mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "content present", page),
        )

        run(cfg)

        # Non-LinkedIn URLs are fetched as-is.
        assert fetch_spy.call_args.args[0] == indeed_url

    def test_preserves_existing_description(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        original_desc = "Original Indeed-provided description that should not be overwritten."
        self._write_filtered_with_desc(filtered, [
            {"title": "Eng", "company": "Acme", "job_url": "https://x.com",
             "description": original_desc, "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        page_html = (
            '<div class="show-more-less-html__markup">DIFFERENT content from page fetch.</div>'
            + 'Apply now! ' * 50
        )
        mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "apply control visible", page_html),
        )

        run(cfg)

        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["description"] == original_desc

    def test_no_backfill_when_extract_finds_nothing(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_with_desc(filtered, [
            {"title": "Eng", "company": "Acme", "job_url": "https://x.com",
             "description": "", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        # Body empty → nothing to extract; row should still pass through.
        mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "apply control visible", ""),
        )

        run(cfg)

        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["description"] == ""

    def test_skips_already_seen_urls_before_fetching(self, tmp_path, monkeypatch, mocker):
        """URLs in scan-history.tsv must not trigger an HTTP fetch — that's
        the whole point of pre-screen dedup."""
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_with_desc(filtered, [
            {"title": "Old", "company": "Acme", "job_url": "https://seen.com",
             "description": "", "relevance_score": 8},
            {"title": "New", "company": "Globex", "job_url": "https://new.com",
             "description": "", "relevance_score": 7},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        # Pre-seed scan-history with the "seen" URL.
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)
        (career_ops / "data" / "scan-history.tsv").write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            "https://seen.com\t2026-05-01\tjobspy\tOld\tAcme\tadded\n",
            encoding="utf-8",
        )

        # Spy on fetch_and_classify — must only be called for the new URL.
        fetch_spy = mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "apply control visible", ""),
        )

        run(cfg, career_ops_path=career_ops)

        # Exactly one fetch — for the unseen URL.
        assert fetch_spy.call_count == 1
        assert fetch_spy.call_args.args[0] == "https://new.com"

        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # Only the unseen URL survives the CSV write-back.
        assert len(rows) == 1
        assert rows[0]["job_url"] == "https://new.com"

    def test_records_dead_urls_to_scan_history(self, tmp_path, monkeypatch, mocker):
        """Expired URLs get appended to scan-history with status `screened-dead`
        so subsequent runs skip them via the same early-dedup path."""
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_with_desc(filtered, [
            {"title": "Dead Role", "company": "Acme", "job_url": "https://dead.com",
             "description": "", "relevance_score": 8},
            {"title": "Live Role", "company": "Globex", "job_url": "https://live.com",
             "description": "", "relevance_score": 7},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)

        def mock_fetch(url, timeout=8):
            if "dead" in url:
                return "expired", "HTTP 404", ""
            return "active", "apply control visible", ""

        mocker.patch.object(screen_mod, "fetch_and_classify", side_effect=mock_fetch)

        run(cfg, career_ops_path=career_ops)

        hist = (career_ops / "data" / "scan-history.tsv").read_text(encoding="utf-8")
        # Header + one screened-dead row.
        lines = [ln for ln in hist.splitlines() if ln.strip()]
        assert lines[0].startswith("url\t")
        assert len(lines) == 2
        assert "https://dead.com" in lines[1]
        assert "screened-dead" in lines[1]
        assert "https://live.com" not in hist

    def test_dead_url_skipped_on_subsequent_run(self, tmp_path, monkeypatch, mocker):
        """End-to-end: first run records the dead URL; second run skips it
        entirely (no fetch attempted)."""
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)

        # Run 1: URL is dead.
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_with_desc(filtered, [
            {"title": "Dead", "company": "Acme", "job_url": "https://gone.com",
             "description": "", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        fetch_spy = mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("expired", "HTTP 404", ""),
        )
        run(cfg, career_ops_path=career_ops)
        assert fetch_spy.call_count == 1  # fetched once on run 1

        # Run 2: same URL appears again in filtered_jobs.csv. We expect ZERO
        # additional fetches because it's now in scan-history.
        self._write_filtered_with_desc(filtered, [
            {"title": "Dead", "company": "Acme", "job_url": "https://gone.com",
             "description": "", "relevance_score": 8},
        ])
        fetch_spy.reset_mock()
        run(cfg, career_ops_path=career_ops)
        assert fetch_spy.call_count == 0

    def test_no_career_ops_path_means_no_dedup_or_recording(self, tmp_path, monkeypatch, mocker):
        """Back-compat: callers that don't pass career_ops_path get the old
        behavior — screen runs everything and records nothing externally."""
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        self._write_filtered_with_desc(filtered, [
            {"title": "Dead", "company": "Acme", "job_url": "https://dead.com",
             "description": "", "relevance_score": 8},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        fetch_spy = mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("expired", "HTTP 404", ""),
        )
        # Default call (no career_ops_path) must not raise and must still fetch.
        run(cfg)
        assert fetch_spy.call_count == 1

    def test_adds_description_column_if_missing_from_csv(self, tmp_path, monkeypatch, mocker):
        """A CSV written without a `description` column should still get one
        added so the backfill survives the write-back."""
        cfg = tmp_path / "search.yml"
        self._write_config(cfg)
        filtered = tmp_path / "filtered_jobs.csv"
        # No description column at all.
        fieldnames = ["title", "company", "job_url", "relevance_score"]
        with open(filtered, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({"title": "Eng", "company": "Acme",
                             "job_url": "https://x.com", "relevance_score": 8})
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        page_html = (
            '<div class="show-more-less-html__markup">JD body.</div>' + "Apply " * 100
        )
        mocker.patch.object(
            screen_mod, "fetch_and_classify",
            return_value=("active", "apply control visible", page_html),
        )

        run(cfg)

        with open(filtered, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert "description" in (reader.fieldnames or [])
            rows = list(reader)
        assert rows[0]["description"] == "JD body."
