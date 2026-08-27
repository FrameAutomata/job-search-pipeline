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

All three published limits are conformed to, and they bind in different places.
RPD is a quota — exceed it and the day's remaining requests are refused — so
`cap_to_rpd` slices the run to it. RPM and TPM are both rates, so both pace
within a run (`paced_caller`), and the LOWER of the two is what a run actually
achieves: a model granting 30 RPM against 16,000 TPM cannot start 30 evaluations
in a minute when each one costs ~8,000 tokens. That gap is why an honest daily
capacity is the lowest of all three limits put in one unit — `_capacity_terms`
spends each rate over a day's 1,440 minutes — rather than the RPD alone. That is
what the warning quotes and what `batch_recommendation` ranks by.
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
    # near 2 requests/minute — far below the 30 RPM the same row grants, and far
    # below what the 14,400 RPD implies. This is the only row where TPM binds,
    # which is why nothing paced on it until #143; the Flash rows' 250K against
    # 5-15 RPM is slack no evaluation prompt can use up.
    "gemma-4-26b-a4b-it":     {"rpm": 30, "tpm": 16_000,  "rpd": 14_400},
    "gemma-4-31b-it":         {"rpm": 30, "tpm": 16_000,  "rpd": 14_400},
}

# What one job evaluation costs in PROMPT tokens: a full JD plus PROFILE.md.
# A nominal, deliberately — the real figure varies per job by a factor of two or
# more, and there is no honest way to know it before the run. Every message
# derived from it therefore SAYS it is nominal and prints the number, so a
# reader whose prompts are twice this size can halve the answer themselves.
# Used only for REPORTING (capacity, the recommendation ranking). The pacer
# itself never guesses: it measures each prompt as it goes.
NOMINAL_PROMPT_TOKENS = 8_000

# What a response is charged. TPM counts tokens in BOTH directions, so a call
# costs its prompt plus whatever comes back — and only the prompt is knowable
# before the call, which is what this number is for. MAX_TOKENS (8,192) is the
# ceiling a caller allows, not what an evaluation returns (~1-2K); reserving the
# ceiling would more than halve every run's throughput to insure against a size
# responses don't reach. So reserve a realistic figure and correct upward
# afterwards — `paced_caller` charges the overrun once the real response is in
# hand. That correction lands after the call that overran, so it can't un-spend
# those tokens; what it prevents is the error compounding across a run.
#
# `_row_capacity` adds it too, so the capacity a message quotes is costed the
# same way the pacer costs a call. Counting the prompt alone there would have
# reported a throughput the pacer then declined to deliver.
_RESERVED_OUTPUT_TOKENS = 1_500

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
    numeric, because they are what pacing and capping read; tpm may be null,
    which is "unlimited" — a real answer for most rows, and the reason it can't
    just be required alongside the other two."""
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
    """Merge `rows` into the user's limits file and return the whole stored table.

    Validates through _clean_entry — the same path a run reads by — so the UI
    cannot persist a row the loader would silently decline, which would look
    saved and do nothing. Raises ValueError on a row that doesn't validate, so
    the caller can 400 rather than write junk.

    MERGED, not replaced. The wizard edits ONE model at a time and posts exactly
    that row (`onboard.js`), and it tells a failover-chain user to add the other
    members to the file by hand — so replacing the file would delete every row
    the user was not looking at, including the ones it had just told them to
    write. Silently, and back onto baked numbers that are wrong for a paid
    project by construction. Deleting a row is therefore something you have to
    ASK for: a row mapping to None removes it, and an empty result removes the
    file rather than leaving `{}` behind, so "clear my overrides" returns to the
    baked table exactly as if the file had never existed."""
    # Validate through _clean_entry directly rather than round-tripping the dict
    # through json.dumps/parse_overrides: same validator, one pass, and it yields
    # the rejected keys instead of recovering them from a set difference.
    cleaned, bad, dropped = {}, [], []
    for model, raw in rows.items():
        if raw is None:
            dropped.append(model)                     # explicit delete
        elif (entry := _clean_entry(model, raw)) is None:
            bad.append(model)
        else:
            cleaned[model] = entry
    if bad:
        raise ValueError(
            f"Invalid limits for {', '.join(sorted(bad))}: rpm and rpd must be "
            f"positive integers; tpm must be a positive integer or null (unlimited)."
        )
    stored = {**_load_overrides(), **cleaned}
    for model in dropped:
        stored.pop(model, None)
    path = override_path()
    _override_cache.pop(str(path), None)
    if not stored:
        path.unlink(missing_ok=True)
        return {}
    # Atomic: _load_overrides mtime-caches this file, so a reader landing
    # mid-write would parse a truncated body, decline it, and silently fall back
    # to the baked table. Imported here rather than at module scope to keep the
    # common import path (which the UI venv takes) off _batch_common.
    from pipeline._batch_common import atomic_write_text

    atomic_write_text(path, json.dumps(stored, indent=2, sort_keys=True) + "\n")
    return stored


def _spec_models(model: str) -> list[str]:
    """Split a model spec into members. A comma-separated `BATCH_MODEL` is a
    failover chain (tried in order); a single value is a one-member chain."""
    return [m.strip() for m in (model or "").split(",") if m.strip()]


def _spec_rpd(model: str) -> int | None:
    """Summed RPD across the spec's known members. A failover chain's daily
    capacity is the SUM — when one member is exhausted (429) the call falls over
    to the next — so a `flash,gemma` chain does ~20 then ~14,400 = 14,420/day.
    None if no member has known limits (paid / non-Gemini / unknown).

    Reads effective_limits(), so a user's own AI Studio numbers set the cap when
    they've supplied them."""
    limits = effective_limits()
    caps = [limits[m]["rpd"] for m in _spec_models(model) if m in limits]
    return sum(caps) if caps else None


def _capacity_terms(row: dict) -> dict[str, int]:
    """Each of the model's three published limits expressed in one unit —
    evaluations per day — so they can be compared at all.

    RPD is already in it. RPM and TPM are rates, so they are spent over a day's
    1,440 minutes: `rpm * 1440`, and `tpm * 1440 / per-call cost`. Continuous
    running is an upper bound nobody reaches, and that is deliberate — a figure
    that overstates even the best case is unambiguously wrong, whereas modelling
    a session length would stack a second guess on top of prompt size.

    Ordered rpd, rpm, tpm so `_row_bound`'s tie goes to RPD: a model whose quota
    and rates agree is not something to report as rate-bound."""
    terms = {"rpd": row["rpd"], "rpm": row["rpm"] * 1440}
    tpm = row.get("tpm")
    if tpm:                                           # null/0 = unlimited
        terms["tpm"] = int(tpm * 1440 / (NOMINAL_PROMPT_TOKENS + _RESERVED_OUTPUT_TOKENS))
    return terms


def _row_capacity(row: dict) -> int:
    """One model's honest evaluations/day: the lowest of its three limits.

    All three — not RPD alone, and not RPD-versus-TPM either. Whichever binds
    first is what the model delivers, and on the baked table that is always RPD
    (every row's RPM and TPM buy thousands a day), which is why "highest RPD" was
    a serviceable ranking until a 16K TPM row arrived. That is also why the fix
    has to be the general rule rather than a TPM special case: a user's own
    numbers reach combinations the table never had, and a 1 RPM row is 1,440
    evaluations a day however large the quota beside it. Ranking that on RPD
    would recommend it as the best model available.

    On Gemma, TPM gives ~2,425 against an RPD of 14,400 — the advertised cap
    overstates the model 5x. For every Flash row the rate terms are enormous
    (250K TPM ≈ 37,000/day) and RPD wins."""
    return min(_capacity_terms(row).values())


def _row_bound(row: dict) -> str:
    """Which published limit is the one the model actually delivers on — the
    reason a capacity sits below the RPD, for the message that quotes it."""
    terms = _capacity_terms(row)
    return min(terms, key=terms.get)


def batch_recommendation() -> str | None:
    """The model to steer batch runs toward: the highest honest daily capacity.

    Computed rather than baked, because the constant this replaced ranked by RPD
    alone and so recommended a model whose advertised 14,400/day is unreachable.
    The ranking has to use the same metric the warning quotes, or the two
    disagree inside the one message that carries both. Computing it also means a
    user who supplied their own AI Studio numbers gets a recommendation for THEIR
    project rather than for a free tier they may not be on.

    The key ends in the model id, which makes it a total order: `min` therefore
    returns the same answer whatever order the table is in. Ranking on capacity
    alone would break ties on dict insertion order, and advice that reshuffles
    when a row is added above it reads as a change of advice."""
    limits = effective_limits()
    if not limits:
        return None
    return min(limits, key=lambda m: (-_row_capacity(limits[m]), -limits[m]["rpd"], m))


def unknown_members(model: str) -> list[str]:
    """Spec members with no known limits, in order. The input to "conforming is
    on but can't act" — see format_unconformable_warning."""
    limits = effective_limits()
    return [m for m in _spec_models(model) if m not in limits]


def free_tier_viability(model: str, pending_jobs: int) -> dict | None:
    """Whether `pending_jobs` fits the model spec's free-tier daily capacity.

    Returns None for a spec with no known free-tier member (paid / non-Gemini /
    unknown). Otherwise {"rpd", "capacity", "exceeds", "bounds"[, "suggestion",
    "suggestion_capacity"]} — `suggestion` is the better model to switch to,
    present only when the run exceeds capacity AND that model isn't already in
    the spec. Chain-aware: both figures sum across the spec's known members,
    because a chain fails over member-to-member (see _spec_rpd).

    `exceeds` compares against `capacity`, not `rpd`, and the difference is the
    whole of #143: on a TPM-bound model the quota is reachable only in theory, so
    answering "will these fit in a day?" with RPD tells a user their 5,000
    pending jobs are fine when the token budget will get through ~2,425 of them.
    `cap_to_rpd` still slices on `rpd` — a quota refuses requests, a rate merely
    slows them, and only the first is a reason to defer work to tomorrow.

    The recommendation is ranked only when the run actually overflows: it sorts
    the whole table, and every batch run calls this whether or not there is
    anything to say."""
    members = _spec_models(model)
    limits = effective_limits()
    rows = [limits[m] for m in members if m in limits]
    if not rows:
        return None
    capacity = sum(_row_capacity(r) for r in rows)
    result = {"rpd": sum(r["rpd"] for r in rows), "capacity": capacity,
              "exceeds": pending_jobs > capacity,
              # Which limits cost the spec its RPD, across the members that lost
              # any — so the message names the one that did, rather than assuming
              # TPM, which is only the reason on the rows the table happens to
              # carry today.
              "bounds": sorted({_row_bound(r) for r in rows
                                if _row_capacity(r) < r["rpd"]})}
    if result["exceeds"]:
        best = batch_recommendation()
        best_capacity = _row_capacity(limits[best]) if best else 0
        # Better, not merely different. "Not already in the spec" alone advised
        # swapping gemma-4-31b-it for its identical twin — and the message now
        # quotes both capacities, so it refuted itself in one line.
        if best and best not in members and best_capacity > capacity:
            result["suggestion"] = best
            # Carried, not re-derived at the print site: this is the number the
            # ranking above chose `best` FOR, and the two disagreeing is how the
            # line came to advertise an RPD it had not ranked by.
            result["suggestion_capacity"] = best_capacity
    return result


def format_free_tier_warning(model: str, pending_jobs: int) -> str | None:
    """A one-line warning when the run won't get through its queue today, else
    None. Callers print it before starting a batch run.

    The number quoted is the honest capacity, not the RPD — the message exists to
    set an expectation, and one that promises 14,400 evaluations from a model
    that can deliver ~2,425 is worse than no message. When the two differ, TPM is
    why, so the message says so rather than leaving the user to wonder which of
    the three published numbers moved.

    It deliberately makes NO claim about what happens to the overflow: with
    conforming on it is deferred by `cap_to_rpd` (whose own line says so), with
    conforming off a quota overflow fails and a rate overflow just runs into
    tomorrow. Three outcomes, one sentence — the old flat "the rest will hit the
    daily cap and fail" was right for one of them."""
    v = free_tier_viability(model, pending_jobs)
    if not v or not v["exceeds"]:
        return None
    bound = ""
    if v["capacity"] < v["rpd"]:
        if v["bounds"] == ["tpm"]:
            label, why = "TPM-bound", (
                f"a nominal {NOMINAL_PROMPT_TOKENS:,}-token prompt and its response "
                f"against its tokens/minute")
        elif v["bounds"] == ["rpm"]:
            label, why = "RPM-bound", "its requests/minute spent over a day"
        else:
            label, why = "rate-bound", "its per-minute limits"
        bound = f" ({label}: {why} — not the {v['rpd']:,} its RPD implies)"
    msg = (f"[batch] ⚠ Gemini free tier on {model} gets through ~{v['capacity']:,} "
           f"evaluations/day{bound}; you have {pending_jobs:,} pending.")
    if v.get("suggestion"):
        msg += (f" Set BATCH_MODEL={v['suggestion']} "
                f"(~{v['suggestion_capacity']:,}/day) for batch runs.")
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
# the LLM stages actively CONFORM to the limits rather than just warning:
# requests are paced to the model's RPM *and* its TPM, and the eval run caps to
# the model's RPD (the rest is deferred to the next run). All three are no-ops
# for paid models / other providers.

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
    model has no known cap.

    RPD only, deliberately — not the TPM-aware capacity the warning quotes.
    Exceeding the quota gets the day's remaining requests refused, which is a
    reason to defer them to tomorrow. Exceeding what the token budget can pace
    doesn't: the run just goes on for as long as it is left running (at
    TPM-bound rates that can be days, not hours), and whatever it doesn't reach
    stays pending for the next run anyway. Slicing on capacity would defer the
    same jobs to a tomorrow with no more tokens per minute than today, which
    buys the user nothing and costs them the ones this run would have done."""
    cap = rpd_cap(model)
    if cap is None or len(items) <= cap:
        return items, 0
    return items[:cap], len(items) - cap


# ~4 characters per token is Google's own rule of thumb for English, and it is
# what we have: the SDK's count_tokens is a network round trip per call, which
# would add latency and its own quota to the very path being paced.
_CHARS_PER_TOKEN = 4

# The window both pacers work in. RPM and TPM are per-MINUTE limits, so this is
# not a knob — a second value here wouldn't be a shorter window, it would be a
# different limit.
_MINUTE = 60.0


def estimate_tokens(text: str) -> int:
    """Approximate token count for a string, rounded up.

    Approximate is the honest word: the budget below is therefore soft, and a
    429 remains possible on a prompt this under-counts. That is acceptable
    because the fallback is intact — `_call_with_retry` backs off on a 429 — and
    the alternative (an exact count per call) costs a round trip on every request
    to a path whose entire purpose is spending fewer of them."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


class TokenBudget:
    """Thread-safe rolling-window token pacer: holds the tokens spent in any
    60-second window to `tpm`, delaying a call that would break it.

    A different shape from RateLimiter, because the cost of a request is not
    fixed: RPM is one min-interval between starts, while TPM is a budget that a
    single 8K-token call can consume half of. So this keeps the charges that are
    still inside the window and schedules the next one at the first instant the
    oldest of them has aged out far enough to make room.

    Charges are scheduled in non-decreasing order (`_floor`), which buys two
    things. It is FIFO — a cheap call can't jump the queue ahead of an expensive
    one already waiting, the same fairness `RateLimiter._next` gives. And it
    makes the check sufficient: a window is only ever tested at the moment a
    charge lands, so with a monotonic schedule every future window is tested by
    the charge that opens it. Without it, placing a small charge *before* an
    already-scheduled larger one would leave the window they share unchecked.

    `tpm <= 0` (or None — "unlimited") means no budget: every method is a no-op,
    so callers don't branch. `monotonic`/`sleep` are injectable for tests, as on
    RateLimiter."""

    def __init__(self, tpm: int | None, *, monotonic=time.monotonic, sleep=time.sleep):
        self._tpm = tpm if tpm and tpm > 0 else 0
        self._monotonic = monotonic
        self._sleep = sleep
        # [(scheduled_at, tokens)], ascending by scheduled_at. Bounded by the
        # calls that fit in one window — tens of entries, so a list is cheaper
        # than the deque a "log" shape suggests, and it can be indexed.
        self._charges: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def _prune(self, upto: float) -> None:
        """Drop the charges that can no longer affect a window ending at `upto`
        or later. Every charge is scheduled at or after the `upto` its caller
        passes, so nothing still needed is dropped."""
        cutoff = upto - _MINUTE
        i = 0
        while i < len(self._charges) and self._charges[i][0] <= cutoff:
            i += 1
        del self._charges[:i]

    def _floor(self, now: float) -> float:
        return max(now, self._charges[-1][0]) if self._charges else now

    def acquire(self, tokens: int) -> None:
        """Wait until `tokens` fits, then charge them. No-op when unlimited."""
        if not self._tpm or tokens <= 0:
            return
        with self._lock:
            now = self._monotonic()
            # Prune at the earliest start this charge could take, not at `now`:
            # what survives is then exactly the window that start competes with,
            # so the cutoff is walked once instead of once per meaning.
            start = self._floor(now)
            self._prune(start)
            live = sum(t for _, t in self._charges)
            i = 0
            # If they leave no room, advance to the moment the oldest of them
            # ages out, repeatedly. Each step drops exactly one charge, so this
            # terminates in at most len(self._charges) iterations.
            while live + tokens > self._tpm and i < len(self._charges):
                start = self._charges[i][0] + _MINUTE
                live -= self._charges[i][1]
                i += 1
            # Falling out with the window empty means this single call costs more
            # than the whole per-minute budget — a prompt larger than TPM. It runs
            # anyway, alone in an empty window: waiting for room that can never
            # exist would hang the run instead of letting the provider answer
            # (and a 429 is recoverable; a hang is not).
            self._charges.append((start, tokens))
            wait = start - now
        if wait > 0:
            self._sleep(wait)

    def charge(self, tokens: int) -> None:
        """Record tokens already spent, without waiting — the response
        reconciliation.

        Landed at `now`, or with the last scheduled charge when one is already
        booked ahead of it, which keeps the list ordered. That errs toward
        counting the overrun for longer than it was live rather than for less,
        which is the safe direction for a budget."""
        if not self._tpm or tokens <= 0:
            return
        with self._lock:
            at = self._floor(self._monotonic())
            self._prune(at)
            self._charges.append((at, tokens))


class RateLimiter:
    """Thread-safe min-interval pacer: spaces request starts by 60/rpm seconds so
    concurrent workers stay within the per-minute rate. `monotonic`/`sleep` are
    injectable for tests. rpm <= 0 means no pacing."""

    def __init__(self, rpm: int, *, monotonic=time.monotonic, sleep=time.sleep):
        self._interval = _MINUTE / rpm if rpm > 0 else 0.0
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


# The pacers, keyed on the model rather than held per caller. See _pacer_for.
_pacers: dict[tuple, tuple] = {}
_pacers_lock = threading.Lock()


def _pacer_for(model: str, row: dict):
    """The (limiter, budget) pair every caller on `model` shares this process.

    Process-wide rather than per caller instance, because the quota is per API
    key and the callers are not: the UI builds a fresh caller per Add-Job
    request thread (`server.py`) and per résumé-tailoring call (`skills.py`), so
    per-caller state hands each of them the model's FULL budget. Under RPM alone
    that was close to invisible — one extra request against 30 RPM is noise —
    but the eval path is *designed* to saturate a 16K TPM budget, so a second
    in-process caller is a doubling, which is the 429 this exists to prevent.

    `handoff.py`'s `_make_tailor_fn` already carries the rule as a comment ("all
    rows share ONE caller … per-row resolution would give every pool worker its
    own rate limiter"). Keying the state on the model makes it a property of
    this module instead of something five call sites have to remember.

    The limits are part of the key, so an override file edited mid-process
    (which the UI can do) yields a pacer built from the new numbers rather than
    one still pacing to the old ones."""
    key = (model, row["rpm"], row["tpm"])
    with _pacers_lock:
        pacer = _pacers.get(key)
        if pacer is None:
            pacer = (RateLimiter(row["rpm"]), TokenBudget(row["tpm"]))
            _pacers[key] = pacer
        return pacer


def paced_caller(caller, model: str):
    """Wrap an LLM caller so each call is paced to the model's free-tier RPM AND
    TPM — only when conforming is on AND the model is in the free-tier table.
    Otherwise returns `caller` unchanged. The limiter and budget come from
    `_pacer_for`, so every caller on this model in this process shares one pace,
    however many of them get built.

    The prompt is measured here rather than passed in, which is what made TPM
    pacing cheap: every caller in the repo has the signature `(system, user)`, so
    the wrapper already holds the whole prompt. Summing the string arguments
    costs one `len()` each — `len` on a str is O(1) whatever its size — and reads
    the prompt without depending on which position it arrives in.

    TPM is acquired BEFORE the RPM slot. When the token budget is the binding
    limit — the case this exists for — that wait is the long one, and taking it
    first means the RPM spacing is measured between calls that actually happen
    rather than between slots reserved and then sat on."""
    limits = effective_limits()
    if not conforming_enabled() or model not in limits:
        return caller
    limiter, budget = _pacer_for(model, limits[model])

    def wrapped(*args, **kwargs):
        prompt = sum(estimate_tokens(v) for v in (*args, *kwargs.values())
                     if isinstance(v, str))
        budget.acquire(prompt + _RESERVED_OUTPUT_TOKENS)
        limiter.acquire()
        result = caller(*args, **kwargs)
        # Reconcile the reservation upward when the response was bigger than it.
        # Only upward: a smaller response leaves the window slightly conservative,
        # which costs a little throughput, while refunding an over-estimate would
        # have the pacer act on a number it already knows is approximate.
        overrun = (estimate_tokens(result) - _RESERVED_OUTPUT_TOKENS
                   if isinstance(result, str) else 0)
        if overrun > 0:
            budget.charge(overrun)
        return result

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
        print(f"[limits] recommended for batch runs: {batch_recommendation()}")
        for model, v in sorted(effective_limits().items()):
            src = "yours" if model in overrides else "baked"
            tpm = "unlimited" if v["tpm"] is None else f"{v['tpm']:,}"
            # Where a rate binds, the row's own RPD is not what the model
            # delivers. Printing the three numbers without saying so is how the
            # table read as "14,400/day" for a year (#143).
            cap, note = _row_capacity(v), ""
            if cap < v["rpd"]:
                bound = _row_bound(v)
                why = (f" at a nominal {NOMINAL_PROMPT_TOKENS:,}-token prompt + response"
                       if bound == "tpm" else "")
                note = f"  ← {bound.upper()}-bound: ~{cap:,}/day{why}"
            print(f"  {model:26} rpm={v['rpm']:<4} tpm={tpm:<10} rpd={v['rpd']:<6} [{src}]{note}")

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
