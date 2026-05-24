"""Tests for pipeline/screen.py"""

import csv
from pathlib import Path

import pytest

from pipeline import screen as screen_mod
from pipeline.screen import classify_liveness, run


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

        def mock_check_liveness(url, timeout=8):
            if "active" in url:
                return "active", "apply control visible"
            return "expired", "HTTP 404"

        mocker.patch.object(screen_mod, "check_liveness", side_effect=mock_check_liveness)
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
        mocker.patch.object(screen_mod, "check_liveness", return_value=("uncertain", "content present, no apply control"))
        result = run(cfg)
        assert result == 0
        with open(filtered, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
