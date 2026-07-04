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


def select(
    career_ops: Path,
    *,
    min_score: float = 4.0,
    limit: int = 0,
    applications_md: Path | None = None,
) -> list[ApplyJob]:
    """Return pending candidates, highest score first.

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
        ))

    jobs.sort(key=lambda j: j.score, reverse=True)   # None scores were filtered above
    return jobs[:limit] if limit else jobs
