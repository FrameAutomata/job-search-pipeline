"""Tests for pipeline/batch_evaluate.py"""

import csv
import json
import os
from pathlib import Path

import pytest

from pipeline import batch_evaluate as eval_mod
from pipeline.batch_evaluate import _check_provider, _detect_provider, run


class TestDetectProvider:
    def test_returns_none_when_no_keys(self, monkeypatch):
        for key in ("BATCH_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        assert _detect_provider() is None

    def test_explicit_batch_provider_wins(self, monkeypatch):
        monkeypatch.setenv("BATCH_PROVIDER", "openai")
        monkeypatch.setenv("GEMINI_API_KEY", "gkey")
        assert _detect_provider() == "openai"

    def test_gemini_detected_first(self, monkeypatch):
        for key in ("BATCH_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gkey")
        monkeypatch.setenv("OPENAI_API_KEY", "okey")
        assert _detect_provider() == "gemini"

    def test_groq_before_openai(self, monkeypatch):
        for key in ("BATCH_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "gkey")
        monkeypatch.setenv("OPENAI_API_KEY", "okey")
        assert _detect_provider() == "groq"

    def test_anthropic_last(self, monkeypatch):
        for key in ("BATCH_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "akey")
        assert _detect_provider() == "anthropic"

    def test_explicit_batch_provider_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("BATCH_PROVIDER", "  Anthropic  ")
        assert _detect_provider() == "anthropic"


class TestCheckProvider:
    def test_ollama_no_key_required(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        assert _check_provider("ollama") is None

    def test_returns_error_when_gemini_key_missing(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        err = _check_provider("gemini")
        assert err is not None
        assert "GEMINI_API_KEY" in err

    def test_returns_none_when_key_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "testkey")
        assert _check_provider("anthropic") is None

    def test_returns_error_when_groq_key_missing(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        err = _check_provider("groq")
        assert err is not None
        assert "GROQ_API_KEY" in err


class TestRunDryRun:
    def _make_career_ops(self, tmp_path: Path) -> Path:
        career_ops = tmp_path / "career-ops"
        batch_dir = career_ops / "batch"
        batch_dir.mkdir(parents=True)
        tsv = batch_dir / "batch-input.tsv"
        with open(tsv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "url", "source", "notes"], delimiter="\t")
            writer.writeheader()
            writer.writerows([
                {"id": "1", "url": "https://a.com", "source": "Acme", "notes": "Eng"},
                {"id": "2", "url": "https://b.com", "source": "Globex", "notes": "Dev"},
            ])
        cv_path = career_ops / "cv.md"
        cv_path.write_text("# My CV\nSoftware Engineer with experience.", encoding="utf-8")
        return career_ops

    def test_no_provider_configured_returns_zero(self, tmp_path, monkeypatch):
        career_ops = self._make_career_ops(tmp_path)
        for key in ("BATCH_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        result = run(career_ops, dry_run=True)
        assert result == 0

    def test_no_tsv_returns_zero(self, tmp_path, monkeypatch):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        result = run(career_ops, dry_run=True)
        assert result == 0

    def test_dry_run_returns_pending_count(self, tmp_path, monkeypatch):
        career_ops = self._make_career_ops(tmp_path)
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        result = run(career_ops, provider="anthropic", dry_run=True)
        assert result == 2

    def test_dry_run_skips_completed(self, tmp_path, monkeypatch):
        career_ops = self._make_career_ops(tmp_path)
        state_path = career_ops / "batch" / "batch-api-state.json"
        state_path.write_text(json.dumps({"jobs": {"1": {"status": "completed"}}}), encoding="utf-8")
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        result = run(career_ops, provider="anthropic", dry_run=True)
        assert result == 1

    def test_unknown_provider_returns_zero(self, tmp_path, monkeypatch):
        career_ops = self._make_career_ops(tmp_path)
        result = run(career_ops, provider="unknownprovider", dry_run=True)
        assert result == 0
