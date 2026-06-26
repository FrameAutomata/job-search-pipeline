"""Agentic apply engine — an adapter that makes the universal claude+MCP runner
(`agent.run_agent`) look like the deterministic engines (linkedin/indeed), so the
CLI dispatch and the UI review worker can drive all three uniformly.

The agentic engine is one-shot, but the apply UX is fill -> hold -> you click
Submit. We preserve that by splitting the agent into two turns around the
existing hold:

  apply_to(session, ..., mode="review")  fills every page, then STOPS at the
      final review step (the agent reports RESULT:READY). The browser is left
      parked there; we map READY to a held APPLIED/submitted=False result.
  submit_application(session)            second turn over the SAME parked
      browser: click the final Submit and confirm (RESULT:APPLIED -> submitted).

  mode="auto" collapses both into one turn (fill + submit unattended).

Unlike the deterministic engines this takes the Session (not just the page): the
agent attaches over the session's CDP endpoint, which only the Session carries.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.apply import agent, prompt
from pipeline.apply.queue import ApplyJob
from pipeline.apply.result import APPLIED, CANCELLED, READY, ApplyResult

# Shown in the review panel when the agent gives no summary of its own — the
# answers list the deterministic engines populate is free-form text for the
# agent, so the panel needs *something* to display.
_HELD_FALLBACK = "Filled by the agent — review the open browser window before submitting."


def apply_to(session, job: ApplyJob, answers, *, mode: str = "review",
             resume_path: Path | None = None, should_cancel=None) -> ApplyResult:
    """Fill `job`'s application via the agent over `session.cdp_endpoint`.
    In review/dry-run the agent stops before submit (held); in auto it submits."""
    if should_cancel and should_cancel():
        return ApplyResult(code=CANCELLED)

    auto = mode == "auto"
    # answers is the shared AnswerEngine; .profile is required, so read it
    # directly — a missing profile should fail here, not deep inside build_prompt
    # where the AttributeError would point at the wrong place.
    prompt_text = prompt.build_prompt(
        job, answers.profile,
        resume_pdf=str(resume_path) if resume_path else "",
        cv_text=getattr(answers, "cv_text", "") or "",
        cover_letter_text=getattr(answers, "cover_letter_text", "") or "",
        dry_run=not auto,
    )
    result = agent.run_agent(prompt_text, cdp_endpoint=session.cdp_endpoint,
                             dry_run=not auto)
    if auto:
        return result  # run_agent already set submitted from dry_run=False

    # Review / dry-run: the agent parks at the review step (READY). Map it to a
    # held APPLIED so the worker's "ready" branch surfaces it for the user, but
    # never submitted. If the agent reports APPLIED here (it shouldn't in dry-run)
    # we STILL hold it — we never claim a submission the user didn't confirm.
    if result.code in (READY, APPLIED):
        return _held(result)
    # Anything else (failure / expired / login_issue) passes straight through so
    # a dead posting still gets marked Discarded and a login wall is reported.
    return result


def submit_application(session) -> ApplyResult:
    """Second agent turn: click the final Submit on the browser parked at the
    review step. Returns an APPLIED/submitted result on success."""
    return agent.run_agent(prompt.build_submit_prompt(),
                           cdp_endpoint=session.cdp_endpoint, dry_run=False)


def _held(result: ApplyResult) -> ApplyResult:
    """A filled-but-not-submitted result carrying the agent's summary for the
    review panel (the agent's free-text stands in for the per-field answers the
    deterministic engines list)."""
    return ApplyResult(
        code=APPLIED, submitted=False,
        answers=(("Agent summary", result.reason or _HELD_FALLBACK),),
    )
