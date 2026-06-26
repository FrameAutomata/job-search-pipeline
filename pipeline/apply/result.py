"""Apply outcome codes and permanent-failure classification.

The deterministic and agentic engines both report a `RESULT:<CODE>` string;
the worker loop maps it to a status and decides whether a retry is worthwhile.
Ported from ApplyPilot's launcher result-handling, trimmed to what the
LinkedIn Easy Apply fast-path needs (the agentic fallback in Phase 3 reuses
the same vocabulary)."""

from __future__ import annotations

from dataclasses import dataclass

# ── Result codes ────────────────────────────────────────────────────────────
# Terminal-success and known-terminal-failure outcomes. `failed:<reason>` is a
# catch-all for anything else; the reason string is free-form.
APPLIED = "applied"            # submitted (or, in review mode, filled and ready)
EXPIRED = "expired"            # posting closed / no longer accepting
CAPTCHA = "captcha"            # blocked by an unsolved challenge
LOGIN_ISSUE = "login_issue"    # not signed in / session expired, or an ATS sign-in
                               # /account wall the agent hit mid-fill. Permanent for
                               # the CLI (no human); the UI worker holds on it like
                               # NEEDS_HUMAN — a person at the browser can sign in.
NOT_ELIGIBLE = "not_eligible"  # a screening answer disqualified the candidate
SKIPPED = "skipped"            # user/queue skipped it (not an attempt)
CANCELLED = "cancelled"        # the user aborted the fill before it finished (UI review)
READY = "ready"                # agent filled the form and PARKED at review without
                               # submitting — the held state the adapter maps to an
                               # APPLIED/submitted=False result (distinct from APPLIED
                               # so "filled" can never be mistaken for "submitted")
DEFER = "defer"                # this engine is the wrong one for the role — hand off
                               # to the engine named in `deferred_to` (agent <-> the
                               # deterministic LinkedIn/Indeed fast-paths)
NEEDS_HUMAN = "needs_human"    # parked on a CAPTCHA/wall the agent couldn't clear —
                               # a person can solve it. NOT permanent: the UI holds,
                               # notifies, waits for the human, then resumes; the CLI
                               # (no human present) reports it as a failure to retry.

# A deterministic engine reports one of these — as the result code (LinkedIn) or
# the reason (Indeed) — when the role has no fast-apply form (apply-on-company-
# site): permanent for THAT engine, but also the signal to hand the role to the
# agentic catch-all instead (see apply._defer_target). The off-site company URL,
# when captured, rides along in ApplyResult.redirect_url so the agent targets it.
# LinkedIn: not_easy_apply / no_easy_apply_button. Indeed: smartapply_did_not_open.
NO_FAST_APPLY_FORM: frozenset[str] = frozenset({
    "not_easy_apply", "no_easy_apply_button", "smartapply_did_not_open",
})

# Failures that will never succeed on retry — recording them as permanent keeps
# the worker from re-attempting the same dead job every run.
PERMANENT_FAILURES: frozenset[str] = frozenset({
    EXPIRED, CAPTCHA, LOGIN_ISSUE, NOT_ELIGIBLE,
    "not_eligible_location", "not_eligible_work_auth", "already_applied",
}) | NO_FAST_APPLY_FORM


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one application attempt.

    `code` is one of the constants above (or "failed"); `reason` carries the
    detail for failures; `answers` holds the drafted field values so the UI /
    CLI can show what would be submitted in review/dry-run mode. `submitted`
    distinguishes an actually-clicked submission (auto mode) from a filled-but-
    held form (review / dry-run) — only the former should mark the tracker."""
    code: str
    reason: str = ""
    answers: tuple[tuple[str, str], ...] = ()
    submitted: bool = False
    deferred_to: str = ""   # for code==DEFER: the engine to re-dispatch this job to
    redirect_url: str = ""  # an off-site URL the next engine should target instead
                            # (e.g. Indeed's apply redirected to the company's ATS)

    @property
    def applied(self) -> bool:
        return self.code == APPLIED

    @property
    def permanent(self) -> bool:
        """True if this outcome should never be retried."""
        return self.code in PERMANENT_FAILURES or self.reason in PERMANENT_FAILURES

    def __str__(self) -> str:
        return f"{self.code}:{self.reason}" if self.reason else self.code


def failed(reason: str) -> ApplyResult:
    return ApplyResult(code="failed", reason=reason)
