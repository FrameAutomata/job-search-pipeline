"""Tests for pipeline/batch_prep.py"""

import csv
from pathlib import Path

import pytest

from pipeline import batch_prep as prep_mod
from pipeline.batch_prep import FIELDNAMES, _load_existing, run


def _write_tsv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


class TestLoadExisting:
    def test_returns_zeros_on_missing_file(self, tmp_path):
        max_id, seen = _load_existing(tmp_path / "missing.tsv")
        assert max_id == 0
        assert seen == set()

    def test_reads_max_id(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        _write_tsv(tsv, [
            {"id": 3, "url": "https://a.com", "source": "A", "notes": ""},
            {"id": 1, "url": "https://b.com", "source": "B", "notes": ""},
            {"id": 5, "url": "https://c.com", "source": "C", "notes": ""},
        ])
        max_id, _ = _load_existing(tsv)
        assert max_id == 5

    def test_collects_seen_urls(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        _write_tsv(tsv, [
            {"id": 1, "url": "https://a.com", "source": "A", "notes": ""},
            {"id": 2, "url": "https://b.com", "source": "B", "notes": ""},
        ])
        _, seen = _load_existing(tsv)
        assert "https://a.com" in seen
        assert "https://b.com" in seen

    def test_ignores_blank_urls(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        _write_tsv(tsv, [
            {"id": 1, "url": "", "source": "A", "notes": ""},
        ])
        _, seen = _load_existing(tsv)
        assert "" not in seen
        assert len(seen) == 0

    def test_handles_non_numeric_ids(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        _write_tsv(tsv, [
            {"id": "abc", "url": "https://a.com", "source": "A", "notes": ""},
            {"id": 3, "url": "https://b.com", "source": "B", "notes": ""},
        ])
        max_id, _ = _load_existing(tsv)
        assert max_id == 3


class TestRun:
    def test_returns_zero_for_empty_offers(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        result = run(career_ops, [])
        assert result == 0

    def test_writes_tsv_with_correct_fields(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [{"url": "https://job.com/1", "company": "Acme", "title": "Software Engineer", "description": ""}]
        run(career_ops, offers)
        tsv = career_ops / "batch" / "batch-input.tsv"
        assert tsv.exists()
        with open(tsv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert len(rows) == 1
        assert rows[0]["url"] == "https://job.com/1"
        assert rows[0]["source"] == "Acme"
        assert rows[0]["notes"] == "Software Engineer"
        assert rows[0]["id"] == "1"

    def test_assigns_sequential_ids_starting_at_one(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [
            {"url": "https://a.com", "company": "A", "title": "Eng", "description": ""},
            {"url": "https://b.com", "company": "B", "title": "Dev", "description": ""},
            {"url": "https://c.com", "company": "C", "title": "Dev", "description": ""},
        ]
        run(career_ops, offers)
        tsv = career_ops / "batch" / "batch-input.tsv"
        with open(tsv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        ids = [int(r["id"]) for r in rows]
        assert ids == [1, 2, 3]

    def test_appends_continuing_ids(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        tsv = career_ops / "batch" / "batch-input.tsv"
        tsv.parent.mkdir(parents=True, exist_ok=True)
        _write_tsv(tsv, [
            {"id": 5, "url": "https://existing.com", "source": "Old", "notes": "Old"},
        ])
        offers = [{"url": "https://new.com", "company": "New", "title": "New Eng", "description": ""}]
        run(career_ops, offers)
        with open(tsv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert len(rows) == 2
        assert rows[1]["id"] == "6"

    def test_deduplicates_by_url(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [
            {"url": "https://same.com", "company": "A", "title": "Eng", "description": ""},
            {"url": "https://same.com", "company": "B", "title": "Dev", "description": ""},
        ]
        count = run(career_ops, offers)
        assert count == 1

    def test_skips_urls_already_in_tsv(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        tsv = career_ops / "batch" / "batch-input.tsv"
        tsv.parent.mkdir(parents=True, exist_ok=True)
        _write_tsv(tsv, [{"id": 1, "url": "https://existing.com", "source": "Old", "notes": ""}])
        offers = [{"url": "https://existing.com", "company": "New", "title": "Eng", "description": ""}]
        count = run(career_ops, offers)
        assert count == 0

    def test_writes_jd_file_when_description_present(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [{"url": "https://job.com/1", "company": "Acme", "title": "Eng", "description": "Must know Python."}]
        run(career_ops, offers)
        jd_file = career_ops / "batch" / "jds" / "1.txt"
        assert jd_file.exists()
        assert jd_file.read_text(encoding="utf-8") == "Must know Python."

    def test_no_jd_file_when_description_empty(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [{"url": "https://job.com/1", "company": "Acme", "title": "Eng", "description": ""}]
        run(career_ops, offers)
        jd_file = career_ops / "batch" / "jds" / "1.txt"
        assert not jd_file.exists()

    def test_skips_blank_url_offers(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [
            {"url": "", "company": "A", "title": "Eng", "description": ""},
            {"url": "https://real.com", "company": "B", "title": "Dev", "description": ""},
        ]
        count = run(career_ops, offers)
        assert count == 1

    def test_returns_correct_count(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        offers = [
            {"url": "https://a.com", "company": "A", "title": "Eng", "description": ""},
            {"url": "https://b.com", "company": "B", "title": "Dev", "description": ""},
            {"url": "", "company": "C", "title": "Mgr", "description": ""},
        ]
        count = run(career_ops, offers)
        assert count == 2
