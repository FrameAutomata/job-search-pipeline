"""Contract for the apply-ladder driver (Phase 4d) — the end-user entrypoint.

run_apply_ladder glues detect_ats → map → compile_answers → run_apply and
returns a human-readable ApplyReport. Tests inject a FakeBrowser (the same
stateful-form fake shape as the Phase 3 suite) so the whole vertical runs
without a live browser.
"""

import re
from dataclasses import dataclass

import pytest

from pipeline.apply_driver import ApplyReport, map_for, run_apply_ladder

PROFILE = """\
## Identity & contact
- **Name:** Thomas Thirlwall
- **Contact:** thomas.thirlwall.dev@gmail.com · +1 (956) 525-3015 · Dallas, TX

## Standing answers
- **Work authorization:** US Citizen — no sponsorship required
- **Location:** Dallas, Texas (CST) — Remote (US)
"""

GH = "https://job-boards.greenhouse.io/acme/jobs/1"


@dataclass
class FF:
    ref: str
    role: str
    label: str
    value: str = ""
    invalid: bool = False
    required: bool = False


class FakeBrowser:
    """Stateful form over the driver's Browser calls. Comboboxes render the
    react-select committed-sibling shape (matches production)."""

    def __init__(self, fields, submit="e_sub"):
        self.fields = list(fields)
        self.submit = submit
        self.opened = None

    def open(self, url):
        self.opened = url

    def snapshot(self):
        from pipeline.openclaw_browser import parse_snapshot

        lines = ["- form [ref=e0]:"]
        for f in self.fields:
            star = "*" if f.required else ""
            inv = " [invalid]" if f.invalid else ""
            if f.role == "combobox":
                lines.append(f"  - generic [ref=w{f.ref}]:")
                if f.value:
                    lines.append(f'    - generic [ref=v{f.ref}]: "{f.value}"')
                lines.append(f'    - {f.role} "{f.label}{star}"{inv} [ref={f.ref}]')
            else:
                val = f": {f.value}" if f.value else ""
                lines.append(f'  - {f.role} "{f.label}{star}"{inv} [ref={f.ref}]{val}')
        lines.append(f'  - button "Submit application" [ref={self.submit}]')
        return parse_snapshot("\n".join(lines) + "\n")

    def act(self, action):
        for f in self.fields:
            if f.ref == action.ref:
                f.value, f.invalid = action.value, False


def full_form():
    return FakeBrowser([
        FF("e1", "textbox", "First Name"),
        FF("e2", "textbox", "Last Name"),
        FF("e3", "combobox", "Do you have a legal right to work in the US?", invalid=True),
    ])


# ── registry / detection ─────────────────────────────────────────────────────

def test_map_for_greenhouse():
    assert map_for("greenhouse") is not None


def test_unsupported_ats_reports_cleanly():
    b = full_form()
    rep = run_apply_ladder("https://workday.example.com/job/1", PROFILE, browser=b)
    assert rep.status == "unsupported-ats"
    assert b.opened is None  # never touched the browser
    assert "workday" in rep.message.lower() or "unsupported" in rep.message.lower()


# ── happy path ───────────────────────────────────────────────────────────────

def test_full_form_fills_and_reports_ready():
    b = full_form()
    rep = run_apply_ladder(GH, PROFILE, browser=b)
    assert rep.ok and rep.status == "ready-to-submit"
    assert b.opened == GH
    assert {"First Name", "Last Name"} <= set(rep.filled)
    assert rep.needs_you == []
    assert rep.submit_ref == "e_sub"


def test_react_select_is_committed_from_profile():
    b = full_form()
    run_apply_ladder(GH, PROFILE, browser=b)
    legal = next(f for f in b.fields if f.label.startswith("Do you"))
    assert legal.value == "Yes" and not legal.invalid  # work_authorization applied + committed


def test_open_can_be_skipped_when_tab_already_loaded():
    b = full_form()
    run_apply_ladder(GH, PROFILE, browser=b, open_url=False)
    assert b.opened is None


# ── human hand-off ───────────────────────────────────────────────────────────

def test_unanswered_field_is_reported_for_the_human():
    # a required field the profile can't answer → needs_you, not silently dropped
    b = FakeBrowser([FF("e1", "textbox", "Why do you want to work here?", required=True)])
    rep = run_apply_ladder(GH, PROFILE, browser=b)
    assert rep.status == "escalated-human"
    assert any("Why do you want" in line for line in rep.needs_you)
    assert not rep.ok


def test_report_message_is_human_readable():
    b = FakeBrowser([FF("e1", "textbox", "First Name"),
                     FF("e9", "textbox", "Explain a gap*", required=True)])
    rep = run_apply_ladder(GH, PROFILE, browser=b)
    # a one-liner a person can act on: what filled, what they must do
    assert re.search(r"\bfilled\b", rep.message, re.I)
    assert "Explain a gap" in " ".join(rep.needs_you)


# ── never submits ────────────────────────────────────────────────────────────

def test_driver_never_actions_submit():
    b = full_form()
    submitted = {"hit": False}
    orig = b.act

    def watch(action):
        if action.ref == b.submit:
            submitted["hit"] = True
        orig(action)

    b.act = watch
    run_apply_ladder(GH, PROFILE, browser=b)
    assert submitted["hit"] is False
