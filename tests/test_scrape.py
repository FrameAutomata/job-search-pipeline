"""Tests for pipeline/scrape.py"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pipeline import scrape as scrape_mod


class TestFilterPasses:
    """Test scrape.filter_passes — picks the searches whose names match."""

    SEARCHES = [
        {"name": "recent DFW", "search_terms": ["a"], "sites": ["indeed"]},
        {"name": "remote US",  "search_terms": ["b"], "sites": ["indeed"]},
        {"name": "easy apply", "search_terms": ["c"], "sites": ["indeed"]},
    ]

    def test_none_returns_all(self):
        assert scrape_mod.filter_passes(self.SEARCHES, None) is self.SEARCHES

    def test_empty_list_returns_all(self):
        # An empty selection should not silently drop everything — it's the
        # same as "no filter requested".
        assert scrape_mod.filter_passes(self.SEARCHES, []) is self.SEARCHES

    def test_blank_strings_treated_as_no_filter(self):
        assert scrape_mod.filter_passes(self.SEARCHES, ["", "   "]) is self.SEARCHES

    def test_single_match(self):
        result = scrape_mod.filter_passes(self.SEARCHES, ["easy apply"])
        assert len(result) == 1
        assert result[0]["name"] == "easy apply"

    def test_multi_match_preserves_input_order(self):
        # Output should preserve original search order, regardless of selector order.
        result = scrape_mod.filter_passes(self.SEARCHES, ["remote US", "recent DFW"])
        assert [r["name"] for r in result] == ["recent DFW", "remote US"]

    def test_match_is_case_insensitive(self):
        result = scrape_mod.filter_passes(self.SEARCHES, ["EASY APPLY"])
        assert len(result) == 1
        assert result[0]["name"] == "easy apply"

    def test_match_trims_whitespace(self):
        result = scrape_mod.filter_passes(self.SEARCHES, ["  easy apply  "])
        assert len(result) == 1

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="matched no searches"):
            scrape_mod.filter_passes(self.SEARCHES, ["does not exist"])


class TestFilterPassesEasyApply:
    """Test the easy_apply_only / no_easy_apply selectors used by the cloud
    workflows. These route by JobSpy field instead of pass name so that
    user-renamed passes don't break the workflow."""

    SEARCHES = [
        {"name": "recent local", "search_terms": ["a"], "sites": ["indeed"]},
        {"name": "remote US",    "search_terms": ["b"], "sites": ["indeed"], "is_remote": True},
        {"name": "easy apply",   "search_terms": ["c"], "sites": ["indeed"], "easy_apply": True},
    ]

    def test_easy_apply_only_keeps_only_true(self):
        result = scrape_mod.filter_passes(self.SEARCHES, easy_apply_only=True)
        assert [r["name"] for r in result] == ["easy apply"]

    def test_no_easy_apply_drops_true(self):
        result = scrape_mod.filter_passes(self.SEARCHES, no_easy_apply=True)
        assert [r["name"] for r in result] == ["recent local", "remote US"]

    def test_easy_apply_only_returns_empty_when_no_easy_apply_pass(self):
        # Critical: workflow no-ops cleanly when user has no easy-apply pass.
        searches = [{"name": "x", "search_terms": ["a"], "sites": ["indeed"]}]
        result = scrape_mod.filter_passes(searches, easy_apply_only=True)
        assert result == []

    def test_no_easy_apply_returns_empty_when_only_easy_apply_pass(self):
        searches = [{"name": "x", "easy_apply": True, "search_terms": ["a"], "sites": ["indeed"]}]
        result = scrape_mod.filter_passes(searches, no_easy_apply=True)
        assert result == []

    def test_easy_apply_only_and_no_easy_apply_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            scrape_mod.filter_passes(
                self.SEARCHES, easy_apply_only=True, no_easy_apply=True,
            )

    def test_easy_apply_routing_does_not_match_truthy_strings(self):
        # Only literal Python True should match — protects against accidental
        # `easy_apply: "true"` strings in YAML being interpreted as a flag.
        searches = [
            {"name": "a", "easy_apply": "true",  "search_terms": ["x"], "sites": ["indeed"]},
            {"name": "b", "easy_apply": 1,        "search_terms": ["x"], "sites": ["indeed"]},
            {"name": "c", "easy_apply": True,     "search_terms": ["x"], "sites": ["indeed"]},
        ]
        result = scrape_mod.filter_passes(searches, easy_apply_only=True)
        assert [r["name"] for r in result] == ["c"]

    def test_only_pass_combines_with_easy_apply_filter(self):
        # If both are specified, only_pass narrows first then easy_apply filters.
        # (--only-pass is in a mutually exclusive group at CLI level, but the
        # underlying function supports the combination — useful for tests and
        # ad-hoc callers.)
        result = scrape_mod.filter_passes(
            self.SEARCHES, only_passes=["easy apply", "remote US"], easy_apply_only=True,
        )
        assert [r["name"] for r in result] == ["easy apply"]


class TestLoadSearches:
    """Test scrape.load_searches function."""

    def test_load_searches_with_searches_key(self, tmp_path):
        """Multi-pass format: searches: [dict1, dict2] returns list of length 2."""
        config_file = tmp_path / "config.yml"
        config_file.write_text("""
searches:
  - name: "pass 1"
    search_terms: ["software engineer"]
    sites: [indeed]
  - name: "pass 2"
    search_terms: ["backend engineer"]
    sites: [linkedin]
""")
        result = scrape_mod.load_searches(config_file)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "pass 1"
        assert result[1]["name"] == "pass 2"

    def test_load_searches_with_legacy_search_key(self, tmp_path):
        """Legacy format: search: {...} returns list of length 1."""
        config_file = tmp_path / "config.yml"
        config_file.write_text("""
search:
  search_terms: ["software engineer"]
  sites: [indeed]
""")
        result = scrape_mod.load_searches(config_file)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["search_terms"] == ["software engineer"]

    def test_load_searches_missing_file_raises(self, tmp_path):
        """Missing config file raises FileNotFoundError."""
        nonexistent = tmp_path / "nonexistent.yml"
        with pytest.raises(FileNotFoundError):
            scrape_mod.load_searches(nonexistent)


class TestStripUnsupportedSites:
    """Only indeed + linkedin are supported scrape sites. Glassdoor and
    ZipRecruiter are Cloudflare-walled (403 on every request, zero rows), and
    Google Jobs serves degraded responses then drops the connection mid-body —
    which jobspy's Google scraper doesn't catch, killing the whole run.
    strip_unsupported_sites removes them at load time so stale configs (e.g.
    an old cloud SEARCH_CONFIG_B64 secret) can't crash or waste requests."""

    def test_supported_sites_are_indeed_and_linkedin(self):
        assert set(scrape_mod.SUPPORTED_SITES) == {"indeed", "linkedin"}

    def test_drops_unsupported_sites_preserving_order(self):
        searches = [{
            "name": "p", "search_terms": ["a"],
            "sites": ["glassdoor", "indeed", "zip_recruiter", "linkedin", "google"],
        }]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed", "linkedin"]

    def test_matching_is_case_insensitive(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["LinkedIn", "Google"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["LinkedIn"]

    def test_pass_with_no_supported_sites_is_dropped(self):
        searches = [
            {"name": "dead", "search_terms": ["a"], "sites": ["google", "glassdoor"]},
            {"name": "live", "search_terms": ["a"], "sites": ["indeed"]},
        ]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert [s["name"] for s in result] == ["live"]

    def test_all_supported_passes_come_back_unchanged(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed", "linkedin"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result == searches

    def test_bare_string_sites_is_not_iterated_character_by_character(self):
        # `sites: indeed` is valid YAML and a valid JobSpy site_name; iterating
        # the string would test 'i', 'n', 'd', ... and drop the whole pass.
        searches = [{"name": "p", "search_terms": ["a"], "sites": "indeed"}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]

    def test_surrounding_whitespace_is_stripped_from_kept_sites(self):
        # jobspy resolves the board with Site[name.upper()], which raises on
        # " LINKEDIN " — so a padded entry must not survive verbatim.
        searches = [{"name": "p", "search_terms": ["a"], "sites": [" linkedin "]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["linkedin"]

    def test_case_variant_duplicates_collapse(self):
        # Both map to Site.INDEED; keeping both scrapes the board twice.
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed", "Indeed"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]

    def test_non_string_entries_do_not_crash_the_warning(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["linkedin", 123]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["linkedin"]

    def test_warning_names_the_dropped_sites_and_pass(self, capsys):
        searches = [{"name": "US Remote", "search_terms": ["a"],
                     "sites": ["indeed", "glassdoor", "google"]}]
        scrape_mod.strip_unsupported_sites(searches)
        out = capsys.readouterr().out
        assert "glassdoor" in out and "google" in out
        assert "US Remote" in out

    def test_no_warning_when_nothing_dropped(self, capsys):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed"]}]
        scrape_mod.strip_unsupported_sites(searches)
        assert capsys.readouterr().out == ""

    def test_missing_sites_key_defaults_to_supported(self):
        # Left as None, jobspy's get_site_type() scrapes list(Site) — every
        # retired board included — so the key has to be filled in.
        searches = [{"name": "p", "search_terms": ["a"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == list(scrape_mod.SUPPORTED_SITES)

    def test_explicitly_null_sites_defaults_to_supported(self):
        # `sites:` with nothing after it — a real None in the mapping, which
        # made validate_limitations raise TypeError.
        searches = [{"name": "p", "search_terms": ["a"], "sites": None}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == list(scrape_mod.SUPPORTED_SITES)

    def test_null_sites_does_not_crash_validate_limitations(self):
        scrape_mod.validate_limitations({"sites": None, "hours_old": 168})  # must not raise

    def test_input_list_is_not_mutated(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed", "google"]}]
        scrape_mod.strip_unsupported_sites(searches)
        assert searches[0]["sites"] == ["indeed", "google"]


class TestRunStripsUnsupportedSites:
    """run() applies the strip to whatever config it loads, so even a
    hand-edited or stale config never reaches jobspy with a dead site."""

    def test_run_scrapes_only_supported_sites(self, tmp_path, patch_scrape_paths, mocker):
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms: ["software engineer"]
    sites: [indeed, glassdoor, zip_recruiter, linkedin, google]
    results_wanted: 50
filter:
  min_score: 5
""")
        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        assert mock_scrape.call_args[1]["site_name"] == ["indeed", "linkedin"]

    def test_run_with_only_unsupported_sites_noops_cleanly(
        self, tmp_path, patch_scrape_paths, mocker
    ):
        # All passes stripped away → same clean no-op as "no searches matched":
        # empty jobs.csv, no scrape_jobs call, downstream stages see zero rows.
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms: ["software engineer"]
    sites: [google, glassdoor]
    results_wanted: 50
filter:
  min_score: 5
""")
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs")

        result = scrape_mod.run(config)

        mock_scrape.assert_not_called()
        assert result == patch_scrape_paths
        assert patch_scrape_paths.exists()


class TestValidateLimitations:
    """Test scrape.validate_limitations function."""

    # Indeed group constraints: only ONE of {hours_old}, {job_type/is_remote}, {easy_apply}
    def test_validate_indeed_hours_old_alone(self):
        """hours_old alone is allowed."""
        cfg = {"sites": ["indeed"], "hours_old": 168}
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_indeed_is_remote_alone(self):
        """is_remote alone is allowed."""
        cfg = {"sites": ["indeed"], "is_remote": True}
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_indeed_job_type_alone(self):
        """job_type alone is allowed."""
        cfg = {"sites": ["indeed"], "job_type": "fulltime"}
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_indeed_easy_apply_alone(self):
        """easy_apply alone is allowed."""
        cfg = {"sites": ["indeed"], "easy_apply": True}
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_indeed_hours_old_and_is_remote_raises(self):
        """hours_old + is_remote raises ValueError."""
        cfg = {"sites": ["indeed"], "hours_old": 168, "is_remote": True}
        with pytest.raises(ValueError, match="Indeed limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_indeed_hours_old_and_easy_apply_raises(self):
        """hours_old + easy_apply raises ValueError."""
        cfg = {"sites": ["indeed"], "hours_old": 168, "easy_apply": True}
        with pytest.raises(ValueError, match="Indeed limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_indeed_is_remote_and_easy_apply_raises(self):
        """is_remote + easy_apply (both Group B/C) raises ValueError."""
        cfg = {"sites": ["indeed"], "is_remote": True, "easy_apply": True}
        with pytest.raises(ValueError, match="Indeed limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_linkedin_hours_old_and_easy_apply_raises(self):
        """LinkedIn: hours_old + easy_apply raises ValueError."""
        cfg = {"sites": ["linkedin"], "hours_old": 168, "easy_apply": True}
        with pytest.raises(ValueError, match="LinkedIn limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_linkedin_hours_old_alone(self):
        """LinkedIn: hours_old alone is allowed."""
        cfg = {"sites": ["linkedin"], "hours_old": 48}
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_linkedin_easy_apply_alone(self):
        """LinkedIn: easy_apply alone is allowed."""
        cfg = {"sites": ["linkedin"], "easy_apply": True}
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_non_restricted_site_any_combo(self):
        """Non-restricted site (e.g. zip_recruiter) allows any combo."""
        cfg = {
            "sites": ["zip_recruiter"],
            "hours_old": 168,
            "is_remote": True,
            "easy_apply": True,
        }
        scrape_mod.validate_limitations(cfg)  # Should not raise

    def test_validate_empty_sites(self):
        """Empty sites list does not trigger validation."""
        cfg = {"sites": [], "hours_old": 168, "is_remote": True}
        scrape_mod.validate_limitations(cfg)  # Should not raise


class TestRun:
    """Test scrape.run function with mocked JobSpy."""

    def test_run_writes_csv_to_output_path(self, cfg_file, patch_scrape_paths, mocker):
        """run() calls scrape_jobs and writes output CSV to patched OUTPUT_PATH."""
        output_path = patch_scrape_paths

        # Mock scrape_jobs to return a DataFrame with one row
        df = pd.DataFrame(
            {
                "job_url": ["https://indeed.com/job1"],
                "title": ["software engineer"],
                "company": ["acme"],
            }
        )
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        result = scrape_mod.run(cfg_file)

        # Verify file was written
        assert output_path.exists()
        assert result == output_path

        # Verify content
        csv_df = pd.read_csv(output_path)
        assert len(csv_df) == 1
        assert csv_df.iloc[0]["title"] == "software engineer"

    def test_run_deduplicates_on_job_url(self, cfg_file, patch_scrape_paths, mocker):
        """Multiple passes returning same job_url deduplicate to single row."""
        output_path = patch_scrape_paths

        # Two DataFrames with the same job_url
        df1 = pd.DataFrame(
            {
                "job_url": ["https://indeed.com/job1"],
                "title": ["software engineer"],
                "company": ["acme"],
            }
        )
        df2 = pd.DataFrame(
            {
                "job_url": ["https://indeed.com/job1"],
                "title": ["software engineer"],
                "company": ["acme"],
            }
        )

        mocker.patch("pipeline.scrape.scrape_jobs", side_effect=[df1, df2])

        scrape_mod.run(cfg_file)

        csv_df = pd.read_csv(output_path)
        assert len(csv_df) == 1  # Deduped

    def test_run_returns_output_path(self, cfg_file, patch_scrape_paths, mocker):
        """run() returns the OUTPUT_PATH."""
        output_path = patch_scrape_paths

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        result = scrape_mod.run(cfg_file)
        assert result == output_path

    def test_run_empty_results_no_crash(self, cfg_file, patch_scrape_paths, mocker):
        """Empty scrape results don't crash; function returns early."""
        output_path = patch_scrape_paths

        # Mock returns empty DataFrame
        df = pd.DataFrame()
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        result = scrape_mod.run(cfg_file)
        assert result == output_path

    def test_run_calls_scrape_per_term(self, tmp_path, patch_scrape_paths, mocker):
        """scrape_jobs called once per search term."""
        # Config with two search terms
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms:
      - "software engineer"
      - "backend engineer"
    sites: [indeed]
    results_wanted: 50
filter:
  min_score: 5
""")

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        # Called twice (once per search term)
        assert mock_scrape.call_count == 2

    def test_run_passes_optional_params(self, tmp_path, patch_scrape_paths, mocker):
        """Optional params from config are passed through to scrape_jobs."""
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms: ["software engineer"]
    sites: [indeed]
    location: "Dallas, TX"
    hours_old: 48
    results_wanted: 100
filter:
  min_score: 5
""")

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        # Check that location and hours_old were passed
        call_kwargs = mock_scrape.call_args[1]
        assert call_kwargs["location"] == "Dallas, TX"
        assert call_kwargs["hours_old"] == 48
        assert call_kwargs["results_wanted"] == 100

    def test_run_creates_output_directory(self, cfg_file, monkeypatch, tmp_path, mocker):
        """run() creates the output directory if it doesn't exist."""
        output_dir = tmp_path / "nonexistent_dir" / "output"
        output_path = output_dir / "jobs.csv"

        monkeypatch.setattr(scrape_mod, "OUTPUT_PATH", output_path)

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(cfg_file)

        assert output_dir.exists()

    def test_run_multiple_passes_merged(self, tmp_path, patch_scrape_paths, mocker):
        """Multiple search passes are merged into single output."""
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "pass 1"
    search_terms: ["software engineer"]
    sites: [indeed]
    results_wanted: 50

  - name: "pass 2"
    search_terms: ["backend engineer"]
    sites: [indeed]
    results_wanted: 50

filter:
  min_score: 5
""")

        # Pass 1 returns 2 rows, Pass 2 returns 2 different rows
        df1 = pd.DataFrame(
            {
                "job_url": [
                    "https://indeed.com/job1",
                    "https://indeed.com/job2",
                ],
                "title": ["software engineer", "software engineer"],
            }
        )
        df2 = pd.DataFrame(
            {
                "job_url": [
                    "https://indeed.com/job3",
                    "https://indeed.com/job4",
                ],
                "title": ["backend engineer", "backend engineer"],
            }
        )

        mocker.patch("pipeline.scrape.scrape_jobs", side_effect=[df1, df2])

        output_path = patch_scrape_paths
        scrape_mod.run(config)

        csv_df = pd.read_csv(output_path)
        assert len(csv_df) == 4  # All rows merged


class TestMarkEasyApply:
    """scrape.mark_easy_apply collapses the per-pass easy_apply flag to a
    per-URL OR — a job returned by both a broad pass (False) and an easy_apply
    pass (True) must end up True, regardless of which row dedup later keeps."""

    def test_url_in_both_passes_becomes_true(self):
        df = pd.DataFrame({
            "job_url": ["https://a", "https://a", "https://b"],
            "easy_apply": [False, True, False],
        })
        out = scrape_mod.mark_easy_apply(df)
        a_rows = out[out["job_url"] == "https://a"]["easy_apply"]
        assert [bool(v) for v in a_rows] == [True, True]
        assert bool(out[out["job_url"] == "https://b"]["easy_apply"].iloc[0]) is False

    def test_url_only_in_easy_pass_is_true(self):
        df = pd.DataFrame({"job_url": ["https://x"], "easy_apply": [True]})
        out = scrape_mod.mark_easy_apply(df)
        assert bool(out["easy_apply"].iloc[0]) is True

    def test_url_only_in_broad_pass_is_false(self):
        df = pd.DataFrame({"job_url": ["https://y"], "easy_apply": [False]})
        out = scrape_mod.mark_easy_apply(df)
        assert bool(out["easy_apply"].iloc[0]) is False

    def test_missing_column_defaults_false(self):
        df = pd.DataFrame({"job_url": ["https://z"]})
        out = scrape_mod.mark_easy_apply(df)
        assert "easy_apply" in out.columns
        assert bool(out["easy_apply"].iloc[0]) is False
