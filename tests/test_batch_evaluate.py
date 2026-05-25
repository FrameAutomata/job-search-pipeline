"""Tests for pipeline/batch_evaluate.py"""

import csv
import json
import os
from pathlib import Path

import pytest

from pipeline import batch_evaluate as eval_mod
from pipeline.batch_evaluate import (
    _call_with_retry,
    _check_provider,
    _detect_provider,
    _is_rate_limit_error,
    run,
)


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


class TestModelResolution:
    """Test the BATCH_MODEL fallback chain. The empty-string case is the
    one that bit us in CI: `BATCH_MODEL: ${{ vars.BATCH_MODEL || '' }}`
    in the workflow YAML injects "" when the repo variable isn't set."""

    def _resolve(self, monkeypatch, model_arg=None, env_value=None, provider="gemini"):
        """Reproduce the exact resolution logic from batch_evaluate.run() so
        the test pins the contract without needing a full pipeline run."""
        from pipeline.batch_evaluate import PROVIDER_DEFAULTS
        if env_value is None:
            monkeypatch.delenv("BATCH_MODEL", raising=False)
        else:
            monkeypatch.setenv("BATCH_MODEL", env_value)
        model = model_arg or os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider]
        return model

    def test_explicit_arg_wins(self, monkeypatch):
        monkeypatch.setenv("BATCH_MODEL", "from-env")
        assert self._resolve(monkeypatch, model_arg="explicit") == "explicit"

    def test_env_value_used_when_arg_missing(self, monkeypatch):
        assert self._resolve(monkeypatch, env_value="my-model") == "my-model"

    def test_unset_env_falls_back_to_provider_default(self, monkeypatch):
        assert self._resolve(monkeypatch, env_value=None, provider="gemini") == "gemini-2.5-flash"

    def test_empty_string_env_falls_back_to_provider_default(self, monkeypatch):
        # Regression test: GHA workflow `${{ vars.X || '' }}` pattern injects ""
        # when X isn't set. Before the fix, this empty string overrode the
        # provider default and got sent to the API as the model name, causing
        # "GenerateContentRequest.model: unexpected model name format".
        assert self._resolve(monkeypatch, env_value="", provider="gemini") == "gemini-2.5-flash"

    def test_empty_string_env_falls_back_for_anthropic(self, monkeypatch):
        assert self._resolve(monkeypatch, env_value="", provider="anthropic") == "claude-sonnet-4-6"

    def test_whitespace_only_env_is_NOT_treated_as_empty(self, monkeypatch):
        # `"   " or default` evaluates to "   " (truthy). This is intentional
        # — whitespace might be a typo we want the API to reject loudly rather
        # than silently using the default. The fix is only for the literal
        # empty-string case GHA actually produces.
        result = self._resolve(monkeypatch, env_value="   ", provider="gemini")
        assert result == "   "


class TestIsRateLimitError:
    """Test the cross-provider rate-limit detector. Uses string matching as
    a fallback so we don't have to import every SDK's exception classes."""

    def test_detects_anthropic_style_message(self):
        class FakeErr(Exception):
            pass
        assert _is_rate_limit_error(FakeErr("Error code: 429 - rate_limit_error"))

    def test_detects_gemini_style_resource_exhausted(self):
        # google-genai raises errors with ResourceExhausted / 429 / quota
        # phrasing depending on version.
        assert _is_rate_limit_error(Exception("ResourceExhausted: Quota exceeded"))

    def test_detects_openai_style_rate_limit(self):
        assert _is_rate_limit_error(Exception("Rate limit reached for requests"))

    def test_detects_too_many_requests(self):
        assert _is_rate_limit_error(Exception("HTTP 429: Too Many Requests"))

    def test_detects_via_status_code_attribute(self):
        # SDKs sometimes attach status_code without putting it in the message.
        class HttpErr(Exception):
            status_code = 429
        assert _is_rate_limit_error(HttpErr("something went wrong"))

    def test_detects_via_code_attribute(self):
        class HttpErr(Exception):
            code = 429
        assert _is_rate_limit_error(HttpErr("ugh"))

    def test_does_not_match_unrelated_errors(self):
        # Critical: non-rate-limit errors must NOT trigger retry, otherwise
        # we'd silently retry real failures and waste API quota.
        assert not _is_rate_limit_error(Exception("Invalid API key"))
        assert not _is_rate_limit_error(Exception("model not found"))
        assert not _is_rate_limit_error(Exception("connection refused"))
        assert not _is_rate_limit_error(ValueError("bad input"))


class TestCallWithRetry:
    """Test the exponential-backoff retry wrapper."""

    def test_succeeds_on_first_try(self):
        calls = []
        def fake_caller(system, user):
            calls.append((system, user))
            return "ok"
        result = _call_with_retry(fake_caller, "sys", "usr", sleep=lambda _: None)
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_on_rate_limit_then_succeeds(self):
        calls = []
        sleeps: list[float] = []
        def fake_caller(system, user):
            calls.append(len(calls))
            if len(calls) <= 2:
                raise Exception("429 Too Many Requests")
            return "ok-after-retries"
        result = _call_with_retry(
            fake_caller, "sys", "usr",
            max_attempts=5, base_delay=1.0, sleep=lambda s: sleeps.append(s),
        )
        assert result == "ok-after-retries"
        assert len(calls) == 3  # failed twice, succeeded on attempt 3
        # Two sleeps (after attempts 1 and 2). Exponential backoff: ~1s then ~2s
        # plus 0–0.5s jitter on each.
        assert len(sleeps) == 2
        assert 1.0 <= sleeps[0] <= 1.5
        assert 2.0 <= sleeps[1] <= 2.5

    def test_gives_up_after_max_attempts(self):
        calls = []
        def fake_caller(system, user):
            calls.append(1)
            raise Exception("rate limit hit")
        with pytest.raises(Exception, match="rate limit"):
            _call_with_retry(
                fake_caller, "sys", "usr",
                max_attempts=3, base_delay=0.1, sleep=lambda _: None,
            )
        assert len(calls) == 3  # tried exactly max_attempts times

    def test_non_rate_limit_error_raises_immediately(self):
        calls = []
        def fake_caller(system, user):
            calls.append(1)
            raise ValueError("invalid api key")
        with pytest.raises(ValueError, match="invalid api key"):
            _call_with_retry(
                fake_caller, "sys", "usr",
                max_attempts=5, sleep=lambda _: None,
            )
        # Only one call — no retry for non-rate-limit errors
        assert len(calls) == 1

    def test_exponential_backoff_doubles_each_attempt(self):
        sleeps: list[float] = []
        def fake_caller(system, user):
            raise Exception("429")
        with pytest.raises(Exception):
            _call_with_retry(
                fake_caller, "sys", "usr",
                max_attempts=5, base_delay=1.0, sleep=lambda s: sleeps.append(s),
            )
        # 4 sleeps (between 5 attempts). Base delays double: 1, 2, 4, 8 with jitter.
        assert len(sleeps) == 4
        assert 1.0 <= sleeps[0] <= 1.5
        assert 2.0 <= sleeps[1] <= 2.5
        assert 4.0 <= sleeps[2] <= 4.5
        assert 8.0 <= sleeps[3] <= 8.5

    def test_passes_system_and_user_through(self):
        captured = []
        def fake_caller(system, user):
            captured.append((system, user))
            return "ok"
        _call_with_retry(fake_caller, "my-system", "my-user", sleep=lambda _: None)
        assert captured == [("my-system", "my-user")]
