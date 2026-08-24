"""Re-check liveness of evaluated tracker roles; mark closed ones Discarded.

The scrape-time screen (pipeline/screen.py) only checks brand-new postings. A
role that closes AFTER it was evaluated sits in the tracker as `Evaluated` until
someone tries to apply. This stage closes that gap: it re-fetches every
Evaluated role and Discards the ones that are demonstrably gone.

Only a definitive `expired` result (HTTP 404/410, an expired-body marker, an
error redirect, an Indeed jobData expired/removed verdict) discards a role. An
`active` OR `uncertain` result leaves it Evaluated — so a transient network
blip, a login wall, or an ambiguous page never drops a live role from the
queue; those land in the `unconfirmed` count so an outage (everything
uncertain) is visible rather than silently reported as "all still open".
Site routing is `screen.classify_liveness_each`: LinkedIn roles ride the
parallel page fetch (guest-endpoint mapping), Indeed roles the batched jobData
API (the posting page is Cloudflare-walled but the scraper's API isn't); the
Discards are one batched dual-write.

Wired into orchestrate via the opt-in `--recheck-liveness` flag (off by default)
and exposed in the UI as a background sweep. Both go through `run()`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import screen
from pipeline._batch_common import atomic_write_text, env_float
from pipeline.app import data

# Per-run defaults (env-overridable). A once-daily run re-checking the entire
# Evaluated backlog (hundreds of roles) trips LinkedIn's per-IP rate limiter, so
# we cap each run to a BUDGET of the least-recently-checked roles and skip any
# re-checked within MIN_AGE_HOURS. The backlog cycles over several days; closed
# postings still get caught, just not all in one throttled burst.
_DEFAULT_BUDGET = 100
_DEFAULT_MIN_AGE_HOURS = 6.0

# drain() (the manual catch-up over a backlog larger than one budget) loops
# budgeted sweeps with this cooldown between bursts so the per-IP limiter can
# recover, bounded by this many cycles so persistent throttling can't loop
# forever. Both env-overridable.
_DEFAULT_DRAIN_COOLDOWN = 60.0
_DEFAULT_DRAIN_MAX_CYCLES = 20


@dataclass(frozen=True)
class RecheckJob:
    num: str
    company: str
    role: str
    url: str


def _select(applications_md: Path) -> tuple[list[RecheckJob], int, int]:
    """(jobs, skipped, unverifiable): the Evaluated roles we can actually
    liveness-check, plus counts of Evaluated rows with no URL (`skipped`) and
    with a URL on a site we have no liveness path for (`unverifiable` — e.g.
    Glassdoor's anti-bot wall, which a plain fetch can't classify and which has
    no API fallback). The unverifiable ones are never fetched."""
    jobs: list[RecheckJob] = []
    skipped = unverifiable = 0
    for row in data.parse_applications(applications_md):
        if row.get("status_canonical") != "Evaluated":
            continue
        url = data.extract_url(row.get("notes", ""))
        if not url:
            skipped += 1
            continue
        if not screen.is_liveness_verifiable(url):
            unverifiable += 1
            continue
        jobs.append(RecheckJob(
            num=row.get("num", ""), company=row.get("company", ""),
            role=row.get("role", ""), url=url,
        ))
    return jobs, skipped, unverifiable


def select_evaluated(applications_md: Path) -> list[RecheckJob]:
    """Every Evaluated tracker role we can liveness-check (has a posting URL on a
    verifiable site). Not score-gated — liveness applies at every score — but it
    IS site-gated: only sites with a working unauthenticated liveness path
    (LinkedIn via the guest endpoint, Indeed via the jobData API) are returned."""
    jobs, _, _ = _select(Path(applications_md))
    return jobs


# ── per-role last-checked state ───────────────────────────────────────────────
#
# `recheck-state.tsv` maps a posting URL → the ISO-8601 timestamp of its last
# CONCLUSIVE re-check. It lets a budgeted run re-check only the stalest roles and
# skip recently-confirmed ones. Persisted in the GitHub Actions pipeline-state
# cache so the cadence survives across cloud runs.

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _load_state(path) -> dict:
    """Parse `recheck-state.tsv` → {url: tz-aware datetime}. Missing file → {}.
    Malformed lines (wrong arity / unparseable timestamp) are skipped, never
    fatal — a corrupt state file degrades to "everything is stale", not a crash."""
    p = Path(path)
    if not p.exists():
        return {}
    state: dict[str, datetime] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 2 or not parts[0]:
            continue
        try:
            ts = datetime.fromisoformat(parts[1])
        except ValueError:
            continue
        # A naive timestamp (older format / hand-edit) is read as UTC so it can
        # be compared against the tz-aware `now` without raising.
        state[parts[0]] = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return state


def _save_state(path, state) -> None:
    """Write {url: datetime} as sorted `url\\tiso8601` lines, atomically."""
    lines = [f"{url}\t{ts.isoformat()}" for url, ts in sorted(state.items())]
    atomic_write_text(Path(path), "".join(f"{ln}\n" for ln in lines))


def _eligible(jobs, *, state, now, min_age_hours):
    """Roles due a re-check — never checked, or last checked more than
    `min_age_hours` ago — sorted stalest-first (unseen sort oldest). No budget
    cap, so callers can both pick the budget and count the genuine remainder."""
    # Stored timestamps are always tz-aware; coerce a naive `now` (e.g. an
    # external caller passing datetime.now()) to UTC so the comparison below
    # can't raise "can't compare offset-naive and offset-aware".
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=min_age_hours)
    due = [j for j in jobs if state.get(j.url) is None or state[j.url] <= cutoff]
    due.sort(key=lambda j: state.get(j.url) or _EPOCH)
    return due


def _filter_by_state(jobs, *, state, now, min_age_hours, budget):
    """The stalest `budget` of the roles due a re-check (see _eligible)."""
    return _eligible(jobs, state=state, now=now, min_age_hours=min_age_hours)[:budget]


def select_for_recheck(applications_md, *, state, now, min_age_hours, budget):
    """Every Evaluated+URL role, narrowed to this run's budget of the stalest
    (the selection `run()` actually fetches). Separated out so the budget/age
    logic is unit-testable without the HTTP layer."""
    jobs, _, _ = _select(Path(applications_md))
    return _filter_by_state(jobs, state=state, now=now,
                            min_age_hours=min_age_hours, budget=budget)


def _resolve_budget(budget) -> int:
    """The per-run budget — env-resolved when unset, clamped to >= 1. A 0 or
    negative `RECHECK_BUDGET` would otherwise disable the sweep or, via the
    `due[:budget]` slice, drop the STALEST roles and make drain's
    `checked < budget` stop unreachable. Shared by run() and drain() so the two
    can't resolve it differently."""
    resolved = int(env_float("RECHECK_BUDGET", _DEFAULT_BUDGET)) if budget is None else budget
    return max(1, resolved)


def run(
    career_ops: Path,
    *,
    applications_md: Path | None = None,
    timeout: int = 8,
    concurrency: int = 4,
    dry_run: bool = False,
    progress=None,
    state_path=None,
    budget=None,
    min_age_hours=None,
) -> dict:
    """Re-check a budget of the stalest Evaluated roles; mark `expired` ones
    Discarded.

    Returns {checked, discarded, dead, skipped, unconfirmed, throttled,
    deferred, unverifiable}: `unverifiable` counts Evaluated roles on sites with
    no liveness path (Glassdoor's anti-bot wall) — never fetched, just
    reported; `dead` is a list of {num, company, role, url, reason};
    `unconfirmed` counts roles we read but couldn't call live-or-dead (ambiguous
    page / fetch error); `throttled` counts roles with no real read — LinkedIn
    rate-limiting or an Indeed jobData batch failure (retried next run, NEVER
    discarded); `deferred` counts roles DUE a
    re-check but cut by the per-run budget (the backlog remainder for a later
    run — not roles merely inside the min-age window). `progress(checked, total,
    dead_so_far)` fires once per checked role. `dry_run` reports without writing.

    Per-role last-checked state lives in `state_path` (default
    `<career_ops>/data/recheck-state.tsv`). Only a non-throttled result stamps a
    role's timestamp, so a throttled (or still-closed-behind-a-wall) posting
    stays stale and bubbles back to the front of the next run instead of being
    recorded as freshly checked. Lowered concurrency (vs. the scrape-time screen)
    keeps the re-check burst under the rate limiter too.
    """
    apps = Path(applications_md) if applications_md else Path(career_ops) / "data" / "applications.md"
    sp = Path(state_path) if state_path else Path(career_ops) / "data" / "recheck-state.tsv"
    budget = _resolve_budget(budget)
    min_age_hours = (env_float("RECHECK_MIN_AGE_HOURS", _DEFAULT_MIN_AGE_HOURS)
                     if min_age_hours is None else min_age_hours)

    now = datetime.now(timezone.utc)
    state = _load_state(sp)
    all_jobs, skipped, unverifiable = _select(apps)
    due = _eligible(all_jobs, state=state, now=now, min_age_hours=min_age_hours)
    jobs = due[:budget]
    # Deferred = roles DUE a re-check but cut by the budget (the genuine backlog
    # remainder, drained over future runs) — NOT roles merely inside the min-age
    # window. This is what makes a fully-drained backlog report deferred=0.
    deferred = len(due) - len(jobs)
    total = len(jobs)

    checked = unconfirmed = throttled = 0
    dead: list[dict] = []
    new_state = dict(state)
    # classify_liveness_each routes each role by site (Indeed → batched jobData
    # API, everything else → per-URL page fetch) and yields one uniform shape.
    for job, result, reason, _body in screen.classify_liveness_each(
        jobs, lambda j: j.url, timeout=timeout, max_workers=max(1, concurrency)
    ):
        checked += 1
        if result == "throttled":
            # No real read — leave the timestamp untouched so this role is
            # re-tried (front of the queue) next run rather than marked done.
            throttled += 1
        else:
            new_state[job.url] = now   # conclusive read → record last-checked
            if result == "expired":
                dead.append({"num": job.num, "company": job.company,
                             "role": job.role, "url": job.url, "reason": reason})
            elif result != "active":   # 'uncertain' (incl. a caught fetch error)
                unconfirmed += 1
        if progress:
            progress(checked, total, len(dead))

    # One batched dual-write for all Discards (the tracker file + the override
    # channel are each rewritten once, not once per dead role).
    discarded = 0
    if dead and not dry_run:
        data.record_status_changes(
            apps, [(d["num"], "Discarded", d["company"], d["role"]) for d in dead])
        discarded = len(dead)

    if not dry_run:
        # Prune entries for URLs no longer in the Evaluated set (since-discarded
        # /applied/rejected roles) so the file stays bounded instead of growing
        # by every role ever checked. Guarded on a non-empty backlog so a
        # transiently empty/unreadable tracker can't wipe the whole cadence.
        if all_jobs:
            live = {j.url for j in all_jobs}
            new_state = {u: t for u, t in new_state.items() if u in live}
        _save_state(sp, new_state)

    summary = {"checked": checked, "discarded": discarded, "dead": dead,
               "skipped": skipped, "unconfirmed": unconfirmed,
               "throttled": throttled, "deferred": deferred,
               "unverifiable": unverifiable}
    print(f"[recheck] checked {checked}, discarded {discarded}, "
          f"{unconfirmed} unconfirmed, {throttled} throttled, "
          f"{deferred} deferred, {unverifiable} unverifiable, {skipped} without URL"
          + (" [dry-run]" if dry_run else ""))
    return summary


def drain(career_ops, *, budget=None, cooldown=None, max_cycles=None,
          progress=None, **run_kw) -> dict:
    """Repeatedly run() budgeted sweeps until the eligible backlog is covered.

    A single sweep only re-checks `budget` of the stalest roles, so a backlog
    larger than one budget needs several. This loops them — each cycle skips the
    roles the previous one stamped (run's per-role min-age state), so it advances
    through fresh roles — with a `cooldown` between bursts so LinkedIn's per-IP
    limiter can recover.

    Stops when a sweep checks FEWER than `budget` (the eligible pool fit in one
    sweep → the backlog is drained to its tail) or after `max_cycles` (a hard cap
    so persistent throttling can't loop forever). `checked`/`discarded`/`dead`
    aggregate across cycles; `throttled`/`unconfirmed`/`deferred` report the final
    cycle's still-unresolved counts. Forwards timeout/dry_run/etc. to run().

    A backlog that already fits in one budget returns after a single cycle with
    no cooldown — so a caller (the UI) can always drain() and only actually loop
    when there's more than a budget of roles to get through. `dry_run` forces a
    single sweep (no state is persisted, so cycles can't advance). If a sweep
    raises, drain stops and returns the partial aggregate with `error` set rather
    than losing the completed cycles' work."""
    budget = _resolve_budget(budget)
    cooldown = (env_float("RECHECK_DRAIN_COOLDOWN", _DEFAULT_DRAIN_COOLDOWN)
                if cooldown is None else cooldown)
    max_cycles = (int(env_float("RECHECK_DRAIN_MAX_CYCLES", _DEFAULT_DRAIN_MAX_CYCLES))
                  if max_cycles is None else max_cycles)
    # Under dry_run run() persists no state, so a cycle can't advance to fresh
    # roles — looping would just re-check the same budget. Do a single sweep.
    if run_kw.get("dry_run"):
        max_cycles = 1

    agg = {"cycles": 0, "checked": 0, "discarded": 0, "dead": [],
           "unconfirmed": 0, "throttled": 0, "deferred": 0, "skipped": 0,
           "unverifiable": 0}

    def cycle_progress(checked, _total, cycle_dead):
        # Cumulative across cycles; total is left open (the backlog shrinks each
        # cycle) so the UI shows a running count, not a per-cycle denominator
        # that resets every loop. agg holds the completed-cycle totals here.
        if progress:
            progress(agg["checked"] + checked, None, agg["discarded"] + cycle_dead)

    # A run() failure in a later cycle must not discard the completed cycles'
    # aggregate — those Discards are already on disk. Catch it, record the error,
    # and return what we have so the caller reports partial progress, not a
    # zeroed failure.
    last = None
    try:
        for cycle in range(max(1, max_cycles)):
            last = run(career_ops, budget=budget, progress=cycle_progress, **run_kw)
            agg["cycles"] += 1
            agg["checked"] += last["checked"]
            agg["discarded"] += last["discarded"]
            agg["unconfirmed"] += last.get("unconfirmed", 0)   # cumulative: stamped, not re-checked
            agg["dead"].extend(last["dead"])
            if last["checked"] < budget:   # budget >= 1, so 0 (empty sweep) also breaks
                break
            if cycle < max_cycles - 1:
                time.sleep(cooldown)
    except Exception as exc:
        agg["error"] = str(exc)

    if last is not None:
        # throttled/deferred are the FINAL cycle's still-unresolved state: a
        # throttled role bubbles and is retried in later cycles, and deferred is
        # the genuine remainder after the last sweep (0 once fully drained).
        for k in ("throttled", "deferred", "skipped", "unverifiable"):
            agg[k] = last.get(k, 0)
    print(f"[recheck:drain] {agg['cycles']} cycle(s): checked {agg['checked']}, "
          f"discarded {agg['discarded']}; last cycle {agg['throttled']} throttled, "
          f"{agg['deferred']} deferred")
    return agg
