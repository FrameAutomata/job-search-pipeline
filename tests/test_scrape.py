"""Tests for pipeline/scrape.py"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pipeline import scrape as scrape_mod


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
        with pytest.raises(ValueError, match="Indeed/Glassdoor limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_indeed_hours_old_and_easy_apply_raises(self):
        """hours_old + easy_apply raises ValueError."""
        cfg = {"sites": ["indeed"], "hours_old": 168, "easy_apply": True}
        with pytest.raises(ValueError, match="Indeed/Glassdoor limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_indeed_is_remote_and_easy_apply_raises(self):
        """is_remote + easy_apply (both Group B/C) raises ValueError."""
        cfg = {"sites": ["indeed"], "is_remote": True, "easy_apply": True}
        with pytest.raises(ValueError, match="Indeed/Glassdoor limitation"):
            scrape_mod.validate_limitations(cfg)

    def test_validate_glassdoor_same_rules_as_indeed(self):
        """Glassdoor has the same group constraints as Indeed."""
        cfg = {"sites": ["glassdoor"], "hours_old": 168, "is_remote": True}
        with pytest.raises(ValueError, match="Indeed/Glassdoor limitation"):
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
