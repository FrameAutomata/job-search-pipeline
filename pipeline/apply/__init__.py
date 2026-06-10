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
from pathlib import Path

from pipeline.app import data as _data
from pipeline._batch_common import atomic_write_text
from pipeline.apply import browser, linkedin, queue
from pipeline.apply.answers import AnswerEngine
from pipeline.apply.profile import ApplyProfile
from pipeline.apply.result import ApplyResult, failed

_VALID_MODES = ("review", "dry-run", "auto")


def run(
    career_ops: Path,
    *,
    mode: str = "review",
    min_score: float = 4.0,
    limit: int = 0,
    headless: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Apply to qualifying LinkedIn Easy Apply jobs. Returns the count applied
    (auto mode) or filled-and-held (review/dry-run)."""
    career_ops = Path(career_ops)
    if mode not in _VALID_MODES:
        mode = "review"

    jobs = queue.select(career_ops, min_score=min_score, limit=limit, linkedin_only=True)
    if not jobs:
        print(f"[apply] no LinkedIn Easy Apply candidates (score >= {min_score}, status Evaluated)")
        return 0

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
    applications_md = career_ops / "data" / "applications.md"

    try:
        with browser.launch(headless=headless) as page:
            if not browser.ensure_logged_in(page, headless=headless):
                print("[apply] not signed in to LinkedIn — aborting. "
                      "Run windowed (not --headless) and sign in when prompted.")
                return 0

            for job in jobs:
                engine.job_context = f"{job.company} — {job.role}"
                try:
                    result = linkedin.apply_to(page, job, engine, mode=mode)
                except Exception as e:  # never let one job kill the batch
                    result = failed(f"exception:{type(e).__name__}")

                applied, held, failures = _report(
                    job, result, mode, applications_md, applied, held, failures,
                )
    except ImportError as e:
        print(f"[apply] {e}")
        return 0

    print(f"[apply] done — {applied} submitted, {held} filled (held for review), "
          f"{failures} failed | {engine.llm_calls} LLM calls, {engine.cache_hits} cache hits")
    return applied if mode == "auto" else held


def _report(job, result: ApplyResult, mode: str, applications_md: Path,
            applied: int, held: int, failures: int) -> tuple[int, int, int]:
    """Log one job's outcome and, for a real submission, mark the tracker."""
    tag = f"#{job.num} {job.company} / {job.role}"[:60]
    if result.applied and result.submitted:
        _mark_applied(applications_md, job.num)
        applied += 1
        print(f"[apply] ✓ SUBMITTED {tag}")
    elif result.applied:  # filled but held (review/dry-run)
        held += 1
        print(f"[apply] · FILLED  {tag} — {len(result.answers)} field(s) drafted, not submitted")
        for q, a in result.answers:
            print(f"          {q[:50]} → {a[:60]}")
    else:
        failures += 1
        print(f"[apply] ✗ {result.code.upper()} {tag}"
              + (f" ({result.reason})" if result.reason else ""))
    return applied, held, failures


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
