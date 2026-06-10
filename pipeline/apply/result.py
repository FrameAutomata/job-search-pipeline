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
LOGIN_ISSUE = "login_issue"    # not signed in / session expired
NOT_ELIGIBLE = "not_eligible"  # a screening answer disqualified the candidate
SKIPPED = "skipped"            # user/queue skipped it (not an attempt)

# Failures that will never succeed on retry — recording them as permanent keeps
# the worker from re-attempting the same dead job every run.
PERMANENT_FAILURES: frozenset[str] = frozenset({
    EXPIRED, CAPTCHA, LOGIN_ISSUE, NOT_ELIGIBLE,
    "not_eligible_location", "not_eligible_work_auth",
    "already_applied", "not_easy_apply", "no_easy_apply_button",
})


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
