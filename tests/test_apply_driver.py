"""Contract for the apply-ladder driver (Phase 4d) — the end-user entrypoint.

run_apply_ladder glues detect_ats → map → compile_answers → run_apply and
returns a human-readable ApplyReport. Tests inject a FakeBrowser (the same
stateful-form fake shape as the Phase 3 suite) so the whole vertical runs
without a live browser.
"""

import re
from dataclasses import dataclass

import pytest

from pipeline.apply_driver import ApplyReport, _wall, map_for, run_apply_ladder
from pipeline.openclaw_browser import parse_snapshot

PROFILE = """\
## Identity & contact
- **Name:** Thomas Thirlwall
- **Contact:** thomas.thirlwall.dev@gmail.com · +1 (956) 525-3015 · Dallas, TX

## Standing answers
- **Work authorization:** US Citizen — no sponsorship required
- **Location:** Dallas, Texas (CST) — Remote (US)
"""

SEEKER_PROFILE = """\
## Identity & contact
- **Name:** Priya Kumar
- **Contact:** priya@x.io · +1 (555) 222-3333

## Standing answers
- **Work authorization:** On F-1 OPT in the US; will require H-1B sponsorship
"""

GH = "https://job-boards.greenhouse.io/acme/jobs/1"


def legal_right_form(country):
    return FakeBrowser([
        FF("e1", "textbox", "First Name"),
        FF("e3", "combobox", f"Do you have a legal right to work in {country}?",
           invalid=True, required=True),
    ])


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

    def __init__(self, fields, submit="e_sub", wall=None):
        self.fields = list(fields)
        self.submit = submit
        self.wall = wall  # None | "cloudflare" | "login" — renders a wall instead
        self.opened = None

    def open(self, url):
        self.opened = url

    def snapshot(self):
        if self.wall == "cloudflare":
            return parse_snapshot('- heading "Just a moment..." [level=1] [ref=eb]\n')
        if self.wall == "login":
            return parse_snapshot(
                '- form [ref=e0]:\n  - textbox "Email" [ref=e1]\n  - textbox "Password" [ref=e2]\n')

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

# ── walls: detect + notify (don't silently move on) ──────────────────────────

def test_wall_detects_cloudflare():
    snap = parse_snapshot('- heading "Just a moment..." [level=1] [ref=eb]\n')
    assert _wall(snap) is not None


def test_wall_detects_login_password_field():
    snap = parse_snapshot('- form:\n  - textbox "Password" [ref=e2]\n')
    reason = _wall(snap)
    assert reason and "sign" in reason.lower()


def test_wall_absent_on_a_normal_form():
    assert _wall(full_form().snapshot()) is None


class Notifier:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message):
        self.calls.append((title, message))


def test_cloudflare_wall_fires_a_notification():
    n = Notifier()
    b = full_form()
    b.wall = "cloudflare"
    rep = run_apply_ladder(GH, PROFILE, browser=b, notifier=n)
    assert rep.status == "escalated-human" and rep.blocker
    assert len(n.calls) == 1
    title, message = n.calls[0]
    assert "wall" in (title + message).lower() or "captcha" in (title + message).lower()


def test_login_wall_fires_a_notification():
    n = Notifier()
    b = full_form()
    b.wall = "login"
    rep = run_apply_ladder(GH, PROFILE, browser=b, notifier=n)
    assert rep.blocker and "sign" in rep.blocker.lower()
    assert n.calls  # the user is told to take over, not silently skipped


def test_clean_run_does_not_notify():
    n = Notifier()
    run_apply_ladder(GH, PROFILE, browser=full_form(), notifier=n)
    assert n.calls == []  # a ready-to-submit form is not an interruption


def test_notifier_is_optional():
    # no notifier passed → no crash, wall still reported in the outcome
    b = full_form()
    b.wall = "cloudflare"
    rep = run_apply_ladder(GH, PROFILE, browser=b)
    assert rep.blocker


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


# ── country-aware work authorization ─────────────────────────────────────────

def test_us_work_auth_fills_yes_for_us_citizen():
    b = legal_right_form("the US")
    rep = run_apply_ladder(GH, PROFILE, browser=b)
    legal = next(f for f in b.fields if f.label.startswith("Do you"))
    assert legal.value == "Yes"
    assert rep.status == "ready-to-submit"


def test_foreign_work_auth_role_is_skipped_for_us_only_candidate():
    # the acceptance-run bug: a Canada work-auth question must NOT be answered
    # "Yes"; for a US-only, no-sponsorship candidate the whole role is skipped
    b = legal_right_form("Canada")
    rep = run_apply_ladder(GH, PROFILE, browser=b)
    assert rep.status == "skipped"
    assert "Canada" in rep.message
    legal = next(f for f in b.fields if f.label.startswith("Do you"))
    assert legal.value == ""  # never filled a false answer
    assert not rep.ok


def test_sponsorship_seeker_applies_to_foreign_role_and_answers_truthfully():
    b = legal_right_form("Canada")
    rep = run_apply_ladder(GH, SEEKER_PROFILE, browser=b)
    assert rep.status != "skipped"  # a seeker WANTS this role
    legal = next(f for f in b.fields if f.label.startswith("Do you"))
    assert legal.value == "No"  # honest: not yet authorized in Canada
