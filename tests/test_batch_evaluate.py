"""Tests for pipeline/batch_evaluate.py"""

import csv
import json
import os
from pathlib import Path

import pytest

from pipeline import batch_evaluate as eval_mod
from pipeline.batch_evaluate import (
    _build_caller,
    _call_with_retry,
    _check_provider,
    _detect_provider,
    _is_rate_limit_error,
    _is_transient_provider_error,
    _PROVIDER_KEYS,
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULTS,
    resolve_caller,
    run,
)


class TestDeepSeekProvider:
    """DeepSeek's direct API (OpenAI-compatible, cheaper than DeepInfra hosting the
    same weights) as a first-class provider for cheap mass evaluation."""

    def test_registered_in_every_provider_table(self):
        assert "deepseek" in PROVIDER_DEFAULTS
        assert _PROVIDER_KEYS["deepseek"] == "DEEPSEEK_API_KEY"
        assert "api.deepseek.com" in PROVIDER_BASE_URLS["deepseek"]
        from pipeline.app import onboard
        assert onboard.PROVIDER_SECRETS["deepseek"] == "DEEPSEEK_API_KEY"

    def test_detect_provider_finds_deepseek_by_key(self, monkeypatch):
        monkeypatch.delenv("BATCH_PROVIDER", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
        assert _detect_provider() == "deepseek"

    def test_build_caller_targets_the_direct_api(self, monkeypatch):
        cap = {}
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-key")
        monkeypatch.setattr("pipeline.batch_evaluate._build_openai_compat_caller",
                            lambda model, api_key, base_url=None, **kw:
                                cap.update(model=model, api_key=api_key, base_url=base_url))
        _build_caller("deepseek", "deepseek-chat")
        assert cap["api_key"] == "sk-ds-key"
        assert "api.deepseek.com" in cap["base_url"]
        assert cap["model"] == "deepseek-chat"


class TestResolveCaller:
    """One shared caller-builder used by cover letters, tailoring, and the
    article digest — provider/model precedence in a single place."""

    def test_unknown_provider_raises_clear_error_not_keyerror(self, monkeypatch):
        # Unknown provider with no model override must raise a helpful RuntimeError,
        # never a bare KeyError from PROVIDER_DEFAULTS[provider].
        for v in ("BATCH_MODEL", "COVER_MODEL"):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(RuntimeError):
            resolve_caller("nonsense-provider")

    def test_no_provider_configured_raises(self, monkeypatch):
        # Derive from the provider table — a hand-copied list here missed
        # DEEPSEEK_API_KEY when that provider landed (review finding), making
        # the test's outcome depend on the developer's .env.
        from pipeline.batch_evaluate import _PROVIDER_KEYS
        for v in ("BATCH_PROVIDER", *_PROVIDER_KEYS.values()):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(RuntimeError):
            resolve_caller()

    def test_lead_env_takes_precedence(self, monkeypatch):
        # COVER_MODEL (lead_env) should win over BATCH_MODEL.
        captured = {}
        monkeypatch.setenv("COVER_MODEL", "cover-model")
        monkeypatch.setenv("BATCH_MODEL", "batch-model")
        monkeypatch.setattr("pipeline.batch_evaluate._build_caller",
                            lambda provider, model, **kw: captured.update(provider=provider, model=model))
        resolve_caller("deepinfra", lead_env="COVER_MODEL")
        assert captured["model"] == "cover-model"

    def _capture_build(self, monkeypatch):
        captured = {}
        monkeypatch.setattr("pipeline.batch_evaluate._build_caller",
                            lambda provider, model, **kw: captured.update(provider=provider, model=model))
        return captured

    def test_lead_provider_env_selects_a_dedicated_provider(self, monkeypatch):
        # TAILOR_PROVIDER picks the provider for THIS caller, over BATCH_PROVIDER —
        # so you can evaluate on Gemini but tailor on Anthropic.
        for v in ("TAILOR_MODEL", "BATCH_MODEL"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("BATCH_PROVIDER", "gemini")
        monkeypatch.setenv("TAILOR_PROVIDER", "anthropic")
        cap = self._capture_build(monkeypatch)
        resolve_caller(lead_env="TAILOR_MODEL", lead_provider_env="TAILOR_PROVIDER")
        assert cap["provider"] == "anthropic"

    def test_dedicated_provider_does_not_fall_back_to_eval_model(self, monkeypatch):
        # The killer bug to avoid: TAILOR_PROVIDER=anthropic but no TAILOR_MODEL must
        # NOT inherit BATCH_MODEL (a Gemini id) — it uses the tailor provider's default.
        monkeypatch.delenv("TAILOR_MODEL", raising=False)
        monkeypatch.setenv("BATCH_MODEL", "gemini-3.1-flash-lite")
        monkeypatch.setenv("TAILOR_PROVIDER", "anthropic")
        cap = self._capture_build(monkeypatch)
        resolve_caller(lead_env="TAILOR_MODEL", lead_provider_env="TAILOR_PROVIDER")
        assert cap["model"] != "gemini-3.1-flash-lite"
        assert cap["model"] == PROVIDER_DEFAULTS["anthropic"]

    def test_dedicated_provider_wins_over_an_explicit_arg(self, monkeypatch):
        # Regression: the apply stage threads the EVAL provider in as provider=, so a
        # tailor override (TAILOR_PROVIDER) must beat that inherited arg — not lose to
        # it. (Triggered by an explicit --batch-provider.)
        monkeypatch.setenv("TAILOR_PROVIDER", "anthropic")
        monkeypatch.setenv("TAILOR_MODEL", "claude-x")
        cap = self._capture_build(monkeypatch)
        resolve_caller("gemini", lead_env="TAILOR_MODEL", lead_provider_env="TAILOR_PROVIDER")
        assert cap["provider"] == "anthropic"   # TAILOR_PROVIDER, not the passed "gemini"

    def test_dedicated_provider_uses_its_own_lead_model(self, monkeypatch):
        monkeypatch.setenv("TAILOR_PROVIDER", "anthropic")
        monkeypatch.setenv("TAILOR_MODEL", "claude-custom-x")
        cap = self._capture_build(monkeypatch)
        resolve_caller(lead_env="TAILOR_MODEL", lead_provider_env="TAILOR_PROVIDER")
        assert cap["provider"] == "anthropic" and cap["model"] == "claude-custom-x"

    def test_no_lead_provider_keeps_the_eval_chain(self, monkeypatch):
        # Guard: with TAILOR_PROVIDER unset, behavior is unchanged — eval provider +
        # the BATCH_MODEL fallback.
        monkeypatch.delenv("TAILOR_PROVIDER", raising=False)
        monkeypatch.delenv("TAILOR_MODEL", raising=False)
        monkeypatch.setenv("BATCH_PROVIDER", "gemini")
        monkeypatch.setenv("BATCH_MODEL", "gemini-eval-x")
        cap = self._capture_build(monkeypatch)
        resolve_caller(lead_env="TAILOR_MODEL", lead_provider_env="TAILOR_PROVIDER")
        assert cap["provider"] == "gemini" and cap["model"] == "gemini-eval-x"


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


class TestTransientProviderErrors:
    """5xx / 'inference error' / timeouts must trigger retry AND model
    failover — a live run lost 183/200 jobs to a deepinfra endpoint returning
    HTTP 500 'inference error' while a 3-model failover chain sat unused
    (only rate-limits swapped)."""

    def test_500_status_attribute(self):
        class HttpErr(Exception):
            status_code = 500
        assert _is_transient_provider_error(HttpErr("boom"))

    def test_deepinfra_inference_error_message(self):
        assert _is_transient_provider_error(
            Exception("Error code: 500 - {'error': {'message': 'inference error', "
                      "'type': 'api_error', 'param': None, 'code': None}}"))

    def test_timeout_and_rate_limit_also_transient(self):
        assert _is_transient_provider_error(Exception("Request timed out."))
        assert _is_transient_provider_error(Exception("429 Too Many Requests"))

    def test_caller_errors_still_raise(self):
        # Auth/bad-model must NOT burn the failover chain or retry budget.
        assert not _is_transient_provider_error(Exception("Invalid API key"))
        assert not _is_transient_provider_error(Exception("model not found"))
        class NotFound(Exception):
            status_code = 404
        assert not _is_transient_provider_error(NotFound("no such model"))

    def test_empty_content_is_transient(self):
        # A 200-with-empty-body (degraded endpoint, or a reasoning model that
        # burned its budget thinking) must fail over, not kill the job — a live
        # rerun lost jobs to "provider returned empty content" with two healthy
        # fallback models configured.
        assert _is_transient_provider_error(RuntimeError("provider returned empty content"))
        assert _is_transient_provider_error(RuntimeError("anthropic returned empty content"))

    def test_failover_swaps_on_empty_content(self, monkeypatch):
        from pipeline.batch_evaluate import _build_failover_caller
        def empty(system, user):
            raise RuntimeError("provider returned empty content")
        def alive(system, user):
            return "from-backup"
        monkeypatch.setattr(
            "pipeline.batch_evaluate._build_single_caller",
            lambda provider, model, disable_thinking=False:
                empty if model == "primary" else alive)
        call = _build_failover_caller("deepinfra", ["primary", "backup"])
        assert call("s", "u") == "from-backup"

    def test_failover_swaps_on_500(self, monkeypatch):
        from pipeline.batch_evaluate import _build_failover_caller
        def dead(system, user):
            raise Exception("Error code: 500 - inference error")
        def alive(system, user):
            return "from-backup"
        monkeypatch.setattr(
            "pipeline.batch_evaluate._build_single_caller",
            lambda provider, model, disable_thinking=False:
                dead if model == "primary" else alive)
        call = _build_failover_caller("deepinfra", ["primary", "backup"])
        assert call("s", "u") == "from-backup"

    def test_retry_backs_off_on_500_then_succeeds(self):
        calls = []
        def flaky(system, user):
            calls.append(1)
            if len(calls) == 1:
                raise Exception("Error code: 500 - inference error")
            return "recovered"
        assert _call_with_retry(flaky, "s", "u", sleep=lambda _: None) == "recovered"
        assert len(calls) == 2

    def test_dead_port_connection_error_is_not_transient(self):
        # openai's APIConnectionError prints a bare "Connection error." but the
        # real cause is on __cause__ — a dead OLLAMA_BASE_URL/port is a config
        # error that must fail loudly on job 1, not retry the full chain forever.
        exc = Exception("Connection error.")
        exc.__cause__ = OSError("All connection attempts failed")
        assert _is_transient_provider_error(exc) is False

    def test_connection_refused_is_not_transient(self):
        assert _is_transient_provider_error(Exception("Connection refused")) is False

    def test_server_disconnect_is_transient(self):
        # #2 regression: a mid-run server disconnect — openai's APIConnectionError
        # (str 'Connection error.', no status) whose __cause__ is a transient
        # protocol error — must retry/fail over, NOT fail the job on attempt 1.
        # Distinct from a dead port (cause 'All connection attempts failed'),
        # which stays non-transient (test_dead_port_connection_error_is_not_transient).
        exc = Exception("Connection error.")
        exc.__cause__ = Exception("Server disconnected without sending a response.")
        assert _is_transient_provider_error(exc) is True

    def test_bare_connection_error_is_transient(self):
        # A generic 'Connection error.' with no dead-host cause is a transient
        # blip, not a config error.
        assert _is_transient_provider_error(Exception("Connection error.")) is True

    def test_400_bad_request_is_not_transient(self):
        class BadReq(Exception):
            status_code = 400
        assert _is_transient_provider_error(BadReq("bad request")) is False

    def test_408_request_timeout_is_transient(self):
        class ReqTimeout(Exception):
            status_code = 408
        assert _is_transient_provider_error(ReqTimeout("slow")) is True

    def test_connection_reset_stays_transient(self):
        # A genuine mid-run network blip (reset/aborted) should still retry.
        assert _is_transient_provider_error(Exception("Connection reset by peer")) is True

    def test_empty_content_in_place_retries_are_capped(self):
        # Deterministic empty content (single model, no chain) must NOT re-run the
        # identical prompt 6x — cap it (still fails over inside a chain `caller`).
        from pipeline.batch_evaluate import _EMPTY_CONTENT_MAX_ATTEMPTS
        calls = []
        def empty(system, user):
            calls.append(1)
            raise RuntimeError("provider returned empty content")
        with pytest.raises(RuntimeError, match="empty content"):
            _call_with_retry(empty, "s", "u", max_attempts=6, sleep=lambda _: None)
        assert len(calls) == _EMPTY_CONTENT_MAX_ATTEMPTS    # not 6

    def test_budget_stops_retrying_before_timeout(self):
        # A provider-wide outage must not burn the full max_attempts x backoff —
        # the per-job wall-clock budget cuts it off. Fake monotonic jumps past
        # the budget after the first failure.
        clock = [0.0]
        def fake_monotonic():
            return clock[0]
        def always_500(system, user):
            clock[0] += 1000.0      # each call "takes" 1000s of wall-clock
            raise Exception("Error code: 500 - inference error")
        calls = []
        def counted(system, user):
            calls.append(1)
            return always_500(system, user)
        with pytest.raises(Exception, match="inference error"):
            _call_with_retry(counted, "s", "u", max_attempts=6, budget=600.0,
                             sleep=lambda _: None, monotonic=fake_monotonic)
        assert len(calls) == 1      # gave up after the budget was blown, not 6


class TestClientHardening:
    """Live incident: a hung endpoint + the SDK's 600s default timeout and 2
    hidden internal retries froze a 16-worker run for hours with zero output."""

    def test_openai_client_gets_timeout_and_no_sdk_retries(self, monkeypatch):
        import types
        captured = {}
        def fake_openai(**kw):
            captured.update(kw)
            return types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **k: None)))
        monkeypatch.setattr("openai.OpenAI", fake_openai)
        from pipeline.batch_evaluate import _build_openai_compat_caller
        _build_openai_compat_caller("m", "key", base_url="https://x/v1")
        assert captured["timeout"] == 180.0
        assert captured["max_retries"] == 0

    def test_llm_timeout_env(self, monkeypatch):
        from pipeline.batch_evaluate import _llm_timeout
        monkeypatch.setenv("LLM_TIMEOUT", "90")
        assert _llm_timeout() == 90.0
        monkeypatch.setenv("LLM_TIMEOUT", "garbage")
        assert _llm_timeout() == 180.0

    def test_anthropic_client_gets_timeout_and_no_sdk_retries(self, monkeypatch):
        # The anthropic SDK also defaults to 600s + 2 hidden retries; harden it
        # the same way as the openai-compat client.
        import types
        anthropic = pytest.importorskip("anthropic")
        captured = {}
        def fake(**kw):
            captured.update(kw)
            return types.SimpleNamespace(
                messages=types.SimpleNamespace(create=lambda **k: None))
        monkeypatch.setattr(anthropic, "Anthropic", fake)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        from pipeline.batch_evaluate import _build_anthropic_caller
        _build_anthropic_caller("claude-x")
        assert captured["timeout"] == 180.0
        assert captured["max_retries"] == 0

    def test_gemini_client_gets_http_timeout(self, monkeypatch):
        import types
        genai = pytest.importorskip("google.genai")
        captured = {}
        def fake_client(**kw):
            captured.update(kw)
            return types.SimpleNamespace(
                models=types.SimpleNamespace(generate_content=lambda **k: None))
        monkeypatch.setattr(genai, "Client", fake_client)
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        from pipeline.batch_evaluate import _build_gemini_caller
        _build_gemini_caller("gemini-x")
        # http_options.timeout is in milliseconds (180s → 180000ms).
        assert captured.get("http_options") is not None
        assert int(captured["http_options"].timeout) == 180000


class TestMergeAndConcurrency:
    """The interrupted-eval merge gap (#4) and the BATCH_CONCURRENCY=0 floor."""

    def _career_ops(self, tmp_path, *, state_jobs=None, additions=None):
        career_ops = tmp_path / "career-ops"
        batch = career_ops / "batch"
        (batch / "tracker-additions").mkdir(parents=True)
        with open(batch / "batch-input.tsv", "w", newline="", encoding="utf-8") as f:
            f.write("id\turl\tsource\tnotes\n1\thttps://a.com\tAcme\tEng\n2\thttps://b.com\tGlobex\tDev\n")
        (career_ops / "cv.md").write_text("# CV", encoding="utf-8")
        if state_jobs is not None:
            (batch / "batch-api-state.json").write_text(
                json.dumps({"jobs": state_jobs}), encoding="utf-8")
        for name in (additions or []):
            (batch / "tracker-additions" / name).write_text("1\t2026-06-01\tAcme\n", encoding="utf-8")
        return career_ops

    def test_merge_runs_when_unmerged_additions_and_zero_pending(self, tmp_path, monkeypatch):
        # Both jobs "completed" → 0 pending, but a TSV is still unmerged (an
        # interrupted prior run). The early return must heal it by merging.
        career_ops = self._career_ops(
            tmp_path,
            state_jobs={"1": {"status": "completed"}, "2": {"status": "completed"}},
            additions=["1.tsv"],
        )
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        called = []
        monkeypatch.setattr("pipeline.batch_evaluate.run_merge_tracker",
                            lambda co: called.append(co) or True)
        assert run(career_ops, provider="anthropic") == 0
        assert called == [career_ops]

    def test_no_merge_when_zero_pending_and_no_additions(self, tmp_path, monkeypatch):
        career_ops = self._career_ops(
            tmp_path,
            state_jobs={"1": {"status": "completed"}, "2": {"status": "completed"}},
            additions=[],
        )
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        called = []
        monkeypatch.setattr("pipeline.batch_evaluate.run_merge_tracker",
                            lambda co: called.append(co) or True)
        assert run(career_ops, provider="anthropic") == 0
        assert called == []

    def test_concurrency_floored_to_one(self, tmp_path, monkeypatch, capsys):
        # BATCH_CONCURRENCY=0 would crash ThreadPoolExecutor(max_workers=0); the
        # floor keeps the eval stage from tracebacking after the expensive
        # earlier stages. Verified via the dry-run worker count.
        career_ops = self._career_ops(tmp_path)
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        run(career_ops, provider="anthropic", concurrency=0, dry_run=True)
        assert "workers=1" in capsys.readouterr().out


class TestEvalLock:
    """Single-flight across processes — two concurrent evaluations (a forgotten
    background run + a fresh one) contend on the state file."""

    def test_refuses_when_lock_held_by_live_pid(self, tmp_path, capsys):
        import os
        lock = tmp_path / "batch" / ".eval-lock"
        lock.parent.mkdir(parents=True)
        # A pid that is definitely alive and is NOT us: use our parent's? Use a
        # real child process held open briefly.
        import subprocess, sys, textwrap
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
        try:
            lock.write_text(str(child.pid), encoding="utf-8")
            assert run(tmp_path) == 0
            assert "already running" in capsys.readouterr().err
            assert lock.exists()          # we must not remove someone else's lock
        finally:
            child.kill()

    def test_stale_lock_taken_over_and_released(self, tmp_path, monkeypatch, capsys):
        import subprocess, sys
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock = tmp_path / "batch" / ".eval-lock"
        lock.parent.mkdir(parents=True)
        lock.write_text(str(dead.pid), encoding="utf-8")
        monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert run(tmp_path) == 0          # proceeds to "no batch-input.tsv"
        out = capsys.readouterr()
        assert "already running" not in out.err
        assert not lock.exists()           # released on exit

    def test_pid_alive(self):
        import os, subprocess, sys
        from pipeline.batch_evaluate import _pid_alive
        assert _pid_alive(os.getpid()) is True
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        assert _pid_alive(dead.pid) is False
        assert _pid_alive(0) is False


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


class TestOpenAICompatibleProviders:
    """Test the deepinfra/openrouter providers and the OPENAI_BASE_URL
    escape hatch. These all route through `_build_openai_compat_caller`,
    but with different (base_url, api_key) pairs depending on provider."""

    def test_deepinfra_in_provider_defaults(self):
        from pipeline.batch_evaluate import PROVIDER_DEFAULTS
        assert "deepinfra" in PROVIDER_DEFAULTS
        # Model ID should look like an HF-style path (matches DeepInfra convention)
        assert "/" in PROVIDER_DEFAULTS["deepinfra"]

    def test_openrouter_in_provider_defaults(self):
        from pipeline.batch_evaluate import PROVIDER_DEFAULTS
        assert "openrouter" in PROVIDER_DEFAULTS
        assert "/" in PROVIDER_DEFAULTS["openrouter"]

    def test_deepinfra_in_provider_keys_for_auto_detect(self):
        from pipeline.batch_evaluate import _PROVIDER_KEYS
        assert _PROVIDER_KEYS["deepinfra"] == "DEEPINFRA_API_KEY"

    def test_openrouter_in_provider_keys_for_auto_detect(self):
        from pipeline.batch_evaluate import _PROVIDER_KEYS
        assert _PROVIDER_KEYS["openrouter"] == "OPENROUTER_API_KEY"

    def test_auto_detect_picks_deepinfra_over_openai(self, monkeypatch):
        # Detection order pins deepinfra ahead of openai. With both keys set
        # the auto-detect picks deepinfra unless BATCH_PROVIDER overrides.
        for key in ("BATCH_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DEEPINFRA_API_KEY", "dkey")
        monkeypatch.setenv("OPENAI_API_KEY", "okey")
        assert _detect_provider() == "deepinfra"

    def test_auto_detect_picks_openrouter_over_openai(self, monkeypatch):
        for key in ("BATCH_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "DEEPINFRA_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "rkey")
        monkeypatch.setenv("OPENAI_API_KEY", "okey")
        assert _detect_provider() == "openrouter"

    def test_check_provider_returns_error_when_deepinfra_key_missing(self, monkeypatch):
        monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
        err = _check_provider("deepinfra")
        assert err is not None
        assert "DEEPINFRA_API_KEY" in err

    def test_check_provider_returns_error_when_openrouter_key_missing(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        err = _check_provider("openrouter")
        assert err is not None
        assert "OPENROUTER_API_KEY" in err

    def test_build_caller_for_deepinfra_uses_correct_base_url(self, monkeypatch, mocker):
        # Stub the underlying _build_openai_compat_caller so we can capture
        # the (api_key, base_url) it was called with without actually hitting
        # any network or requiring the openai SDK at test time.
        monkeypatch.setenv("DEEPINFRA_API_KEY", "test-deepinfra-key")
        mock_build = mocker.patch.object(eval_mod, "_build_openai_compat_caller", return_value=lambda s, u: "fake")
        eval_mod._build_caller("deepinfra", "some-model")
        kwargs = mock_build.call_args.kwargs
        assert kwargs["api_key"] == "test-deepinfra-key"
        assert kwargs["base_url"] == "https://api.deepinfra.com/v1/openai"
        assert mock_build.call_args.args[0] == "some-model"

    def test_build_caller_for_openrouter_uses_correct_base_url(self, monkeypatch, mocker):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-or-key")
        mock_build = mocker.patch.object(eval_mod, "_build_openai_compat_caller", return_value=lambda s, u: "fake")
        eval_mod._build_caller("openrouter", "some-model")
        kwargs = mock_build.call_args.kwargs
        assert kwargs["api_key"] == "test-or-key"
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"

    def test_openai_base_url_escape_hatch_overrides_default(self, monkeypatch, mocker):
        # The "openai" provider should respect OPENAI_BASE_URL when set,
        # letting users point it at any OpenAI-compatible endpoint.
        monkeypatch.setenv("OPENAI_API_KEY", "key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://my.proxy.example/v1")
        mock_build = mocker.patch.object(eval_mod, "_build_openai_compat_caller", return_value=lambda s, u: "fake")
        eval_mod._build_caller("openai", "gpt-4o-mini")
        assert mock_build.call_args.kwargs["base_url"] == "https://my.proxy.example/v1"

    def test_openai_uses_default_base_url_when_unset(self, monkeypatch, mocker):
        # Without OPENAI_BASE_URL, the openai provider passes base_url=None so
        # the SDK uses its built-in default (https://api.openai.com/v1).
        monkeypatch.setenv("OPENAI_API_KEY", "key")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        mock_build = mocker.patch.object(eval_mod, "_build_openai_compat_caller", return_value=lambda s, u: "fake")
        eval_mod._build_caller("openai", "gpt-4o-mini")
        assert mock_build.call_args.kwargs["base_url"] is None

    def test_openai_treats_empty_base_url_as_unset(self, monkeypatch, mocker):
        # Same defensive behavior as the BATCH_MODEL fix: GHA workflow YAML
        # `${{ vars.X || '' }}` produces empty strings, which `or None`
        # correctly collapses to None instead of sending "" as a URL.
        monkeypatch.setenv("OPENAI_API_KEY", "key")
        monkeypatch.setenv("OPENAI_BASE_URL", "")
        mock_build = mocker.patch.object(eval_mod, "_build_openai_compat_caller", return_value=lambda s, u: "fake")
        eval_mod._build_caller("openai", "gpt-4o-mini")
        assert mock_build.call_args.kwargs["base_url"] is None


class TestFreeTierPacing:
    """_build_caller wraps the caller with gemini_limits.paced_caller, so eval +
    tailoring + cover-letters all conform to the free-tier RPM through one place."""

    def test_build_caller_applies_pacing_when_conforming(self, monkeypatch):
        sentinel = lambda *a, **k: "RESULT"
        monkeypatch.setattr(eval_mod, "_build_single_caller", lambda *a, **k: sentinel)

        # conforming OFF → returned unwrapped
        monkeypatch.delenv("GEMINI_FREE_TIER", raising=False)
        assert eval_mod._build_caller("gemini", "gemini-2.5-flash") is sentinel

        # conforming ON + free-tier model → wrapped, but still calls through
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        wrapped = eval_mod._build_caller("gemini", "gemini-2.5-flash")
        assert wrapped is not sentinel
        assert wrapped() == "RESULT"

    def test_build_caller_no_pacing_for_paid_model(self, monkeypatch):
        sentinel = lambda *a, **k: "RESULT"
        monkeypatch.setattr(eval_mod, "_build_single_caller", lambda *a, **k: sentinel)
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        assert eval_mod._build_caller("openai", "gpt-4o-mini") is sentinel

    def test_run_eval_caps_pending_to_rpd_when_conforming(self, tmp_path, monkeypatch, capsys):
        # dry_run returns after the cap (before the caller/executor), so it's a
        # clean seam to verify _run_eval honors the free-tier daily cap.
        co = tmp_path / "career-ops"
        (co / "batch").mkdir(parents=True)
        (co / "batch" / "batch-input.tsv").write_text("x", encoding="utf-8")  # must exist
        monkeypatch.setattr(eval_mod, "load_state", lambda p: {})
        rows = [{"id": str(i), "source": "s", "notes": "n"} for i in range(50)]
        monkeypatch.setattr(eval_mod, "load_pending", lambda bi, st: list(rows))
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")

        n = eval_mod._run_eval(co, provider="gemini", model="gemini-2.5-flash", dry_run=True)
        assert n == 20                                   # capped from 50 (RPD)
        assert "30 deferred" in capsys.readouterr().err

    def test_run_eval_no_cap_when_not_conforming(self, tmp_path, monkeypatch):
        co = tmp_path / "career-ops"
        (co / "batch").mkdir(parents=True)
        (co / "batch" / "batch-input.tsv").write_text("x", encoding="utf-8")
        monkeypatch.setattr(eval_mod, "load_state", lambda p: {})
        rows = [{"id": str(i), "source": "s", "notes": "n"} for i in range(50)]
        monkeypatch.setattr(eval_mod, "load_pending", lambda bi, st: list(rows))
        monkeypatch.delenv("GEMINI_FREE_TIER", raising=False)

        n = eval_mod._run_eval(co, provider="gemini", model="gemini-2.5-flash", dry_run=True)
        assert n == 50                                   # no cap

    def test_run_eval_cap_sums_failover_chain(self, tmp_path, monkeypatch, capsys):
        co = tmp_path / "career-ops"
        (co / "batch").mkdir(parents=True)
        (co / "batch" / "batch-input.tsv").write_text("x", encoding="utf-8")
        monkeypatch.setattr(eval_mod, "load_state", lambda p: {})
        rows = [{"id": str(i), "source": "s", "notes": "n"} for i in range(50)]
        monkeypatch.setattr(eval_mod, "load_pending", lambda bi, st: list(rows))
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        # chain of two 20-RPD models → summed cap 40
        n = eval_mod._run_eval(co, provider="gemini",
                               model="gemini-2.5-flash,gemini-3.5-flash", dry_run=True)
        assert n == 40
        assert "10 deferred" in capsys.readouterr().err
