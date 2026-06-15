"""Tests for pipeline/verify_models.py — verify configured model IDs against
each provider's live catalog. A stale model ID is a silent run-failure (it just
errors at call time), so this is a maintenance guard you can re-run after editing
model config.

The network fetch is injected (`fetcher`), so the pure logic is hermetic:
- collect_configured(env): which (provider -> models) to check, from
  PROVIDER_DEFAULTS plus the BATCH_MODEL/APPLY_MODEL/COVER_MODEL failover chains
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
               "APPLY_MODEL": "org/C", "COVER_MODEL": "org/A"}
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
