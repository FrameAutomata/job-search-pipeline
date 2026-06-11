"""Select which evaluated jobs to apply to.

Reads the application tracker (the same parser the UI uses), keeps rows that
scored well enough and are still pending a decision, pulls the posting URL out
of the notes column, and — for the LinkedIn Easy Apply fast-path — narrows to
linkedin.com/jobs/view URLs. The engine confirms Easy Apply once the page is
open; this stage just picks plausible candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pipeline.app import data as _data

_URL_RE = re.compile(r"https?://\S+")

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


def _extract_url(notes: str) -> str:
    m = _URL_RE.search(notes or "")
    return m.group(0).rstrip(".,);]") if m else ""


def is_linkedin_job(url: str) -> bool:
    """True for a LinkedIn job-posting URL (the Easy Apply fast-path target)."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host.endswith("linkedin.com") and "/jobs/view/" in url


def select(
    career_ops: Path,
    *,
    min_score: float = 4.0,
    limit: int = 0,
    linkedin_only: bool = True,
    applications_md: Path | None = None,
) -> list[ApplyJob]:
    """Return apply candidates, highest score first.

    min_score: skip rows scoring below this (rows with no score are skipped).
    limit: cap the number returned (0 = no cap).
    linkedin_only: keep only linkedin.com/jobs/view URLs (the Phase 2 engine).
    applications_md: tracker to read; defaults to career_ops/data/applications.md.
        The apply stage points this at a freshly-downloaded GitHub artifact so it
        applies against current cloud evaluations, not a stale local copy."""
    tracker = Path(applications_md) if applications_md else Path(career_ops) / "data" / "applications.md"
    rows = _data.parse_applications(tracker)

    jobs: list[ApplyJob] = []
    for row in rows:
        if row.get("status_canonical") not in _PENDING_STATUSES:
            continue
        score = row.get("score_value")
        if score is None or score < min_score:
            continue
        url = _extract_url(row.get("notes", ""))
        if not url:
            continue
        if linkedin_only and not is_linkedin_job(url):
            continue
        jobs.append(ApplyJob(
            num=row.get("num", ""),
            company=row.get("company", ""),
            role=row.get("role", ""),
            url=url,
            score=score,
            report_path=row.get("report_path", ""),
        ))

    jobs.sort(key=lambda j: (j.score is not None, j.score or 0.0), reverse=True)
    return jobs[:limit] if limit else jobs
