"""Auto-apply stage — routes each evaluated job to the engine that can drive it.

`run()` selects evaluated jobs from the tracker and dispatches each by URL: the
deterministic LinkedIn Easy Apply and Indeed SmartApply engines for those sites,
and the agentic catch-all (a claude + Playwright-MCP runner) for everything else
(off-site employer ATS, arbitrary forms). Each engine fills the form, calling the
answer engine only for fields it can't fill from the profile. Three modes:

  review  (default) — fill every form, stop before Submit, print the drafted
                      answers for you to eyeball. Nothing is submitted.
  dry-run           — same as review; an explicit rehearsal.
  auto              — click Submit unattended and mark the tracker Applied.
                      Higher throughput, higher risk (site ToS) — opt-in.

Local-only: it needs real logged-in browser sessions, so it never runs in the
cloud."""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

from pipeline.app import data as _data
from pipeline._batch_common import normalize_company, read_text
from pipeline.apply import agent_engine, browser, indeed, linkedin, queue
from pipeline.apply.answers import AnswerEngine, salary_from_report
from pipeline.apply.profile import ApplyProfile
from pipeline.apply.result import ApplyResult, DEFER, EXPIRED, NO_FAST_APPLY_FORM, failed

ROOT = Path(__file__).resolve().parent.parent.parent
_VALID_MODES = ("review", "dry-run", "auto")
# The engines we can drive, in dispatch order. "agent" is the universal catch-all
# (any off-site ATS / arbitrary form) the deterministic LinkedIn/Indeed engines
# can't handle; it runs last so the cheap deterministic paths go first. The
# agent's CDP port + persistent profile live in browser.py beside the other
# sessions' config.
_APPLY_SITES = ("linkedin", "indeed", "agent")
# The workflow whose artifact carries the latest applications.md (mirror of
# server.py's list — the daily pipeline uploads the pipeline-output-* artifact).
_PIPELINE_WORKFLOWS = ["daily-pipeline.yml"]


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
    """Apply to qualifying jobs (LinkedIn / Indeed / off-site ATS via the agent).
    Returns the count applied (auto mode) or filled-and-held (review/dry-run).

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
                            sites=_APPLY_SITES, applications_md=applications_md)
        if not jobs:
            print(f"[apply] no apply candidates "
                  f"(score >= {min_score}, status Evaluated) in {applications_md.name}")
            return 0
    if mode == "auto" and refresh:
        print("[apply] note: status write-back goes to the downloaded tracker copy; "
              "it won't reach the cloud until pushed (UI Refresh/Push or Edit Tracker).")

    print(f"[apply] {len(jobs)} candidate(s) | mode={mode} | "
          f"{'headless' if headless else 'windowed'}")

    engine = build_engine(career_ops, provider=provider, model=model)
    # Reports live next to the tracker (career-ops/reports OR the refreshed
    # artifact's reports/), not necessarily under the local career-ops — so a
    # cloud-only-evaluated job's report is found rather than read as empty.
    report_root = applications_md.parent.parent

    # Each engine uses a different browser, so a site's jobs run in their own
    # session. job_site routes LinkedIn/Indeed to their deterministic engines and
    # everything else navigable to the agentic catch-all ("agent"); only a
    # non-navigable URL falls through to the LinkedIn default.
    groups: dict[str, list] = {}
    for job in jobs:
        groups.setdefault(queue.job_site(job.url) or "linkedin", []).append(job)

    job_kwargs = dict(
        career_ops=career_ops, report_root=report_root, applications_md=applications_md,
        mode=mode, headless=headless, provider=provider, model=model,
        tailor_min_score=tailor_min_score,
    )
    applied = held = failures = 0

    def dispatch(buckets: dict, deferrals: list | None) -> None:
        nonlocal applied, held, failures
        for site in _APPLY_SITES:
            if not buckets.get(site):
                continue
            a, h, f = _apply_jobs(site, buckets[site], engine, deferrals=deferrals,
                                  **job_kwargs)
            applied += a
            held += h
            failures += f

    # Round 1 collects deferrals (a job routed to the wrong engine). Round 2
    # re-runs each under its target with deferrals=None, so a second defer just
    # reports as a failure — a 2-round cap that stops a deterministic<->agent
    # ping-pong.
    deferrals: list = []
    dispatch(groups, deferrals)
    if deferrals:
        regroups: dict[str, list] = {}
        for job, target, result in deferrals:
            # If the deferring engine captured an off-site redirect (e.g. Indeed's
            # Apply bounced to the company's own ATS), point the next engine at THAT
            # URL — otherwise the agent lands back on the original (Indeed) and
            # ping-pongs / hits the bot wall.
            if result.redirect_url:
                job = dataclasses.replace(job, url=result.redirect_url)
            regroups.setdefault(target, []).append(job)
        dispatch(regroups, None)

    print(f"[apply] done — {applied} submitted, {held} filled (held for review), "
          f"{failures} failed | {engine.llm_calls} LLM calls, {engine.cache_hits} cache hits")
    return applied if mode == "auto" else held


def _apply_jobs(site: str, jobs: list, engine: AnswerEngine, *, career_ops: Path,
                report_root: Path, applications_md: Path, mode: str, headless: bool,
                provider: str | None, model: str | None,
                tailor_min_score: float,
                deferrals: list | None = None) -> tuple[int, int, int]:
    """Apply to one site's jobs in its own browser session. LinkedIn uses the
    bundled-Chromium Easy Apply engine; Indeed the patchright SmartApply engine on
    the pre-captured login; "agent" the CDP-attached agentic engine. Returns
    (applied, held, failures).

    `deferrals`: when given, a job this engine reports as the wrong fit (DEFER, or
    a deterministic no-fast-apply-form) is appended as (job, target, result) for
    the caller to re-dispatch, instead of being reported here."""
    applied = held = failures = 0
    try:
        if site == "indeed":
            session, apply_fn = browser.launch_indeed(headless=headless), indeed.apply_to
        elif site == "agent":
            # Real Chrome with a CDP endpoint the agent's Playwright-MCP attaches
            # to; `page` below is the Session (carrying that endpoint), which
            # agent_engine.apply_to needs — not a bare page like the others.
            session = browser.launch_agent_session(headless=headless)
            apply_fn = agent_engine.apply_to
        else:
            session, apply_fn = browser.launch(headless=headless), linkedin.apply_to
        with session as page:
            if site == "indeed":
                if not browser.is_logged_in_indeed(page):
                    print("[apply] Indeed apply profile isn't signed in. Run the one-time "
                          "capture-login first: `./run.ps1 --capture-indeed-login` (sign in "
                          "once in the normal browser that opens).")
                    return 0, 0, 0
            elif site == "agent":
                pass  # the agent signs into each ATS itself; no pre-flight login gate
            elif not browser.ensure_logged_in(page, headless=headless):
                print("[apply] not signed in to LinkedIn — aborting. Run windowed "
                      "(not --headless) and sign in when prompted.")
                return 0, 0, 0

            for job in jobs:
                configure_engine_for_job(
                    engine, job, career_ops=career_ops, report_root=report_root,
                    provider=provider, model=model, tailor_min_score=tailor_min_score,
                )
                resume = _resolve_resume(career_ops, job)
                try:
                    result = apply_fn(page, job, engine, mode=mode, resume_path=resume)
                except Exception as e:  # never let one job kill the batch
                    result = failed(f"exception:{type(e).__name__}")
                target = _defer_target(site, result)
                if deferrals is not None and target and target != site:
                    # Wrong engine for this role — hand it off for re-dispatch.
                    deferrals.append((job, target, result))
                    via = f" (via {result.redirect_url[:60]})" if result.redirect_url else ""
                    print(f"[apply] [->]   DEFER {job.company} / {job.role} -> {target}{via}")
                    continue
                applied, held, failures = _report(
                    job, result, mode, applications_md, applied, held, failures,
                    unanswered=list(engine.unanswered),
                )
    except ImportError as e:
        print(f"[apply] {e}")
    except Exception as e:
        # Most commonly the browser window was closed mid-run (Playwright raises a
        # target-closed error). Report cleanly; whatever finished still counts.
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"[apply] {site} session ended early ({type(e).__name__}: {msg[:80]}) — "
              "did the browser window close?")
    return applied, held, failures


def build_engine(career_ops: Path, *, provider: str | None, model: str | None) -> AnswerEngine:
    """The shared answer engine (profile + cache + LLM caller). One per session;
    reconfigured per job by configure_engine_for_job. Used by both the CLI run()
    loop and the UI review worker so the two apply paths wire it identically."""
    return AnswerEngine(
        profile=ApplyProfile.load(career_ops),
        cache_path=career_ops / "batch" / "apply-answers.json",
        caller=_build_caller(provider, model),
        # The candidate's CV grounds experience questions ("years with X") — the
        # same cv.md the cover-letter generator uses. read_text returns "" if it's
        # missing, so the engine degrades to the profile-only context.
        cv_text=read_text(career_ops / "cv.md"),
    )


def configure_engine_for_job(engine: AnswerEngine, job, *, career_ops: Path, report_root: Path,
                             provider: str | None, model: str | None,
                             tailor_min_score: float) -> None:
    """Set the per-job fields on a shared engine: role context, the role's
    researched market comp (from its report) for salary fields, and the lazy
    cover-letter / tailored-resume providers (generated only if the form asks).
    Extracted so the CLI loop and the UI review worker don't drift."""
    from pipeline import cover_letters
    engine.job_context = f"{job.company} — {job.role}"
    engine.unanswered = []   # per-job, so the review surfaces only this job's
    report_text = (read_text(report_root / job.report_path)
                   if getattr(job, "report_path", "") else "")
    engine.role_salary_target = salary_from_report(report_text)
    # Cover letter generated lazily — only if this form has a cover-letter field
    # (request-gated); it builds its own quality-first COVER_MODEL caller.
    engine.cover_letter_text = ""
    engine.cover_letter_provider = (
        lambda j=job: cover_letters.generate_for_job(
            career_ops, j, report_base=report_root, provider=provider, model=model)
    )
    # For forms whose cover-letter field is a PDF upload, render on demand.
    engine.cover_pdf_provider = (
        lambda j=job: cover_letters.ensure_cover_pdf(career_ops, j.company)
    )
    # Per-job tailored resume for jobs clearing the threshold. Lazy + memoized so
    # a failing generation doesn't re-run the full LLM backoff on every form step.
    engine.resume_provider = None
    if _should_tailor(job, tailor_min_score):
        from pipeline import resume_tailor
        engine.resume_provider = _memoized(
            lambda j=job: resume_tailor.generate_for_job(
                career_ops, j, report_base=report_root, provider=provider, model=model)
        )


def _defer_target(site: str, result: ApplyResult) -> str | None:
    """The engine to hand `result`'s job off to, or None to keep it here. The
    agent emits RESULT:DEFER:<engine> when it lands on a fast-apply flow; a
    deterministic engine that finds no Easy-Apply/SmartApply form is the inverse
    signal — re-route those to the agentic catch-all."""
    if result.code == DEFER:
        return result.deferred_to or None
    # Indeed signals no-SmartApply in the reason (code stays "failed"); LinkedIn in
    # the code — accept either.
    if site != "agent" and (result.code in NO_FAST_APPLY_FORM
                            or result.reason in NO_FAST_APPLY_FORM):
        return "agent"
    return None


def _report(job, result: ApplyResult, mode: str, applications_md: Path,
            applied: int, held: int, failures: int,
            unanswered: list[str] | None = None) -> tuple[int, int, int]:
    """Log one job's outcome and, for a real submission, mark the tracker."""
    # ASCII-only markers: Windows consoles default to cp1252, which can't encode
    # glyphs like ✓/✗/→ and would crash the whole run on the print.
    tag = f"#{job.num} {job.company} / {job.role}"[:60]
    if result.applied and result.submitted:
        _mark_applied(applications_md, job)
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
        if result.code == EXPIRED:
            # The posting is no longer accepting applications — mark it Discarded
            # so it leaves the active queue (parallel to submit -> Applied).
            _mark_status(applications_md, job, "Discarded")
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
    slug = normalize_company(job.company)
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


def _mark_status(applications_md: Path, job, status: str) -> None:
    """Record `status` for `job` in the tracker file + the UI override channel.
    Thin wrapper over the shared dual-write (data.record_status_change has the
    identity-anchor rationale). Used for submit -> Applied and a closed posting
    -> Discarded; the same write the liveness re-check uses for gone -> Discarded."""
    _data.record_status_change(
        applications_md, getattr(job, "num", "") or "", status,
        company=getattr(job, "company", "") or "",
        role=getattr(job, "role", "") or "",
    )


def _mark_applied(applications_md: Path, job) -> None:
    """An auto-submitted application -> Applied (thin wrapper over _mark_status)."""
    _mark_status(applications_md, job, "Applied")


def _build_caller(provider: str | None, model: str | None):
    """Build the answer-engine LLM caller. Returns None to let AnswerEngine
    auto-detect from env; builds explicitly only when a provider is named."""
    if not provider:
        return None
    from pipeline.batch_evaluate import resolve_caller
    from pipeline.apply.answers import thinking_disabled
    return resolve_caller(provider, model, disable_thinking=thinking_disabled())
