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
from pipeline._batch_common import atomic_write_text, read_text
from pipeline.apply import browser, linkedin, queue
from pipeline.apply.answers import AnswerEngine, salary_from_report
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
    tailor_min_score: float = 4.0,
) -> int:
    """Apply to qualifying LinkedIn Easy Apply jobs. Returns the count applied
    (auto mode) or filled-and-held (review/dry-run).

    target_url: apply to this one posting, bypassing the tracker queue (for a
        one-off apply or to reproduce a specific job). Skips refresh/selection.
    refresh: pull the latest applications.md from the most recent GitHub pipeline
        artifact before selecting (defaults on, since evaluations accumulate in
        the cloud). Falls back to the local tracker when gh/network is unavailable.
    tailor_min_score: jobs scoring at or above this get a per-job TAILORED resume
        (a slot-edited copy of resumes/resume.docx, one-page verified via
        LibreOffice); below it, the default resume. Raise it to tailor only top
        matches; set it absurdly high (e.g. 99) to disable tailoring."""
    career_ops = Path(career_ops)
    if mode not in _VALID_MODES:
        mode = "review"
    # Lazy import: avoids a circular import (cover_letters imports pipeline.apply).
    from pipeline import cover_letters

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

            # Reports live next to the tracker (career-ops/reports OR the refreshed
            # artifact's reports/), not necessarily under the local career-ops — so a
            # cloud-only-evaluated job's report is found rather than read as empty.
            report_root = applications_md.parent.parent

            for job in jobs:
                engine.job_context = f"{job.company} — {job.role}"
                engine.unanswered = []   # per-job, so _report surfaces only this job's
                # Role's researched market comp (from the report) for salary fields.
                report_text = (read_text(report_root / job.report_path)
                               if getattr(job, "report_path", "") else "")
                engine.role_salary_target = salary_from_report(report_text)
                # Cover letter generated lazily — only if this form has a cover-
                # letter field (request-gated). Builds its own caller so it can
                # use COVER_MODEL (quality-first) rather than the speed-first
                # APPLY_MODEL chain the short answers use.
                engine.cover_letter_text = ""
                engine.cover_letter_provider = (
                    lambda j=job: cover_letters.generate_for_job(
                        career_ops, j, report_base=report_root, provider=provider, model=model)
                )
                # For forms whose cover-letter field is a PDF upload (not a
                # textarea), render the generated letter to a PDF on demand.
                engine.cover_pdf_provider = (
                    lambda j=job: cover_letters.ensure_cover_pdf(career_ops, j.company)
                )
                # Per-job tailored resume (slot-edited copy of the candidate's own
                # .docx, one-page verified) for jobs clearing the tailor threshold.
                # Lazy: generated only when a resume-upload field actually appears,
                # so expired/off-site jobs never burn the LLM call.
                engine.resume_provider = None
                if _should_tailor(job, tailor_min_score):
                    from pipeline import resume_tailor
                    # Memoized: _handle_file_inputs runs per form step (and again
                    # after a validation error), and while a SUCCESSFUL generation
                    # is cheap to repeat (cache hit), a FAILING one would re-run
                    # the full LLM-call-with-backoff every time — minutes per job
                    # with the provider down.
                    engine.resume_provider = _memoized(
                        lambda j=job: resume_tailor.generate_for_job(
                            career_ops, j, report_base=report_root,
                            provider=provider, model=model)
                    )
                resume = _resolve_resume(career_ops, job)
                try:
                    result = linkedin.apply_to(page, job, engine, mode=mode, resume_path=resume)
                except Exception as e:  # never let one job kill the batch
                    result = failed(f"exception:{type(e).__name__}")

                applied, held, failures = _report(
                    job, result, mode, applications_md, applied, held, failures,
                    unanswered=list(engine.unanswered),
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
            applied: int, held: int, failures: int,
            unanswered: list[str] | None = None) -> tuple[int, int, int]:
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
        if unanswered:
            # Fields the LLM couldn't answer (left blank/placeholder) — call them
            # out so they're reviewed, not silently submitted.
            print(f"               [!] {len(unanswered)} field(s) NEED REVIEW (LLM unavailable): "
                  + "; ".join(q[:40] for q in unanswered[:5]))
    else:
        failures += 1
        print(f"[apply] [XX]   {result.code.upper()} {tag}"
              + (f" ({result.reason})" if result.reason else ""))
    return applied, held, failures


def _memoized(fn):
    """Call fn once and replay the result (including None) on later calls."""
    cell: list = []

    def call():
        if not cell:
            cell.append(fn())
        return cell[0]
    return call


def _should_tailor(job, tailor_min_score: float) -> bool:
    """Tailor only when the job's evaluation score clears the threshold. A
    target-url one-off has no score → no tailoring (use --apply-url after
    pre-generating, or the default resume)."""
    return job.score is not None and job.score >= tailor_min_score


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
    # Exclude cover letters: "<Company> - cover.pdf" also contains the company
    # slug. Match 'cover'/'letter' as whole words only — a bare substring wrongly
    # excludes real companies ("Discovery" contains "cover", "Recover" too).
    # .docx included: the tailor stage caches a docx when no PDF renderer exists;
    # its transient ".work" files are never valid uploads.
    matches = [p for ext in ("*.pdf", "*.docx") for p in tdir.glob(ext)
               if _stem_matches_company(p.stem, slug)
               and ".work" not in p.stem
               and not re.search(r"\b(cover|letter)\b", p.stem.lower())]
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def _stem_matches_company(stem: str, slug: str) -> bool:
    """Whether a filename stem refers to this company. The company slug must
    equal a CONTIGUOUS run of the stem's word-tokens — a bare substring test
    let 'Meta' match 'Metabase - resume.pdf' and upload another company's
    tailored resume."""
    tokens = re.findall(r"[a-z0-9]+", stem.lower())
    for i in range(len(tokens)):
        joined = ""
        for j in range(i, len(tokens)):
            joined += tokens[j]
            if joined == slug:
                return True
            if len(joined) > len(slug):
                break
    return False


def _resolve_resume(career_ops: Path, job) -> Path | None:
    """Tailored resume for this job when available, else the configured default."""
    return _find_tailored_resume(career_ops, job) or linkedin._resume_pdf()


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
    """Record an auto-submitted application's status everywhere it matters:

    1. The tracker copy this run selected from (direct Status-cell edit) — but
       with refresh on that's the downloaded artifact copy, which nothing else
       reads, so on its own the change was effectively invisible.
    2. The UI's status-override channel (same one a kanban drag uses) — the UI
       immediately shows the row as Applied (pending), and the existing Push
       button carries it to the cloud tracker."""
    if not num:
        return
    if applications_md.exists():
        text = applications_md.read_text(encoding="utf-8")
        updated = _data.set_status_in_text(text, num, "Applied")
        if updated != text:
            atomic_write_text(applications_md, updated)
    _data.record_status_override(num, "Applied")


def _build_caller(provider: str | None, model: str | None):
    """Build the answer-engine LLM caller. Returns None to let AnswerEngine
    auto-detect from env; builds explicitly only when a provider is named."""
    if not provider:
        return None
    from pipeline.batch_evaluate import resolve_caller
    from pipeline.apply.answers import thinking_disabled
    return resolve_caller(provider, model, disable_thinking=thinking_disabled())
