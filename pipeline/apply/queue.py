"""Select which evaluated jobs to apply to.

Reads the application tracker (the same parser the UI uses), keeps rows that
scored well enough and are still pending a decision, pulls the posting URL out
of the notes column, and — for the LinkedIn Easy Apply fast-path — narrows to
linkedin.com/jobs/view URLs. The engine confirms Easy Apply once the page is
open; this stage just picks plausible candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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


def is_linkedin_job(url: str) -> bool:
    """True for a LinkedIn job-posting URL (the Easy Apply fast-path target)."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host.endswith("linkedin.com") and "/jobs/view/" in url


def is_indeed_job(url: str) -> bool:
    """True for an Indeed job-posting URL (the SmartApply target)."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host.endswith("indeed.com") and ("/viewjob" in url or "jk=" in url)


def job_site(url: str) -> str | None:
    """The apply engine for a URL: 'linkedin' / 'indeed' for the deterministic
    fast-paths, else 'agent' for the agentic catch-all that drives any other
    navigable posting (off-site employer ATS, arbitrary forms). None only for a
    non-navigable string (garbage / non-http scheme), which no engine can open."""
    if is_linkedin_job(url):
        return "linkedin"
    if is_indeed_job(url):
        return "indeed"
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return None
    return "agent" if scheme in ("http", "https") else None


def select(
    career_ops: Path,
    *,
    min_score: float = 4.0,
    limit: int = 0,
    linkedin_only: bool = True,
    sites: tuple[str, ...] | None = None,
    applications_md: Path | None = None,
) -> list[ApplyJob]:
    """Return apply candidates, highest score first.

    min_score: skip rows scoring below this (rows with no score are skipped).
    limit: cap the number returned (0 = no cap).
    sites: when given, keep only jobs whose engine (job_site) is in this set —
        e.g. ("linkedin", "indeed"). Takes precedence over linkedin_only.
    linkedin_only: legacy gate kept for back-compat (used when sites is None):
        keep only linkedin.com/jobs/view URLs.
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
        url = _data.extract_url(row.get("notes", ""))
        if not url:
            continue
        if sites is not None:
            if job_site(url) not in sites:
                continue
        elif linkedin_only and not is_linkedin_job(url):
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
