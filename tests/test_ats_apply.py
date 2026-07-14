"""Contract for the apply-ladder escalation state machine (Phase 3).

`run_apply` drives one application through deterministic → agent → human over an
abstract browser. These tests use a FakeBrowser that models a live form: acting
on a field sets its value and clears its `[invalid]` flag (so re-planning
converges), unless the field is "sticky" (value set but stays invalid — the
custom-widget case the deterministic tier can't finish). Walls, dynamic reveals,
and agent resolution are injected. The one inviolable rule: the submit control
is located but never actioned.
"""

import re
from dataclasses import dataclass, field

import pytest

from pipeline.ats_apply import (
    ESCALATED_HUMAN,
    NO_FORM,
    READY_TO_SUBMIT,
    run_apply,
)
from pipeline.ats_fill import greenhouse_map

SUBMIT = "e_sub"
ANSWERS = {"first_name": "Thomas", "last_name": "Thirlwall", "work_authorization": "Yes"}


@dataclass
class FF:
    """A fake form field."""

    ref: str
    role: str
    label: str
    value: str = ""
    invalid: bool = False
    required: bool = False
    filter_only: bool = False  # combobox: value is uncommitted filter text


class FakeBrowser:
    """A scriptable live form. `act` fills a field (clearing invalid unless the
    field is sticky); `on_act(self, action)` lets a test mutate form state after
    an action (reveal a field, raise a wall). `snapshot` renders the aria tree."""

    def __init__(self, fields, *, submit=SUBMIT, sticky=(), on_act=None):
        self.fields = list(fields)
        self.submit = submit
        self.sticky = set(sticky)
        self.on_act = on_act
        self.blocked = False
        self.acts = []
        self.snaps = 0

    def snapshot(self):
        from pipeline.openclaw_browser import parse_snapshot

        self.snaps += 1
        return parse_snapshot(self._render())

    def act(self, action):
        self.acts.append(action)
        for f in self.fields:
            if f.ref == action.ref:
                f.value = action.value
                if f.ref not in self.sticky:
                    f.invalid = False
        if self.on_act:
            self.on_act(self, action)

    def _render(self):
        lines = ["- form [ref=e0]:"]
        if self.blocked:
            lines.append('  - heading "Just a moment..." [level=1] [ref=eb]')
            return "\n".join(lines) + "\n"
        for f in self.fields:
            attrs = " [invalid]" if f.invalid else ""
            star = "*" if f.required else ""
            if f.role == "combobox":
                # react-select shape (live-capture): a committed value renders as
                # a SIBLING node; uncommitted filter text sits on the combobox
                # itself and must NOT count as satisfaction.
                lines.append(f"  - generic [ref=w{f.ref}]:")
                if f.value and not f.filter_only:
                    lines.append(f'    - generic [ref=v{f.ref}]: "{f.value}"')
                ft = f': "{f.value}"' if f.value and f.filter_only else ""
                lines.append(f'    - {f.role} "{f.label}{star}"{attrs} [ref={f.ref}]{ft}')
            else:
                val = f": {f.value}" if f.value else ""
                lines.append(f'  - {f.role} "{f.label}{star}"{attrs} [ref={f.ref}]{val}')
        if self.submit:
            lines.append(f'  - button "Submit application" [ref={self.submit}]')
        return "\n".join(lines) + "\n"

    def acted_refs(self):
        return [a.ref for a in self.acts]


def cloudflare_wall(snap):
    return "cloudflare challenge" if snap.find("heading", re.compile("just a moment", re.I)) else None


def agent_fills(*labels):
    """An agent double that resolves the named fields (by label, ignoring *)."""
    want = {l.rstrip("*") for l in labels}

    def agent(browser, unresolved):
        for f in browser.fields:
            if f.label.rstrip("*") in want:
                f.value = f.value or "AGENT"
                f.invalid = False

    return agent


def standard_form():
    return FakeBrowser([
        FF("e1", "textbox", "First Name"),
        FF("e2", "textbox", "Last Name"),
        FF("e3", "combobox", "Do you have a legal right to work in the US?", invalid=True),
    ])


def run(browser, **kw):
    return run_apply(browser, greenhouse_map(), ANSWERS, **kw)


# ── happy path ───────────────────────────────────────────────────────────────

def test_happy_path_ready_to_submit():
    b = standard_form()
    out = run(b)
    assert out.status == READY_TO_SUBMIT
    assert out.submit_ref == SUBMIT
    assert out.escalated == []


def test_deterministic_fields_are_filled():
    b = standard_form()
    run(b)
    assert {"e1", "e2", "e3"} <= set(b.acted_refs())


def test_submit_is_never_actioned():
    b = standard_form()
    run(b)
    assert SUBMIT not in b.acted_refs()


def test_re_snapshots_after_acting():
    # the machine must re-observe the form, not trust its opening snapshot
    b = standard_form()
    run(b)
    assert b.snaps >= 2


def test_idempotent_no_refill_of_correct_field():
    b = FakeBrowser([FF("e1", "textbox", "First Name", value="Thomas")])
    run(b)
    assert "e1" not in b.acted_refs()  # already correct → left alone


# ── verification & convergence ───────────────────────────────────────────────

def test_replans_against_revealed_field():
    # filling First Name reveals Last Name; the machine must re-snapshot, see it,
    # and fill it — proving it re-plans against fresh state, not the first plan
    def reveal(b, action):
        if action.ref == "e1" and not any(f.ref == "e2" for f in b.fields):
            b.fields.append(FF("e2", "textbox", "Last Name"))

    b = FakeBrowser([FF("e1", "textbox", "First Name")], on_act=reveal)
    out = run(b)
    assert out.status == READY_TO_SUBMIT
    assert {"e1", "e2"} <= set(b.acted_refs())


def test_sticky_invalid_bounded_by_max_rounds():
    # a field that never clears invalid must not loop forever
    b = FakeBrowser([FF("e3", "combobox", "Do you have a legal right to work in the US?",
                        invalid=True)], sticky=["e3"])
    out = run(b, max_rounds=3)
    assert out.rounds <= 3
    assert out.status == ESCALATED_HUMAN


# ── agent tier ───────────────────────────────────────────────────────────────

def test_sticky_field_resolved_by_agent():
    b = FakeBrowser([FF("e3", "combobox", "Do you have a legal right to work in the US?",
                        invalid=True)], sticky=["e3"])
    out = run(b, agent=agent_fills("Do you have a legal right to work in the US?"))
    assert out.status == READY_TO_SUBMIT


def test_agent_resolves_required_unmapped_field():
    # a required field no rule covers goes to the agent
    b = FakeBrowser([FF("e9", "textbox", "Why do you want to work here?", required=True)])
    out = run(b, agent=agent_fills("Why do you want to work here?"))
    assert out.status == READY_TO_SUBMIT


def test_agent_resolves_required_unmapped_combobox():
    # a required no-rule react-select: the agent commits it (value on the sibling
    # node, combobox's own value empty). _still_unresolved must judge it satisfied
    # by the committed value, not by the empty combobox — else false escalation.
    b = FakeBrowser([FF("e1", "combobox", "Gender", required=True)])
    out = run(b, agent=agent_fills("Gender"))
    assert out.status == READY_TO_SUBMIT


def test_uncommitted_combobox_filter_text_still_escalates():
    # the agent leaves only filter text (no committed option) → not satisfied
    def type_filter_only(browser, unresolved):
        for f in browser.fields:
            if f.label.rstrip("*") == "Gender":
                f.filter_only = True
                f.value = "Ma"  # filter text, not a commitment

    b = FakeBrowser([FF("e1", "combobox", "Gender", required=True)])
    out = run(b, agent=type_filter_only)
    assert out.status == ESCALATED_HUMAN


def test_unmapped_without_agent_escalates_to_human():
    b = FakeBrowser([FF("e9", "textbox", "Why do you want to work here?", required=True)])
    out = run(b)  # no agent
    assert out.status == ESCALATED_HUMAN
    assert any("Why do you want" in u.label for u in out.escalated)


def test_unresolved_after_agent_escalates_to_human():
    # the agent is invoked but fixes nothing → human
    b = FakeBrowser([FF("e9", "textbox", "Why do you want to work here?", required=True)])
    out = run(b, agent=agent_fills("some other field"))
    assert out.status == ESCALATED_HUMAN
    assert any("Why do you want" in u.label for u in out.escalated)


# ── human tier / walls ───────────────────────────────────────────────────────

def test_wall_up_front_escalates_immediately():
    b = standard_form()
    b.blocked = True
    out = run(b, wall=cloudflare_wall)
    assert out.status == ESCALATED_HUMAN
    assert out.blocker == "cloudflare challenge"
    assert b.acts == []  # nothing filled behind a wall
    assert SUBMIT not in b.acted_refs()


def test_wall_mid_fill_escalates():
    def block_after_first(b, action):
        b.blocked = True

    b = FakeBrowser([FF("e1", "textbox", "First Name"),
                     FF("e2", "textbox", "Last Name")], on_act=block_after_first)
    out = run(b, wall=cloudflare_wall)
    assert out.status == ESCALATED_HUMAN
    assert SUBMIT not in b.acted_refs()


# ── degenerate ───────────────────────────────────────────────────────────────

def test_no_form_returns_no_form():
    b = FakeBrowser([], submit=None)
    out = run(b)
    assert out.status == NO_FORM
    assert b.acts == []


# ── review fixes: post-agent handling ────────────────────────────────────────

def test_agent_revealed_mapped_field_is_filled():
    # the agent clears a gate and a standard mapped field renders; the
    # deterministic tier must fill it on re-entry, not escalate it (fix #2)
    def agent(browser, unresolved):
        for f in browser.fields:
            if f.label.rstrip("*").startswith("Why"):
                f.value, f.invalid = "AGENT", False
        browser.fields.append(FF("e2", "textbox", "Last Name"))  # revealed section

    b = FakeBrowser([FF("e9", "textbox", "Why do you want to work here?", required=True)])
    out = run(b, agent=agent)
    assert out.status == READY_TO_SUBMIT
    assert "e2" in b.acted_refs()


def test_agent_revealed_unmapped_required_is_not_dropped():
    # the agent resolves the blocker but surfaces a NEW required no-rule field;
    # it must escalate, not vanish into a false READY (fix #1)
    def agent(browser, unresolved):
        for f in browser.fields:
            f.invalid = False  # clear the sticky blocker
        browser.fields.append(FF("e9", "textbox", "Why do you want to work here?", required=True))

    b = FakeBrowser([FF("e3", "combobox", "Do you have a legal right to work in the US?",
                        invalid=True)], sticky=["e3"])
    out = run(b, agent=agent)
    assert out.status == ESCALATED_HUMAN
    assert any("Why do you want" in u.label for u in out.escalated)


def test_act_exception_escalates_not_crashes():
    class Boom(FakeBrowser):
        def act(self, action):
            if action.ref == "e1":
                raise RuntimeError("stale ref")
            super().act(action)

    b = Boom([FF("e1", "textbox", "First Name")])
    out = run(b)  # must return an outcome, not propagate
    assert out.status == ESCALATED_HUMAN


def test_duplicate_label_empty_field_not_masked():
    # two same-label required fields; the agent fills only one — the other must
    # still escalate rather than be masked behind its filled twin (fix #4)
    def agent(browser, unresolved):
        for f in browser.fields:
            if f.label.rstrip("*") == "Address":
                f.value, f.invalid = "123 Main St", False
                break

    b = FakeBrowser([FF("e1", "textbox", "Address", required=True),
                     FF("e2", "textbox", "Address", required=True)])
    out = run(b, agent=agent)
    assert out.status == ESCALATED_HUMAN


def test_prefilled_unmapped_required_satisfied_without_agent():
    # a required no-rule field already holding a value (autofill / a resumed
    # session) is satisfied; the outcome must not depend on whether an agent
    # callable was injected — form state decides, symmetrically on both paths
    def prefilled():
        return FakeBrowser([FF("e9", "textbox", "Why do you want to work here?",
                               value="Because I love the mission.", required=True)])

    no_agent = run(prefilled())
    noop_agent = run(prefilled(), agent=lambda b, u: None)
    assert no_agent.status == noop_agent.status == READY_TO_SUBMIT


def test_escalation_payload_not_double_counted():
    # a sticky field the agent can't fix appears once in escalated, not twice (fix #5)
    b = FakeBrowser([FF("e3", "combobox", "Do you have a legal right to work in the US?",
                        invalid=True)], sticky=["e3"])
    out = run(b, agent=agent_fills("some other field"))
    assert out.status == ESCALATED_HUMAN
    assert len(out.escalated) == 1
