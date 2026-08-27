"""Gemini rate limits — the user's own numbers first, a baked table as fallback.

There's no Gemini API to fetch a project's rate limits. Google's rate-limits doc
no longer prints per-model numbers at all; it defers to AI Studio's
`/rate-limit` page, which is login-gated HTML. `models.list` returns the model
IDs but nothing about quota. So the numbers cannot be fetched, only the IDs —
and `python -m pipeline.gemini_limits --check-models` diffs those against the
table so a retired or added ID is a report rather than a surprise.

That splits the file in two, and the halves rot differently:

- `GEMINI_FREE_TIER_LIMITS` is a hardcoded snapshot of AI Studio's free-tier
  page. It is wrong twice over the moment Google moves: stale for the free
  tier, and never right for a PAID tier, where the same model IDs carry
  completely different numbers.
- The override file (`config/gemini-limits.json`, `GEMINI_LIMITS_FILE`) is the
  user's own AI Studio numbers, which are authoritative for their project by
  construction — anyone who can mint an API key can read `/rate-limit` for the
  project behind it. It wins per model, per key.

The override is the answer to a failure that is worse than a wrong number.
Conforming (`GEMINI_FREE_TIER=true`) is keyed on membership in the table, so a
model Google added after the snapshot got no pacing, no cap AND no warning —
`paced_caller` returned the caller unchanged and `cap_to_rpd` returned the list
unchanged. Ticking "I'm on the free tier" bought silence, not conformance. So
an unknown model is now something the run SAYS (`format_unconformable_warning`),
pointing at the file that fixes it.

The binding constraint for batch evaluation is RPD (requests per day ≈ jobs per
day); RPM only paces within a run.
"""

import json
import os
import threading
import time
from pathlib import Path

from pipeline.stdio import line_buffer_stdout

# model_id → {rpm, tpm, rpd}. tpm = None means "unlimited" on the free tier.
# Source: AI Studio → Rate limits by model (free tier). The FALLBACK, not the
# source of truth — there is no programmatic way to fetch these, so a user's
# override file (below) beats it per model. Run `--check-models` to find IDs
# Google has retired or added since this was written.
# Refreshed 2026-08-27 from a free-tier AI Studio /rate-limit page. Only models
# that can evaluate a job are listed: text-out plus the Gemma pair. Deliberately
# omitted are the text-out models the free tier grants NO quota at all (0/0/0 —
# gemini-2-flash, gemini-2-flash-lite, gemini-2.5-pro, gemini-3.1-pro), and the
# TTS/embedding/image/robotics models, which share the namespace but can't score
# a role.
#
# The row IDs marked (id?) below are inferred from AI Studio's DISPLAY names,
# which is all that page shows — run `--check-models` with a key to confirm them
# against models.list before trusting a `retired` verdict on one.
GEMINI_FREE_TIER_LIMITS: dict[str, dict] = {
    "gemini-2.5-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20},
    "gemini-2.5-flash-lite":  {"rpm": 10, "tpm": 250_000, "rpd": 20},
    "gemini-3-flash":         {"rpm": 5,  "tpm": 250_000, "rpd": 20},     # (id?)
    "gemini-3.1-flash-lite":  {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini-3.5-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20},
    "gemini-3.5-flash-lite":  {"rpm": 15, "tpm": 250_000, "rpd": 500},    # (id?)
    "gemini-3.6-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20},     # (id?)
    "gemini-3.7-flash":       {"rpm": 5,  "tpm": 250_000, "rpd": 20},     # (id?)
    # The Gemma pair was wrong in all three dimensions before this refresh
    # (15 / unlimited / 1500). TPM is the correction that bites: 16K is not
    # "unlimited", and at a realistic ~8K-token evaluation prompt it caps you
    # near 2 requests/minute — far below the 30 RPM the same row grants. Nothing
    # here paces on TPM yet, so the high RPD is not reachable in practice.
    "gemma-4-26b-a4b-it":     {"rpm": 30, "tpm": 16_000,  "rpd": 14_400},
    "gemma-4-31b-it":         {"rpm": 30, "tpm": 16_000,  "rpd": 14_400},
}

# The highest free-tier RPD model — what we steer batch users toward.
BATCH_RECOMMENDATION = "gemma-4-26b-a4b-it"

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OVERRIDE_FILE = "config/gemini-limits.json"

# Cached on (path, mtime) like the career-ops contract loader — but for
# freshness, not speed. Every reader here is called a small constant number of
# times per run (the caller is built ONCE before the job loop and shared by all
# pool workers — see _build_caller and handoff's _make_tailor_fn), so the cache
# buys microseconds. What it buys that matters: a limits file edited in the UI
# takes effect on the next read without restarting the server.
_override_cache: dict[str, tuple[int, dict]] = {}


def override_path() -> Path:
    """Where the user's own limits live. GEMINI_LIMITS_FILE, else
    config/gemini-limits.json under the repo root.

    A RELATIVE GEMINI_LIMITS_FILE resolves against the repo root, not the
    process CWD — the same rule career_ops_dir() uses, and for the same reason:
    run-ui.sh never cds, so a CWD-relative read would silently find nothing and
    fall back to the baked table, which is the exact failure this file exists
    to end."""
    raw = os.environ.get("GEMINI_LIMITS_FILE") or _DEFAULT_OVERRIDE_FILE
    p = Path(raw)
    return p if p.is_absolute() else (_ROOT / p).resolve()


def _positive_int(v: object) -> bool:
    """A usable limit: a positive int. `bool` is an int subclass, so it is
    excluded explicitly — otherwise `true` in the JSON would read as rpd=1 and
    cap a run to a single job."""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _clean_entry(model: str, raw: object) -> dict | None:
    """Validate one override row against the baked entry as its base, so a
    partial override ({"rpd": 1000}) keeps the rpm/tpm it didn't mention.

    Returns None to decline the row. rpm and rpd must end up present and
    numeric, because they are what pacing and capping read; tpm is advisory
    here and may be null ("unlimited")."""
    if not isinstance(raw, dict):
        return None
    merged = {**GEMINI_FREE_TIER_LIMITS.get(model, {}), **raw}
    out = {}
    for key in ("rpm", "rpd"):
        if not _positive_int(merged.get(key)):
            return None
        out[key] = merged[key]
    # tpm alone may be null, which means "unlimited" — the shape the baked
    # Gemma rows already use.
    tpm = merged.get("tpm")
    if tpm is not None and not _positive_int(tpm):
        return None
    out["tpm"] = tpm
    return out


def parse_overrides(text: str) -> dict | None:
    """Parse the override file: a flat {model_id: {rpm, tpm, rpd}} map, the same
    shape as the baked table so the two can be read side by side.

    Declines (None) a file that isn't a JSON object; skips individual rows it
    can't read rather than dropping the whole file, so one bad row doesn't cost
    the user the others. Shared with the UI's save endpoint, so what the wizard
    accepts and what a run reads cannot drift."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for model, raw in data.items():
        if not isinstance(model, str):
            continue
        entry = _clean_entry(model, raw)
        if entry is not None:
            out[model] = entry
    return out


def _load_overrides() -> dict:
    """The user's limits file, cached until its mtime changes. Never raises: an
    absent, malformed or unreadable file degrades to "no overrides", never takes
    a run down."""
    key = str(override_path())
    try:
        mtime = os.stat(key).st_mtime_ns
    except OSError:
        return {}
    entry = _override_cache.get(key)
    if entry is not None and entry[0] == mtime:
        return entry[1]
    try:
        with open(key, encoding="utf-8") as fh:
            value = parse_overrides(fh.read())
    except Exception:
        value = None
    if value is None:
        value = {}
    _override_cache[key] = (mtime, value)
    return value


def user_limits() -> dict[str, dict]:
    """Just the user's own rows, no baked entries. The UI needs these separately
    to show which numbers came from the user (and to prefill only those)."""
    return dict(_load_overrides())


def effective_limits() -> dict[str, dict]:
    """The baked free-tier table overlaid with the user's own numbers.

    Merged per MODEL rather than wholesale: a user who read one model's row off
    AI Studio shouldn't lose the table's other entries to do it."""
    return {**GEMINI_FREE_TIER_LIMITS, **_load_overrides()}


def save_user_limits(rows: dict) -> dict:
    """Write the user's limits file and return what was stored.

    Validates through parse_overrides — the same path a run reads by — so the UI
    cannot persist a row the loader would silently decline, which would look
    saved and do nothing. Raises ValueError on a row that doesn't validate, so
    the caller can 400 rather than write junk.

    A row mapping to None deletes it, and an empty result removes the file
    entirely rather than leaving `{}` behind, so "clear my overrides" returns to
    the baked table exactly as if the file had never existed."""
    # Validate through _clean_entry directly rather than round-tripping the dict
    # through json.dumps/parse_overrides: same validator, one pass, and it yields
    # the rejected keys instead of recovering them from a set difference.
    cleaned, bad = {}, []
    for model, raw in rows.items():
        if raw is None:
            continue                                  # explicit delete
        entry = _clean_entry(model, raw)
        if entry is None:
            bad.append(model)
        else:
            cleaned[model] = entry
    if bad:
        raise ValueError(
            f"Invalid limits for {', '.join(sorted(bad))}: rpm and rpd must be "
            f"positive integers; tpm must be a positive integer or null (unlimited)."
        )
    path = override_path()
    _override_cache.pop(str(path), None)
    if not cleaned:
        path.unlink(missing_ok=True)
        return {}
    # Atomic: _load_overrides mtime-caches this file, so a reader landing
    # mid-write would parse a truncated body, decline it, and silently fall back
    # to the baked table. Imported here rather than at module scope to keep the
    # common import path (which the UI venv takes) off _batch_common.
    from pipeline._batch_common import atomic_write_text

    atomic_write_text(path, json.dumps(cleaned, indent=2, sort_keys=True) + "\n")
    return cleaned


def _spec_models(model: str) -> list[str]:
    """Split a model spec into members. A comma-separated `BATCH_MODEL` is a
    failover chain (tried in order); a single value is a one-member chain."""
    return [m.strip() for m in (model or "").split(",") if m.strip()]


def _spec_rpd(model: str) -> int | None:
    """Summed RPD across the spec's known members. A failover chain's daily
    capacity is the SUM — when one member is exhausted (429) the call falls over
    to the next — so a `flash,gemma` chain does ~20 then ~1,500 = 1,520/day.
    None if no member has known limits (paid / non-Gemini / unknown).

    Reads effective_limits(), so a user's own AI Studio numbers set the cap when
    they've supplied them."""
    limits = effective_limits()
    caps = [limits[m]["rpd"] for m in _spec_models(model) if m in limits]
    return sum(caps) if caps else None


def unknown_members(model: str) -> list[str]:
    """Spec members with no known limits, in order. The input to "conforming is
    on but can't act" — see format_unconformable_warning."""
    limits = effective_limits()
    return [m for m in _spec_models(model) if m not in limits]


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
        # Read the suggestion's cap rather than restating it: this line claimed a
        # flat "1,500/day" while the number it came from sat two screens up, and
        # a user override can change it per project.
        cap = effective_limits().get(v["suggestion"], {}).get("rpd")
        cap_note = f" ({cap:,}/day)" if cap else ""
        msg += f" Set BATCH_MODEL={v['suggestion']}{cap_note} for batch runs."
    return msg


def format_unconformable_warning(model: str) -> str | None:
    """Conforming is ON but the model has no known limits, so it cannot act.

    Without this the run is silent in the one case that most deserves noise:
    `paced_caller` hands back an unpaced caller and `cap_to_rpd` hands back an
    uncapped list, so ticking "I'm on the free tier" reads as protection while
    doing nothing. Google adding a model ID is enough to reach it. Names the
    file that fixes it, since the numbers can only come from the user."""
    if not conforming_enabled():
        return None
    unknown = unknown_members(model)
    if not unknown:
        return None
    return (f"[batch] ⚠ GEMINI_FREE_TIER is on but {', '.join(unknown)} has no "
            f"known rate limits — requests will NOT be paced or capped. Add its "
            f"RPM/TPM/RPD from aistudio.google.com/rate-limit to "
            f"{override_path()} (or via the UI's Setup → local model settings).")


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
    limits = effective_limits()
    if not conforming_enabled() or model not in limits:
        return caller
    limiter = RateLimiter(limits[model]["rpm"])

    def wrapped(*args, **kwargs):
        limiter.acquire()
        return caller(*args, **kwargs)

    return wrapped


# ── models.list refresh ──────────────────────────────────────────────────────
# The IDs are fetchable even though the numbers aren't, so the half of the table
# that CAN be checked automatically is. A retired ID is the expensive kind of
# stale: `BATCH_MODEL` naming it 404s the run, and until the table is corrected
# the conforming path also treats it as unknown.

_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _urllib_get(url: str, *, headers: dict, params: dict) -> dict:
    """The default getter: stdlib only, so this module stays importable from the
    jobspy-free UI venv without pulling in `requests`."""
    import urllib.parse
    import urllib.request

    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=headers)
    with urllib.request.urlopen(req, timeout=20.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_model_ids(api_key: str, *, get=None, generate_content_only: bool = True) -> list[str]:
    """Live model IDs from models.list, bare (no `models/` prefix), sorted.

    The key goes in the x-goog-api-key HEADER, never the URL query, so it can't
    leak through a request URL in an exception or a log line — `verify_models`
    learned that one already and `tests/test_verify_models.py` pins it.

    Pages through `nextPageToken`: one page does not return the whole catalogue,
    and a truncated list would report every model it missed as retired, which is
    worse than not checking.

    `get` is injectable (same signature as verify_models' `_http_get`) so that
    module can delegate here instead of keeping a second copy of Google's
    pagination contract, and so both are testable without network.
    `generate_content_only` filters to models that can actually evaluate a job —
    embedding and TTS models share the namespace. verify_models wants the whole
    catalogue, since it checks IDs a user may have configured for any purpose."""
    fetch = get or _urllib_get
    ids: list[str] = []
    token = ""
    while True:
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        payload = fetch(_MODELS_URL, headers={"x-goog-api-key": api_key}, params=params)
        for m in payload.get("models") or []:
            name = (m.get("name") or "").split("/")[-1]
            if not name:
                continue
            if generate_content_only and "generateContent" not in (
                m.get("supportedGenerationMethods") or []
            ):
                continue
            ids.append(name)
        token = payload.get("nextPageToken") or ""
        if not token:
            break
    return sorted(set(ids))


def diff_models(live_ids: list[str]) -> dict[str, list[str]]:
    """Compare the table against the live catalogue.

    `retired` is what the table names and Google no longer serves — the rows to
    fix. `unlisted` is everything live that the table doesn't cover, which is
    most of the catalogue by design (the table only carries text models used for
    evaluation), so it's reported as candidates, never as an error."""
    live = set(live_ids)
    table = set(effective_limits())
    return {
        "retired": sorted(table - live),
        "unlisted": sorted(live - table),
    }


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m pipeline.gemini_limits",
        description="Show the effective Gemini limits, and check the table's "
                    "model IDs against the live catalogue.",
    )
    ap.add_argument("--check-models", action="store_true",
                    help="fetch models.list and diff it against the table "
                         "(needs GEMINI_API_KEY)")
    ap.add_argument("--show", action="store_true",
                    help="print the effective limits (baked table + your overrides)")
    args = ap.parse_args(argv)
    if not (args.check_models or args.show):
        args.show = True

    if args.show:
        overrides = user_limits()
        path = override_path()
        print(f"[limits] overrides: {path}"
              f"{'' if path.exists() else '  (not present — using baked table only)'}")
        for model, v in sorted({**GEMINI_FREE_TIER_LIMITS, **overrides}.items()):
            src = "yours" if model in overrides else "baked"
            tpm = "unlimited" if v["tpm"] is None else f"{v['tpm']:,}"
            print(f"  {model:26} rpm={v['rpm']:<4} tpm={tpm:<10} rpd={v['rpd']:<6} [{src}]")

    if args.check_models:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            print("[limits] GEMINI_API_KEY is not set — cannot reach models.list.")
            return 2
        try:
            live = fetch_model_ids(key)
        except Exception as exc:                      # network, auth, bad payload
            print(f"[limits] models.list failed: {type(exc).__name__}: {exc}")
            return 2
        d = diff_models(live)
        print(f"[limits] {len(live)} generateContent models live")
        if d["retired"]:
            print("[limits] ⚠ in the table but NOT live (retired — fix these):")
            for m in d["retired"]:
                print(f"    {m}")
        else:
            print("[limits] every model in the table is still live")
        if d["unlisted"]:
            print("[limits] live but not in the table (candidates; add limits "
                  "from aistudio.google.com/rate-limit):")
            for m in d["unlisted"]:
                print(f"    {m}")
        # Rate limits are not in this payload and there is no endpoint that has
        # them, so this can never be more than an ID check. Say so, or the clean
        # run above reads as "the numbers are verified" — which it is not.
        print("[limits] note: models.list carries no rate limits — RPM/TPM/RPD "
              "still come from the table or your override file.")
    return 0


if __name__ == "__main__":                            # pragma: no cover
    line_buffer_stdout()

    import sys

    raise SystemExit(_main(sys.argv[1:]))
