"""Deterministic ATS fill planner (Phase 2 of the apply ladder).

The apply ladder's fast tier: given a parsed live form (Phase 1 SnapshotIndex)
and the candidate's canonical answers, emit a batched FILL PLAN — which ref
gets which value via which widget recipe — without a single model call. Fields
the map can't finish (no rule, no usable answer, or a widget that won't resolve
to an actionable ref) are reported for the agent tier; the submit control is
located but never actioned (human gate).

Pure functions only: no browser, no I/O. A later phase adds the OpenClaw client
that executes the plan and the PROFILE.md -> answers compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from pipeline.openclaw_browser import SnapshotIndex

# Widget recipes the executor knows how to run.
TEXT = "text"          # type value into a textbox
SELECT = "select"      # open a combobox, click the option matching value
TYPEAHEAD = "typeahead"  # type value, wait for suggestions, click the first
UPLOAD = "upload"      # arm an upload with value (a file path), click Attach

# aria roles that carry a fillable control. Used to decide whether an unmapped
# required field must escalate to the agent tier (rather than silently vanish);
# broad on purpose so a required radio/checkbox the map misses is caught.
# "group" is deliberately excluded: Greenhouse wraps fields (Phone, Name) in a
# required-labeled group, but the group is a container, not a field — its inner
# control is handled on its own, so escalating the group is a false positive.
CONTROL_ROLES = frozenset({
    "textbox", "combobox", "checkbox", "radio", "radiogroup",
    "listbox", "slider", "spinbutton", "switch",
})


@dataclass
class FieldRule:
    """One field-map entry: a label pattern → canonical answer key + widget."""

    label: re.Pattern
    answer_key: str
    widget: str
    role: str  # expected aria role for disambiguation


@dataclass
class FieldMap:
    name: str
    rules: list[FieldRule] = field(default_factory=list)
    submit_label: re.Pattern | None = None


@dataclass
class FillAction:
    ref: str
    widget: str
    value: str
    label: str
    answer_key: str


@dataclass
class Unmapped:
    label: str
    # "no-rule": required field the map doesn't cover; "no-answer": rule matched
    # but no usable answer; "unresolved": matched with an answer, but the widget
    # couldn't be resolved to an actionable ref (also used by the state machine
    # for fills that never converged).
    reason: str


@dataclass
class FillPlan:
    actions: list[FillAction] = field(default_factory=list)
    unmapped: list[Unmapped] = field(default_factory=list)
    submit_ref: str | None = None
    snapshot_id: str = ""  # the capture the refs belong to (stale-ref detection)


def _rule(pattern: str, answer_key: str, widget: str, role: str) -> FieldRule:
    return FieldRule(re.compile(pattern, re.I), answer_key, widget, role)


def greenhouse_map() -> FieldMap:
    """The Greenhouse application field map (first ATS covered)."""
    return FieldMap(
        name="greenhouse",
        rules=[
            # legal-name rules come FIRST so "Legal First Name" is claimed for
            # the legal name, not swept up by the generic "first name" rule.
            _rule(r"legal first name", "legal_first_name", TEXT, "textbox"),
            _rule(r"legal last name", "legal_last_name", TEXT, "textbox"),
            _rule(r"first name", "first_name", TEXT, "textbox"),
            _rule(r"last name", "last_name", TEXT, "textbox"),
            _rule(r"^email", "email", TEXT, "textbox"),
            _rule(r"phone", "phone", TEXT, "textbox"),
            # no "country" rule: live testing showed it claims the phone
            # country-code widget; a real country question escalates instead.
            _rule(r"\blocation\b", "location_city", TYPEAHEAD, "combobox"),
            _rule(r"how did you hear", "referral_source", TEXT, "textbox"),
            _rule(r"linkedin", "linkedin", TEXT, "textbox"),
            _rule(r"github", "github", TEXT, "textbox"),
            _rule(r"^website|personal website|portfolio", "website", TEXT, "textbox"),
            _rule(r"pronouns", "pronouns", TEXT, "textbox"),
            _rule(r"preferred name", "first_name", TEXT, "textbox"),
            _rule(r"legal right to work", "work_authorization", SELECT, "combobox"),
            _rule(r"immigration sponsorship", "sponsorship", SELECT, "combobox"),
            _rule(r"salary|compensation", "salary_expectation", TEXT, "textbox"),
            _rule(r"resume/cv", "resume", UPLOAD, "group"),
        ],
        submit_label=re.compile(r"submit application", re.I),
    )


def detect_ats(url: str) -> str | None:
    """Classify a posting/apply URL to an ATS name, or None if unknown."""
    host = (urlparse(url).hostname or "").lower()
    if not host:  # scheme-less URL: urlparse stuffs the host into the path
        host = (urlparse("//" + url).hostname or "").lower()
    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        return "greenhouse"
    return None


def _is_required(el, index: SnapshotIndex) -> bool:
    """Greenhouse marks required fields with a trailing "*", but on the adjacent
    LABEL node ("LinkedIn Profile*"), not the control's own aria label. So a
    field is required if its own label ends with "*" OR a sibling/label node
    carrying "<its label>*" exists on the page."""
    if not el.label:
        return False
    if el.label.rstrip().endswith("*"):
        return True
    marked = el.label.strip() + "*"
    return any(e.value and e.value.strip() == marked for e in index.elements)


def _committed_option(index: SnapshotIndex, el) -> str | None:
    """A react-select combobox's committed value, or None.

    Live-captured shape: a committed value renders as a value-bearing `generic`
    node that is a DIRECT sibling of the combobox (react-select's singleValue);
    the combobox's OWN value is only the typed filter text and never proves
    commitment. Anchoring to a direct `generic` sibling (not any descendant)
    keeps an unrelated field's value, or the aria-live `log` node ("option Yes,
    selected."), from being read as the selection."""
    if el.parent is None:
        return None
    for sib in index.elements:
        if sib.parent is el.parent and sib is not el and sib.role == "generic" and sib.value:
            return sib.value
    return None


def committed_value(index: SnapshotIndex, el) -> str | None:
    """The value a field currently holds for satisfaction checks: a combobox's
    committed react-select sibling (filter text ignored), otherwise its own
    value. Shared by the planner and the state machine so both judge a field the
    same way."""
    return _committed_option(index, el) if el.role == "combobox" else el.value


def _option_matches(committed: str, answer: str) -> bool:
    """Case-insensitive equality, or — when the answer is itself multi-part — a
    comma-bounded prefix, so answer "Dallas, Texas" is satisfied by "Dallas,
    Texas, United States" but bare "Dallas" is NOT satisfied by "Dallas, Oregon"
    (a wrong city), and "Dallas, Texas" is not satisfied by "Dallas, Texasville".
    """
    c, a = committed.strip().casefold(), answer.strip().casefold()
    return c == a or ("," in a and c.startswith(a + ","))


def _is_satisfied(index: SnapshotIndex, el, rule, answer: str) -> bool:
    """Widget-aware 'already holds the right value' check ([invalid] is the
    caller's concern)."""
    if rule.widget in (SELECT, TYPEAHEAD):
        committed = _committed_option(index, el)
        return committed is not None and _option_matches(committed, answer)
    return el.value == answer


def _plan_upload(index, group, answer, rule, actions, unmapped) -> None:
    """Plan an UPLOAD: skip if a file is already attached (idempotent), escalate
    if the Attach control can't be located, else target the Attach button."""
    inner = index.within(group)
    if inner.find("button", re.compile("remove", re.I)) is not None:
        return  # a file is already attached → nothing to do
    attach = inner.find("button", re.compile("attach", re.I))
    if attach is None or attach.ref is None:
        unmapped.append(Unmapped(group.label, "unresolved"))
        return
    actions.append(FillAction(attach.ref, UPLOAD, answer, group.label, rule.answer_key))


def plan_fill(index: SnapshotIndex, field_map: FieldMap, answers: dict[str, str]) -> FillPlan:
    """Plan the deterministic fill for a live form against a field map.

    Fields already holding the right value are skipped (idempotent) unless the
    live form still flags them `[invalid]` — custom select widgets don't
    register typed text, so an invalid field is re-actioned even when its value
    looks right. One document-order pass over the live fields yields the actions
    in order; the submit control is located but never actioned.
    """
    invalid = {id(el) for el in index.invalid_fields()}
    # Claim EVERY field a rule matches (not just the first), so duplicates like
    # "First Name" AND "Legal First Name" both get filled; a field a
    # higher-priority rule already claimed is not re-claimed by a later one.
    claimed: dict[int, FieldRule] = {}
    for rule in field_map.rules:
        for el in index.find_all(rule.role, rule.label):
            claimed.setdefault(id(el), rule)

    actions: list[FillAction] = []
    unmapped: list[Unmapped] = []
    for el in index.elements:  # document order
        rule = claimed.get(id(el))
        if rule is None:
            # A control no rule covers is surfaced so nothing is silently
            # dropped: a required one escalates ("no-rule"); an empty optional
            # one is reported ("optional") for the human to fill if they want.
            if el.role in CONTROL_ROLES and el.label:
                if _is_required(el, index):
                    unmapped.append(Unmapped(el.label, "no-rule"))
                elif not committed_value(index, el):
                    unmapped.append(Unmapped(el.label, "optional"))
            continue
        answer = answers.get(rule.answer_key)
        if not answer:  # None or "" — no usable value; hand off rather than clobber
            # a required field we can't answer blocks (no-answer); an optional
            # one is only reported (optional), never blocks a ready-to-submit
            unmapped.append(Unmapped(el.label, "no-answer" if _is_required(el, index) else "optional"))
        elif rule.widget == UPLOAD:
            _plan_upload(index, el, answer, rule, actions, unmapped)
        elif el.ref is None:  # matched but the parser lost the ref → unexecutable
            unmapped.append(Unmapped(el.label, "unresolved"))
        elif not _is_satisfied(index, el, rule, answer) or id(el) in invalid:
            actions.append(FillAction(el.ref, rule.widget, answer, el.label, rule.answer_key))

    submit_ref = None
    if field_map.submit_label is not None:
        btn = index.find("button", field_map.submit_label)
        submit_ref = btn.ref if btn else None

    return FillPlan(actions, unmapped, submit_ref, index.snapshot_id)
