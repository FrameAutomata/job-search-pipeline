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
from pathlib import Path

from pipeline import screen
from pipeline.app import data


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


def run(
    career_ops: Path,
    *,
    applications_md: Path | None = None,
    timeout: int = 8,
    concurrency: int = 8,
    dry_run: bool = False,
    progress=None,
) -> dict:
    """Re-check every Evaluated tracker role; mark `expired` ones Discarded.

    Returns {checked, discarded, dead, skipped, unconfirmed} where `dead` is a
    list of {num, company, role, url, reason} and `unconfirmed` counts roles we
    couldn't confirm live or dead (uncertain page / fetch error — left as-is).
    `progress(checked, total, dead_so_far)` fires once per role. `dry_run`
    reports the dead set without writing anything.
    """
    apps = Path(applications_md) if applications_md else Path(career_ops) / "data" / "applications.md"
    jobs, skipped = _select(apps)
    total = len(jobs)

    checked = unconfirmed = 0
    dead: list[dict] = []
    for job, result, reason, _body in screen.classify_each(
        jobs, lambda j: j.url, timeout=timeout, max_workers=max(1, concurrency)
    ):
        checked += 1
        if result == "expired":
            dead.append({"num": job.num, "company": job.company, "role": job.role,
                         "url": job.url, "reason": reason})
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

    summary = {"checked": checked, "discarded": discarded, "dead": dead,
               "skipped": skipped, "unconfirmed": unconfirmed}
    print(f"[recheck] checked {checked}, discarded {discarded}, "
          f"{unconfirmed} unconfirmed, {skipped} without URL"
          + (" [dry-run]" if dry_run else ""), flush=True)
    return summary
