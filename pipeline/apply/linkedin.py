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

import os
from pathlib import Path

from pipeline.apply.answers import AnswerEngine
from pipeline.apply.queue import ApplyJob
from pipeline.apply.result import ApplyResult, APPLIED, EXPIRED, failed

ROOT = Path(__file__).resolve().parent.parent.parent

_MODAL = "[role='dialog']"
_MAX_STEPS = 14  # generous cap; real Easy Apply flows are 1-5 steps


def apply_to(page, job: ApplyJob, answers: AnswerEngine, *, mode: str = "review",
             resume_path: Path | None = None) -> ApplyResult:
    """Drive one LinkedIn Easy Apply application. Pure side effect on `page`.

    resume_path: the PDF to upload (a per-job tailored resume when the caller
    found one); falls back to the configured default resume if None."""
    submit = mode == "auto"
    page.goto(job.url, wait_until="domcontentloaded")
    _wait_for_apply_cta(page)

    # Check for the Easy Apply button FIRST. Only when it's absent do we try to
    # explain why — otherwise a stray "no longer accepting applications" string
    # elsewhere on a live page (e.g. a recommended-jobs module) falsely flags a
    # real Easy Apply posting as EXPIRED.
    btn = _easy_apply_button(page)
    if btn is None:
        if _already_applied(page):
            return ApplyResult(code="already_applied")
        if _closed(page):
            # Carry the CTA debug so a false EXPIRED (selector miss on a live
            # job) is distinguishable from a genuinely closed posting.
            return ApplyResult(code=EXPIRED, reason=_apply_cta_debug(page))
        # No CTA at all usually means a logged-out guest view; the reason carries
        # the page URL/title and whether a sign-in wall is present.
        return ApplyResult(code="not_easy_apply", reason=_apply_cta_debug(page))

    try:
        btn.click()
    except Exception:
        return failed("easy_apply_click_failed")
    # The dialog mounts a couple seconds after the click — wait for it explicitly
    # rather than a fixed sleep that races the render.
    try:
        page.wait_for_selector(_MODAL, timeout=8000, state="visible")
    except Exception:
        return failed("modal_did_not_open")
    page.wait_for_timeout(400)  # brief settle for the first step's fields to mount

    drafted: list[tuple[str, str]] = []
    for _ in range(_MAX_STEPS):
        if not _modal_open(page):
            return failed("modal_closed_unexpectedly")

        _fill_visible_fields(page, answers, drafted, resume_path)

        primary = _primary_button(page)
        if primary is None:
            return failed("no_primary_button")
        label = _btn_label(primary)

        if "submit application" in label:
            if os.environ.get("APPLY_DEBUG"):
                _debug_dump_optins(page)
            if not submit:
                return ApplyResult(code=APPLIED, reason=f"not submitted (mode={mode})",
                                   answers=tuple(drafted), submitted=False)
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
        page.wait_for_timeout(1500)
        if _has_validation_error(page):
            # One more fill pass can clear a freshly-revealed required field.
            _fill_visible_fields(page, answers, drafted, resume_path)
            if _has_validation_error(page):
                # In review/dry-run, a field we couldn't satisfy isn't a failure —
                # hand the partly-filled form to the human to complete and submit.
                # Auto mode can't proceed past an invalid required field.
                if not submit:
                    return ApplyResult(code=APPLIED, reason="needs review (validation)",
                                       answers=tuple(drafted), submitted=False)
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


# LinkedIn ships obfuscated/hashed CSS classes and renders the Easy Apply CTA as
# an <a> anchor (aria-label "Easy Apply to this job", text "Easy Apply") — NOT a
# .jobs-apply-button <button>. So we match on aria-label / role / text, never on
# class. This selector covers both the anchor and the older button form.
_EASY_APPLY_SEL = (
    "a[aria-label*='Easy Apply' i], button[aria-label*='Easy Apply' i], "
    "a:has-text('Easy Apply'), button:has-text('Easy Apply')"
)


def _wait_for_apply_cta(page) -> None:
    """LinkedIn renders the apply CTA via client-side JS after load. Wait for it
    (anchor or button) before probing, falling back to a short fixed wait."""
    try:
        page.wait_for_selector(_EASY_APPLY_SEL, timeout=15000, state="visible")
    except Exception:
        page.wait_for_timeout(1500)


def _easy_apply_button(page):
    """The 'Easy Apply' CTA (anchor or button). Returns a locator or None. Matches
    the Easy Apply label in the aria-label or as the exact visible text, so it
    never grabs a recommended-job card link that merely contains the words, nor a
    plain off-site 'Apply' button."""
    loc = page.locator(_EASY_APPLY_SEL)
    try:
        n = loc.count()
    except Exception:
        return None
    for i in range(n):
        el = loc.nth(i)
        try:
            aria = (el.get_attribute("aria-label") or "").lower()
            txt = " ".join((el.inner_text() or "").split()).lower()
        except Exception:
            continue
        if "easy apply" in aria or txt == "easy apply":
            return el
    return None


def _already_applied(page) -> bool:
    """True if LinkedIn shows this job as already applied to."""
    txt = _text(page, "button.jobs-apply-button, .jobs-s-apply, "
                      ".artdeco-inline-feedback").strip().lower()
    return (txt.startswith("applied")
            or "application submitted" in txt
            or "you've applied" in txt)


def _apply_cta_debug(page) -> str:
    """Explain a non-Easy-Apply bail: which apply buttons were seen, plus the page
    URL/title and whether a sign-in wall is present. A sign-in wall or an authwall
    URL means the session is logged out (the most common cause of 'no apply CTA')."""
    try:
        loc = page.locator("a[aria-label*='Apply' i], button[aria-label*='Apply' i], "
                           "a:has-text('Apply'), button:has-text('Apply')")
        labels: list[str] = []
        for i in range(min(loc.count(), 3)):
            el = loc.nth(i)
            label = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
            if label:
                labels.append(label[:40])
        seen = "; ".join(labels) if labels else "no apply CTA at all"
        signin = False
        try:
            signin = page.locator("a[href*='/login'], .authwall, form.login__form, "
                                  ".join-form, [data-test-id='sign-in-form']").count() > 0
        except Exception:
            pass
        url = (page.url or "")[:70]
        title = (page.title() or "")[:50]
        return f"saw: {seen} | signin_wall={signin} | title='{title}' | url={url}"
    except Exception:
        return "no Easy Apply button found"


# aria-labels of the Easy Apply footer actions, used both to find the primary
# button and to identify WHICH [role=dialog] is the Easy Apply modal.
_PRIMARY_NEEDLES = ("submit application", "review your application", "review",
                    "continue to next", "next", "continue")
_PRIMARY_SEL = ", ".join(f"button[aria-label*='{n}' i]" for n in _PRIMARY_NEEDLES)


def _modal(page):
    """Return the Easy Apply dialog locator, or None.

    LinkedIn can have several [role=dialog] mounted at once (the messaging
    overlay is one), and their DOM order varies — so `.first` is unreliable and
    sometimes lands on a non-form dialog (symptom: 0 fields filled +
    no_primary_button). Pick the dialog that actually holds the Easy Apply flow:
    one with a Submit/Review/Next/Continue button, else one with form controls."""
    dialogs = page.locator(_MODAL)
    try:
        n = dialogs.count()
    except Exception:
        return None
    if n == 0:
        return None
    for i in range(n):
        d = dialogs.nth(i)
        try:
            if d.locator(_PRIMARY_SEL).count() > 0:
                return d
        except Exception:
            continue
    for i in range(n):
        d = dialogs.nth(i)
        try:
            if d.locator("input, select, textarea").count() > 0:
                return d
        except Exception:
            continue
    return dialogs.first


def _modal_open(page) -> bool:
    return _modal(page) is not None


def _primary_button(page):
    """The modal's primary action (Submit / Review / Next / Continue), matched by
    aria-label in priority order — submit beats review beats next, so we never
    advance past the submit step. (LinkedIn's button aria-labels are stable even
    though the wrapper classes are hashed.)"""
    dialog = _modal(page)
    if dialog is None:
        return None
    for needle in _PRIMARY_NEEDLES:
        loc = dialog.locator(f"button[aria-label*='{needle}' i]")
        try:
            if loc.count():
                return loc.first
        except Exception:
            continue
    # Last resort: a primary-styled button (artdeco classes are not hashed).
    loc = dialog.locator("button.artdeco-button--primary")
    return loc.first if loc.count() else None


def _btn_label(button) -> str:
    try:
        return ((button.get_attribute("aria-label") or "") + " " +
                (button.inner_text() or "")).strip().lower()
    except Exception:
        return ""


def _has_validation_error(page) -> bool:
    # artdeco-* component classes are NOT hashed, so the inline-error class is a
    # reliable hook; role=alert is the layout-independent backstop.
    dialog = _modal(page)
    if dialog is None:
        return False
    try:
        return dialog.locator(".artdeco-inline-feedback--error, [role='alert']").count() > 0
    except Exception:
        return False


# ── field filling ─────────────────────────────────────────────────────────────

def _fill_visible_fields(page, answers: AnswerEngine, drafted: list[tuple[str, str]],
                         resume_path: Path | None = None) -> None:
    """Fill every field in the currently visible modal step.

    LinkedIn wraps fields in hashed-class divs, so we don't key on wrappers: we
    walk the actual controls and read each one's question from its label[for=id]
    (or aria-label). Radio/checkbox groups live in <fieldset>s (question =
    <legend>). Per-field try/except keeps one odd widget from aborting the run."""
    dialog = _modal(page)
    if dialog is None:
        return

    # Resume / file-upload step: set the file input directly (Playwright can do
    # this even when the input is visually hidden behind an "Upload resume"
    # button). Uploading also covers the none-selected case; if a previous
    # resume is already selected, the upload simply replaces it with the
    # current one — which is what we want anyway.
    try:
        _handle_file_inputs(dialog, drafted, resume_path)
    except Exception:
        pass

    # Radio groups — their question is the fieldset legend.
    fsets = dialog.locator("fieldset")
    try:
        for i in range(fsets.count()):
            try:
                _fill_fieldset(fsets.nth(i), answers, drafted)
            except Exception:
                continue
    except Exception:
        pass

    # Standalone controls (text / select / textarea / checkbox).
    fields = dialog.locator("input, select, textarea")
    try:
        n = fields.count()
    except Exception:
        n = 0
    for i in range(n):
        try:
            _fill_field(dialog, fields.nth(i), answers, drafted)
        except Exception:
            continue

    # Definitively handle the pre-checked Follow-company box (stable id), which
    # is a visually-hidden input the generic pass can't reliably toggle.
    try:
        _handle_follow_company(dialog, drafted)
    except Exception:
        pass


def _debug_dump_optins(page) -> None:
    """APPLY_DEBUG: dump every checkbox/switch control and every element whose own
    text mentions 'Follow', with structure, so we can target a styled toggle."""
    dialog = _modal(page)
    if dialog is None:
        return
    try:
        data = dialog.evaluate(r"""
        d => {
          const inputs = [];
          d.querySelectorAll("input[type=checkbox], [role=switch], [role=checkbox]").forEach(el => {
            inputs.push({
              tag: el.tagName, type: el.getAttribute('type'), role: el.getAttribute('role'),
              ariaChecked: el.getAttribute('aria-checked'),
              checked: el.checked === true,
              id: el.id || '',
              label: (el.closest('label')?.innerText || '').replace(/\s+/g,' ').trim().slice(0, 60)
            });
          });
          const follow = [];
          d.querySelectorAll('label,span,div,button,p').forEach(el => {
            const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
              .map(n => n.textContent).join('').replace(/\s+/g,' ').trim();
            if (/follow/i.test(own)) follow.push({
              tag: el.tagName, role: el.getAttribute('role'), text: own.slice(0, 50),
              html: el.outerHTML.replace(/\s+/g,' ').slice(0, 200)
            });
          });
          return { inputs, follow: follow.slice(0, 6) };
        }
        """)
        print("[apply-debug] inputs:", data.get("inputs"), flush=True)
        for f in data.get("follow", []):
            print("[apply-debug] follow:", f, flush=True)
    except Exception as e:
        print(f"[apply-debug] dump failed: {e}", flush=True)


def _is_optout_checkbox(label: str) -> bool:
    """True for follow-company / marketing / subscription opt-in checkboxes —
    things we never want auto-accepted."""
    l = label.lower()
    return any(k in l for k in (
        "follow", "stay up to date", "subscribe", "newsletter",
        "marketing", "promotional", "opt in", "opt-in",
    ))


def _follow_companies_opt_in() -> bool:
    return os.environ.get("APPLY_FOLLOW_COMPANIES", "").strip().lower() in ("1", "true", "yes")


def _set_checkbox_state(dialog, el, want: bool) -> bool:
    """Set a (possibly visually-hidden) checkbox to `want`.

    LinkedIn hides the real <input> behind a styled <label>, so check()/uncheck()
    on the input fail Playwright's actionability check and throw. We read state
    via the input property (works even when hidden) and toggle by clicking the
    associated <label>, falling back to a forced set. Returns whether the final
    state matches `want`."""
    try:
        if el.is_checked() == want:
            return True
    except Exception:
        pass
    eid = ""
    try:
        eid = el.get_attribute("id") or ""
    except Exception:
        pass
    if eid:
        try:
            lab = dialog.locator(f"label[for='{eid}']")
            if lab.count():
                lab.first.click()
        except Exception:
            pass
    try:
        if el.is_checked() != want:
            el.set_checked(want, force=True)
    except Exception:
        pass
    try:
        return el.is_checked() == want
    except Exception:
        return False


def _handle_follow_company(dialog, drafted: list[tuple[str, str]]) -> None:
    """Uncheck LinkedIn's pre-checked 'Follow <company>' box (stable id
    follow-company-checkbox) so we don't follow every employer we apply to —
    that volume looks like bot behavior. Opt back in with APPLY_FOLLOW_COMPANIES."""
    cb = dialog.locator("#follow-company-checkbox").first
    try:
        if not cb.count():
            return
    except Exception:
        return
    want = _follow_companies_opt_in()
    ok = _set_checkbox_state(dialog, cb, want)
    state = ("checked" if want else "unchecked") + ("" if ok else " (FAILED)")
    drafted.append(("Follow company", state))


def _field_label(dialog, el) -> str:
    """The question for a control. LinkedIn associates labels inconsistently, so
    try several strategies in order: <label for=id>, a wrapping/closest <label>
    (common for checkboxes like 'Follow <company>'), aria-label, then
    aria-labelledby. Returning "" caused fields — including the follow-company
    opt-out — to be silently skipped, so the broad coverage matters."""
    # 1. <label for="id">
    try:
        eid = el.get_attribute("id")
        if eid:
            lab = dialog.locator(f"label[for='{eid}']")
            if lab.count():
                txt = " ".join((lab.first.inner_text() or "").split())
                if txt:
                    return txt
    except Exception:
        pass
    # 2. Closest <label> ancestor — checkboxes/radios usually wrap their text.
    try:
        txt = el.evaluate("e => { const l = e.closest('label'); return l ? (l.innerText || '') : ''; }")
        txt = " ".join((txt or "").split())
        if txt:
            return txt
    except Exception:
        pass
    # 3. aria-label
    try:
        aria = (el.get_attribute("aria-label") or "").strip()
        if aria:
            return aria
    except Exception:
        pass
    # 4. aria-labelledby → concatenated text of the referenced node(s)
    try:
        ref = (el.get_attribute("aria-labelledby") or "").strip()
        if ref:
            parts = []
            for rid in ref.split():
                node = dialog.locator(f"[id='{rid}']")
                if node.count():
                    parts.append(" ".join((node.first.inner_text() or "").split()))
            joined = " ".join(p for p in parts if p)
            if joined:
                return joined
    except Exception:
        pass
    return ""


def _fill_field(dialog, el, answers: AnswerEngine, drafted: list[tuple[str, str]]) -> None:
    try:
        tag = (el.evaluate("e => e.tagName") or "").lower()
    except Exception:
        return
    typ = (el.get_attribute("type") or "").lower()
    if tag == "input" and typ == "radio":
        return  # handled in the fieldset pass
    if typ in ("file", "hidden", "submit", "button", "image"):
        return  # file inputs handled separately; the rest aren't form questions

    label = _field_label(dialog, el)
    if not label:
        return

    if tag == "select":
        opts = [o for o in el.locator("option").all_inner_texts()
                if o.strip() and not o.strip().lower().startswith("select")]
        value = answers.answer(label, "select", opts)
        try:
            el.select_option(label=value)
        except Exception:
            return
        drafted.append((label, value))
        return

    if tag == "textarea":
        # Cover letter: paste the tailored one career-ops generated for this job,
        # if we have it (works even when the field is optional — a tailored cover
        # letter strengthens the application).
        if "cover letter" in label.lower() and answers.cover_letter_text:
            el.fill(answers.cover_letter_text)
            drafted.append((label, f"tailored cover letter ({len(answers.cover_letter_text)} chars)"))
            return
        # Other free-text (summary, "why are you a fit"). Skip OPTIONAL ones — a
        # cover letter with no tailored text is usually optional, and generating
        # generic prose per job is slow and unnecessary. Only spend an LLM call
        # on a required free-text field.
        if not _is_required(el):
            drafted.append((label, "(skipped optional)"))
            return
        value = answers.answer(label, "textarea")
        if value and _already_has_value(el, value):
            return  # already correct (e.g. carried over from a prior step)
        el.fill(value)
        drafted.append((label, value))
        return

    if typ == "checkbox":
        eid = (el.get_attribute("id") or "")
        if eid == "follow-company-checkbox":
            return  # handled definitively by _handle_follow_company
        # Marketing / subscription opt-ins are pre-checked by LinkedIn — uncheck
        # them by default and never ask the LLM. (toggle via label: hidden input.)
        if _is_optout_checkbox(label):
            if _set_checkbox_state(dialog, el, False):
                drafted.append((label, "unchecked"))
            return
        # Consent / yes-no checkbox.
        verdict = answers.answer(label, "select", ["Yes", "No"])
        if verdict.strip().lower() in ("yes", "true", "agree", "i agree"):
            _set_checkbox_state(dialog, el, True)
            drafted.append((label, "checked"))
        return

    # text / email / tel / number
    field_type = "numeric" if typ == "number" else "text"
    value = answers.answer(label, field_type)
    if value and _already_has_value(el, value):
        return  # LinkedIn carried this value forward — skip the redundant re-fill

    # Location/city fields are typeahead comboboxes: plain fill() leaves an
    # "invalid location" because LinkedIn requires choosing from the suggestion
    # listbox. Type and select a suggestion instead.
    if value and (_is_typeahead(el) or _is_location_label(label)):
        ok, chosen = _fill_typeahead(el, value)
        drafted.append((label, chosen + ("" if ok else " (typed; no suggestion matched)")))
        return

    el.fill(value)
    drafted.append((label, value))


def _already_has_value(el, value: str) -> bool:
    """True if the input already holds the intended value — lets us skip a
    redundant fill (and the scroll-into-view it triggers) when LinkedIn has
    pre-filled or carried a field forward across steps."""
    try:
        return (el.input_value() or "").strip() == value.strip()
    except Exception:
        return False


def _is_required(el) -> bool:
    """True if the control is marked required (so we know whether an empty free-
    text field will block the form). LinkedIn marks these with required /
    aria-required; unmarked fields are treated as optional."""
    try:
        if el.get_attribute("required") is not None:
            return True
        if (el.get_attribute("aria-required") or "").lower() == "true":
            return True
    except Exception:
        pass
    return False


def _is_typeahead(el) -> bool:
    """A combobox/autocomplete input (suggestions must be selected, not typed)."""
    try:
        if (el.get_attribute("role") or "").lower() == "combobox":
            return True
        if (el.get_attribute("aria-autocomplete") or "").lower() in ("list", "both"):
            return True
    except Exception:
        pass
    return False


def _is_location_label(label: str) -> bool:
    l = label.lower()
    return "location" in l or l.strip() in ("city", "current city", "town")


def _fill_typeahead(el, value: str) -> tuple[bool, str]:
    """Type into a LinkedIn typeahead and select a suggestion from the listbox.

    Tries the city (text before the first comma) first — LinkedIn's location
    suggestions key off the city — then the full value. Scopes the [role=option]
    search to the modal so we don't click a stray listbox. Returns
    (selected_a_suggestion?, chosen_text); on no match, leaves the typed text."""
    page = el.page
    container = _modal(page) or page
    city = value.split(",")[0].strip()
    queries = [q for q in (city, value.strip()) if q]
    seen: list[str] = []
    for q in queries:
        if q in seen:
            continue
        seen.append(q)
        try:
            el.click()
            try:
                el.fill("")
            except Exception:
                pass
            el.type(q, delay=40)
            page.wait_for_timeout(1200)
            opts = container.locator("[role='option']")
            if opts.count() > 0:
                chosen = " ".join((opts.first.inner_text() or q).split())
                opts.first.click()
                page.wait_for_timeout(300)
                return True, chosen or q
        except Exception:
            continue
    try:
        el.fill(value)
    except Exception:
        pass
    return False, value


def _resume_pdf() -> Path | None:
    """The resume PDF to upload.

    Tries RESUME_PATH from .env first (coerced to .pdf — it may point at the
    .txt sibling used for keyword extraction), then falls back to the default
    resumes/resume.pdf. The fallback matters: a stale RESUME_PATH (e.g. a
    renamed file) shouldn't silently disable resume upload when the default
    resume is right there."""
    candidates: list[Path] = []
    raw = os.environ.get("RESUME_PATH")
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        if p.suffix.lower() != ".pdf":
            p = p.with_suffix(".pdf")
        candidates.append(p)
    candidates.append(ROOT / "resumes" / "resume.pdf")
    for c in candidates:
        if c.exists():
            return c
    return None


def _handle_file_inputs(dialog, drafted: list[tuple[str, str]],
                        resume_path: Path | None = None) -> None:
    """Upload the resume into any resume file input on the visible step.

    Uses the caller-supplied resume_path (a per-job tailored PDF when available),
    falling back to the configured default. Cover-letter inputs are left alone
    (optional on Easy Apply, and we don't generate per-job cover letters here)."""
    files = dialog.locator("input[type=file]")
    try:
        count = files.count()
    except Exception:
        return
    if not count:
        return
    pdf = resume_path if (resume_path and Path(resume_path).exists()) else _resume_pdf()
    if pdf is None:
        drafted.append(("Resume upload", "SKIPPED - no resume PDF found (set RESUME_PATH)"))
        return
    for i in range(count):
        el = files.nth(i)
        try:
            label = (_field_label(dialog, el) or el.get_attribute("name") or "").lower()
            if "cover" in label:
                continue
            el.set_input_files(str(pdf))
            drafted.append((f"Upload ({label or 'resume'})", pdf.name))
        except Exception:
            continue


def _fill_fieldset(fs, answers: AnswerEngine, drafted: list[tuple[str, str]]) -> None:
    legend = fs.locator("legend").first
    question = ""
    if legend.count():
        question = " ".join((legend.inner_text() or "").split())
    if not question:
        question = (fs.get_attribute("aria-label") or "").strip()
    if not question:
        return

    labels = fs.locator("label")
    options: list[str] = []
    try:
        for i in range(labels.count()):
            t = " ".join((labels.nth(i).inner_text() or "").split())
            if t:
                options.append(t)
    except Exception:
        pass
    if not options:
        return

    value = answers.answer(question, "radio", options)
    _choose_radio(fs, value)
    drafted.append((question, value))


def _choose_radio(fs, value: str) -> None:
    """Click the radio whose label best matches `value`; first option as fallback."""
    labels = fs.locator("label")
    target = value.strip().lower()
    try:
        count = labels.count()
    except Exception:
        return
    for i in range(count):
        try:
            lt = " ".join((labels.nth(i).inner_text() or "").split()).lower()
            if lt == target or (target and target in lt):
                labels.nth(i).click()
                return
        except Exception:
            continue
    try:
        labels.first.click()
    except Exception:
        pass
