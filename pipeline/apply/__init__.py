"""Auto-apply stage — deterministic LinkedIn Easy Apply fast-path.

`run()` selects evaluated jobs from the tracker, opens a logged-in browser, and
walks each LinkedIn Easy Apply form with the deterministic engine, calling the
answer engine only for fields it can't fill from the profile. Three modes:

  review  (default) — fill every form, stop before Submit, print the drafted
                      answers for you to eyeball. Nothing is submitted.
  dry-run           — same as review; an explicit rehearsal.
  auto              — click Submit unattended and mark the tracker Applied.
                      Higher throughput, higher risk (LinkedIn ToS) — opt-in.

Local-only: it needs a real LinkedIn session, so it never runs in the cloud."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pipeline.app import data as _data
from pipeline._batch_common import atomic_write_text
from pipeline.apply import browser, linkedin, queue
from pipeline.apply.answers import AnswerEngine
from pipeline.apply.profile import ApplyProfile
from pipeline.apply.result import ApplyResult, failed

ROOT = Path(__file__).resolve().parent.parent.parent
_VALID_MODES = ("review", "dry-run", "auto")
# The workflows whose artifact carries the latest applications.md (mirror of
# server.py's list — both pipelines upload the same pipeline-output-* artifact).
_PIPELINE_WORKFLOWS = ["daily-pipeline.yml", "easy-apply-pipeline.yml"]


def run(
    career_ops: Path,
    *,
    mode: str = "review",
    min_score: float = 4.0,
    limit: int = 0,
    headless: bool = False,
    refresh: bool = True,
    target_url: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Apply to qualifying LinkedIn Easy Apply jobs. Returns the count applied
    (auto mode) or filled-and-held (review/dry-run).

    target_url: apply to this one posting, bypassing the tracker queue (for a
        one-off apply or to reproduce a specific job). Skips refresh/selection.
    refresh: pull the latest applications.md from the most recent GitHub pipeline
        artifact before selecting (defaults on, since evaluations accumulate in
        the cloud). Falls back to the local tracker when gh/network is unavailable."""
    career_ops = Path(career_ops)
    if mode not in _VALID_MODES:
        mode = "review"

    # Always bound (used by _report/_mark_applied). For a --apply-url one-off it
    # stays the local default and is effectively unused (the synthetic job has no
    # tracker row, so _mark_applied no-ops on its empty num).
    applications_md = career_ops / "data" / "applications.md"
    if target_url:
        jobs = [queue.ApplyJob(num="", company="(target)", role="", url=target_url, score=None)]
        print(f"[apply] targeting single URL: {target_url}")
    else:
        if refresh:
            applications_md = _refresh_tracker(career_ops)
        jobs = queue.select(career_ops, min_score=min_score, limit=limit,
                            linkedin_only=True, applications_md=applications_md)
        if not jobs:
            print(f"[apply] no LinkedIn Easy Apply candidates "
                  f"(score >= {min_score}, status Evaluated) in {applications_md.name}")
            return 0
    if mode == "auto" and refresh:
        print("[apply] note: status write-back goes to the downloaded tracker copy; "
              "it won't reach the cloud until pushed (UI Refresh/Push or Edit Tracker).")

    print(f"[apply] {len(jobs)} candidate(s) | mode={mode} | "
          f"{'headless' if headless else 'windowed'}")

    profile = ApplyProfile.load(career_ops)
    caller = _build_caller(provider, model)
    engine = AnswerEngine(
        profile=profile,
        cache_path=career_ops / "batch" / "apply-answers.json",
        caller=caller,
    )

    applied = held = failures = 0

    try:
        with browser.launch(headless=headless) as page:
            if not browser.ensure_logged_in(page, headless=headless):
                print("[apply] not signed in to LinkedIn — aborting. "
                      "Run windowed (not --headless) and sign in when prompted.")
                return 0

            for job in jobs:
                engine.job_context = f"{job.company} — {job.role}"
                engine.cover_letter_text = _find_tailored_cover_letter(career_ops, job)
                resume = _resolve_resume(career_ops, job)
                try:
                    result = linkedin.apply_to(page, job, engine, mode=mode, resume_path=resume)
                except Exception as e:  # never let one job kill the batch
                    result = failed(f"exception:{type(e).__name__}")

                applied, held, failures = _report(
                    job, result, mode, applications_md, applied, held, failures,
                )
    except ImportError as e:
        print(f"[apply] {e}")
        return 0
    except Exception as e:
        # Most commonly the browser window was closed mid-run (Playwright raises
        # a target-closed / navigation-aborted error). Report cleanly rather than
        # dumping a traceback; whatever finished before the close still counts.
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"[apply] session ended early ({type(e).__name__}: {msg[:80]}) — "
              "did the browser window close?")

    print(f"[apply] done — {applied} submitted, {held} filled (held for review), "
          f"{failures} failed | {engine.llm_calls} LLM calls, {engine.cache_hits} cache hits")
    return applied if mode == "auto" else held


def _report(job, result: ApplyResult, mode: str, applications_md: Path,
            applied: int, held: int, failures: int) -> tuple[int, int, int]:
    """Log one job's outcome and, for a real submission, mark the tracker."""
    # ASCII-only markers: Windows consoles default to cp1252, which can't encode
    # glyphs like ✓/✗/→ and would crash the whole run on the print.
    tag = f"#{job.num} {job.company} / {job.role}"[:60]
    if result.applied and result.submitted:
        _mark_applied(applications_md, job.num)
        applied += 1
        print(f"[apply] [OK]   SUBMITTED {tag}")
    elif result.applied:  # filled but held (review/dry-run)
        held += 1
        print(f"[apply] [..]   FILLED {tag} -- {len(result.answers)} field(s) drafted, not submitted")
        for q, a in result.answers:
            print(f"               {q[:50]} -> {a[:60]}")
    else:
        failures += 1
        print(f"[apply] [XX]   {result.code.upper()} {tag}"
              + (f" ({result.reason})" if result.reason else ""))
    return applied, held, failures


def _find_tailored_resume(career_ops: Path, job) -> Path | None:
    """A per-job tailored resume PDF, if one exists. Searches APPLY_TAILORED_DIR
    (default career-ops/output, where career-ops' pdf mode writes tailored CVs)
    for a .pdf whose filename contains the company slug; returns the most recent
    match. Returns None when there's no tailored resume for this job."""
    base = os.environ.get("APPLY_TAILORED_DIR")
    tdir = Path(base) if base else career_ops / "output"
    if not tdir.exists():
        return None
    slug = re.sub(r"[^a-z0-9]+", "", (job.company or "").lower())
    if not slug:
        return None
    matches = [p for p in tdir.glob("*.pdf")
               if slug in re.sub(r"[^a-z0-9]+", "", p.stem.lower())]
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def _resolve_resume(career_ops: Path, job) -> Path | None:
    """Tailored resume for this job when available, else the configured default."""
    return _find_tailored_resume(career_ops, job) or linkedin._resume_pdf()


def _find_tailored_cover_letter(career_ops: Path, job) -> str:
    """Tailored cover-letter TEXT for this job, if career-ops generated one.

    Searches APPLY_COVER_DIR (default career-ops/output, where career-ops writes
    generated documents) for a .txt/.md whose filename contains the company slug
    and 'cover', returning the most recent match's text. Empty string when there
    isn't one — the engine then skips the (optional) cover-letter field."""
    base = os.environ.get("APPLY_COVER_DIR")
    cdir = Path(base) if base else career_ops / "output"
    slug = re.sub(r"[^a-z0-9]+", "", (job.company or "").lower())
    if not cdir.exists() or not slug:
        return ""
    matches = []
    for p in cdir.glob("*"):
        if p.suffix.lower() not in (".txt", ".md"):
            continue
        name = re.sub(r"[^a-z0-9]+", "", p.stem.lower())
        if slug in name and "cover" in name:
            matches.append(p)
    if not matches:
        return ""
    best = max(matches, key=lambda p: p.stat().st_mtime)
    try:
        return best.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _refresh_tracker(career_ops: Path) -> Path:
    """Download the latest pipeline artifact's applications.md from GitHub so we
    apply against current cloud evaluations. Returns the refreshed file's path,
    or the local tracker on any failure (no gh, offline, no runs yet)."""
    local = career_ops / "data" / "applications.md"
    try:
        from pipeline.app import gh
        run_info = gh.latest_successful_run(_PIPELINE_WORKFLOWS)
        if not run_info:
            print("[apply] refresh: no successful pipeline run on GitHub — using local tracker")
            return local
        data_dir = gh.download_artifact(run_info["databaseId"], ROOT / ".ui-cache" / "apply")
        apps = data_dir / "data" / "applications.md"
        if apps.exists():
            print(f"[apply] refreshed tracker from GitHub run {run_info['databaseId']}")
            return apps
        print("[apply] refresh: artifact has no applications.md — using local tracker")
    except Exception as e:
        print(f"[apply] refresh failed ({type(e).__name__}: {e}) — using local tracker")
    return local


def _mark_applied(applications_md: Path, num: str) -> None:
    """Set the tracker row's status to Applied (reuses the UI's editor)."""
    if not num or not applications_md.exists():
        return
    text = applications_md.read_text(encoding="utf-8")
    updated = _data.set_status_in_text(text, num, "Applied")
    if updated != text:
        atomic_write_text(applications_md, updated)


def _build_caller(provider: str | None, model: str | None):
    """Build the answer-engine LLM caller. Returns None to let AnswerEngine
    auto-detect from env; builds explicitly only when a provider is named."""
    if not provider:
        return None
    from pipeline.batch_evaluate import _build_caller as _bc, PROVIDER_DEFAULTS
    return _bc(provider, model or os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider])
