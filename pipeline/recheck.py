"""Re-check liveness of evaluated tracker roles; mark closed ones Discarded.

The scrape-time screen (pipeline/screen.py) only checks brand-new postings. A
role that closes AFTER it was evaluated sits in the tracker as `Evaluated` until
someone tries to apply. This stage closes that gap: it re-fetches every
Evaluated role and Discards the ones that are demonstrably gone.

Only a definitive `expired` result (HTTP 404/410, an expired-body marker, an
error redirect) discards a role. An `active` OR `uncertain` result leaves it
Evaluated — so a transient network blip, a login wall, or an ambiguous page
never drops a live role from the queue; those land in the `unconfirmed` count so
an outage (everything uncertain) is visible rather than silently reported as
"all still open". The parallel fetch + LinkedIn guest-endpoint mapping is the
shared `screen.classify_each`; the Discards are one batched dual-write.

Wired into orchestrate via the opt-in `--recheck-liveness` flag (off by default)
and exposed in the UI as a background sweep. Both go through `run()`.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class RecheckJob:
    num: str
    company: str
    role: str
    url: str


def _select(applications_md: Path) -> tuple[list[RecheckJob], int]:
    """(jobs, skipped): every Evaluated row that has a posting URL, plus a count
    of Evaluated rows that carried no URL to re-check."""
    jobs: list[RecheckJob] = []
    skipped = 0
    for row in data.parse_applications(applications_md):
        if row.get("status_canonical") != "Evaluated":
            continue
        url = data.extract_url(row.get("notes", ""))
        if not url:
            skipped += 1
            continue
        jobs.append(RecheckJob(
            num=row.get("num", ""), company=row.get("company", ""),
            role=row.get("role", ""), url=url,
        ))
    return jobs, skipped


def select_evaluated(applications_md: Path) -> list[RecheckJob]:
    """Every Evaluated tracker role with a posting URL to re-check. Unlike the
    apply queue this is neither score-gated nor LinkedIn-only — liveness applies
    to every site and every score."""
    jobs, _ = _select(Path(applications_md))
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


def _filter_by_state(jobs, *, state, now, min_age_hours, budget):
    """The subset of `jobs` to re-check this run: drop any checked within
    `min_age_hours`, then take the `budget` least-recently-checked (unseen roles
    sort oldest, so they go first). Stable on ties, so file order breaks them."""
    # Stored timestamps are always tz-aware; coerce a naive `now` (e.g. an
    # external caller passing datetime.now()) to UTC so the comparison below
    # can't raise "can't compare offset-naive and offset-aware".
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=min_age_hours)
    eligible = [j for j in jobs
                if state.get(j.url) is None or state[j.url] <= cutoff]
    eligible.sort(key=lambda j: state.get(j.url) or _EPOCH)
    return eligible[:budget]


def select_for_recheck(applications_md, *, state, now, min_age_hours, budget):
    """Every Evaluated+URL role, narrowed to this run's budget of the stalest
    (the selection `run()` actually fetches). Separated out so the budget/age
    logic is unit-testable without the HTTP layer."""
    jobs, _ = _select(Path(applications_md))
    return _filter_by_state(jobs, state=state, now=now,
                            min_age_hours=min_age_hours, budget=budget)


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
    deferred}: `dead` is a list of {num, company, role, url, reason};
    `unconfirmed` counts roles we read but couldn't call live-or-dead (ambiguous
    page / fetch error); `throttled` counts roles LinkedIn rate-limited (no real
    read — retried next run, NEVER discarded); `deferred` counts Evaluated roles
    skipped this run by the budget / min-age window. `progress(checked, total,
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
    budget = int(env_float("RECHECK_BUDGET", _DEFAULT_BUDGET)) if budget is None else budget
    min_age_hours = (env_float("RECHECK_MIN_AGE_HOURS", _DEFAULT_MIN_AGE_HOURS)
                     if min_age_hours is None else min_age_hours)

    now = datetime.now(timezone.utc)
    state = _load_state(sp)
    all_jobs, skipped = _select(apps)
    jobs = _filter_by_state(all_jobs, state=state, now=now,
                            min_age_hours=min_age_hours, budget=budget)
    deferred = len(all_jobs) - len(jobs)
    total = len(jobs)

    checked = unconfirmed = throttled = 0
    dead: list[dict] = []
    new_state = dict(state)
    for job, result, reason, _body in screen.classify_each(
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
               "throttled": throttled, "deferred": deferred}
    print(f"[recheck] checked {checked}, discarded {discarded}, "
          f"{unconfirmed} unconfirmed, {throttled} throttled, "
          f"{deferred} deferred, {skipped} without URL"
          + (" [dry-run]" if dry_run else ""), flush=True)
    return summary
