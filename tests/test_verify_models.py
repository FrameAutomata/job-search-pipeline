"""Tests for pipeline/verify_models.py — verify configured model IDs against
each provider's live catalog. A stale model ID is a silent run-failure (it just
errors at call time), so this is a maintenance guard you can re-run after editing
model config.

The network fetch is injected (`fetcher`), so the pure logic is hermetic:
- collect_configured(env): which (provider -> models) to check, from
  PROVIDER_DEFAULTS plus the BATCH_MODEL/TAILOR_MODEL/COVER_MODEL failover chains
  (chains attach to the configured/active provider).
- classify(configured, catalogs): per (provider, model) -> ok | missing |
  skipped (skipped when a provider's catalog couldn't be fetched / no key).
- run(env, fetcher, out): orchestrates and returns an exit code — 1 iff some
  configured model is missing from a catalog we could actually read.
"""

import pytest

from pipeline import verify_models as vm
from pipeline.batch_evaluate import PROVIDER_DEFAULTS


class TestCollectConfigured:
    def test_includes_each_provider_default(self):
        cfg = vm.collect_configured({})
        for prov, default in PROVIDER_DEFAULTS.items():
            assert default in cfg.get(prov, set()), prov

    def test_chain_models_attach_to_configured_provider(self):
        env = {"BATCH_PROVIDER": "deepinfra", "BATCH_MODEL": "org/A,org/B",
               "TAILOR_MODEL": "org/C", "COVER_MODEL": "org/A"}
        cfg = vm.collect_configured(env)
        assert {"org/A", "org/B", "org/C"} <= cfg["deepinfra"]
        # A provider that isn't the configured one carries only its own default.
        assert cfg["openai"] == {PROVIDER_DEFAULTS["openai"]}

    def test_active_provider_autodetected_from_key(self):
        # No BATCH_PROVIDER → fall back to the first provider whose key is set,
        # so the chain is checked against the right catalog.
        env = {"GROQ_API_KEY": "x", "BATCH_MODEL": "groq/thing"}
        cfg = vm.collect_configured(env)
        assert "groq/thing" in cfg["groq"]

    def test_tailor_model_attaches_to_tailor_provider(self):
        # Review bug: the split-provider setup (eval on DeepInfra, tailor on
        # Anthropic) checked TAILOR_MODEL against DeepInfra's catalog — a
        # guaranteed false "missing" while the real chain went unverified.
        env = {"BATCH_PROVIDER": "deepinfra", "BATCH_MODEL": "org/A",
               "TAILOR_PROVIDER": "anthropic", "TAILOR_MODEL": "claude-x"}
        cfg = vm.collect_configured(env)
        assert "claude-x" in cfg["anthropic"]
        assert "claude-x" not in cfg["deepinfra"]
        assert "org/A" in cfg["deepinfra"]


class TestClassify:
    def test_ok_and_missing(self):
        rows = vm.classify({"deepinfra": {"a", "b"}}, {"deepinfra": {"a"}})
        assert {(r["model"], r["status"]) for r in rows} == {("a", "ok"), ("b", "missing")}

    def test_none_catalog_is_skipped(self):
        # No key / unfetchable catalog → not a failure, just unverified.
        rows = vm.classify({"groq": {"m"}}, {"groq": None})
        assert [r["status"] for r in rows] == ["skipped"]


class TestRun:
    def test_exit_1_when_a_model_is_missing(self):
        env = {"BATCH_PROVIDER": "deepinfra", "BATCH_MODEL": "org/ghost"}

        def fetcher(provider, _env):
            # DeepInfra catalog is readable but lacks the chain model; others
            # have no key (None) and are skipped.
            return {PROVIDER_DEFAULTS["deepinfra"]} if provider == "deepinfra" else None

        out = []
        assert vm.run(env=env, fetcher=fetcher, out=out.append) == 1
        assert any("ghost" in line and "missing" in line.lower() for line in out)

    def test_exit_0_when_all_present_or_skipped(self):
        env = {"BATCH_PROVIDER": "deepinfra", "BATCH_MODEL": "org/A"}

        def fetcher(provider, _env):
            return {PROVIDER_DEFAULTS["deepinfra"], "org/A"} if provider == "deepinfra" else None

        assert vm.run(env=env, fetcher=fetcher, out=lambda *_: None) == 0

    def test_unknown_batch_provider_fails_loudly(self):
        # A typo'd BATCH_PROVIDER would make the real pipeline error at call time;
        # the verifier must flag it, not silently pass with the chain unchecked.
        env = {"BATCH_PROVIDER": "deepinftypo", "BATCH_MODEL": "x/y"}
        out = []
        assert vm.run(env=env, fetcher=lambda p, e: None, out=out.append) == 1
        assert any("deepinftypo" in line for line in out)

    def test_transient_error_is_surfaced_but_not_failed(self):
        # A reachable provider that errors (timeout/5xx) is "error", distinct from
        # "skipped (no key)" — reported so it isn't a silent pass, but it doesn't
        # fail the run (a network blip isn't a stale model → no flaky CI).
        env = {"BATCH_PROVIDER": "deepinfra", "BATCH_MODEL": "org/A"}

        def fetcher(provider, _env):
            return vm.FETCH_ERROR if provider == "deepinfra" else None

        out = []
        assert vm.run(env=env, fetcher=fetcher, out=out.append) == 0
        assert any("error" in line.lower() for line in out)


class TestClassifyError:
    def test_fetch_error_is_error_status(self):
        rows = vm.classify({"deepinfra": {"m"}}, {"deepinfra": vm.FETCH_ERROR})
        assert [r["status"] for r in rows] == ["error"]


class TestLiveFetch:
    """The per-provider catalog fetch, with the HTTP getter injected so the
    response-shaping (pagination, key handling, base-URL resolution) is hermetic."""

    def test_no_key_returns_none_not_error(self):
        assert vm._live_fetch("gemini", {}, get=lambda *a, **k: pytest.fail("should not fetch")) is None

    def test_request_failure_returns_fetch_error_sentinel(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        assert vm._live_fetch("deepinfra", {"DEEPINFRA_API_KEY": "k"}, get=boom) is vm.FETCH_ERROR

    def test_gemini_key_in_header_not_url_and_parses_names(self):
        seen = {}

        def fake(url, *, headers=None, params=None):
            seen.update(url=url, headers=headers or {}, params=params or {})
            return {"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemma-9"}]}

        out = vm._live_fetch("gemini", {"GEMINI_API_KEY": "SECRET"}, get=fake)
        assert out == {"gemini-2.5-flash", "gemma-9"}
        assert seen["headers"].get("x-goog-api-key") == "SECRET"        # key in header (#6)
        assert "SECRET" not in seen["url"] and "SECRET" not in str(seen["params"])

    def test_openai_honors_base_url_escape_hatch(self):
        seen = {}

        def fake(url, *, headers=None, params=None):
            seen["url"] = url
            return {"data": [{"id": "local-model"}]}

        out = vm._live_fetch("openai", {"OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "http://proxy:8000/v1"}, get=fake)
        assert out == {"local-model"}
        assert seen["url"].startswith("http://proxy:8000/v1/models")

    def test_anthropic_paginates_via_has_more(self):
        pages = [{"data": [{"id": "a"}], "has_more": True, "last_id": "a"},
                 {"data": [{"id": "b"}], "has_more": False}]
        calls = []

        def fake(url, *, headers=None, params=None):
            calls.append(params or {})
            return pages[len(calls) - 1]

        out = vm._live_fetch("anthropic", {"ANTHROPIC_API_KEY": "k"}, get=fake)
        assert out == {"a", "b"} and len(calls) == 2

    def test_gemini_paginates_via_next_page_token(self):
        pages = [{"models": [{"name": "models/x"}], "nextPageToken": "TOK"},
                 {"models": [{"name": "models/y"}]}]
        calls = []

        def fake(url, *, headers=None, params=None):
            calls.append(params or {})
            return pages[len(calls) - 1]

        out = vm._live_fetch("gemini", {"GEMINI_API_KEY": "k"}, get=fake)
        assert out == {"x", "y"} and len(calls) == 2

    def test_openrouter_is_public_no_key_needed(self):
        def fake(url, *, headers=None, params=None):
            assert "Authorization" not in (headers or {})   # public, no bearer
            return {"data": [{"id": "meta-llama/llama-3.3-70b-instruct"}]}

        out = vm._live_fetch("openrouter", {}, get=fake)
        assert out == {"meta-llama/llama-3.3-70b-instruct"}
