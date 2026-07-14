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
  - How heard    textbox with a rule but NO answer              -> unmapped(no-answer)
  - Why us?*     required free-text the map doesn't cover        -> unmapped(no-rule)
  - Preferred    optional, unmapped, not required               -> ignored
  - Submit       located, never actioned

The react-select section below pins the committed/uncommitted widget shapes
captured live on Clover's Greenhouse board (2026-07-13): a committed select
renders its value in a SIBLING node while the combobox's own value holds only
the typed filter text — satisfaction must read the sibling, never the combobox.
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
  - textbox "How did you hear about this job?" [ref=e10]
  - textbox "Why do you want to work here?*" [ref=e11]
  - textbox "Anything else you would like us to know?" [ref=e12]
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
    # deliberately no "referral_source" answer
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

def test_mapped_field_without_answer_is_reported(plan):
    # "How did you hear" is mapped (referral_source) but ANSWERS omits it, and
    # the field is optional → reported "optional", never filled with an empty
    heard = [u for u in plan.unmapped if "How did you hear" in u.label]
    assert heard and heard[0].reason == "optional"
    assert "e10" not in _by_ref(plan)  # not actioned with an empty value


def test_required_unmapped_field_escalates(plan):
    why = [u for u in plan.unmapped if u.label.startswith("Why do you want")]
    assert why and why[0].reason == "no-rule"


def test_optional_unmapped_field_is_reported_not_actioned(plan):
    # an unmapped, empty, non-required field is not filled, but IS reported as
    # "optional" so the human can choose to fill it (nothing silently dropped)
    assert "e12" not in _by_ref(plan)
    optional = [u for u in plan.unmapped if "Anything else" in u.label]
    assert optional and optional[0].reason == "optional"


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


def test_legal_and_preferred_first_name_route_separately():
    # "First Name" gets the preferred name, "Legal First Name" the legal one —
    # they are distinct fields and must not be conflated
    snap = (
        "- form [ref=e0]:\n"
        '  - textbox "First Name" [ref=e1]\n'
        '  - textbox "Legal First Name" [ref=e2]\n'
    )
    p = plan_fill(parse_snapshot(snap), greenhouse_map(),
                  {"first_name": "Bob", "legal_first_name": "Robert"})
    by_ref = {a.ref: a.value for a in p.actions}
    assert by_ref == {"e1": "Bob", "e2": "Robert"}


def test_duplicate_first_name_fields_both_fill():
    # two plain "First Name" fields both match one rule → both filled
    snap = (
        "- form [ref=e0]:\n"
        '  - textbox "First Name" [ref=e1]\n'
        '  - textbox "Preferred First Name" [ref=e2]\n'
    )
    p = plan_fill(parse_snapshot(snap), greenhouse_map(), {"first_name": "Bob"})
    assert {a.ref for a in p.actions} == {"e1", "e2"}


def test_wrapper_group_does_not_escalate():
    # Greenhouse wraps the phone field in a required-labeled "Phone" GROUP; the
    # group is a container, not a field — it must not escalate as no-rule
    snap = (
        "- form [ref=e0]:\n"
        '  - group "Phone" [ref=e1]:\n'
        "    - generic [ref=e2]: Phone*\n"
        '    - textbox "Phone" [ref=e3]: +1 (555) 000-1111\n'
    )
    p = plan_fill(parse_snapshot(snap), greenhouse_map(), {"phone": "+1 (555) 000-1111"})
    assert not any(u.reason == "no-rule" for u in p.unmapped)


def test_required_marker_on_adjacent_label_node_escalates():
    # real Greenhouse: the required "*" rides the label node, not the control's
    # own aria label — an unmapped required field must STILL escalate, not vanish
    snap = (
        "- form [ref=e0]:\n"
        "  - generic [ref=e1]:\n"
        "    - generic [ref=e2]: Describe a hard bug*\n"
        '    - textbox "Describe a hard bug" [ref=e3]\n'
    )
    p = plan_fill(parse_snapshot(snap), greenhouse_map(), {})
    assert any(u.reason == "no-rule" and "Describe a hard bug" in u.label for u in p.unmapped)


def test_optional_field_with_no_marker_is_reported_not_required():
    snap = (
        "- form [ref=e0]:\n"
        "  - generic [ref=e1]:\n"
        "    - generic [ref=e2]: Anything else?\n"
        '    - textbox "Anything else?" [ref=e3]\n'
    )
    p = plan_fill(parse_snapshot(snap), greenhouse_map(), {})
    # no "*" marker → optional (reported), NOT required (no-rule)
    assert [(u.label, u.reason) for u in p.unmapped] == [("Anything else?", "optional")]


def test_map_covers_linkedin_and_website_fields():
    gm = greenhouse_map()
    linkedin = next((r for r in gm.rules if r.label.search("LinkedIn Profile")), None)
    website = next((r for r in gm.rules if r.label.search("Website")), None)
    assert linkedin and linkedin.answer_key == "linkedin"
    assert website and website.answer_key == "website"


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
    # a REQUIRED field (marker on the label node) with an empty answer must
    # block (no-answer) and never overwrite an existing value with ""
    idx = parse_snapshot('- form:\n  - generic [ref=e2]: Last Name*\n  - textbox "Last Name" [ref=e1]: existing\n')
    p = plan_fill(idx, greenhouse_map(), {"last_name": ""})
    assert p.actions == []
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


# ── react-select satisfaction (live-capture shapes, 2026-07-13) ──────────────
#
# Greenhouse selects are react-select: the combobox's own value is only the
# typed FILTER text; the committed value renders as a sibling generic under the
# combobox's parent. Verbatim committed shape from the live capture:
#
#   - generic:                                ← question container
#     - generic: Do you have a legal right...*   ← label node
#     - generic:
#       - log: option Yes, selected.          ← transient aria-live
#       - generic:
#         - generic: "Yes"                    ← THE COMMITTED VALUE
#         - combobox "Do you have a legal..." ← value: empty
#       - button "Clear selections" ...

def select_widget(label, *, committed=None, filter_text=None, invalid=False, ref="e5"):
    # verbatim react-select nesting: the aria-live `log` sits a level up (sibling
    # of the value container), the committed singleValue `generic` sits inside it
    # right before the combobox. Both are rendered so the fixture pins that the
    # committed lookup reads the singleValue, not the log's "option X, selected."
    inv = " [invalid]" if invalid else ""
    val = f': "{filter_text}"' if filter_text else ""
    log_line = f'      - log [ref=e30]: option {committed}, selected.\n' if committed else ""
    committed_line = f'        - generic [ref=e900]: "{committed}"\n' if committed else ""
    return (
        "- form [ref=e0]:\n"
        "  - generic [ref=e1]:\n"
        f"    - generic [ref=e2]: {label}\n"
        "    - generic [ref=e3]:\n"
        f"{log_line}"
        "      - generic [ref=e4]:\n"
        f"{committed_line}"
        f'        - combobox "{label}"{inv} [ref={ref}]{val}\n'
    )


SPONSOR = "Will you now or in the future require immigration sponsorship in the United States?"
LEGAL = "Do you have a legal right to work in the US?"


def _plan(snapshot_text, answers=ANSWERS):
    return plan_fill(parse_snapshot(snapshot_text), greenhouse_map(), answers)


def test_committed_select_matching_answer_is_skipped():
    p = _plan(select_widget(SPONSOR, committed="No"))
    assert p.actions == []


def test_committed_select_match_is_case_insensitive():
    p = _plan(select_widget(SPONSOR, committed="no"))
    assert p.actions == []


def test_committed_select_wrong_value_is_reactioned():
    p = _plan(select_widget(SPONSOR, committed="Yes"))  # answer is "No"
    assert [a.ref for a in p.actions] == ["e5"]


def test_filter_text_in_combobox_is_not_commitment():
    # the overnight bug: "Yes" typed into the combobox (filter text) without
    # selecting an option — looks like value==answer but nothing is committed
    p = _plan(select_widget(LEGAL, filter_text="Yes"))
    assert [a.ref for a in p.actions] == ["e5"]


def test_committed_but_invalid_is_reactioned():
    p = _plan(select_widget(SPONSOR, committed="No", invalid=True))
    assert [a.ref for a in p.actions] == ["e5"]


def test_typeahead_committed_prefix_before_comma_satisfies():
    # answer "Dallas, Texas" vs committed "Dallas, Texas, United States"
    p = _plan(select_widget("Location (City)", committed="Dallas, Texas, United States"))
    assert p.actions == []


def test_typeahead_committed_different_city_is_reactioned():
    p = _plan(select_widget("Location (City)", committed="Dallas, Oregon, United States"))
    assert [a.ref for a in p.actions] == ["e5"]


def test_typeahead_prefix_must_break_at_comma():
    # "Dallas, Texas" must NOT satisfy a committed "Dallas, Texasville, USA"
    p = _plan(select_widget("Location (City)", committed="Dallas, Texasville, USA"))
    assert [a.ref for a in p.actions] == ["e5"]


def test_bare_city_answer_requires_exact_not_prefix():
    # answer "Dallas" (no comma) must NOT be satisfied by a wrong "Dallas, Oregon"
    p = _plan(select_widget("Location (City)", committed="Dallas, Oregon, United States"),
              {**ANSWERS, "location_city": "Dallas"})
    assert [a.ref for a in p.actions] == ["e5"]


def test_committed_lookup_ignores_the_aria_live_log():
    # the log node ("option No, selected.") must not be read as the value
    p = _plan(select_widget(SPONSOR, committed="No"))
    assert p.actions == []  # reads the singleValue "No", not the log text


def test_committed_match_tolerates_whitespace():
    p = _plan(select_widget(SPONSOR, committed="No"), {**ANSWERS, "sponsorship": " No "})
    assert p.actions == []


def test_unrelated_sibling_value_does_not_satisfy_select():
    # a flat form: the legal-right select is empty, but a filled First Name
    # textbox sits in the same container — it must NOT count as the committed value
    snap = (
        "- form [ref=e0]:\n"
        '  - textbox "First Name" [ref=e1]: Yes\n'
        f'  - combobox "{LEGAL}" [ref=e5]\n'
    )
    p = plan_fill(parse_snapshot(snap), greenhouse_map(), {"work_authorization": "Yes"})
    assert [a.ref for a in p.actions] == ["e5"]  # select re-actioned, not falsely satisfied


# ── country rule removed (live finding: it claimed the phone country-code) ───

def test_no_country_rule_in_map():
    assert not any(r.answer_key == "country" for r in greenhouse_map().rules)


def test_phone_country_code_widget_untouched():
    # verbatim shape: the phone row nests a combobox labeled "Country" showing
    # "+1" — it must be neither actioned nor escalated (it's not required)
    snap = (
        "- form [ref=e0]:\n"
        "  - generic [ref=e143]:\n"
        "    - generic [ref=e144]: Phone*\n"
        "    - generic [ref=e133]:\n"
        "      - generic [ref=e134]:\n"
        '        - generic [ref=e467]: "+1"\n'
        '        - combobox "Country" [ref=e136]\n'
        '      - textbox "Phone" [ref=e146]\n'
    )
    p = _plan(snap, {"phone": "(956) 525-3015"})
    assert [a.ref for a in p.actions] == ["e146"]
    assert p.unmapped == []
