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


class TestIndeedJobKey:
    """Extract the `jk` job key from an Indeed posting URL. The key is what the
    jobData GraphQL liveness check queries by (see TestFetchIndeedExpiry) — the
    page itself is Cloudflare-walled to a plain fetch, so the URL's only role is
    carrying the key. Non-Indeed hosts must never match: a jk-shaped param on
    another site is not an Indeed key."""

    def test_viewjob_url(self):
        assert screen_mod.indeed_job_key(
            "https://www.indeed.com/viewjob?jk=abc123def4567890"
        ) == "abc123def4567890"

    def test_jk_among_other_params(self):
        assert screen_mod.indeed_job_key(
            "https://www.indeed.com/viewjob?from=serp&jk=1a2b3c4d5e6f7a8b&tk=xyz"
        ) == "1a2b3c4d5e6f7a8b"

    def test_mobile_and_country_subdomains(self):
        assert screen_mod.indeed_job_key(
            "https://m.indeed.com/viewjob?jk=abc123def4567890") == "abc123def4567890"
        assert screen_mod.indeed_job_key(
            "https://de.indeed.com/viewjob?jk=abc123def4567890") == "abc123def4567890"

    def test_redirect_style_url_with_jk(self):
        # rc/clk-style tracking URLs still carry the jk param.
        assert screen_mod.indeed_job_key(
            "https://www.indeed.com/rc/clk?jk=abc123def4567890&from=web"
        ) == "abc123def4567890"

    def test_first_top_level_jk_wins_over_nested_jk(self):
        """Regression: a greedy scan used to capture the LAST jk= in the string,
        so an un-encoded jk inside a redirect param's VALUE hijacked the key —
        the wrong key comes back absent from jobData and the LIVE role gets
        discarded. The genuine posting key is the top-level jk param."""
        assert screen_mod.indeed_job_key(
            "https://www.indeed.com/rc/clk?jk=abc123def4567890&url=http://o/a?jk=aaaaaaaaaaaaaaaa"
        ) == "abc123def4567890"
        assert screen_mod.indeed_job_key(
            "https://www.indeed.com/viewjob?jk=abc123def4567890&next=https://x.com/v?jk=bbbbbbbbbbbbbbbb"
        ) == "abc123def4567890"

    def test_jk_only_inside_a_param_value_does_not_count(self):
        # ?url=...?jk=... carries no top-level jk — there is no posting key here.
        assert screen_mod.indeed_job_key(
            "https://www.indeed.com/rc/clk?url=http://o/a?jk=zzzzzzzzzzzzzzzz"
        ) is None

    def test_indeed_url_without_jk(self):
        assert screen_mod.indeed_job_key("https://www.indeed.com/jobs?q=engineer") is None

    def test_non_indeed_host_with_jk_param(self):
        assert screen_mod.indeed_job_key("https://evil.example/viewjob?jk=abc123") is None
        assert screen_mod.indeed_job_key(
            "https://www.linkedin.com/jobs/view/123?jk=abc123") is None

    def test_lookalike_domain(self):
        assert screen_mod.indeed_job_key("https://notindeed.com/viewjob?jk=abc123") is None

    def test_empty_or_none_input(self):
        assert screen_mod.indeed_job_key("") is None
        assert screen_mod.indeed_job_key(None) is None


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

    def test_short_body_is_held_not_expired(self):
        """`expired` requires positive evidence that the posting is GONE. A body
        too short to judge is unread, not removed — it is equally an unrecognised
        challenge, a JS shell, or a truncated response, and `expired` would write
        it to scan-history as permanently dead."""
        result, reason = classify_liveness(200, "https://example.com", "Short page")
        assert result == "throttled"
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


SCREEN_COLS = ["title", "company", "job_url", "relevance_score"]
DESC_COLS = ["title", "company", "job_url", "description", "relevance_score"]


def write_filtered_csv(path: Path, cols_or_rows, rows=None) -> None:
    """A filtered_jobs.csv for the run() tests.

    Module-level so every run() test class shares one copy — the column list is
    what the stage screens against, and it had accumulated a copy per class.
    Called as (path, rows) for the default columns, or (path, cols, rows) when a
    test needs the description column too.
    """
    cols, rows = (SCREEN_COLS, cols_or_rows) if rows is None else (cols_or_rows, rows)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)


def write_screen_config(path: Path, liveness: bool = False, timeout: int = 8) -> None:
    path.write_text(
        f"screen:\n  liveness: {'true' if liveness else 'false'}\n  liveness_timeout: {timeout}\n",
        encoding="utf-8",
    )


class TestRunScreen:
    def test_liveness_disabled_is_noop(self, tmp_path, monkeypatch):
        cfg = tmp_path / "search.yml"
        write_screen_config(cfg, liveness=False)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, [
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
        write_screen_config(cfg, liveness=True)
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", tmp_path / "nonexistent.csv")
        result = run(cfg)
        assert result == 0

    def test_records_easy_apply_urls_before_dedup(self, tmp_path, monkeypatch, career_ops_dir):
        # A SmartApply role already in scan-history is dropped by the pre-screen
        # dedup, but its easy_apply flag must still be recorded so the UI can
        # gate its apply button — the recording must happen before the drop.
        from pipeline.bridge import load_easy_apply_urls

        cfg = tmp_path / "search.yml"
        write_screen_config(cfg, liveness=True)
        (career_ops_dir / "data" / "scan-history.tsv").write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            "https://www.indeed.com/viewjob?jk=seen\t2026-01-01\tjobspy\teng\tacme\tadded\n",
            encoding="utf-8",
        )
        filtered = tmp_path / "filtered_jobs.csv"
        with open(filtered, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["title", "company", "job_url", "relevance_score", "easy_apply"]
            )
            w.writeheader()
            w.writerow({"title": "Eng", "company": "Acme",
                        "job_url": "https://www.indeed.com/viewjob?jk=seen",
                        "relevance_score": 8, "easy_apply": "True"})
            w.writerow({"title": "Dev", "company": "Globex",
                        "job_url": "https://www.indeed.com/viewjob?jk=new",
                        "relevance_score": 7, "easy_apply": "False"})
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        monkeypatch.setattr(screen_mod, "fetch_and_classify",
                            lambda url, timeout=8: ("active", "apply control visible", ""))

        run(cfg, career_ops_dir)

        urls = load_easy_apply_urls(career_ops_dir)
        assert "https://www.indeed.com/viewjob?jk=seen" in urls   # recorded despite dedup
        assert "https://www.indeed.com/viewjob?jk=new" not in urls  # easy_apply False

    def test_drops_expired_keeps_active(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, [
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

    def test_throttled_job_held_not_kept_and_not_recorded_dead(self, tmp_path, monkeypatch, mocker):
        """A rate-limited (throttled) fetch is HELD for the next run, not
        finalized this run: the job is dropped from the output (otherwise it
        reaches evaluation with an empty JD, since a throttle wall isn't
        backfillable) AND it is NOT recorded screened-dead (it's not gone). With
        neither the kept nor the dead path recording its URL, the next scrape
        re-finds and re-checks it — mirroring recheck's 'retry next run'."""
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        cfg = tmp_path / "search.yml"
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, [
            {"title": "Active Job", "company": "Acme", "job_url": "https://active.com", "relevance_score": 8},
            {"title": "Throttled Job", "company": "Globex", "job_url": "https://throttled.com", "relevance_score": 6},
        ])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        monkeypatch.setattr(screen_mod.time, "sleep", lambda *_: None)  # skip retry backoff

        def mock_fetch(url, timeout=8):
            if "active" in url:
                return "active", "apply control visible", ""
            return "throttled", "HTTP 403 (rate-limited / sign-in wall)", ""
        mocker.patch.object(screen_mod, "fetch_and_classify", side_effect=mock_fetch)

        run(cfg, career_ops_path=co)

        with open(filtered, newline="", encoding="utf-8") as f:
            titles = {r["title"] for r in csv.DictReader(f)}
        assert titles == {"Active Job"}   # throttled job held back, not kept
        sh = co / "data" / "scan-history.tsv"
        sh_text = sh.read_text(encoding="utf-8") if sh.exists() else ""
        assert "throttled.com" not in sh_text   # not recorded screened-dead


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

    def test_backfills_empty_description(self, tmp_path, monkeypatch, mocker):
        cfg = tmp_path / "search.yml"
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        view_url = "https://www.linkedin.com/jobs/view/4419521927"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        indeed_url = "https://www.indeed.com/viewjob?jk=abc123"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        original_desc = "Original Indeed-provided description that should not be overwritten."
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)

        # Run 1: URL is dead.
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, DESC_COLS, [
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
        write_screen_config(cfg, liveness=True)
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


class TestClassifyThrottle:
    """LinkedIn rate-limits a burst of guest-endpoint fetches with a 403/429/999
    anti-bot sign-in wall. classify_liveness must report that as `throttled`
    (couldn't read it) — NEVER active/expired — so a wall can't be read as live
    or cause a false discard, and the retry/skip layers can react."""

    def test_throttle_statuses_are_throttled(self):
        from pipeline.screen import classify_liveness
        for st in (403, 429, 999):
            # Body even contains apply text — the throttle status must still win.
            res, _ = classify_liveness(st, "https://www.linkedin.com/x", "Easy Apply " * 50)
            assert res == "throttled", (st, res)

    def test_404_410_still_expired(self):
        from pipeline.screen import classify_liveness
        assert classify_liveness(404, "https://x", "")[0] == "expired"
        assert classify_liveness(410, "https://x", "")[0] == "expired"

    def test_closed_body_still_expired(self):
        from pipeline.screen import classify_liveness
        assert classify_liveness(200, "https://x",
                                 "This job is no longer accepting applications.")[0] == "expired"

    def test_live_page_still_active(self):
        from pipeline.screen import classify_liveness
        body = "Senior Software Engineer. " * 30 + "Apply now to join."
        assert classify_liveness(200, "https://x", body)[0] == "active"

    def test_expired_jd_redirect_url_is_expired(self):
        # A closed job's human /jobs/view/ page 302s to a search page tagged
        # expired_jd_redirect — treat that as expired (the non-guest fallback path).
        from pipeline.screen import classify_liveness
        body = "Software engineer jobs " * 50
        res, _ = classify_liveness(
            200, "https://www.linkedin.com/jobs/acme-jobs?trk=expired_jd_redirect", body)
        assert res == "expired"


class TestClassifyEachRetry:
    """A `throttled` fetch is retried with backoff before giving up — a transient
    rate-limit shouldn't leave a role unverified if a retry gets the real page."""

    def test_retries_throttled_then_succeeds(self, monkeypatch):
        seq = iter([("throttled", "HTTP 403", ""), ("expired", "HTTP 404", "")])
        monkeypatch.setattr(screen_mod, "fetch_and_classify", lambda url, timeout=8: next(seq))
        monkeypatch.setattr(screen_mod.time, "sleep", lambda *_: None)
        rows = list(screen_mod.classify_each(["https://x"], lambda u: u, timeout=8, max_workers=1))
        assert rows[0][1] == "expired"      # retried past the throttle wall

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr(screen_mod, "fetch_and_classify",
                            lambda url, timeout=8: ("throttled", "HTTP 403", ""))
        monkeypatch.setattr(screen_mod.time, "sleep", lambda *_: None)
        rows = list(screen_mod.classify_each(["https://x"], lambda u: u, timeout=8, max_workers=1))
        assert rows[0][1] == "throttled"


class TestIsLivenessVerifiable:
    """A URL is liveness-verifiable only if we have a working unauthenticated way
    to read it: LinkedIn /jobs/view (the guest JD endpoint) and Indeed viewjob
    URLs (the jobData GraphQL API — the page is Cloudflare-walled but the API the
    scraper already uses returns a definitive `expired` flag per job key).
    Glassdoor still has no path, so the re-check skips it."""

    def test_linkedin_view_is_verifiable(self):
        from pipeline.screen import is_liveness_verifiable
        assert is_liveness_verifiable("https://www.linkedin.com/jobs/view/4342114687/")
        assert is_liveness_verifiable("https://linkedin.com/jobs/view/eng-at-acme-555")

    def test_indeed_viewjob_is_verifiable(self):
        from pipeline.screen import is_liveness_verifiable
        assert is_liveness_verifiable("https://www.indeed.com/viewjob?jk=abc123")

    def test_indeed_without_job_key_not_verifiable(self):
        from pipeline.screen import is_liveness_verifiable
        assert not is_liveness_verifiable("https://www.indeed.com/jobs?q=engineer")

    def test_glassdoor_not_verifiable(self):
        from pipeline.screen import is_liveness_verifiable
        assert not is_liveness_verifiable("https://www.glassdoor.com/job-listing/123")

    def test_empty_or_none_not_verifiable(self):
        from pipeline.screen import is_liveness_verifiable
        assert not is_liveness_verifiable("")
        assert not is_liveness_verifiable(None)


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _jobdata_payload(*jobs):
    """A jobData GraphQL response body: jobs are (key, expired) pairs."""
    return {"data": {"jobData": {"results": [
        {"job": {"key": k, "expired": e}} for k, e in jobs
    ]}}}


class TestFetchIndeedExpiry:
    """fetch_indeed_expiry(keys) POSTs ONE jobData GraphQL batch to
    apis.indeed.com (the same endpoint + mobile-app headers the JobSpy scraper
    uses — the Cloudflare wall only guards the website, not this API) and returns
    {jk: expired} for every job the API returned. Any transport- or query-level
    failure RAISES — the caller maps that to `throttled` (couldn't read), so a
    broken request can never look like a verdict."""

    @pytest.fixture
    def fake_post(self, monkeypatch):
        import requests
        state = {"resp": _FakeResp(payload=_jobdata_payload()), "calls": []}

        def _post(url, headers=None, json=None, timeout=None, **kw):
            state["calls"].append(
                {"url": url, "headers": headers or {}, "json": json or {}, "timeout": timeout})
            resp = state["resp"]
            if isinstance(resp, Exception):
                raise resp
            return resp

        monkeypatch.setattr(requests, "post", _post)
        return state

    def test_returns_expired_flag_per_key(self, fake_post):
        fake_post["resp"] = _FakeResp(payload=_jobdata_payload(("aaa", False), ("bbb", True)))
        out = screen_mod.fetch_indeed_expiry(["aaa", "bbb"])
        assert out == {"aaa": False, "bbb": True}

    def test_omitted_keys_are_absent_not_guessed(self, fake_post):
        """The API silently omits a job key that no longer exists; the dict must
        reflect exactly what came back — absence semantics belong to the caller."""
        fake_post["resp"] = _FakeResp(payload=_jobdata_payload(("aaa", False)))
        out = screen_mod.fetch_indeed_expiry(["aaa", "gone"])
        assert out == {"aaa": False}

    def test_single_batched_post_with_all_keys(self, fake_post):
        screen_mod.fetch_indeed_expiry(["k1abc", "k2def", "k3ghi"])
        assert len(fake_post["calls"]) == 1
        call = fake_post["calls"][0]
        assert call["url"] == "https://apis.indeed.com/graphql"
        query = call["json"]["query"]
        assert "jobData" in query
        assert all(k in query for k in ("k1abc", "k2def", "k3ghi"))

    def test_sends_mobile_api_headers(self, fake_post):
        """The request must carry the scraper's mobile-app credentials — that's
        what sails past the anti-bot wall a browser UA + no key would hit."""
        screen_mod.fetch_indeed_expiry(["aaa"])
        headers = fake_post["calls"][0]["headers"]
        assert headers.get("indeed-api-key")
        assert headers.get("indeed-co")

    # Failure contract: every transport/shape failure surfaces as RuntimeError
    # (json decode errors wrapped too), so the classifier has ONE thing to catch
    # and map to `throttled`.

    def test_http_error_raises(self, fake_post):
        fake_post["resp"] = _FakeResp(status_code=500, payload={})
        with pytest.raises(RuntimeError):
            screen_mod.fetch_indeed_expiry(["aaa"])

    def test_graphql_errors_raise(self, fake_post):
        fake_post["resp"] = _FakeResp(payload={
            "errors": [{"message": "field 'expired' no longer exists"}],
            "data": None,
        })
        with pytest.raises(RuntimeError):
            screen_mod.fetch_indeed_expiry(["aaa"])

    def test_malformed_payload_raises(self, fake_post):
        fake_post["resp"] = _FakeResp(payload={"data": {}})
        with pytest.raises(RuntimeError):
            screen_mod.fetch_indeed_expiry(["aaa"])

    def test_non_json_body_raises(self, fake_post):
        fake_post["resp"] = _FakeResp(payload=ValueError("not json"))
        with pytest.raises(RuntimeError):
            screen_mod.fetch_indeed_expiry(["aaa"])


class TestClassifyIndeedEach:
    """classify_indeed_each(items, key_of) — the Indeed counterpart of
    classify_each, yielding the same (item, result, reason, body) shape so
    recheck's accounting loop consumes both without site logic. Verdicts:

      expired=False in the batch          -> active
      expired=True in the batch           -> expired (definitive)
      absent from a NON-EMPTY batch       -> expired (removed from Indeed —
                                             the API omits unknown keys)
      absent from an EMPTY batch          -> uncertain (can't tell 'all removed'
                                             from a silently rejected query)
      batch request raised                -> throttled for the whole chunk (no
                                             read — recheck leaves state
                                             unstamped and retries next run)
      key_of(item) is falsy               -> uncertain (defensive)

    No in-call retry (unlike the LinkedIn throttle retry): a batch failure is
    batch-wide, and the recheck's staleness state IS the retry mechanism."""

    @pytest.fixture
    def fake_expiry(self, monkeypatch):
        """Stub fetch_indeed_expiry with a scripted per-call behavior list (or a
        single dict/Exception applied to every call) + a log of key batches."""
        state = {"behavior": {}, "batches": []}

        def _expiry(keys, timeout=8):
            state["batches"].append(list(keys))
            b = state["behavior"]
            if isinstance(b, list):
                b = b[len(state["batches"]) - 1]
            if isinstance(b, Exception):
                raise b
            if b == "echo-live":
                return {k: False for k in keys}
            return b

        monkeypatch.setattr(screen_mod, "fetch_indeed_expiry", _expiry, raising=False)
        state["behavior"] = "echo-live"
        return state

    @staticmethod
    def _classify(items, **kw):
        rows = list(screen_mod.classify_indeed_each(
            items, lambda it: it.get("jk"), **kw))
        return {id(item): (item, result, reason, body) for item, result, reason, body in rows}

    def test_live_key_is_active(self, fake_expiry):
        item = {"jk": "aaa"}
        _, result, reason, body = self._classify([item])[id(item)]
        assert result == "active"
        assert body == ""

    def test_expired_key_is_expired(self, fake_expiry):
        fake_expiry["behavior"] = {"aaa": True}
        item = {"jk": "aaa"}
        _, result, reason, _ = self._classify([item])[id(item)]
        assert result == "expired"
        assert "expired" in reason.lower()

    def test_absent_from_nonempty_batch_is_expired(self, fake_expiry):
        """A key the API omitted while returning others is definitively gone —
        the bogus-key probe confirmed silent omission is the removal signal."""
        fake_expiry["behavior"] = {"aaa": False}          # bbb omitted
        keep, gone = {"jk": "aaa"}, {"jk": "bbb"}
        out = self._classify([keep, gone])
        assert out[id(keep)][1] == "active"
        assert out[id(gone)][1] == "expired"
        assert "removed" in out[id(gone)][2].lower()

    def test_absent_from_empty_batch_is_uncertain(self, fake_expiry):
        """An empty result set could be 'every key removed' OR a silently
        rejected query — never discard on it."""
        fake_expiry["behavior"] = {}
        item = {"jk": "aaa"}
        _, result, _, _ = self._classify([item])[id(item)]
        assert result == "uncertain"

    def test_failed_batch_throttles_whole_chunk(self, fake_expiry):
        fake_expiry["behavior"] = RuntimeError("api down")
        a, b = {"jk": "aaa"}, {"jk": "bbb"}
        out = self._classify([a, b])
        assert out[id(a)][1] == "throttled"
        assert out[id(b)][1] == "throttled"

    def test_failed_batch_does_not_abort_later_chunks(self, fake_expiry):
        """Per-chunk containment, mirroring classify_each's per-item containment:
        one failed batch must not strand the rest of the sweep."""
        fake_expiry["behavior"] = [RuntimeError("blip"), "echo-live"]
        items = [{"jk": f"k{i}"} for i in range(4)]
        out = self._classify(items, chunk_size=2)
        assert [out[id(it)][1] for it in items] == \
            ["throttled", "throttled", "active", "active"]
        assert len(fake_expiry["batches"]) == 2

    def test_chunks_keys_in_input_order(self, fake_expiry):
        items = [{"jk": f"k{i}"} for i in range(5)]
        self._classify(items, chunk_size=2)
        assert fake_expiry["batches"] == [["k0", "k1"], ["k2", "k3"], ["k4"]]

    def test_item_without_key_is_uncertain_and_not_queried(self, fake_expiry):
        keyless, keyed = {"nope": 1}, {"jk": "aaa"}
        out = self._classify([keyless, keyed])
        assert out[id(keyless)][1] == "uncertain"
        assert out[id(keyed)][1] == "active"
        assert all("aaa" == k for batch in fake_expiry["batches"] for k in batch)

    def test_empty_items_makes_no_requests(self, fake_expiry):
        assert list(screen_mod.classify_indeed_each([], lambda it: it.get("jk"))) == []
        assert fake_expiry["batches"] == []

    def test_import_error_propagates_not_throttled(self, fake_expiry):
        """A missing dependency (jobspy/requests not installed — e.g. a UI-only
        venv) is permanent for this process: masking it as a retryable
        `throttled` would re-queue the whole backlog forever behind a 'will
        retry' that never can. It must surface as the real error instead."""
        fake_expiry["behavior"] = ImportError("No module named 'jobspy'")
        with pytest.raises(ImportError):
            list(screen_mod.classify_indeed_each([{"jk": "aaa"}], lambda it: it.get("jk")))


class TestClassifyLivenessEach:
    """The site router must be safe for any iterable input: it materializes
    `items` once, so a one-shot iterator (generator) classifies every item
    instead of silently losing whichever partition is built second."""

    def test_generator_input_classifies_every_item(self, monkeypatch):
        monkeypatch.setattr(screen_mod, "fetch_and_classify",
                            lambda url, timeout=8: ("active", "ok", "<html/>"))
        monkeypatch.setattr(screen_mod, "fetch_indeed_expiry",
                            lambda keys, timeout=8: {k: False for k in keys})
        items = iter([
            {"url": "https://www.linkedin.com/jobs/view/111"},
            {"url": "https://www.indeed.com/viewjob?jk=abc123def4567890"},
            {"url": "https://www.linkedin.com/jobs/view/222"},
        ])
        rows = list(screen_mod.classify_liveness_each(
            items, lambda it: it["url"], timeout=8, max_workers=1))
        assert len(rows) == 3
        assert all(result == "active" for _, result, _, _ in rows)


class TestScreenEmptyOutputShape:
    """Every "nothing survived" exit writes zero bytes, not a header.

    A header-only file is not zero bytes, so bridge's "did upstream produce
    anything" test read one as a file with content. pipeline.rowio is the shared
    answer; these pin both exits that used to write one.
    """

    def _cfg(self, tmp_path):
        cfg = tmp_path / "search.yml"
        write_screen_config(cfg, liveness=True)
        return cfg

    def _filtered(self, tmp_path, urls):
        filtered = tmp_path / "filtered_jobs.csv"
        write_filtered_csv(filtered, [
            {"title": f"Eng {i}", "company": "Acme", "job_url": u, "relevance_score": 8}
            for i, u in enumerate(urls)
        ])
        return filtered

    def test_all_seen_exit_truncates(self, tmp_path, monkeypatch, career_ops_dir):
        url = "https://www.indeed.com/viewjob?jk=seen"
        (career_ops_dir / "data" / "scan-history.tsv").write_text(
            "url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n"
            f"{url}\t2026-01-01\tjobspy\teng\tacme\tadded\n",
            encoding="utf-8",
        )
        filtered = self._filtered(tmp_path, [url])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        run(self._cfg(tmp_path), career_ops_dir)

        assert filtered.read_text(encoding="utf-8") == ""

    def test_everything_dropped_exit_truncates(self, tmp_path, monkeypatch):
        # The exit the issue didn't name: `kept` is empty when liveness drops
        # every job, and that wrote a header too.
        filtered = self._filtered(tmp_path, ["https://expired-a.com", "https://expired-b.com"])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        monkeypatch.setattr(screen_mod, "fetch_and_classify",
                            lambda url, timeout=8: ("expired", "HTTP 404", ""))

        run(self._cfg(tmp_path))

        assert filtered.read_text(encoding="utf-8") == ""

    def test_everything_held_exit_truncates(self, tmp_path, monkeypatch):
        # The other way `kept` empties, and the likelier one in production:
        # LinkedIn rate-limits a burst and every posting comes back a sign-in
        # wall. It takes a different branch from "dropped" — `continue` before
        # kept.append, no dead_entries, no scan-history write — so the dropped
        # test alone would ship a regression in the held path.
        filtered = self._filtered(tmp_path, ["https://a.com", "https://b.com"])
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        monkeypatch.setattr(screen_mod, "fetch_and_classify",
                            lambda url, timeout=8: ("throttled", "sign-in wall", ""))

        run(self._cfg(tmp_path))

        assert filtered.read_text(encoding="utf-8") == ""

    def test_a_header_only_file_is_converged_to_zero_bytes(self, tmp_path, monkeypatch):
        # Reading it as empty is half the contract; the other half is that a
        # producer leaves exactly zero bytes. Without this the stale shape
        # survives every later --skip-filter run.
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text("title,company,job_url,relevance_score\n", encoding="utf-8")
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)

        run(self._cfg(tmp_path))

        assert filtered.read_text(encoding="utf-8") == ""

    def test_a_missing_file_is_not_created(self, tmp_path, monkeypatch):
        missing = tmp_path / "filtered_jobs.csv"
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", missing)

        assert run(self._cfg(tmp_path)) == 0
        assert not missing.exists()

    def test_a_header_only_file_left_by_an_older_run_is_read_as_empty(
        self, tmp_path, monkeypatch
    ):
        # Upgrade path: the shape screen used to write may still be on disk.
        # Asserted as "no posting was fetched" rather than "returned 0" — the
        # liveness-disabled path and a clean run return 0 too, so the return
        # code alone would pass for reasons unrelated to the claim.
        filtered = tmp_path / "filtered_jobs.csv"
        filtered.write_text("title,company,job_url,relevance_score\n", encoding="utf-8")
        monkeypatch.setattr(screen_mod, "FILTERED_PATH", filtered)
        monkeypatch.setattr(screen_mod, "fetch_and_classify",
                            lambda *a, **kw: pytest.fail("fetched a posting for a file with no rows"))

        assert run(self._cfg(tmp_path)) == 0


class TestLivenessNotPermanentlyDead:
    """A posting is recorded `screened-dead` in scan-history.tsv — permanently,
    across every future run — only on an `expired` verdict. These are the cases
    that used to reach `expired` through the "insufficient content" fallthrough
    despite carrying no evidence the posting was gone."""

    CLOUDFLARE = (
        "<html><head><title>Just a moment...</title></head><body>"
        "Enable JavaScript and cookies to continue. Ray ID: 8f2a1b3c9d0e"
        "</body></html>"
    )

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 520])
    def test_server_error_holds_rather_than_expires(self, status):
        """A 5xx is the site being broken, not the posting being removed. Its
        error page is short and has no apply control, so it used to land on
        `insufficient content` -> expired. `throttled` and not `uncertain`:
        uncertain KEEPS the row and the caller mines the body for a missing
        description, which would make an nginx error page the job description."""
        result, _ = classify_liveness(status, "https://x/j/1", "<html>502 Bad Gateway</html>")
        assert result == "throttled"

    def test_bot_challenge_served_200_is_throttled(self):
        """Cloudflare answers the challenge with HTTP 200, so the status-based
        throttle check never sees it."""
        result, reason = classify_liveness(200, "https://x/j/1", self.CLOUDFLARE)
        assert result == "throttled"
        assert "anti-bot" in reason

    def test_live_posting_wins_over_challenge_wording(self):
        """The bot-challenge patterns are matched against the whole page, JD
        prose included. A posting that carries an apply control is live, whatever
        its copy says — a false `throttled` holds the row on every run, so the
        job is never evaluated at all."""
        body = ("<html><body>" + "Great infra role. " * 30 +
                "It takes just a moment to apply. We use Ray for training."
                "<button>Apply now</button></body></html>")
        result, _ = classify_liveness(200, "https://x/j/1", body)
        assert result == "active"

    def test_bare_ray_mention_is_not_a_challenge(self):
        """"Ray" without Cloudflare's hex id is an ML framework, not a wall."""
        body = "<html><body>" + "We use Ray and Kubernetes. " * 20 + "</body></html>"
        result, _ = classify_liveness(200, "https://x/j/1", body)
        assert result != "throttled"



class TestThrottleRetryScope:
    """`throttled` now covers three different situations, but only one of them
    is worth re-fetching. Retrying the other two triples the burst into the
    limiter the recheck budget exists to cap — and against an anti-bot wall it
    deepens the block we are already under."""

    def test_only_the_transient_limiter_is_retryable(self):
        import pipeline.screen as screen_mod
        limiter = classify_liveness(429, "https://x/j/1", "")[1]
        server = classify_liveness(502, "https://x/j/1", "<html>502</html>")[1]
        wall = classify_liveness(
            200, "https://x/j/1",
            "<html><title>Just a moment...</title>Ray ID: 8f2a1b3c9d0e</html>")[1]
        assert limiter.startswith(screen_mod._RETRYABLE_REASONS)
        assert not server.startswith(screen_mod._RETRYABLE_REASONS)
        assert not wall.startswith(screen_mod._RETRYABLE_REASONS)

    def test_retryable_set_tracks_the_throttle_statuses(self):
        """Derived, so the two can't drift apart."""
        import pipeline.screen as screen_mod
        assert len(screen_mod._RETRYABLE_REASONS) == len(screen_mod._THROTTLE_STATUSES)


class TestExpiredRequiresPositiveEvidence:
    """The invariant, not its instances: `expired` is the only verdict that
    writes `screened-dead` — permanent, never re-checked — so it must be
    reachable ONLY from a signal that says the posting is gone. Everything else
    resolves non-fatally. Guards the rule so the next ported case can't quietly
    re-open the fallthrough that 5xx, bot walls and redirect-off-posting all
    reached."""

    KILLS = {
        "404": (404, "https://x/j/1", ""),
        "410": (410, "https://x/j/1", ""),
        "error redirect": (200, "https://x/jobs?error=true", "x" * 400),
        "closure banner": (200, "https://x/j/1", "x " * 200 + "This job is no longer available"),
        "listing page": (200, "https://x/j/1", "x " * 200 + "42 jobs found"),
    }
    SPARES = {
        "live JD": (200, "https://x/j/1", "Great role. " * 40 + "<button>Apply</button>"),
        "rate limiter": (429, "https://x/j/1", ""),
        "server error": (502, "https://x/j/1", "<html>502 Bad Gateway</html>"),
        "bot challenge": (200, "https://x/j/1",
                          "<title>Just a moment...</title>Ray ID: 8f2a1b3c9d0e"),
        "unreadable body": (200, "https://x/j/1", "<html></html>"),
        "no apply control": (200, "https://x/j/1", "This is a job listing. " * 30),
    }

    @pytest.mark.parametrize("name", list(KILLS))
    def test_positive_evidence_expires(self, name):
        assert classify_liveness(*self.KILLS[name])[0] == "expired"

    @pytest.mark.parametrize("name", list(SPARES))
    def test_everything_else_is_non_fatal(self, name):
        verdict = classify_liveness(*self.SPARES[name])[0]
        assert verdict != "expired", f"{name} would be recorded permanently dead"
