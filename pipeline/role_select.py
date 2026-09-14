"""Select evaluated tracker roles for downstream per-role work.

Reads the application tracker (the same parser the UI uses), keeps rows that
scored well enough and are still pending a decision, and pulls the posting URL
out of the notes column. Resume tailoring and cover letters use this to pick
which roles to build artifacts for; the browser-agent handoff selects from the
scored queue instead (pipeline/handoff.py)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.app import data as _data

# Statuses that mean "evaluated, not yet acted on". Anything else (Applied,
# Rejected, Interview, Offer, Discarded, SKIP) is intentionally left alone.
_PENDING_STATUSES = {"Evaluated"}


@dataclass(frozen=True)
class ApplyJob:
    num: str
    company: str
    role: str
    url: str
    score: float | None
    report_path: str = ""
    report_num: str = ""     # the row's `[N]`: resolves a dead link the path alone cannot (#162)
    area: str = ""           # "On-site Chicago, IL" when the role needs a body outside
                             # every commutable metro (#180); "" when it does not


def select(
    career_ops: Path,
    *,
    min_score: float = 4.0,
    limit: int = 0,
    applications_md: Path | None = None,
) -> list[ApplyJob]:
    """Return pending candidates: reachable roles first, then highest score.

    Reachable FIRST, and here rather than in each consumer, because this is the
    selector that decides what gets money spent on it — `cover_letters` makes an
    LLM call and caches a file per role, and the bulk tailoring path renders a
    PDF each. Ranking on score alone meant `--limit N` spent its whole cap on the
    roles the work-order simultaneously tells the agent not to submit: on the
    copy #180 came from the top four scorers were all out of area, and 20 of 61
    queued roles were. The verdict rides in on the row (`data.parse_applications`
    derives it), so this is an ordering change, not a second rule.

    min_score: skip rows scoring below this (rows with no score are skipped).
    limit: cap the number returned (0 = no cap).
    applications_md: tracker to read; defaults to career_ops/data/applications.md
        (an override seam for callers holding a merged/refreshed copy)."""
    tracker = Path(applications_md) if applications_md else Path(career_ops) / "data" / "applications.md"
    rows = _data.parse_applications(tracker)

    jobs: list[ApplyJob] = []
    for row in rows:
        if row.get("status_canonical") not in _PENDING_STATUSES:
            continue
        score = row.get("score_value")
        if score is None or score < min_score:
            continue
        url = _data.extract_url(row.get("notes", ""))
        if not url:
            continue
        jobs.append(ApplyJob(
            num=row.get("num", ""),
            company=row.get("company", ""),
            role=row.get("role", ""),
            url=url,
            score=score,
            report_path=row.get("report_path", ""),
            report_num=row.get("report_num", ""),
            area=str(row.get("area") or "").strip(),
        ))

    # None scores were filtered above; out-of-area roles keep their score and
    # their place in the list, they just stop taking the top of a capped run.
    jobs.sort(key=lambda j: (bool(j.area), -j.score))
    return jobs[:limit] if limit else jobs
