"""Hardcoded Gemini free-tier rate limits + a batch-viability check.

There's no Gemini API to fetch a project's rate limits — Google only shows them
in AI Studio's "Rate limits by model" page — so the free-tier numbers are
hardcoded here from that page (verified against models.list for the IDs). Only
the text models usable for resume evaluation/tailoring are listed; the binding
constraint for batch evaluation is RPD (requests per day ≈ jobs per day).

Used to warn, before a run, when the configured Gemini model's free-tier daily
cap is below the number of pending jobs — so users can pick a viable model
(Gemma 4 gives 1,500/day vs the Flash models' 20/day) instead of silently
losing most evaluations to the daily quota. With GEMINI_FREE_TIER=true the LLM
stages actively conform (RPM pacing + RPD capping), not just warn.
"""

import os
import threading
import time

# model_id → {rpm, tpm, rpd}. tpm = None means "unlimited" on the free tier.
# Source: AI Studio → Rate limits by model (free tier). Update if Google changes
# them — there is no programmatic way to fetch these.
GEMINI_FREE_TIER_LIMITS: dict[str, dict] = {
    "gemini-2.5-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20},
    "gemini-2.5-flash-lite":  {"rpm": 10, "tpm": 250_000, "rpd": 20},
    "gemini-3.1-flash-lite":  {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini-3-flash-preview": {"rpm": 5,  "tpm": 250_000, "rpd": 20},
    "gemini-3.5-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20},
    "gemma-4-26b-a4b-it":     {"rpm": 15, "tpm": None,    "rpd": 1500},
    "gemma-4-31b-it":         {"rpm": 15, "tpm": None,    "rpd": 1500},
}

# The highest free-tier RPD model — what we steer batch users toward.
BATCH_RECOMMENDATION = "gemma-4-26b-a4b-it"


def _spec_models(model: str) -> list[str]:
    """Split a model spec into members. A comma-separated `BATCH_MODEL` is a
    failover chain (tried in order); a single value is a one-member chain."""
    return [m.strip() for m in (model or "").split(",") if m.strip()]


def _spec_rpd(model: str) -> int | None:
    """Summed free-tier RPD across the spec's known members. A failover chain's
    daily capacity is the SUM — when one member is exhausted (429) the call falls
    over to the next — so a `flash,gemma` chain does ~20 then ~1,500 = 1,520/day.
    None if no member is a known free-tier model (paid / non-Gemini / unknown)."""
    caps = [GEMINI_FREE_TIER_LIMITS[m]["rpd"] for m in _spec_models(model)
            if m in GEMINI_FREE_TIER_LIMITS]
    return sum(caps) if caps else None


def free_tier_viability(model: str, pending_jobs: int) -> dict | None:
    """Whether `pending_jobs` fits the model spec's free-tier daily capacity.

    Returns None for a spec with no known free-tier member (paid / non-Gemini /
    unknown). Otherwise {"rpd", "exceeds": pending > rpd[, "suggestion"]} —
    `suggestion` is the recommended higher-cap model, present only when the run
    exceeds the cap AND that model isn't already in the spec. Chain-aware: rpd is
    the sum across members (see _spec_rpd)."""
    rpd = _spec_rpd(model)
    if rpd is None:
        return None
    result = {"rpd": rpd, "exceeds": pending_jobs > rpd}
    if result["exceeds"] and BATCH_RECOMMENDATION not in _spec_models(model):
        result["suggestion"] = BATCH_RECOMMENDATION
    return result


def format_free_tier_warning(model: str, pending_jobs: int) -> str | None:
    """A one-line warning when the run will exceed the free-tier daily cap, else
    None. Callers print it before starting a batch run."""
    v = free_tier_viability(model, pending_jobs)
    if not v or not v["exceeds"]:
        return None
    msg = (f"[batch] ⚠ Gemini free tier on {model} allows ~{v['rpd']} "
           f"evaluations/day; you have {pending_jobs} pending — the rest will "
           f"hit the daily cap and fail.")
    if v.get("suggestion"):
        msg += f" Set BATCH_MODEL={v['suggestion']} (1,500/day) for batch runs."
    return msg


# ── Conforming mode ──────────────────────────────────────────────────────────
# When the user opts into "I'm on Gemini's free tier" (GEMINI_FREE_TIER=true),
# the LLM stages actively CONFORM to the limits rather than just warning: requests
# are paced to the model's RPM, and the eval run caps to the model's RPD (the rest
# is deferred to the next run). Both are no-ops for paid models / other providers.

def conforming_enabled() -> bool:
    """Whether the user opted into Gemini free-tier conforming (GEMINI_FREE_TIER)."""
    return os.environ.get("GEMINI_FREE_TIER", "").strip().lower() in ("1", "true", "yes", "on")


def rpd_cap(model: str) -> int | None:
    """The spec's free-tier daily request cap (summed across a failover chain)
    when conforming is on, else None."""
    if not conforming_enabled():
        return None
    return _spec_rpd(model)


def cap_to_rpd(items: list, model: str) -> tuple[list, int]:
    """Slice a job list to the model's free-tier daily cap. Returns (kept,
    deferred_count). No-op (returns the list unchanged) when not conforming or the
    model has no known cap."""
    cap = rpd_cap(model)
    if cap is None or len(items) <= cap:
        return items, 0
    return items[:cap], len(items) - cap


class RateLimiter:
    """Thread-safe min-interval pacer: spaces request starts by 60/rpm seconds so
    concurrent workers stay within the per-minute rate. `monotonic`/`sleep` are
    injectable for tests. rpm <= 0 means no pacing."""

    def __init__(self, rpm: int, *, monotonic=time.monotonic, sleep=time.sleep):
        self._interval = 60.0 / rpm if rpm > 0 else 0.0
        self._monotonic = monotonic
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
            wait = start - now
        if wait > 0:
            self._sleep(wait)


def paced_caller(caller, model: str):
    """Wrap an LLM caller so each call is paced to the model's free-tier RPM —
    only when conforming is on AND the model is in the free-tier table. Otherwise
    returns `caller` unchanged. One limiter is shared across the wrapper, so
    parallel eval workers sharing this caller share the pace."""
    if not conforming_enabled() or model not in GEMINI_FREE_TIER_LIMITS:
        return caller
    limiter = RateLimiter(GEMINI_FREE_TIER_LIMITS[model]["rpm"])

    def wrapped(*args, **kwargs):
        limiter.acquire()
        return caller(*args, **kwargs)

    return wrapped
