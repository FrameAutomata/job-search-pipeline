"""Contract for the deterministic ATS fill planner (Phase 2 of the apply ladder).

The planner turns a parsed live form + canonical answers into a batched fill
plan, decides which fields must escalate to the agent tier, and locates (but
never actions) the submit control. It is the fast path that skips the per-field
perceive-think-act loop for known ATS forms.

The fixture is a condensed but faithful Greenhouse application built from the
2026-07-13 Clover Health capture, exercising every widget recipe plus the
escalation and idempotence edges the ladder depends on:

  - First Name   already filled with the right value            -> SKIP
  - Last Name    empty textbox                                  -> TEXT
  - Location     typeahead, [invalid]                           -> TYPEAHEAD
  - legal right  select showing "Yes" but still [invalid]       -> SELECT (refill)
  - sponsorship  empty select                                   -> SELECT
  - base salary  textbox                                        -> TEXT
  - Resume/CV*   group wrapping an Attach button                -> UPLOAD (button ref)
  - Country      select with a rule but NO answer               -> unmapped(no-answer)
  - Why us?*     required free-text the map doesn't cover        -> unmapped(no-rule)
  - Preferred    optional, unmapped, not required               -> ignored
  - Submit       located, never actioned
"""

import re

import pytest

from pipeline.ats_fill import (
    SELECT,
    TEXT,
    TYPEAHEAD,
    UPLOAD,
    detect_ats,
    greenhouse_map,
    plan_fill,
)
from pipeline.openclaw_browser import parse_snapshot

GREENHOUSE_FORM = """\
- form [ref=e0]:
  - textbox "First Name" [ref=e1]: Thomas
  - textbox "Last Name" [ref=e2]
  - textbox "Email" [ref=e3]
  - combobox "Location (City)" [invalid] [ref=e4]
  - combobox "Do you have a legal right to work in the US?" [invalid] [ref=e5]: "Yes"
  - combobox "Will you now or in the future require immigration sponsorship in the United States?" [invalid] [ref=e6]
  - textbox "Please indicate the base salary you expect for the role that you are applying for:" [ref=e7]
  - group "Resume/CV*" [ref=e8]:
    - paragraph [ref=e80]: Attach a file
    - button "Attach" [ref=e9]
  - combobox "Country" [ref=e10]
  - textbox "Why do you want to work here?*" [ref=e11]
  - textbox "Preferred Name" [ref=e12]
  - button "Submit application" [ref=e13]
"""

ANSWERS = {
    "first_name": "Thomas",
    "last_name": "Thirlwall",
    "email": "thomas.thirlwall.dev@gmail.com",
    "location_city": "Dallas, Texas",
    "work_authorization": "Yes",
    "sponsorship": "No",
    "salary_expectation": "150000",
    "resume": r"C:\Users\Corbi\resume.pdf",
    # deliberately no "country" answer
}


@pytest.fixture
def plan():
    return plan_fill(parse_snapshot(GREENHOUSE_FORM), greenhouse_map(), ANSWERS)


def _by_ref(plan):
    return {a.ref: a for a in plan.actions}


# ── ATS detection ────────────────────────────────────────────────────────────

def test_detect_greenhouse_from_url():
    assert detect_ats("https://job-boards.greenhouse.io/cloverhealth/jobs/8031845") == "greenhouse"
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"


def test_detect_non_greenhouse_is_none():
    assert detect_ats("https://www.linkedin.com/jobs/view/4431893371") is None


# ── widget recipes ───────────────────────────────────────────────────────────

def test_empty_textbox_gets_text_action(plan):
    a = _by_ref(plan)["e2"]
    assert (a.widget, a.value, a.answer_key) == (TEXT, "Thirlwall", "last_name")


def test_typeahead_widget_for_location(plan):
    a = _by_ref(plan)["e4"]
    assert (a.widget, a.value) == (TYPEAHEAD, "Dallas, Texas")


def test_select_widget_for_dropdown(plan):
    a = _by_ref(plan)["e6"]
    assert (a.widget, a.value, a.answer_key) == (SELECT, "No", "sponsorship")


def test_upload_targets_attach_button_inside_group(plan):
    # the rule matches the "Resume/CV*" group; the action must target the
    # Attach button within it (resolved via SnapshotIndex.within), not the group
    a = _by_ref(plan)["e9"]
    assert a.widget == UPLOAD
    assert a.value == r"C:\Users\Corbi\resume.pdf"
    assert "e8" not in _by_ref(plan)  # never the group ref


# ── idempotence & invalid refill ─────────────────────────────────────────────

def test_already_filled_field_is_skipped(plan):
    # First Name already holds the target value and is not [invalid] -> no action
    assert "e1" not in _by_ref(plan)


def test_invalid_field_refilled_even_when_value_matches(plan):
    # legal-right-to-work shows "Yes" (our answer) but is [invalid]: custom select
    # widgets don't register typed text, so it must be re-actioned anyway
    a = _by_ref(plan)["e5"]
    assert (a.widget, a.value) == (SELECT, "Yes")


# ── escalation ───────────────────────────────────────────────────────────────

def test_mapped_field_without_answer_escalates(plan):
    country = [u for u in plan.unmapped if "Country" in u.label]
    assert country and country[0].reason == "no-answer"
    assert "e10" not in _by_ref(plan)  # not actioned with an empty value


def test_required_unmapped_field_escalates(plan):
    why = [u for u in plan.unmapped if u.label.startswith("Why do you want")]
    assert why and why[0].reason == "no-rule"


def test_optional_unmapped_field_is_ignored(plan):
    # "Preferred Name" has no rule and no required (*) marker -> neither actioned
    # nor escalated
    assert "e12" not in _by_ref(plan)
    assert not any("Preferred Name" in u.label for u in plan.unmapped)


# ── submit gate ──────────────────────────────────────────────────────────────

def test_submit_located_but_never_actioned(plan):
    assert plan.submit_ref == "e13"
    assert "e13" not in _by_ref(plan)


def test_plan_carries_snapshot_id():
    # the plan's refs are only meaningful within their capture; Phase 3 uses
    # this to detect a plan built against a superseded snapshot
    index = parse_snapshot(GREENHOUSE_FORM)
    p = plan_fill(index, greenhouse_map(), ANSWERS)
    assert p.snapshot_id and p.snapshot_id == index.snapshot_id


# ── ordering & completeness ──────────────────────────────────────────────────

def test_actions_in_document_order(plan):
    refs = [a.ref for a in plan.actions]
    assert refs == ["e2", "e3", "e4", "e5", "e6", "e7", "e9"]


def test_absent_field_produces_no_action_or_escalation():
    # a form missing a mapped field must not fabricate an action or an escalation
    small = parse_snapshot('- form:\n  - textbox "Last Name" [ref=e1]\n')
    p = plan_fill(small, greenhouse_map(), ANSWERS)
    assert [a.ref for a in p.actions] == ["e1"]
    assert p.unmapped == []
    assert p.submit_ref is None


def test_map_labels_match_real_greenhouse_noise():
    # trailing "*" and the long question labels must match the map's patterns
    gm = greenhouse_map()
    assert any(r.label.search("Resume/CV*") for r in gm.rules)
    assert any(r.label.search("Do you have a legal right to work in the US?") for r in gm.rules)


# ── review fixes: never silently drop a needed field ─────────────────────────

def _rule_for(answer_key):
    return next(r for r in greenhouse_map().rules if r.answer_key == answer_key)


def test_upload_with_no_attach_button_escalates():
    # a Resume group whose Attach control can't be located must escalate, not
    # produce a résumé-less plan with an empty unmapped list
    idx = parse_snapshot('- group "Resume/CV*" [ref=e1]:\n  - paragraph [ref=e2]: Drop a file here\n')
    p = plan_fill(idx, greenhouse_map(), ANSWERS)
    assert p.actions == []
    assert [(u.label, u.reason) for u in p.unmapped] == [("Resume/CV*", "unresolved")]


def test_upload_skipped_when_file_already_attached():
    # the real Clover group shows a "Remove file" button once a résumé is up;
    # re-planning must not re-attach it
    idx = parse_snapshot(
        '- group "Resume/CV*" [ref=e1]:\n'
        "  - paragraph [ref=e2]: Thomas_resume.pdf\n"
        '  - button "Remove file" [ref=e3]\n'
        '  - button "Attach" [ref=e4]\n'
    )
    p = plan_fill(idx, greenhouse_map(), ANSWERS)
    assert p.actions == []
    assert p.unmapped == []


def test_required_non_text_control_escalates():
    # a required checkbox/radiogroup the map doesn't cover must reach the agent
    idx = parse_snapshot(
        "- form:\n"
        '  - checkbox "I certify the above is accurate*" [ref=e1]\n'
        '  - radiogroup "Gender*" [ref=e2]\n'
    )
    p = plan_fill(idx, greenhouse_map(), ANSWERS)
    reasons = {u.label: u.reason for u in p.unmapped}
    assert reasons["I certify the above is accurate*"] == "no-rule"
    assert reasons["Gender*"] == "no-rule"


def test_empty_string_answer_escalates_not_clobbers():
    idx = parse_snapshot('- form:\n  - textbox "Last Name" [ref=e1]: existing\n')
    p = plan_fill(idx, greenhouse_map(), {"last_name": ""})
    assert p.actions == []  # must NOT overwrite "existing" with ""
    assert [(u.label, u.reason) for u in p.unmapped] == [("Last Name", "no-answer")]


def test_matched_field_without_ref_escalates():
    idx = parse_snapshot('- form:\n  - textbox "Last Name"\n')  # no [ref=eN]
    p = plan_fill(idx, greenhouse_map(), {"last_name": "Thirlwall"})
    assert p.actions == []
    assert [(u.label, u.reason) for u in p.unmapped] == [("Last Name", "unresolved")]


# ── review fixes: map patterns vs real Greenhouse phrasings ──────────────────

def test_location_pattern_excludes_relocation():
    loc = _rule_for("location_city")
    assert loc.label.search("Location (City)")
    assert not loc.label.search("Are you willing to relocate?")


def test_salary_pattern_matches_common_phrasings():
    sal = _rule_for("salary_expectation")
    for label in ("Please indicate the base salary you expect:",
                  "What are your salary expectations?*",
                  "Desired compensation*"):
        assert sal.label.search(label), label


def test_detect_greenhouse_without_scheme():
    assert detect_ats("job-boards.greenhouse.io/cloverhealth/jobs/8031845") == "greenhouse"
