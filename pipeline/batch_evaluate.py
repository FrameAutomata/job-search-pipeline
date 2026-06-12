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
  ollama       qwen2.5:32b       (default)             OLLAMA_BASE_URL (default: http://localhost:11434)

Hosted open-model providers (deepinfra, openrouter) serve open-weight models
like Llama 3.3 70B and DeepSeek V3 over OpenAI-compatible APIs. See
QUICKSTART.md "Which provider should I pick?" for guidance on choosing
between providers based on use case.

Escape hatch: set OPENAI_BASE_URL to point the `openai` provider at any
OpenAI-compatible endpoint (e.g. local vLLM server, internal proxy, a
provider we haven't enumerated above). Use BATCH_PROVIDER=openai +
OPENAI_API_KEY=<their-key> + OPENAI_BASE_URL=<their-url>.

Provider auto-detection: BATCH_PROVIDER env var, then first key found in the
order: gemini → groq → deepinfra → openrouter → openai → anthropic.

Requirements (install only the provider you need):
  pip install anthropic                  # anthropic
  pip install google-genai               # gemini (NOT the deprecated google-generativeai)
  pip install openai                     # openai / groq / deepinfra / openrouter / ollama
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pipeline._batch_common import (
    MAX_TOKENS,
    assign_job_numbers,
    atomic_write_text,
    build_system_prompt,
    build_user_message,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    read_text,
    run_merge_tracker,
    write_job_result,
)

ROOT = Path(__file__).resolve().parent.parent

PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    # gemini-2.5-flash, NOT gemini-2.0-flash: the 2.0 model was deprecated by
    # Google (shutdown 2026-06-01) and its free-tier quota collapsed months
    # ahead of that. 2.5-flash is supported and won't fail with a deprecation
    # error — but its free-tier RPD is only ~20/day, so users running >20
    # evaluations per day should override BATCH_MODEL to one of the
    # higher-RPD options (gemma-4-26b-it has 1.5K RPD + unlimited TPM,
    # gemini-3.1-flash-lite has 500 RPD). See .env.example.
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    # Hosted open-weight providers. The model ID conventions differ across
    # providers (capital vs lowercase namespaces), so these aren't typos.
    # Override via BATCH_MODEL.
    "deepinfra": "deepseek-ai/DeepSeek-V4-Flash",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "ollama": "qwen2.5:32b",
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
_TRANSIENT_SERVER_MARKERS = (
    "inference error",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "overloaded",
    "timed out",
    "timeout",
    "connection error",
    # Our own callers raise "<provider> returned empty content" when a model
    # answers 200 with an empty body — a degraded endpoint, or a reasoning
    # model that burned its whole token budget thinking. Either way the model
    # is unusable for this call: retry, and above all FAIL OVER to a sibling.
    "empty content",
)


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
    raw = (os.environ.get("LLM_TIMEOUT") or "").strip()
    try:
        return float(raw) if raw else 180.0
    except ValueError:
        return 180.0


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Rate limits PLUS server-side failures (5xx, timeouts, 'inference
    error'): everything where a retry or a failover to a sibling model makes
    sense. Caller errors (401 auth, 404 bad model id) stay non-transient so a
    misconfiguration still fails loudly instead of burning the whole chain."""
    if _is_rate_limit_error(exc):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    try:
        if status is not None and 500 <= int(status) <= 599:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_SERVER_MARKERS)


def _call_with_retry(
    caller: Caller,
    system: str,
    user: str,
    *,
    max_attempts: int = 6,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Invoke `caller(system, user)` with exponential backoff on transient
    provider errors (rate limits, 5xx/'inference error', timeouts). Caller
    errors (auth, bad model id) raise immediately — no retry.

    Backoff: 1, 2, 4, 8, 16 seconds, each with up to 0.5s of random jitter
    to avoid thundering-herd when multiple workers throttle simultaneously.
    With max_attempts=6 the total worst-case wait before giving up is ~31s
    plus 5 calls — comfortably under any reasonable per-job timeout.

    `sleep` is parameterized so tests don't have to actually wait."""
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return caller(system, user)
        except Exception as exc:
            if not _is_transient_provider_error(exc) or attempt == max_attempts:
                raise
            jittered = delay + random.uniform(0, 0.5)
            print(
                f"  provider busy (attempt {attempt}/{max_attempts}): "
                f"sleeping {jittered:.1f}s — {exc}",
                flush=True,
            )
            sleep(jittered)
            delay *= 2
    # Unreachable: the for-loop above either returns or raises on the last attempt.
    raise RuntimeError("retry loop exited without return or raise")  # pragma: no cover


def _build_anthropic_caller(model: str) -> Caller:
    import anthropic as _a
    client = _a.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

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
                          f"failing over to {built[i + 1][0]}", flush=True)
        raise last_exc if last_exc else RuntimeError("no models configured")

    return call


def _build_caller(provider: str, model: str, *, disable_thinking: bool = False) -> Caller:
    """Build the LLM caller. `model` may be a comma-separated failover chain
    (tried in order on overload). `disable_thinking` is honored per the
    single-model builder."""
    models = _split_models(model)
    if len(models) > 1:
        return _build_failover_caller(provider, models, disable_thinking=disable_thinking)
    return _build_single_caller(provider, models[0] if models else model,
                                disable_thinking=disable_thinking)


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
    if provider == "groq":
        return _build_openai_compat_caller(
            model, api_key=os.environ["GROQ_API_KEY"],
            base_url="https://api.groq.com/openai/v1",
        )
    if provider == "deepinfra":
        return _build_openai_compat_caller(
            model, api_key=os.environ["DEEPINFRA_API_KEY"],
            base_url="https://api.deepinfra.com/v1/openai", disable_thinking=toggle,
        )
    if provider == "openrouter":
        return _build_openai_compat_caller(
            model, api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1", disable_thinking=toggle,
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
                   lead_env: str | None = None, disable_thinking: bool = False):
    """Build an LLM caller, resolving provider and model from one place (the apply
    answers, cover letters, and the apply stage all funnel through here instead of
    each re-implementing the precedence chain).

    provider: explicit, else BATCH_PROVIDER / first provider key found.
    model: explicit → `lead_env` override (e.g. COVER_MODEL) → APPLY_MODEL →
        BATCH_MODEL → the provider's default. Each may be a comma-separated
        failover chain. Raises a clear error (never a bare KeyError) when the
        provider is unknown/unconfigured."""
    provider = (provider or _detect_provider() or "").strip()
    if not provider:
        raise RuntimeError(
            "no LLM provider configured — set a provider key (GEMINI_API_KEY, "
            "DEEPINFRA_API_KEY, ...) or BATCH_PROVIDER in .env"
        )
    chain = [model, os.environ.get(lead_env) if lead_env else None,
             os.environ.get("APPLY_MODEL"), os.environ.get("BATCH_MODEL"),
             PROVIDER_DEFAULTS.get(provider)]
    model = next((m for m in chain if m), None)
    if not model:
        raise RuntimeError(f"unknown LLM provider '{provider}' — no default model; "
                           "set APPLY_MODEL/BATCH_MODEL or use a known provider")
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
) -> tuple[bool, str, str | None]:
    """Evaluate one job. Returns (success, job_id, error_or_None)."""
    jid = job_meta["id"]
    try:
        response = _call_with_retry(caller, system_prompt, build_user_message(job_meta, today))
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

def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for the eval lock (cross-platform; on
    Windows os.kill(pid, 0) would SEND CTRL_C_EVENT, so use OpenProcess)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            # OpenProcess succeeds on a zombie while any handle to it is held —
            # only an exit code of STILL_ACTIVE means actually running.
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run(
    career_ops: Path,
    provider: str | None = None,
    model: str | None = None,
    concurrency: int = 3,
    dry_run: bool = False,
) -> int:
    """Evaluate pending jobs synchronously. Returns number of jobs processed.

    Single-flight across processes: two concurrent evaluations (e.g. a
    forgotten background run plus a fresh one — a live incident) contend on
    the same state file and double-hit the provider. A pid lock file refuses
    the second run; a lock whose pid is dead (crash, os._exit on Ctrl-C) is
    taken over."""
    lock_path = Path(career_ops) / "batch" / ".eval-lock"
    try:
        holder = int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        holder = 0
    if holder and holder != os.getpid() and _pid_alive(holder):
        print(f"[batch-eval] another evaluation (pid {holder}) is already running — "
              "wait for it to finish or kill it first (concurrent runs corrupt "
              "shared state)", file=sys.stderr)
        return 0
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _run_eval(career_ops, provider, model, concurrency, dry_run)
    finally:
        lock_path.unlink(missing_ok=True)


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
        return 0

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

    system_prompt = build_system_prompt(
        cv,
        read_text(career_ops / "config" / "profile.yml"),
        read_text(career_ops / "modes" / "_profile.md"),
        read_text(career_ops / "article-digest.md"),
    )

    report_counter = max_report_num(reports_dir, state)
    tracker_counter = max_tracker_num(applications_md, state)

    reports_dir.mkdir(parents=True, exist_ok=True)
    tracker_dir.mkdir(parents=True, exist_ok=True)

    jobs = assign_job_numbers(pending, state, report_counter, tracker_counter, career_ops)

    state["provider"] = provider
    state["model"] = model
    state["status"] = "in_progress"

    caller = _build_caller(provider, model)

    state_lock = threading.Lock()
    processed = failed = 0

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _process_one,
                meta, system_prompt, caller,
                reports_dir, tracker_dir, today,
                state, state_lock,
            ): meta
            for meta in jobs
        }
        try:
            for future in as_completed(futures):
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

                # Atomic snapshot+write under the lock so concurrent writers can't
                # interleave or leave a partially-written file.
                with state_lock:
                    snapshot = json.dumps(state, indent=2, ensure_ascii=False)
                    atomic_write_text(state_path, snapshot)
        except KeyboardInterrupt:
            # Worker threads may sit in long HTTP waits; they're non-daemon, so
            # a normal exit would block on them — the exact "Ctrl-C does
            # nothing" hang this handles. State was saved after every finished
            # job and unfinished jobs stay pending, so a hard exit is safe.
            with state_lock:
                atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
            print(f"\n[batch-eval] interrupted — {processed} processed, {failed} failed; "
                  "remaining jobs stay pending for the next run.", flush=True)
            os._exit(130)

    state["status"] = "completed"
    state["completed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
    print(f"[batch-eval] done — processed={processed} failed={failed}")

    if processed > 0:
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
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    args = _parse_argv(sys.argv[1:])
    career_ops_path = Path(os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")).resolve()
    sys.exit(
        0 if run(career_ops_path, args.provider, args.model, args.concurrency, args.dry_run) >= 0 else 1
    )
