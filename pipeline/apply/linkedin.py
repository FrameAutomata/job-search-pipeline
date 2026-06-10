"""Deterministic LinkedIn Easy Apply engine.

The fast-path: walk the Easy Apply modal with fixed selectors (no LLM in the
navigation loop), calling the answer engine only to fill individual fields. This
is the AIHawk approach — `_fill_application_form` loops "fill the visible step,
then click Next/Review/Submit" until it submits or hits a wall.

Selectors target LinkedIn's current Easy Apply markup and are best-effort: the
markup shifts over time, so verify with `--apply-mode dry-run` against a live
posting before trusting auto-submit. Every per-field operation is wrapped so a
single unrecognized control degrades to "skip that field" rather than aborting
the whole application.

mode:
  "auto"     → clicks Submit unattended (at the user's own risk)
  "review"   → fills everything, stops at the Submit step, returns the drafted
               answers for a human to confirm (the UI flow drives the click)
  "dry-run"  → same as review but explicitly a rehearsal
Only "auto" submits; review/dry-run leave the form filled and unsubmitted.
"""

from __future__ import annotations

from pipeline.apply.answers import AnswerEngine
from pipeline.apply.queue import ApplyJob
from pipeline.apply.result import ApplyResult, APPLIED, EXPIRED, failed

_MODAL = "div.jobs-easy-apply-modal, div[data-test-modal][role='dialog']"
_MAX_STEPS = 14  # generous cap; real Easy Apply flows are 1-5 steps


def apply_to(page, job: ApplyJob, answers: AnswerEngine, *, mode: str = "review") -> ApplyResult:
    """Drive one LinkedIn Easy Apply application. Pure side effect on `page`."""
    submit = mode == "auto"
    page.goto(job.url, wait_until="domcontentloaded")
    _wait_for_apply_cta(page)

    if _already_applied(page):
        return ApplyResult(code="already_applied")
    if _closed(page):
        return ApplyResult(code=EXPIRED)

    btn = _easy_apply_button(page)
    if btn is None:
        # Not an Easy Apply posting (or the CTA didn't render). The reason carries
        # what we DID see, so non-Easy-Apply bails are diagnosable from the log.
        return ApplyResult(code="not_easy_apply", reason=_apply_cta_debug(page))

    try:
        btn.click()
    except Exception:
        return failed("easy_apply_click_failed")
    page.wait_for_timeout(1200)

    drafted: list[tuple[str, str]] = []
    for _ in range(_MAX_STEPS):
        if not _modal_open(page):
            return failed("modal_closed_unexpectedly")

        _fill_visible_fields(page, answers, drafted)

        primary = _primary_button(page)
        if primary is None:
            return failed("no_primary_button")
        label = _btn_label(primary)

        if "submit application" in label:
            if not submit:
                return ApplyResult(code=APPLIED, reason=f"not submitted (mode={mode})",
                                   answers=tuple(drafted), submitted=False)
            _uncheck_follow_company(page)
            try:
                primary.click()
            except Exception:
                return failed("submit_click_failed")
            page.wait_for_timeout(2000)
            return ApplyResult(code=APPLIED, answers=tuple(drafted), submitted=True)

        # Next / Review / Continue → advance, then check for validation errors.
        try:
            primary.click()
        except Exception:
            return failed("next_click_failed")
        page.wait_for_timeout(1200)
        if _has_validation_error(page):
            # One more fill pass can clear a freshly-revealed required field; if
            # the error persists we stop rather than loop on a field we can't fill.
            _fill_visible_fields(page, answers, drafted)
            if _has_validation_error(page):
                return failed("validation_error")

    return failed("max_steps_exceeded")


# ── page probes ──────────────────────────────────────────────────────────────

def _text(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        return loc.inner_text(timeout=1500) if loc.count() else ""
    except Exception:
        return ""


def _closed(page) -> bool:
    body = _text(page, "body").lower()
    return any(s in body for s in (
        "no longer accepting applications",
        "this job is no longer available",
        "the job you were looking for was not found",
    ))


def _wait_for_apply_cta(page) -> None:
    """LinkedIn renders the apply button via client-side JS after load. Wait for
    any apply CTA to appear before probing, falling back to a short fixed wait."""
    try:
        page.wait_for_selector(
            "button.jobs-apply-button, button[aria-label*='Apply' i]",
            timeout=10000, state="visible",
        )
    except Exception:
        page.wait_for_timeout(1500)


def _easy_apply_button(page):
    """The 'Easy Apply' CTA. Returns a locator or None. Matches on the Easy Apply
    label in EITHER the aria-label or the visible text (so an icon-rendered button
    whose text node is empty still counts), and never on a plain 'Apply' (off-site)
    button. There can be two CTAs (top + sticky bar); the first matching wins."""
    loc = page.locator(
        "button.jobs-apply-button, "
        "button[aria-label*='Easy Apply' i], "
        "button:has-text('Easy Apply')"
    )
    try:
        n = loc.count()
    except Exception:
        return None
    for i in range(n):
        b = loc.nth(i)
        try:
            aria = (b.get_attribute("aria-label") or "").lower()
            txt = (b.inner_text() or "").lower()
        except Exception:
            continue
        if "easy apply" in aria or "easy apply" in txt:
            return b
    return None


def _already_applied(page) -> bool:
    """True if LinkedIn shows this job as already applied to."""
    txt = _text(page, "button.jobs-apply-button, .jobs-s-apply, "
                      ".artdeco-inline-feedback").strip().lower()
    return (txt.startswith("applied")
            or "application submitted" in txt
            or "you've applied" in txt)


def _apply_cta_debug(page) -> str:
    """Summarize the apply buttons we DID see, to explain a non-Easy-Apply bail.
    Surfaced in the per-job log so selectors can be tuned against real postings."""
    try:
        loc = page.locator("button[aria-label*='Apply' i], button.jobs-apply-button")
        labels: list[str] = []
        for i in range(min(loc.count(), 3)):
            el = loc.nth(i)
            label = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
            if label:
                labels.append(label[:40])
        return "no Easy Apply button; saw: " + ("; ".join(labels) if labels else "no apply CTA at all")
    except Exception:
        return "no Easy Apply button found"


def _modal_open(page) -> bool:
    try:
        return page.locator(_MODAL).count() > 0
    except Exception:
        return False


def _primary_button(page):
    """The modal footer's primary action (Next / Review / Submit)."""
    for sel in (
        f"{_MODAL} footer button[aria-label*='Submit application' i]",
        f"{_MODAL} footer button[aria-label*='Review' i]",
        f"{_MODAL} footer button[aria-label*='Continue to next' i]",
        f"{_MODAL} footer button.artdeco-button--primary",
        f"{_MODAL} button.artdeco-button--primary",
    ):
        loc = page.locator(sel).first
        try:
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def _btn_label(button) -> str:
    try:
        return ((button.get_attribute("aria-label") or "") + " " +
                (button.inner_text() or "")).strip().lower()
    except Exception:
        return ""


def _has_validation_error(page) -> bool:
    try:
        return page.locator(f"{_MODAL} [data-test-form-element-error-messages], "
                            f"{_MODAL} .artdeco-inline-feedback--error").count() > 0
    except Exception:
        return False


def _uncheck_follow_company(page) -> None:
    """LinkedIn pre-checks 'Follow <company>' on the submit step. Leave it unchecked."""
    try:
        cb = page.locator(f"{_MODAL} input#follow-company-checkbox").first
        if cb.count() and cb.is_checked():
            cb.uncheck()
    except Exception:
        pass


# ── field filling ─────────────────────────────────────────────────────────────

def _fill_visible_fields(page, answers: AnswerEngine, drafted: list[tuple[str, str]]) -> None:
    """Fill every input/select/radio/checkbox in the currently visible modal step.

    Each form element is a labelled grouping; we read the label, classify the
    control, ask the answer engine, and fill. Per-element try/except keeps one
    odd widget from killing the whole application."""
    groups = page.locator(f"{_MODAL} .fb-dash-form-element, {_MODAL} [data-test-form-element]")
    try:
        count = groups.count()
    except Exception:
        return

    for i in range(count):
        group = groups.nth(i)
        try:
            _fill_group(group, answers, drafted)
        except Exception:
            continue  # unrecognized widget → skip, don't abort the application


def _group_label(group) -> str:
    for sel in ("label", "legend", "span.fb-dash-form-element__label"):
        try:
            loc = group.locator(sel).first
            if loc.count():
                txt = (loc.inner_text() or "").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


def _fill_group(group, answers: AnswerEngine, drafted: list[tuple[str, str]]) -> None:
    label = _group_label(group)
    if not label:
        return

    # <select> dropdown
    sel = group.locator("select").first
    if sel.count():
        options = [o for o in sel.locator("option").all_inner_texts()
                   if o.strip() and "select" not in o.strip().lower()[:7]]
        value = answers.answer(label, "select", options)
        sel.select_option(label=value)
        drafted.append((label, value))
        return

    # radio group
    radios = group.locator("input[type=radio]")
    if radios.count():
        options = _radio_options(group)
        value = answers.answer(label, "radio", options)
        _choose_radio(group, value)
        drafted.append((label, value))
        return

    # checkbox (agreements / single yes-no) — affirm via the answer engine
    checkbox = group.locator("input[type=checkbox]").first
    if checkbox.count():
        verdict = answers.answer(label, "select", ["Yes", "No"])
        if verdict.strip().lower() in ("yes", "true", "agree", "i agree"):
            if not checkbox.is_checked():
                checkbox.check()
            drafted.append((label, "checked"))
        return

    # text / numeric / textarea
    textarea = group.locator("textarea").first
    if textarea.count():
        value = answers.answer(label, "textarea")
        textarea.fill(value)
        drafted.append((label, value))
        return

    text = group.locator("input[type=text], input[type=email], input[type=tel], "
                         "input[type=number], input:not([type])").first
    if text.count():
        field_type = "numeric" if (text.get_attribute("type") == "number") else "text"
        value = answers.answer(label, field_type)
        text.fill(value)
        drafted.append((label, value))


def _radio_options(group) -> list[str]:
    opts: list[str] = []
    labels = group.locator("label")
    try:
        for i in range(labels.count()):
            t = (labels.nth(i).inner_text() or "").strip()
            if t:
                opts.append(t)
    except Exception:
        pass
    return opts


def _choose_radio(group, value: str) -> None:
    """Click the radio whose label best matches `value`."""
    labels = group.locator("label")
    target = value.strip().lower()
    for i in range(labels.count()):
        try:
            lt = (labels.nth(i).inner_text() or "").strip().lower()
            if lt == target or (target and target in lt):
                labels.nth(i).click()
                return
        except Exception:
            continue
    # Fall back to the first option so a required radio is at least set.
    try:
        labels.first.click()
    except Exception:
        pass
