"""Verify configured LLM model IDs against each provider's live catalog.

A model deprecation is a *silent* run-failure — a stale default or failover-chain
entry just errors at call time. This checks the models you actually run (every
provider's `PROVIDER_DEFAULTS` plus the BATCH_MODEL/APPLY_MODEL/COVER_MODEL
failover chains, which attach to the configured/active provider) against each
provider's `/models` endpoint, so you can re-run it after editing model config
or on a schedule.

Statuses: `ok` (in the catalog), `missing` (catalog read but the ID is absent),
`error` (a key was set but the catalog couldn't be read — a transient blip, NOT
a stale model), `skipped` (no key / not applicable). Exit code is 1 iff some
model is `missing` or BATCH_PROVIDER names an unknown provider — both are
definite config errors the pipeline would hit at call time. A provider with no
key, or a transient `error`, never fails the run.

Keys are read from the environment and never printed (Gemini's key goes in a
header, not the URL, so it can't leak via a request exception either).

    python -m pipeline.verify_models
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pipeline.batch_evaluate import PROVIDER_BASE_URLS, PROVIDER_DEFAULTS, _PROVIDER_KEYS

ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT = 40
_UA = "job-search-pipeline/verify-models"  # some catalogs reject a UA-less request
_DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
_CHAIN_VARS = ("BATCH_MODEL", "APPLY_MODEL", "COVER_MODEL")

# Catalog sentinel: a key WAS present but the fetch failed (timeout / 5xx / bad
# shape). Distinct from None ("skipped — no key"), so a provider outage surfaces
# as `error` instead of silently passing as `skipped`.
FETCH_ERROR = object()


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
    provider (those chains are written in that provider's ID format). An unknown
    active provider can't host a chain here; run() flags it separately."""
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


def classify(configured: dict[str, set[str]], catalogs: dict[str, object]) -> list[dict]:
    """Per (provider, model) → a row with status `ok` / `missing` / `error`
    (catalog is FETCH_ERROR) / `skipped` (catalog is None). Sorted by provider
    then model."""
    rows: list[dict] = []
    for provider in sorted(configured):
        catalog = catalogs.get(provider)
        for model in sorted(configured[provider]):
            if catalog is FETCH_ERROR:
                status = "error"
            elif catalog is None:
                status = "skipped"
            elif model in catalog:
                status = "ok"
            else:
                status = "missing"
            rows.append({"provider": provider, "model": model, "status": status})
    return rows


def _http_get(url, *, headers=None, params=None) -> dict:
    import requests
    r = requests.get(url, headers={"User-Agent": _UA, **(headers or {})},
                     params=params or {}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fetch_gemini(get, key) -> set[str]:
    """Gemini's catalog, following nextPageToken. The key goes in the
    x-goog-api-key HEADER (not the URL), so it can't leak via a request URL in
    any exception/log."""
    ids: set[str] = set()
    headers = {"x-goog-api-key": key}
    token = None
    while True:
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        data = get("https://generativelanguage.googleapis.com/v1beta/models",
                   headers=headers, params=params)
        ids |= {m.get("name", "").split("/")[-1] for m in data.get("models", [])}
        token = data.get("nextPageToken")
        if not token:
            return ids


def _fetch_anthropic(get, key) -> set[str]:
    """Anthropic's catalog, following the cursor (has_more / last_id). The default
    page limit is small, so an unpaginated fetch would miss older-but-valid models."""
    ids: set[str] = set()
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    after = None
    while True:
        params = {"limit": 1000}
        if after:
            params["after_id"] = after
        data = get("https://api.anthropic.com/v1/models", headers=headers, params=params)
        items = data.get("data", [])
        ids |= {m.get("id", "") for m in items}
        if not items or not data.get("has_more"):
            return ids
        after = items[-1].get("id")


def _openai_style_base(provider: str, env) -> str:
    """The OpenAI-compatible base URL for a provider. openai honors the
    OPENAI_BASE_URL escape hatch (vLLM/proxy), exactly like batch_evaluate's
    caller; the rest come from the shared PROVIDER_BASE_URLS."""
    if provider == "openai":
        return (env.get("OPENAI_BASE_URL") or _DEFAULT_OPENAI_BASE).rstrip("/")
    return PROVIDER_BASE_URLS[provider].rstrip("/")


def _live_fetch(provider: str, env, *, get=_http_get) -> object:
    """The set of model IDs in `provider`'s live catalog, or None (no key / not
    applicable) or FETCH_ERROR (key present, fetch failed). `get` is injected so
    the response-shaping is testable without the network. Keys read from `env`,
    never printed."""
    try:
        if provider == "gemini":
            key = env.get("GEMINI_API_KEY")
            return _fetch_gemini(get, key) if key else None
        if provider == "anthropic":
            key = env.get("ANTHROPIC_API_KEY")
            return _fetch_anthropic(get, key) if key else None
        if provider == "ollama":
            base = env.get("OLLAMA_BASE_URL")  # local; only checkable with a base URL
            if not base:
                return None
            data = get(base.rstrip("/") + "/api/tags")
            return {m.get("name", "") for m in data.get("models", [])}
        # OpenAI-compatible: openai / groq / deepinfra / openrouter
        if provider == "openrouter":
            headers = {}  # public catalog, no key
        else:
            keyvar = _PROVIDER_KEYS.get(provider)
            key = env.get(keyvar) if keyvar else None
            if not key:
                return None
            headers = {"Authorization": f"Bearer {key}"}
        data = get(_openai_style_base(provider, env) + "/models", headers=headers)
        return {m.get("id", "") for m in data.get("data", [])}
    except Exception:
        # A key was set (we passed the no-key guards) but the catalog couldn't be
        # read → `error`, not a silent `skipped`. The exception (which for some
        # providers could carry the URL) is discarded, never logged.
        return FETCH_ERROR


_TAG = {"ok": "OK     ", "missing": "MISSING", "error": "ERROR  ", "skipped": "skip   "}


def run(env=None, fetcher=None, out=print) -> int:
    """Check every configured model against its provider's catalog, print a
    report, and return an exit code: 1 iff a model is `missing` or BATCH_PROVIDER
    is an unknown provider (both are config errors the pipeline would hit). A
    transient `error` or a keyless `skipped` never fails the run."""
    env = os.environ if env is None else env
    fetcher = fetcher or _live_fetch
    configured = collect_configured(env)
    catalogs = {p: fetcher(p, env) for p in configured}
    rows = classify(configured, catalogs)

    counts = {"ok": 0, "missing": 0, "error": 0, "skipped": 0}
    for r in rows:
        counts[r["status"]] += 1
        out(f"  {_TAG[r['status']]}  {r['provider']:<11} {r['model']}")

    # A typo'd / unknown BATCH_PROVIDER would make the real pipeline error at call
    # time; flag it instead of silently leaving its model chain unverified.
    active = _active_provider(env)
    unknown = bool(active) and active not in PROVIDER_DEFAULTS
    if unknown:
        out(f"  ERROR    BATCH_PROVIDER={active!r} is not a known provider — its "
            f"model chain was NOT verified")

    out(f"[verify-models] {counts['ok']} ok, {counts['missing']} missing, "
        f"{counts['error']} error (unreadable), {counts['skipped']} skipped (no key)")
    if counts["missing"] or unknown:
        out("[verify-models] FAIL — update the stale model ID in .env / PROVIDER_DEFAULTS "
            "(or fix BATCH_PROVIDER).")
    return 1 if (counts["missing"] or unknown) else 0


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return run()


if __name__ == "__main__":
    sys.exit(main())
