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
# broad on purpose so a required radio/checkbox/upload the map misses is caught.
CONTROL_ROLES = frozenset({
    "textbox", "combobox", "checkbox", "radio", "radiogroup",
    "listbox", "group", "slider", "spinbutton", "switch",
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
            _rule(r"first name", "first_name", TEXT, "textbox"),
            _rule(r"last name", "last_name", TEXT, "textbox"),
            _rule(r"^email", "email", TEXT, "textbox"),
            _rule(r"phone", "phone", TEXT, "textbox"),
            _rule(r"\blocation\b", "location_city", TYPEAHEAD, "combobox"),
            _rule(r"how did you hear", "referral_source", TEXT, "textbox"),
            _rule(r"legal right to work", "work_authorization", SELECT, "combobox"),
            _rule(r"immigration sponsorship", "sponsorship", SELECT, "combobox"),
            _rule(r"salary|compensation", "salary_expectation", TEXT, "textbox"),
            _rule(r"country", "country", SELECT, "combobox"),
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


def _is_required(el) -> bool:
    # Greenhouse marks required fields with a trailing "*" in the label. This
    # convention is ATS-specific; when a second map lands, lift it onto FieldMap.
    return bool(el.label and el.label.rstrip().endswith("*"))


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
    # Resolve each rule to its first matching field (rule priority); a field a
    # higher-priority rule already claimed is not re-claimed by a later one.
    claimed: dict[int, FieldRule] = {}
    for rule in field_map.rules:
        el = index.find(rule.role, rule.label)
        if el is not None:
            claimed.setdefault(id(el), rule)

    actions: list[FillAction] = []
    unmapped: list[Unmapped] = []
    for el in index.elements:  # document order
        rule = claimed.get(id(el))
        if rule is None:
            # A required control no rule covers must escalate, not vanish.
            if el.role in CONTROL_ROLES and _is_required(el):
                unmapped.append(Unmapped(el.label, "no-rule"))
            continue
        answer = answers.get(rule.answer_key)
        if not answer:  # None or "" — no usable value; hand off rather than clobber
            unmapped.append(Unmapped(el.label, "no-answer"))
        elif rule.widget == UPLOAD:
            _plan_upload(index, el, answer, rule, actions, unmapped)
        elif el.ref is None:  # matched but the parser lost the ref → unexecutable
            unmapped.append(Unmapped(el.label, "unresolved"))
        elif el.value != answer or id(el) in invalid:
            actions.append(FillAction(el.ref, rule.widget, answer, el.label, rule.answer_key))

    submit_ref = None
    if field_map.submit_label is not None:
        btn = index.find("button", field_map.submit_label)
        submit_ref = btn.ref if btn else None

    return FillPlan(actions, unmapped, submit_ref, index.snapshot_id)
