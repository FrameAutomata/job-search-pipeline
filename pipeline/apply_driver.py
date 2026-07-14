"""Apply-ladder driver — the end-user entrypoint (Phase 4d stub).

Glues the pieces into one call the CLI (`run.ps1 --apply-ladder <url>`) invokes:
detect the ATS from the URL, load the field map, compile answers from PROFILE.md,
drive the user's logged-in browser through the ladder, and return a
human-readable report of what was filled and what still needs the human. The
browser and profile source are injected so this is testable without a live
browser; `apply_to_url` wires the real OpenClawBrowser in production.

Deterministic + human only for now: no agent tier is passed to run_apply, so
anything the map can't finish is reported for the human (who has the form open).
The submit is never actioned — the human reviews and submits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.apply_answers import (
    compile_answers,
    country_of,
    parse_work_auth,
    resolve_work_auth,
)
from pipeline.ats_apply import (
    ESCALATED_HUMAN,
    NO_FORM,
    READY_TO_SUBMIT,
    run_apply,
)
from pipeline.ats_fill import FieldMap, detect_ats, greenhouse_map
from pipeline.openclaw_browser import SnapshotIndex

_MAPS = {"greenhouse": greenhouse_map}
_WALL = re.compile(r"just a moment|verify you are human|checking your browser", re.I)


@dataclass
class ApplyReport:
    url: str
    status: str            # run_apply status, or "unsupported-ats" / "no-profile"
    filled: list[str] = field(default_factory=list)   # field labels filled
    needs_you: list[str] = field(default_factory=list)  # "label (reason)" lines
    blocker: str | None = None
    submit_ref: str | None = None
    message: str = ""      # one-line human summary

    @property
    def ok(self) -> bool:
        return self.status == "ready-to-submit"


def map_for(ats: str | None) -> FieldMap | None:
    """The field map for a detect_ats() name, or None if unsupported."""
    builder = _MAPS.get(ats or "")
    return builder() if builder else None


def _wall(index: SnapshotIndex) -> str | None:
    if index.find("heading", _WALL):
        return "verification wall (CAPTCHA / sign-in)"
    return None


def _dedup(labels) -> list[str]:
    seen, out = set(), []
    for label in labels:
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def _resolve_work_auth(index, profile_md, answers) -> str | None:
    """Set country-correct work_authorization / sponsorship answers for the live
    form's work-auth questions. Returns a skip reason if the role requires
    authorization the candidate lacks and won't seek (a deal-breaker), else None.
    Country-aware so a US answer never lands on a foreign question."""
    policy = parse_work_auth(profile_md)
    for el in index.elements:
        if el.role != "combobox" or not el.label:
            continue
        ans = resolve_work_auth(el.label, policy)
        if ans is None:
            continue
        if ans.dealbreaker:
            return (f"Skipped: this role needs work authorization in {country_of(el.label)}, "
                    "which you don't have and aren't seeking sponsorship for.")
        if re.search(r"sponsor", el.label, re.I):
            answers["sponsorship"] = ans.sponsorship
        else:
            answers["work_authorization"] = ans.legal_right
    return None


def run_apply_ladder(
    url: str,
    profile_md: str,
    *,
    browser,
    resume_path: str | None = None,
    open_url: bool = True,
) -> ApplyReport:
    """Drive one application and return a report. Orchestration over the injected
    browser; failures surface as report fields, never exceptions to the caller."""
    field_map = map_for(detect_ats(url))
    if field_map is None:
        host = re.sub(r"^https?://", "", url).split("/", 1)[0]
        return ApplyReport(url, "unsupported-ats",
                           message=f"{host}: unsupported ATS — apply by hand for now.")

    answers = compile_answers(profile_md, resume_path=resume_path)
    if open_url:
        browser.open(url)

    # Country-aware work authorization: resolve the live form's work-auth
    # questions before filling, and skip the role entirely if it's a deal-breaker.
    skip = _resolve_work_auth(browser.snapshot(), profile_md, answers)
    if skip:
        return ApplyReport(url, "skipped", message=skip)

    outcome = run_apply(browser, field_map, answers, wall=_wall)

    filled = _dedup(a.label for a in outcome.filled)
    needs_you = [f"{u.label} ({u.reason})" for u in outcome.escalated]

    if outcome.status == READY_TO_SUBMIT:
        message = f"Filled {len(filled)} field(s). Review the form and submit."
    elif outcome.status == NO_FORM:
        message = "Filled nothing — no application form found on the page."
    elif outcome.blocker:
        message = (f"Filled {len(filled)} field(s), then hit a wall: {outcome.blocker}. "
                   "Take over in your browser.")
    else:
        message = (f"Filled {len(filled)} field(s); {len(needs_you)} need you. "
                   "Complete them in your browser, then submit.")

    return ApplyReport(url, outcome.status, filled, needs_you,
                       blocker=outcome.blocker, submit_ref=outcome.submit_ref, message=message)


def apply_to_url(url: str, *, profile_path: str, resume_path: str | None = None) -> ApplyReport:
    """Production wiring: the real OpenClaw relay + PROFILE.md from disk."""
    from pipeline.openclaw_client import OpenClawBrowser

    profile_md = Path(profile_path).read_text(encoding="utf-8")
    return run_apply_ladder(url, profile_md, browser=OpenClawBrowser(), resume_path=resume_path)
