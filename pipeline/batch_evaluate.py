"""Synchronous parallel batch evaluator supporting multiple LLM providers.

Processes jobs from batch-input.tsv immediately (no async wait) using parallel
workers. Results are written as they complete.

Supported providers
-------------------
  anthropic    claude-sonnet-4-6 (default)             ANTHROPIC_API_KEY
  gemini       gemini-2.5-flash  (default)             GEMINI_API_KEY
  openai       gpt-4o-mini       (default)             OPENAI_API_KEY
  groq         llama-3.3-70b-versatile (default)       GROQ_API_KEY
  deepinfra    deepseek-ai/DeepSeek-V4-Flash (def)     DEEPINFRA_API_KEY
  openrouter   meta-llama/llama-3.3-70b-instruct (def) OPENROUTER_API_KEY
  deepseek     deepseek-chat     (default)             DEEPSEEK_API_KEY
  ollama       qwen2.5:32b       (default)             OLLAMA_BASE_URL (default: http://localhost:11434)

Hosted open-model providers (deepinfra, openrouter) serve open-weight models
like Llama 3.3 70B and DeepSeek V3 over OpenAI-compatible APIs. See
QUICKSTART.md "Which provider should I pick?" for guidance on choosing
between providers based on use case.

Escape hatch: set OPENAI_BASE_URL to point the `openai` provider at any
OpenAI-compatible endpoint (e.g. local vLLM server, internal proxy, a
provider we haven't enumerated above). Use BATCH_PROVIDER=openai +
OPENAI_API_KEY=<their-key> + OPENAI_BASE_URL=<their-url>.

DeepSeek's own API (DEEPSEEK_API_KEY, https://api.deepseek.com) is a first-class
provider — cheaper than DeepInfra hosting the same weights, for high-volume runs.

Provider auto-detection: BATCH_PROVIDER env var, then first key found in the
order: gemini → groq → deepinfra → openrouter → deepseek → openai → anthropic.

Requirements (install only the provider you need):
  pip install anthropic                  # anthropic
  pip install google-genai               # gemini (NOT the deprecated google-generativeai)
  pip install openai                     # openai / groq / deepinfra / openrouter / ollama
"""

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pipeline._batch_common import (
    MAX_TOKENS,
    acquire_process_lock,
    assign_job_numbers,
    atomic_write_text,
    build_user_message,
    env_float,
    eval_system_prompt,
    has_pending_tracker_additions,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    pid_alive,
    read_process_lock,
    refresh_process_lock,
    release_process_lock,
    read_text,
    run_merge_tracker,
    write_job_result,
)
from pipeline import gemini_limits
from pipeline.stdio import line_buffer_stdout

# Hoisted to _batch_common (shared with the UI's local-run orphan guard); kept
# under the old private name for callers/tests that import it from here.
_pid_alive = pid_alive

ROOT = Path(__file__).resolve().parent.parent

PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    # gemini-2.5-flash, NOT gemini-2.0-flash: the 2.0 model was deprecated by
    # Google (shutdown 2026-06-01) and its free-tier quota collapsed months
    # ahead of that. 2.5-flash is supported — but its free-tier RPD is only
    # ~20/day, so users running >20 evaluations per day should override
    # BATCH_MODEL to gemma-4-26b-a4b-it (14.4K RPD, but 16K TPM — which is what
    # actually binds, at ~2.4K evaluations/day). See gemini_limits.py for the
    # per-model free-tier caps + the run-time warning, and .env.example.
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    # Hosted open-weight providers. The model ID conventions differ across
    # providers (capital vs lowercase namespaces), so these aren't typos.
    # Override via BATCH_MODEL.
    "deepinfra": "deepseek-ai/DeepSeek-V4-Flash",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    # DeepSeek's own API (cheaper than DeepInfra hosting the same weights).
    # "deepseek-chat" is the stable flagship alias; override via BATCH_MODEL for a
    # specific version (e.g. DeepSeek-V4-Pro).
    "deepseek": "deepseek-chat",
    "ollama": "qwen2.5:32b",
}

# OpenAI-compatible chat base URLs for the providers that use a fixed host.
# (openai uses OPENAI_BASE_URL or the SDK default; ollama uses OLLAMA_BASE_URL.)
# Shared so pipeline/verify_models derives each catalog endpoint from the same
# source instead of re-hardcoding it (one place to update on a host migration).
PROVIDER_BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com",
}


# ── Provider client factories ────────────────────────────────────────────────
# Each factory builds the SDK client once and returns a `(system, user) -> str`
# callable. Workers reuse the same client across all jobs.

Caller = Callable[[str, str], str]


# ── Rate-limit retry ────────────────────────────────────────────────────────
# Free tiers (especially Gemini's 15 RPM / 1500 RPD) throttle aggressively
# when the pipeline runs against hundreds of jobs. Without retry, a single
# 429 marks a job permanently failed and we lose evaluations to transient
# throttling. The helpers below wrap each call with exponential backoff so
# rate-limit hiccups self-heal.

# Substrings that consistently appear in rate-limit error messages across
# the providers we support (Anthropic, OpenAI, Groq, Gemini, Ollama-via-
# OpenAI). Match against the lowercased str(exc) so we don't need to import
# every SDK's exception classes (and create hard import-time dependencies on
# providers the user hasn't installed).
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "quota",
    "resourceexhausted",
    "resource_exhausted",
    "too many requests",
)

# Server-side trouble that is NOT the caller's fault: worth retrying in place
# and worth failing over to a sibling model. (A live run hit a deepinfra model
# endpoint returning HTTP 500 "inference error" for every request — only
# rate-limits triggered failover, so 183 jobs failed against a configured
# 3-model chain.) Distinct from 4xx caller errors (bad model id, auth), which
# must keep raising immediately.
#
# NB: a bare "connection error" is deliberately NOT here — openai's
# APIConnectionError stringifies to exactly that for a DEAD endpoint (wrong
# OLLAMA_BASE_URL/port), which is a config error that must fail loudly, not
# retry the full backoff x chain for every job (see _CONFIG_ERROR_MARKERS).
# Genuine mid-run network blips (reset/aborted) stay transient.
_TRANSIENT_SERVER_MARKERS = (
    "inference error",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "overloaded",
    "timed out",
    "timeout",
    # A bare "connection error" (openai's APIConnectionError) is a transient
    # blip by default — a dead host/port is caught FIRST by _CONFIG_ERROR_MARKERS
    # (checked against __cause__), so this only retries genuine disconnects.
    "connection error",
    "connection reset",
    "connection aborted",
    "server disconnected",
    "temporarily unavailable",
    # Our own callers raise "<provider> returned empty content" when a model
    # answers 200 with an empty body — a degraded endpoint, or a reasoning
    # model that burned its whole token budget thinking. Either way the model
    # is unusable for this call: retry, and above all FAIL OVER to a sibling.
    "empty content",
)

# Definitive misconfiguration: a dead host/port or unresolvable name. Retrying
# these for every job just burns ~31s x chain-width x backlog on an error that
# won't self-heal — fail on job 1 instead. openai's APIConnectionError prints a
# bare "Connection error." but carries the real cause ("All connection attempts
# failed" / "Connection refused") on __cause__, so we match against both.
_CONFIG_ERROR_MARKERS = (
    "connection refused",
    "all connection attempts failed",
    "failed to establish a new connection",
    "nodename nor servname",
    "name or service not known",
    "getaddrinfo failed",
    "no address associated with hostname",
)

# An empty-content failure is deterministic on a single model (a reasoning model
# exhausting MAX_TOKENS, a safety block) — failover to a SIBLING is the right
# move, but re-running the identical prompt against the SAME model many times is
# near-pure waste (each is a full-length generation). Cap the in-place retries.
_EMPTY_CONTENT_MAX_ATTEMPTS = 2


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Best-effort detection of provider rate-limit errors.

    Checks numeric status_code/code attributes first (most reliable when the
    SDK exposes them), then falls back to substring matching against the
    error message."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if status == 429 or status == "429":
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _llm_timeout() -> float:
    """Per-request timeout in seconds (LLM_TIMEOUT env, default 180). Long
    enough for a reasoning model writing a full evaluation, short enough that
    a dead endpoint fails over instead of freezing a worker."""
    return env_float("LLM_TIMEOUT", 180.0)


def _llm_job_budget() -> float:
    """Per-job wall-clock budget in seconds (LLM_JOB_BUDGET env, default 600).
    Bounds how long _call_with_retry keeps STARTING new attempts: once the next
    backoff would cross the deadline it gives up. It cannot interrupt a call
    already in flight, so the true worst case is budget + one more caller
    invocation (a failover chain = chain-width x per-call timeout). Still far
    better than the un-budgeted 6 attempts x chain-width x timeout."""
    return env_float("LLM_JOB_BUDGET", 600.0)


def _is_empty_content_error(exc: BaseException) -> bool:
    return "empty content" in str(exc).lower()


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Rate limits PLUS server-side failures (5xx, timeouts, 'inference
    error'): everything where a retry or a failover to a sibling model makes
    sense. Definitive caller/config errors (401 auth, 404 bad model id, a dead
    host/port) stay non-transient so a misconfiguration fails loudly on job 1
    instead of burning the whole chain x backlog."""
    # Combine the message with its __cause__ — openai's APIConnectionError
    # stringifies to a bare "Connection error." and puts the real reason
    # ("All connection attempts failed") on the cause.
    text = (str(exc) + " " + str(getattr(exc, "__cause__", "") or "")).lower()
    if any(marker in text for marker in _CONFIG_ERROR_MARKERS):
        return False
    if _is_rate_limit_error(exc):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if code is not None:
        if code in (408, 429):     # request timeout / too many requests — transient
            return True
        if 400 <= code < 500:      # other 4xx is the caller's fault — never retry
            return False
        if 500 <= code <= 599:
            return True
    return any(marker in text for marker in _TRANSIENT_SERVER_MARKERS)


def _call_with_retry(
    caller: Caller,
    system: str,
    user: str,
    *,
    max_attempts: int = 6,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    budget: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Invoke `caller(system, user)` with exponential backoff on transient
    provider errors (rate limits, 5xx/'inference error', timeouts). Caller
    errors (auth, bad model id, a dead host/port) raise immediately — no retry.

    Two bounds keep a provider outage from freezing a worker for tens of
    minutes (`caller` may itself be a multi-model failover chain, so a naive
    `max_attempts x chain-width x per-call timeout` is the worst case):

      * `budget` — a per-job wall-clock deadline (default `_llm_job_budget()`):
        once the next backoff sleep would cross it, give up now.
      * empty-content is capped at `_EMPTY_CONTENT_MAX_ATTEMPTS` in place — it's
        deterministic per model, so re-running the same prompt is near-pure
        waste (the failover to a sibling already happened inside `caller`).

    Backoff: 1, 2, 4, 8, 16 seconds, each with up to 0.5s of random jitter to
    avoid thundering-herd when multiple workers throttle simultaneously.

    `sleep`/`monotonic` are parameterized so tests don't have to actually wait."""
    if budget is None:
        budget = _llm_job_budget()
    deadline = monotonic() + budget
    delay = base_delay
    empty_attempts = 0
    for attempt in range(1, max_attempts + 1):
        try:
            return caller(system, user)
        except Exception as exc:
            if not _is_transient_provider_error(exc) or attempt == max_attempts:
                raise
            if _is_empty_content_error(exc):
                empty_attempts += 1
                if empty_attempts >= _EMPTY_CONTENT_MAX_ATTEMPTS:
                    raise
            jittered = delay + random.uniform(0, 0.5)
            if monotonic() + jittered >= deadline:
                raise  # the next wait would blow the per-job budget
            print(f"  provider busy (attempt {attempt}/{max_attempts}): "
                  f"sleeping {jittered:.1f}s — {exc}")
            sleep(jittered)
            delay *= 2
    # Unreachable: the for-loop above either returns or raises on the last attempt.
    raise RuntimeError("retry loop exited without return or raise")  # pragma: no cover


def _build_anthropic_caller(model: str) -> Caller:
    import anthropic as _a
    # Same hardening as the openai-compat client: an explicit timeout (the SDK
    # default is 600s) and max_retries=0 so OUR retry/failover layer owns
    # recovery instead of the SDK silently multiplying every wait by its
    # built-in 2 retries.
    client = _a.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                          timeout=_llm_timeout(), max_retries=0)

    def call(system: str, user: str) -> str:
        msg = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = msg.content[0].text if msg.content else None
        if not text:
            raise RuntimeError("anthropic returned empty content")
        return text

    return call


def _build_gemini_caller(model: str) -> Caller:
    # Uses the modern `google-genai` SDK (package: google-genai). The older
    # `google-generativeai` package was deprecated in early 2026 — it still
    # works but logs a FutureWarning and won't get bug fixes.
    from google import genai  # type: ignore[import]
    from google.genai import types  # type: ignore[import]
    # http_options.timeout is in MILLISECONDS. Without it the client has no
    # request timeout, so a hung endpoint blocks the worker forever and our
    # retry/failover never gets a chance to fire (LLM_TIMEOUT silently ignored).
    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(timeout=int(_llm_timeout() * 1000)),
    )

    def call(system: str, user: str) -> str:
        resp = client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system),
        )
        text = getattr(resp, "text", None)
        if not text:
            raise RuntimeError("gemini returned empty content")
        return text

    return call


# Providers whose OpenAI-compatible backends are vLLM/TGI-style and accept
# `chat_template_kwargs` — so we can turn off a reasoning model's <think> phase.
# Real OpenAI / Groq reject unknown body params, so they never get the toggle.
_THINKING_TOGGLE_PROVIDERS = frozenset({"deepinfra", "openrouter", "ollama"})


def _build_openai_compat_caller(model: str, api_key: str, base_url: str | None = None,
                                disable_thinking: bool = False) -> Caller:
    from openai import OpenAI  # type: ignore[import]
    # Explicit timeout: the SDK default is 600s, so a hung endpoint silently
    # stalls a worker for 10 minutes per attempt (a live MiMo outage froze a
    # 16-worker run for hours with zero output). max_retries=0 because OUR
    # retry/failover layer owns recovery — the SDK's hidden 2 retries
    # multiplied every wait.
    client = OpenAI(api_key=api_key, base_url=base_url,
                    timeout=_llm_timeout(), max_retries=0)
    # Reasoning models (MiMo, Qwen3, DeepSeek-R1) emit long <think> traces by
    # default — slow, and they can exhaust max_tokens before producing the answer
    # (→ empty content). For short/direct outputs we don't need it, so pass
    # chat_template_kwargs to disable it. Non-reasoning models ignore the field.
    extra_body = {"chat_template_kwargs": {"enable_thinking": False}} if disable_thinking else None

    def call(system: str, user: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=extra_body,
        )
        content = resp.choices[0].message.content
        if not content:
            # None happens on refusals / tool-only responses. Treat as failure.
            raise RuntimeError("provider returned empty content")
        return content

    return call


def _split_models(model: str) -> list[str]:
    """Parse a model spec into a list. Comma-separated = a failover chain tried
    in order; a single value = no failover (back-compatible)."""
    return [m.strip() for m in (model or "").split(",") if m.strip()]


def _build_failover_caller(provider: str, models: list[str], *,
                           disable_thinking: bool = False) -> Caller:
    """Try each model in order, falling over to the next when one is overloaded
    (429 / "engine_overloaded"). Raises the last overload error if ALL are busy —
    which _call_with_retry then backs off and retries (re-trying the whole chain).
    Non-overload errors (e.g. a bad model id) raise immediately and aren't masked."""
    built = [(m, _build_single_caller(provider, m, disable_thinking=disable_thinking))
             for m in models]

    def call(system: str, user: str) -> str:
        last_exc: Exception | None = None
        for i, (model, caller) in enumerate(built):
            try:
                return caller(system, user)
            except Exception as exc:
                if not _is_transient_provider_error(exc):
                    raise
                last_exc = exc
                if i + 1 < len(built):
                    print(f"  model unavailable ({model}: {str(exc)[:60]}) — "
                          f"failing over to {built[i + 1][0]}")
        raise last_exc if last_exc else RuntimeError("no models configured")

    return call


def _build_caller(provider: str, model: str, *, disable_thinking: bool = False) -> Caller:
    """Build the LLM caller. `model` may be a comma-separated failover chain
    (tried in order on overload). `disable_thinking` is honored per the
    single-model builder."""
    models = _split_models(model)
    if len(models) > 1:
        caller = _build_failover_caller(provider, models, disable_thinking=disable_thinking)
    else:
        caller = _build_single_caller(provider, models[0] if models else model,
                                      disable_thinking=disable_thinking)
    # Gemini free-tier conforming: pace each call to the (lead) model's RPM and
    # TPM. No-op unless GEMINI_FREE_TIER is set and the lead model is a known
    # free-tier model.
    lead = models[0] if models else model
    # Warn HERE rather than at the eval stage, because this is where the no-op
    # happens: every caller in the repo is built through this function
    # (resume_tailor, cover_letters, article_digest and handoff's --handoff-tailor
    # all reach it via resolve_caller), so warning at one caller's stage left the
    # other four silently unpaced — the same silence, one layer up.
    #
    # Gated on the provider, not just the model name: GEMINI_FREE_TIER is a
    # global flag, so a cross-provider split (evaluate on Gemini, TAILOR_PROVIDER
    # =anthropic) would otherwise reach this with a Claude model and advise
    # looking it up in AI Studio, which has no row for it.
    #
    # Passed the whole SPEC, not `lead`: unknown_members is spec-shaped and
    # cap_to_rpd already sums across the chain, so an unknown non-lead member is
    # exactly the unpaced-but-silent case this exists to end.
    if provider == "gemini":
        unconformable = gemini_limits.format_unconformable_warning(model)
        if unconformable:
            print(unconformable, file=sys.stderr)
    return gemini_limits.paced_caller(caller, lead)


def _build_single_caller(provider: str, model: str, *, disable_thinking: bool = False) -> Caller:
    """Build a caller bound to one model. `disable_thinking` turns off reasoning
    models' <think> phase, but only for providers whose backend supports the
    toggle (vLLM/TGI-style); ignored elsewhere so callers can pass it safely
    regardless of which provider is active."""
    toggle = disable_thinking and provider in _THINKING_TOGGLE_PROVIDERS
    if provider == "anthropic":
        return _build_anthropic_caller(model)
    if provider == "gemini":
        return _build_gemini_caller(model)
    if provider == "openai":
        # OPENAI_BASE_URL escape hatch — lets users point the openai provider
        # at any OpenAI-compatible endpoint (local vLLM, internal proxy, a
        # provider we haven't added explicitly). Empty string handled the same
        # way as unset.
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        return _build_openai_compat_caller(
            model, api_key=os.environ["OPENAI_API_KEY"], base_url=base_url,
        )
    if provider in PROVIDER_BASE_URLS:
        # Hosted OpenAI-compatible providers (groq/deepinfra/deepseek/openrouter):
        # one branch keyed off the shared tables so adding a provider is a
        # table-only change. Passing `toggle` uniformly is safe — it's already
        # gated to _THINKING_TOGGLE_PROVIDERS, so it's False for e.g. groq.
        return _build_openai_compat_caller(
            model, api_key=os.environ[_PROVIDER_KEYS[provider]],
            base_url=PROVIDER_BASE_URLS[provider], disable_thinking=toggle,
        )
    if provider == "ollama":
        # `or DEFAULT` not `get(VAR, DEFAULT)` — see BATCH_MODEL fix below for
        # the rationale. Empty-string OLLAMA_BASE_URL would otherwise produce
        # a bogus "/v1" base URL.
        base = (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/") + "/v1"
        return _build_openai_compat_caller(model, api_key="ollama", base_url=base, disable_thinking=toggle)
    raise ValueError(f"Unknown provider: {provider!r}. Choose: {', '.join(PROVIDER_DEFAULTS)}")


# ── Provider validation ──────────────────────────────────────────────────────

# Detection order matters when a user has multiple keys configured. We check
# free-tier providers first (Gemini, Groq), then hosted open-model providers
# (DeepInfra, OpenRouter), then closed-weights paid APIs (OpenAI, Anthropic).
# Users can always override with BATCH_PROVIDER if they want a specific one.
# ollama has no required key, so it is excluded from auto-detection.
_PROVIDER_KEYS: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _detect_provider() -> str | None:
    explicit = os.environ.get("BATCH_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    for provider, key in _PROVIDER_KEYS.items():
        if os.environ.get(key):
            return provider
    return None


def _check_provider(provider: str) -> str | None:
    key = _PROVIDER_KEYS.get(provider)
    if key and not os.environ.get(key):
        return f"{key} not set"
    return None


def resolve_caller(provider: str | None = None, model: str | None = None, *,
                   lead_env: str | None = None, lead_provider_env: str | None = None,
                   disable_thinking: bool = False):
    """Build an LLM caller, resolving provider and model from one place (cover
    letters, resume tailoring, and the article digest all funnel through here
    instead of each re-implementing the precedence chain).

    provider: `lead_provider_env` (e.g. TAILOR_PROVIDER, a deliberate per-caller
        override) if set wins, else the explicit arg, else BATCH_PROVIDER / first
        provider key found. The dedicated env wins over the arg because callers
        thread the EVAL provider in as `provider=`, and a tailor override must
        beat that inherited value, not lose to it.
    model: explicit → `lead_env` override (e.g. COVER_MODEL) → BATCH_MODEL →
        the provider's default. Each may be a comma-separated failover chain.
        Raises a clear error (never a bare KeyError) when the provider is
        unknown/unconfigured.

    lead_provider_env: a provider chosen specifically for THIS caller (TAILOR_PROVIDER
        — evaluate on one provider, tailor on another). When set, the model resolves
        from explicit → lead_env → the provider's default ONLY; BATCH_MODEL is
        skipped, since it names a model for the EVAL provider and would be a
        wrong/invalid id on a different one (e.g. a Gemini model id sent to Anthropic)."""
    dedicated = (os.environ.get(lead_provider_env, "").strip() if lead_provider_env else "")
    provider = (dedicated or provider or _detect_provider() or "").strip()
    if not provider:
        raise RuntimeError(
            "no LLM provider configured — set a provider key (GEMINI_API_KEY, "
            "DEEPINFRA_API_KEY, ...) or BATCH_PROVIDER in .env"
        )
    lead = os.environ.get(lead_env) if lead_env else None
    chain = ([model, lead, PROVIDER_DEFAULTS.get(provider)] if dedicated else
             [model, lead, os.environ.get("BATCH_MODEL"),
              PROVIDER_DEFAULTS.get(provider)])
    model = next((m for m in chain if m), None)
    if not model:
        raise RuntimeError(f"unknown LLM provider '{provider}' — no default model; "
                           "set BATCH_MODEL or use a known provider")
    return _build_caller(provider, model, disable_thinking=disable_thinking)


# ── Worker ───────────────────────────────────────────────────────────────────

def _process_one(
    job_meta: dict,
    system_prompt: str,
    caller: Caller,
    reports_dir: Path,
    tracker_dir: Path,
    today: str,
    state: dict,
    state_lock: threading.Lock,
    *,
    max_attempts: int = 6,
    budget: float | None = None,
) -> tuple[bool, str, str | None]:
    """Evaluate one job. Returns (success, job_id, error_or_None)."""
    jid = job_meta["id"]
    try:
        response = _call_with_retry(
            caller, system_prompt, build_user_message(job_meta, today),
            max_attempts=max_attempts, budget=budget,
        )
        out = write_job_result(response, job_meta, reports_dir, tracker_dir, today)

        with state_lock:
            state["jobs"][jid]["status"] = "completed"
            state["jobs"][jid]["report"] = f"reports/{out['report_file']}" if out["report_file"] else None
            if out["summary"].get("score") is not None:
                state["jobs"][jid]["score"] = out["summary"]["score"]

        return True, jid, None

    except Exception as exc:
        with state_lock:
            state["jobs"][jid]["status"] = "failed"
            state["jobs"][jid]["error"] = str(exc)
        return False, jid, str(exc)


# ── Main entry point ─────────────────────────────────────────────────────────

def _eval_lock_max_age() -> float:
    """How long a lock may go un-heartbeated before another run may reclaim it
    (EVAL_LOCK_MAX_AGE env, default 30 min). The holder refreshes its timestamp
    after every job, so a genuine live run never approaches this — it's the
    pid-reuse safety valve, not a run-length cap."""
    return env_float("EVAL_LOCK_MAX_AGE", 1800.0)


def run(
    career_ops: Path,
    provider: str | None = None,
    model: str | None = None,
    concurrency: int = 3,
    dry_run: bool = False,
) -> int:
    """Evaluate pending jobs synchronously. Returns number of jobs processed.

    Single-flight across processes: two concurrent evaluations (e.g. a
    forgotten background run plus a fresh one — a live incident) contend on the
    same state file and double-hit the provider. The shared atomic pid+timestamp
    lock refuses the second run loudly; a lock whose pid is dead (crash, os._exit
    on Ctrl-C) or whose heartbeat went stale is reclaimed automatically."""
    lock_path = Path(career_ops) / "batch" / ".eval-lock"
    if not acquire_process_lock(lock_path, max_age=_eval_lock_max_age()):
        pid, _ = read_process_lock(lock_path)
        print(f"[batch-eval] REFUSED: another evaluation (pid {pid}) is already "
              "running — wait for it to finish or kill it first (concurrent runs "
              "corrupt shared state). Nothing was evaluated this run.",
              file=sys.stderr)
        return 0
    try:
        return _run_eval(career_ops, provider, model, concurrency, dry_run)
    finally:
        # Releases only if we still own it (release_process_lock guards against
        # deleting a lock a stale-takeover handed to another process).
        release_process_lock(lock_path)


def _run_eval(
    career_ops: Path,
    provider: str | None = None,
    model: str | None = None,
    concurrency: int = 3,
    dry_run: bool = False,
) -> int:
    provider = (provider or _detect_provider() or "").strip().lower()
    if not provider:
        print(
            "error: no LLM provider configured. Set BATCH_PROVIDER or one of: "
            "GEMINI_API_KEY, GROQ_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, "
            "or OLLAMA_BASE_URL with BATCH_PROVIDER=ollama",
            file=sys.stderr,
        )
        return 0

    if provider not in PROVIDER_DEFAULTS:
        print(f"error: unknown provider {provider!r}. Choose: {', '.join(PROVIDER_DEFAULTS)}", file=sys.stderr)
        return 0

    # Note: `os.environ.get("BATCH_MODEL", DEFAULT)` would NOT fall back to
    # DEFAULT when the env var is set to an empty string — and that's exactly
    # what the GHA workflow does when `vars.BATCH_MODEL` isn't configured
    # (`BATCH_MODEL: ${{ vars.BATCH_MODEL || '' }}` injects ""). The empty
    # string then propagated to the Gemini SDK and produced
    # `GenerateContentRequest.model: unexpected model name format`. Treat
    # both unset and empty-string as "use the per-provider default".
    model = model or os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider]
    today = datetime.now().strftime("%Y-%m-%d")

    batch_input = career_ops / "batch" / "batch-input.tsv"
    state_path = career_ops / "batch" / "batch-api-state.json"
    reports_dir = career_ops / "reports"
    tracker_dir = career_ops / "batch" / "tracker-additions"
    applications_md = career_ops / "data" / "applications.md"

    if not batch_input.exists():
        print("[batch-eval] no batch-input.tsv found — nothing to evaluate")
        return 0

    state = load_state(state_path)
    pending = load_pending(batch_input, state)

    if not pending:
        print("[batch-eval] all jobs already evaluated — nothing to do")
        # A previous run may have been interrupted (Ctrl-C os._exit / UI cancel)
        # after writing reports + tracker TSVs but BEFORE merging. With zero
        # pending we'd otherwise skip the merge forever, leaving those
        # evaluations invisible in applications.md until some future run happens
        # to process ≥1 new job. Heal it here — merge-tracker moves merged TSVs
        # out, so this is a no-op once they're folded in.
        if not dry_run and has_pending_tracker_additions(tracker_dir):
            run_merge_tracker(career_ops)
        return 0

    # ThreadPoolExecutor(max_workers=0) raises ValueError; a typo'd
    # BATCH_CONCURRENCY ("0", "0.9"→0) shouldn't traceback the eval stage after
    # scrape/filter/screen/bridge already did the expensive work.
    concurrency = max(1, int(concurrency))

    # Gemini free tier: when conforming (GEMINI_FREE_TIER), cap the run to the
    # model's daily RPD and defer the rest to the next run; otherwise just warn.
    # (Per-minute RPM *and* per-minute TPM pacing happen inside the caller — see
    # _build_caller.)
    #
    # The warning comes first and prints whether or not the cap fires, because
    # the two lines answer different questions: the warning is what the model can
    # get through in a day (which its RPD overstates whenever TPM binds), the cap
    # line is what this run will attempt. Under the old if/else the capped case —
    # the biggest queue, the one most in need of the number — was the one case
    # that never heard it.
    total = len(pending)
    warning = gemini_limits.format_free_tier_warning(model, total)
    if warning:
        print(warning, file=sys.stderr)
    pending, deferred = gemini_limits.cap_to_rpd(pending, model)
    if deferred:
        print(f"[batch-eval] free-tier cap: evaluating {len(pending)} of {total} today; "
              f"{deferred} deferred to the next run.", file=sys.stderr)

    # The "conforming is on but this model's limits are unknown" warning is not
    # printed here — _build_caller emits it, so every caller-building path gets
    # it rather than just this stage.

    print(f"[batch-eval] {len(pending)} job(s) | provider={provider} | model={model} | workers={concurrency}")

    if dry_run:
        for row in pending[:5]:
            print(f"  [{row['id']}] {row.get('source') or '?'} / {row.get('notes') or '?'}")
        if len(pending) > 5:
            print(f"  ... and {len(pending) - 5} more")
        return len(pending)

    err = _check_provider(provider)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 0

    cv = read_text(career_ops / "cv.md")
    if not cv:
        print("error: career-ops/cv.md not found — cannot evaluate without a CV", file=sys.stderr)
        return 0

    # cv.md above is still the "profile configured" sentinel (a PROFILE.md-only
    # setup is a later-commit concern). eval_system_prompt is the shared builder:
    # it uses the living PROFILE.md when present, else the seed files — the same
    # resolution the UI add-job path uses, so the two never diverge.
    system_prompt = eval_system_prompt(career_ops)

    report_counter = max_report_num(reports_dir, state)
    tracker_counter = max_tracker_num(applications_md, state)

    reports_dir.mkdir(parents=True, exist_ok=True)
    tracker_dir.mkdir(parents=True, exist_ok=True)

    jobs = assign_job_numbers(pending, state, report_counter, tracker_counter, career_ops)

    state["provider"] = provider
    state["model"] = model
    state["status"] = "in_progress"

    caller = _build_caller(provider, model)
    # A configured failover chain already tries each sibling within one call, so
    # it needs far fewer whole-chain retries than a single model (which has no
    # sibling to fall over to). Bounds the worst case alongside the per-job budget.
    attempts = 3 if len(_split_models(model)) > 1 else 6
    job_budget = _llm_job_budget()

    state_lock = threading.Lock()
    processed = failed = 0
    interrupted = {"flag": False}

    def _save_and_exit(*_a) -> None:
        # Re-entrant: a SECOND Ctrl-C while we're still saving goes straight to a
        # hard exit, instead of falling back into the blocking thread-join that
        # froze the old code. State was persisted after every finished job and
        # unfinished jobs stay pending, so a hard exit loses nothing.
        if interrupted["flag"]:
            os._exit(130)
        interrupted["flag"] = True
        with state_lock:
            atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
        print(f"\n[batch-eval] interrupted — {processed} processed, {failed} failed; "
              "remaining jobs stay pending for the next run.")
        # os._exit skips stdio flushing and atexit entirely, so this is the one
        # place a print needs an explicit push: line_buffer_stdout() swallows
        # its own failure by design, and if the reconfigure didn't take (a
        # supervisor or embedding host replacing sys.stdout) the buffered
        # summary would simply be discarded. Every other retired flush=True
        # degraded to "shows up later"; this one degraded to "never".
        sys.stdout.flush()
        os._exit(130)

    # Install a SIGINT handler so a second Ctrl-C re-enters cleanly. Only works
    # from the main thread; if we're not on it (ValueError), the timed-wait loop
    # below still catches KeyboardInterrupt.
    try:
        old_sigint = signal.signal(signal.SIGINT, _save_and_exit)
    except (ValueError, OSError):
        old_sigint = None

    lock_path = career_ops / "batch" / ".eval-lock"
    # Heartbeat the lock often enough that even a single slow job (up to the
    # per-job budget) can't let it look stale to a would-be reclaimer. Throttle
    # the state flush: the in-memory state is updated per job under state_lock,
    # but re-serializing the whole growing dict on EVERY job is O(N^2); flushing
    # at most every FLUSH_S keeps it O(N). The interrupt handler and the final
    # write below always persist the current state, so a clean exit never loses
    # a result; only a hard-kill (SIGKILL/power loss) re-runs the <FLUSH_S of
    # jobs completed since the last flush (numbering stays correct — it's scanned
    # from the reports dir, not just state).
    HEARTBEAT_S, FLUSH_S = 5.0, 2.0
    last_heartbeat = last_flush = time.monotonic()

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(
                    _process_one,
                    meta, system_prompt, caller,
                    reports_dir, tracker_dir, today,
                    state, state_lock,
                    max_attempts=attempts, budget=job_budget,
                ): meta
                for meta in jobs
            }
            remaining = set(futures)
            while remaining:
                # TIMED wait, not an unbounded as_completed(): the latter sits in
                # a lock.acquire that CPython only makes signal-interruptible on
                # POSIX, so on Windows a Ctrl-C is deferred until the next future
                # resolves (up to a whole per-job budget). A short timed wait
                # returns to Python bytecode every 0.5s, where the pending signal
                # (or KeyboardInterrupt) is delivered promptly.
                try:
                    done, remaining = wait(remaining, timeout=0.5, return_when=FIRST_COMPLETED)
                except KeyboardInterrupt:
                    _save_and_exit()
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_S:
                    refresh_process_lock(lock_path)
                    last_heartbeat = now
                for future in done:
                    meta = futures[future]
                    success, jid, err_msg = future.result()
                    if success:
                        report = state["jobs"][jid].get("report", "")
                        score = state["jobs"][jid].get("score", "?")
                        print(f"  [{jid}] {meta['company'] or '?'} -> score={score} {report or '(no report)'}")
                        processed += 1
                    else:
                        print(f"  [{jid}] FAILED: {err_msg}")
                        failed += 1
                if done and now - last_flush >= FLUSH_S:
                    # Atomic snapshot+write under the lock so concurrent writers
                    # can't interleave or leave a partially-written file.
                    with state_lock:
                        atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
                    last_flush = now
    finally:
        if old_sigint is not None:
            try:
                signal.signal(signal.SIGINT, old_sigint)
            except (ValueError, OSError):
                pass

    state["status"] = "completed"
    state["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
    print(f"[batch-eval] done — processed={processed} failed={failed}")

    # Merge when we produced results OR when unmerged TSVs are sitting around
    # from an earlier interrupted run. merge-tracker moves merged files out, so
    # a clean run won't re-merge endlessly.
    if processed > 0 or has_pending_tracker_additions(tracker_dir):
        run_merge_tracker(career_ops)

    return processed


def _parse_argv(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Evaluate pending jobs via a synchronous LLM provider.")
    ap.add_argument("--provider", default=None, help="anthropic|gemini|openai|groq|ollama")
    ap.add_argument("--model", default=None, help="overrides BATCH_MODEL env var")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args(argv)


if __name__ == "__main__":
    line_buffer_stdout()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    args = _parse_argv(sys.argv[1:])
    career_ops_path = Path(os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")).resolve()
    sys.exit(
        0 if run(career_ops_path, args.provider, args.model, args.concurrency, args.dry_run) >= 0 else 1
    )
