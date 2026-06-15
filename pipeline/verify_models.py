"""Verify configured LLM model IDs against each provider's live catalog.

A model deprecation is a *silent* run-failure — a stale default or failover-chain
entry just errors at call time. This checks the models you actually run (every
provider's `PROVIDER_DEFAULTS` plus the BATCH_MODEL/APPLY_MODEL/COVER_MODEL
failover chains, which attach to the configured/active provider) against each
provider's `/models` endpoint, so you can re-run it after editing model config
or on a schedule.

Only providers with a key set are checked live (OpenRouter is public, so it's
always checkable); the rest report as `skipped`, never a failure. Keys are read
from the environment and never printed.

    python -m pipeline.verify_models      # exit 1 if any configured model is MISSING
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pipeline.batch_evaluate import PROVIDER_DEFAULTS, _PROVIDER_KEYS

ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT = 40
# Failover-chain env vars, in the same precedence the pipeline reads them.
_CHAIN_VARS = ("BATCH_MODEL", "APPLY_MODEL", "COVER_MODEL")


def _active_provider(env) -> str | None:
    """The provider whose failover chains apply: BATCH_PROVIDER if set, else the
    first provider with a key — matching batch_evaluate._detect_provider's order."""
    explicit = (env.get("BATCH_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    for provider, keyvar in _PROVIDER_KEYS.items():
        if env.get(keyvar):
            return provider
    return None


def collect_configured(env=None) -> dict[str, set[str]]:
    """`{provider: {models to verify}}` — every provider's default model, plus
    the failover-chain models (BATCH/APPLY/COVER_MODEL) attached to the active
    provider (those chains are written in that provider's ID format)."""
    env = os.environ if env is None else env
    configured: dict[str, set[str]] = {p: {default} for p, default in PROVIDER_DEFAULTS.items()}
    active = _active_provider(env)
    if active in configured:
        for var in _CHAIN_VARS:
            for model in (env.get(var) or "").split(","):
                model = model.strip()
                if model:
                    configured[active].add(model)
    return configured


def classify(configured: dict[str, set[str]], catalogs: dict[str, set[str] | None]) -> list[dict]:
    """Per (provider, model) → a row with status `ok` (in the catalog),
    `missing` (catalog read but model absent), or `skipped` (catalog is None —
    unreadable / no key, NOT a failure). Sorted by provider then model."""
    rows: list[dict] = []
    for provider in sorted(configured):
        catalog = catalogs.get(provider)
        for model in sorted(configured[provider]):
            if catalog is None:
                status = "skipped"
            elif model in catalog:
                status = "ok"
            else:
                status = "missing"
            rows.append({"provider": provider, "model": model, "status": status})
    return rows


# OpenAI-style `/models` endpoints (return {"data": [{"id": ...}]}): keyvar, URL,
# and the auth headers built from the key.
_OPENAI_STYLE: dict[str, tuple] = {
    "deepinfra": ("DEEPINFRA_API_KEY", "https://api.deepinfra.com/v1/openai/models",
                  lambda k: {"Authorization": f"Bearer {k}"}),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1/models",
             lambda k: {"Authorization": f"Bearer {k}"}),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/models",
               lambda k: {"Authorization": f"Bearer {k}"}),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models",
                  lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
}


def _live_fetch(provider: str, env) -> set[str] | None:
    """The set of model IDs in `provider`'s live catalog, or None when it can't
    be read (no key, or a request error) — None means `skipped`, not `failed`.
    Reads keys from `env`; never logs them."""
    import requests

    def _get(url, **kw):
        r = requests.get(url, timeout=_TIMEOUT, **kw)
        r.raise_for_status()
        return r.json()

    try:
        if provider == "openrouter":  # public catalog, no key needed
            data = _get("https://openrouter.ai/api/v1/models")
            return {m.get("id", "") for m in data.get("data", [])}
        if provider == "gemini":
            key = env.get("GEMINI_API_KEY")
            if not key:
                return None
            data = _get("https://generativelanguage.googleapis.com/v1beta/models",
                        params={"key": key, "pageSize": 1000})
            return {m.get("name", "").split("/")[-1] for m in data.get("models", [])}
        if provider == "ollama":
            base = env.get("OLLAMA_BASE_URL")
            if not base:
                return None  # local; only checkable when a base URL is configured
            data = _get(base.rstrip("/") + "/api/tags")
            return {m.get("name", "") for m in data.get("models", [])}
        spec = _OPENAI_STYLE.get(provider)
        if spec is None:
            return None
        keyvar, url, headers_fn = spec
        key = env.get(keyvar)
        if not key:
            return None
        data = _get(url, headers=headers_fn(key))
        return {m.get("id", "") for m in data.get("data", [])}
    except Exception:
        return None  # unreachable / bad key / unexpected shape → unverifiable, not a failure


_TAG = {"ok": "OK     ", "missing": "MISSING", "skipped": "skip   "}


def run(env=None, fetcher=None, out=print) -> int:
    """Check every configured model against its provider's catalog and print a
    report. Returns 1 iff some model is MISSING from a catalog we could read
    (so it's CI-gateable); a provider with no key is `skipped`, never a failure."""
    env = os.environ if env is None else env
    fetcher = fetcher or _live_fetch
    configured = collect_configured(env)
    catalogs = {p: fetcher(p, env) for p in configured}
    rows = classify(configured, catalogs)

    counts = {"ok": 0, "missing": 0, "skipped": 0}
    for r in rows:
        counts[r["status"]] += 1
        out(f"  {_TAG[r['status']]}  {r['provider']:<11} {r['model']}")
    out(f"[verify-models] {counts['ok']} ok, {counts['missing']} missing, "
        f"{counts['skipped']} skipped (no key / unreadable)")
    if counts["missing"]:
        out("[verify-models] FAIL — a configured model is not in its provider's catalog "
            "(stale default or chain entry); update the model ID in .env / PROVIDER_DEFAULTS.")
    return 1 if counts["missing"] else 0


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return run()


if __name__ == "__main__":
    sys.exit(main())
