"""Escalation state machine for the apply ladder (Phase 3).

Drives one job application through the three tiers over an abstract browser:

  1. DETERMINISTIC — plan_fill the live form, execute the actions, re-snapshot
     and re-plan to verify convergence (a field that won't take is retried up to
     max_rounds, then handed up).
  2. AGENT — whatever the deterministic tier can't resolve (unmapped fields, or
     fills that never converged) is passed to an injected agent handler; the
     machine re-snapshots and re-verifies after it runs.
  3. HUMAN — a detected wall (login / CAPTCHA) or anything still unresolved after
     the agent ends the run as escalated; the submit control is located but
     NEVER actioned — submitting is always the human's call.

The Browser/agent/wall collaborators are injected so this module is pure control
flow, exercised in tests with a fake browser. The real OpenClaw client and the
PROFILE.md -> answers compiler arrive in Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from pipeline.ats_fill import FieldMap, FillAction, Unmapped, committed_value, plan_fill
from pipeline.openclaw_browser import SnapshotIndex

READY_TO_SUBMIT = "ready-to-submit"
ESCALATED_HUMAN = "escalated-human"
NO_FORM = "no-form"


class Browser(Protocol):
    def snapshot(self) -> SnapshotIndex: ...
    def act(self, action: FillAction) -> None: ...


# Agent tier: try to resolve the given fields (acts through the browser however
# it likes); the machine re-snapshots afterward. Wall detector: return a blocker
# description if the snapshot is a login/CAPTCHA wall, else None.
AgentFn = Callable[[Browser, list[Unmapped]], None]
WallFn = Callable[[SnapshotIndex], "str | None"]


@dataclass
class ApplyOutcome:
    status: str
    submit_ref: str | None = None
    filled: list[FillAction] = field(default_factory=list)
    escalated: list[Unmapped] = field(default_factory=list)
    blocker: str | None = None
    rounds: int = 0


def _stuck(action: FillAction) -> Unmapped:
    """A deterministic fill that never converged → hand it to the next tier."""
    return Unmapped(action.label, "unresolved")


def _blocking(items: list[Unmapped]) -> list[Unmapped]:
    """The items that actually block a ready-to-submit / need the agent —
    everything except purely-informational optional-unfilled fields."""
    return [u for u in items if u.reason != "optional"]


def _unfinished(snap: SnapshotIndex, plan) -> list[Unmapped]:
    """What still needs a tier above: unmapped fields that are genuinely
    unsatisfied on the live form (a no-rule field pre-filled by autofill or a
    part-completed session counts as done), plus fills that never converged."""
    return _still_unresolved(snap, plan.unmapped) + [_stuck(a) for a in plan.actions]


def _still_unresolved(index: SnapshotIndex, items: list[Unmapped]) -> list[Unmapped]:
    """An unmapped item is resolved if its field is gone or now holds a value
    and is no longer flagged invalid (a re-plan can't see this for no-rule
    fields, which never had a rule to drop out of). Labels can repeat, so an
    item survives while ANY field bearing its label is still unsatisfied —
    never mask an empty duplicate behind a filled one."""
    invalid_ids = {id(el) for el in index.invalid_fields()}
    by_label: dict[str, list] = {}
    for el in index.elements:
        by_label.setdefault(el.label, []).append(el)

    def unsatisfied(el):
        # widget-aware: a combobox is satisfied by its committed react-select
        # value, never by leftover filter text on the combobox itself
        return id(el) in invalid_ids or not committed_value(index, el)

    return [u for u in items if any(unsatisfied(el) for el in by_label.get(u.label, ()))]


def _drive(browser, field_map, answers, snap, filled, detect_wall, budget):
    """Deterministic fill/verify loop: plan, act, re-observe, re-plan until the
    form converges (nothing left to fill) or the retry budget runs out. An
    action that raises (e.g. a ref gone stale) is skipped, not fatal — the field
    stays unconverged and is escalated later. Returns (snap, plan, rounds,
    blocker); blocker is set if a wall rose mid-fill."""
    rounds = 0
    plan = plan_fill(snap, field_map, answers)
    while rounds < budget and plan.actions:
        for action in plan.actions:
            try:
                browser.act(action)
            except Exception:
                continue  # couldn't act → field stays unconverged, escalated below
            filled.append(action)
        snap = browser.snapshot()
        rounds += 1
        blocker = detect_wall(snap)
        if blocker:
            return snap, plan, rounds, blocker
        plan = plan_fill(snap, field_map, answers)
    return snap, plan, rounds, None


def run_apply(
    browser: Browser,
    field_map: FieldMap,
    answers: dict[str, str],
    *,
    agent: AgentFn | None = None,
    wall: WallFn | None = None,
    max_rounds: int = 3,
) -> ApplyOutcome:
    """Drive one application through the ladder; never actions the submit."""
    filled: list[FillAction] = []
    detect_wall = wall or (lambda _snap: None)

    snap = browser.snapshot()
    blocker = detect_wall(snap)
    if blocker:
        return ApplyOutcome(ESCALATED_HUMAN, blocker=blocker)

    plan = plan_fill(snap, field_map, answers)
    if not plan.actions and not plan.unmapped and plan.submit_ref is None:
        return ApplyOutcome(NO_FORM)

    rounds = 0

    def escalated_at_wall() -> ApplyOutcome:
        return ApplyOutcome(ESCALATED_HUMAN, plan.submit_ref, filled, blocker=blocker, rounds=rounds)

    # Deterministic tier.
    snap, plan, rounds, blocker = _drive(browser, field_map, answers, snap, filled, detect_wall, max_rounds)
    if blocker:
        return escalated_at_wall()
    remaining = _unfinished(snap, plan)

    # Agent tier: hand it whatever the deterministic tier couldn't FINISH (the
    # blocking items — optional-unfilled fields never block or invoke the agent),
    # then re-run the deterministic tier on anything the agent revealed.
    blocking = _blocking(remaining)
    if blocking and agent is not None:
        try:
            agent(browser, blocking)
        except Exception:
            pass  # agent couldn't help → fall through to human with progress so far
        snap = browser.snapshot()
        blocker = detect_wall(snap)
        if blocker:
            return escalated_at_wall()
        snap, plan, agent_rounds, blocker = _drive(
            browser, field_map, answers, snap, filled, detect_wall, max_rounds)
        rounds += agent_rounds
        if blocker:
            return escalated_at_wall()
        remaining = _unfinished(snap, plan)

    # Optional-unfilled fields are reported (in `escalated`) but don't block a
    # ready-to-submit — only unresolved required/mapped fields do.
    status = ESCALATED_HUMAN if _blocking(remaining) else READY_TO_SUBMIT
    return ApplyOutcome(status, plan.submit_ref, filled, remaining, rounds=rounds)
