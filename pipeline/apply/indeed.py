"""Deterministic Indeed SmartApply engine.

Indeed sits behind Cloudflare, so the apply session runs on **patchright** (stealth
real Chrome) with a pre-captured login (see browser.launch_indeed /
capture_indeed_login) — not the bundled-Chromium LinkedIn path. This module then
walks Indeed's SmartApply form, which is page-based (not a modal): a sequence of
steps at smartapply.indeed.com/.../form/<module>/<step> — resume-selection →
questions → demographic → review → submit — each with a Continue button, and a
Submit on the final review step.

Like linkedin.py, the page-driving is verified MANUALLY via `--apply-mode dry-run`
(selectors shift); the pure helpers here (cookie prep, step classification,
primary-action selection) are unit-tested. Reuses AnswerEngine for field answers
and the container-agnostic helpers from linkedin.py for uploads/labels.

mode: "auto" submits; "review"/"dry-run" fill and stop before Submit.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pipeline.apply import linkedin as _lk  # reuse container-agnostic fill helpers
from pipeline.apply.answers import AnswerEngine
from pipeline.apply.queue import ApplyJob
from pipeline.apply.result import ApplyResult, APPLIED, CANCELLED, EXPIRED, failed

# Cloudflare-managed cookies are bound to the original IP/User-Agent, so they must
# NOT be injected into the apply browser — patchright clears Cloudflare itself.
_CF_COOKIES = frozenset({"cf_clearance", "__cf_bm", "__cflb", "_cfuvid"})

_VALID_SAMESITE = ("Strict", "Lax", "None")

_SMARTAPPLY_HOST = "smartapply.indeed.com"
_MAX_STEPS = 12  # SmartApply is typically 2-5 steps; generous cap


def prepare_indeed_cookies(raw, *, now=None, persist_days: int = 30):  # -> list[dict]
    """Turn live cookies (from a normal-Chrome login capture) into Playwright
    add_cookies entries for the apply profile. Drops the Cloudflare cookies (the
    apply browser clears Cloudflare itself), and gives session-scoped cookies a
    future expiry so the captured login PERSISTS across restarts instead of
    vanishing when the apply browser closes."""
    import time
    base = time.time() if now is None else now
    persist = int(base + persist_days * 86400)
    out = []
    for c in raw:
        if c.get("name") in _CF_COOKIES:
            continue
        ck = {"name": c["name"], "value": c["value"],
              "domain": c["domain"], "path": c.get("path", "/")}
        exp = c.get("expires", -1)
        ck["expires"] = exp if (exp and exp > 0) else persist
        if "httpOnly" in c:
            ck["httpOnly"] = c["httpOnly"]
        if "secure" in c:
            ck["secure"] = c["secure"]
        if c.get("sameSite") in _VALID_SAMESITE:
            ck["sameSite"] = c["sameSite"]
        out.append(ck)
    return out


def _classify_step(url: str, heading: str = "") -> str:
    """Classify a SmartApply step from its URL module (and heading as a backstop).
    Order matters: 'demographic-questions' contains 'questions', so demographic is
    checked first."""
    u = (url or "").lower()
    h = (heading or "").lower()
    if "review" in u or "review" in h:
        return "review"
    if "resume-selection" in u or "resume" in u:
        return "resume-selection"
    if "demographic" in u:
        return "demographic"
    if "questions" in u:
        return "questions"
    return "unknown"


def _primary_action(labels) -> str:
    """The step's primary action from the visible button labels: 'submit' if a
    Submit-application button is present (it must win so we never advance past the
    final step), else 'continue', else 'none'."""
    joined = " | ".join((l or "").lower() for l in labels)
    if "submit application" in joined or "submit your application" in joined:
        return "submit"
    if "continue" in joined or "next" in joined:
        return "continue"
    return "none"


# Voluntary self-ID (EEO) consent prefs — platform-agnostic (LinkedIn and other
# ATS ask the same), set once in setup/UI (persisted to profile.yml ->
# ApplyProfile); an env var overrides for a one-off. (env_var, profile_attr,
# default). Default: AGREE to the data-processing consent some apply forms require
# to submit (we provide no demographic data anyway), but do NOT save/share answers.
_DEMOGRAPHIC_PREFS = {
    "consent": ("APPLY_EEO_DATA_CONSENT", "eeo_data_consent", True),
    "save": ("APPLY_EEO_SAVE_ANSWERS", "eeo_save_answers", False),
    "share": ("APPLY_EEO_SHARE_ANSWERS", "eeo_share_answers", False),
}


def _demographic_pref(kind: str, profile=None) -> bool:
    """Whether to check a demographic-consent box (consent/save/share): env
    override → the candidate's profile pref → hardcoded default."""
    env_name, prof_attr, default = _DEMOGRAPHIC_PREFS[kind]
    v = os.environ.get(env_name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    if profile is not None:
        return bool(getattr(profile, prof_attr, default))
    return default


# ── page-driving (manual-verify via --apply-mode dry-run) ──────────────────────

def apply_to(page, job: ApplyJob, answers: AnswerEngine, *, mode: str = "review",
             resume_path: Path | None = None, should_cancel=None) -> ApplyResult:
    """Drive one Indeed SmartApply application on a patchright (logged-in) page.

    Click Apply, then walk the SmartApply steps — filling each via the answer
    engine and clicking Continue — until the review step. "auto" submits there;
    "review"/"dry-run" stop with the form filled and unsubmitted."""
    submit = mode == "auto"
    page.goto(job.url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    if _login_redirect(page):
        # The captured session expired — caller should re-run capture_indeed_login.
        return failed("not_logged_in")
    if _closed(page):
        return ApplyResult(code=EXPIRED, reason="not accepting applications")
    if _already_applied(page):
        return ApplyResult(code="already_applied")

    btn = _apply_button(page)
    if btn is None:
        return failed(f"no_apply_button | {_debug(page)}")
    try:
        btn.click()
    except Exception:
        return failed("apply_click_failed")

    sa = _await_smartapply(page)
    if sa is None:
        return failed(f"smartapply_did_not_open | url={(page.url or '')[:80]}")

    drafted: list[tuple[str, str]] = []
    for _ in range(_MAX_STEPS):
        if should_cancel is not None and should_cancel():
            return ApplyResult(code=CANCELLED, reason="cancelled during fill")
        sa.wait_for_timeout(2500)
        step = _classify_step(sa.url, _heading(sa))
        action = _primary_action(_button_labels(sa))

        if action == "submit" or step == "review":
            if not submit:
                return ApplyResult(code=APPLIED, reason=f"not submitted (mode={mode})",
                                   answers=tuple(drafted), submitted=False)
            if not _click_submit(sa):
                return failed("submit_click_failed")
            sa.wait_for_timeout(2500)
            return ApplyResult(code=APPLIED, answers=tuple(drafted), submitted=True)

        _fill_step(sa, step, answers, drafted, resume_path)

        prev = sa.url
        if not _click_continue(sa):
            # No way forward and not at submit — hand off for review (auto fails).
            if not submit:
                return ApplyResult(code=APPLIED, reason=f"needs review (no continue at {step})",
                                   answers=tuple(drafted), submitted=False)
            return failed(f"no_continue:{step}")
        sa.wait_for_timeout(3000)
        if sa.url == prev:
            # Continue didn't advance — a required field we couldn't satisfy.
            if not submit:
                return ApplyResult(code=APPLIED, reason=f"needs review (stuck at {step})",
                                   answers=tuple(drafted), submitted=False)
            return failed(f"stuck:{step}")

    return failed("max_steps_exceeded")


def _await_smartapply(page, timeout_ms: int = 12000):
    """After clicking Apply, return the page now on smartapply.indeed.com — the
    same tab (it usually navigates) or a newly-opened one. None if it never gets
    there (e.g. an off-site 'Apply on company site' redirect)."""
    waited = 0
    while waited < timeout_ms:
        if _SMARTAPPLY_HOST in (page.url or ""):
            return page
        for p in page.context.pages:
            if _SMARTAPPLY_HOST in (p.url or ""):
                return p
        page.wait_for_timeout(1000)
        waited += 1000
    return page if _SMARTAPPLY_HOST in (page.url or "") else None


def _fill_step(sa, step: str, answers: AnswerEngine, drafted: list[tuple[str, str]],
               resume_path: Path | None) -> None:
    """Fill the current SmartApply step. resume-selection relies on Indeed's
    pre-selected resume (Continue advances); questions/demographic are filled via
    the answer engine — EEO declines itself, so demographic fields are left unset
    (voluntary, so Continue still advances)."""
    if step == "resume-selection":
        _upload_resume(sa, answers, drafted, resume_path)
        return

    if step == "demographic":
        # The actual EEO questions are declined by the answer engine below; the
        # blocker is the required data-processing CONSENT — handle it per the
        # candidate's configured prefs, then fall through to decline any EEO field.
        _handle_demographic(sa, drafted, getattr(answers, "profile", None))
        # Toggling the consent re-renders the advance button (Continue -> Review)
        # a beat later; wait so the loop's click lands on the enabled button.
        sa.wait_for_timeout(2500)

    # File-upload fields on this step (cover letter / supporting documents). Reuses
    # linkedin's handler: a cover-letter field gets the rendered cover-letter PDF
    # (generated only because the form asks); other file fields get the resume.
    try:
        _lk._handle_file_inputs(sa, answers, drafted, resume_path)
    except Exception:
        pass

    # Radio groups (fieldset legend = question).
    try:
        fsets = sa.locator("fieldset")
        for i in range(fsets.count()):
            try:
                _lk._fill_fieldset(fsets.nth(i), answers, drafted)
            except Exception:
                continue
    except Exception:
        pass

    # Standalone text / select / textarea. _fill_field uses `sa` only as the
    # label-lookup scope (page.locator works there), and skips radios/checkboxes.
    try:
        fields = sa.locator("input, select, textarea")
        for i in range(fields.count()):
            try:
                _lk._fill_field(sa, fields.nth(i), answers, drafted)
            except Exception:
                continue
    except Exception:
        pass


def _upload_resume(sa, answers: AnswerEngine, drafted: list[tuple[str, str]],
                   resume_path: Path | None) -> None:
    """Upload our resume to the resume-selection step's (hidden) file input —
    the per-job TAILORED resume when one applies, else the default — with the
    recruiter-facing filename. Reuses linkedin's uploader. Falls back to Indeed's
    pre-selected resume if there's no upload field or no resume PDF."""
    before = len(drafted)
    try:
        _lk._handle_file_inputs(sa, answers, drafted, resume_path)
    except Exception:
        pass
    if len(drafted) > before:
        sa.wait_for_timeout(3000)  # let SmartApply process the upload before Continue
    else:
        drafted.append(("Resume", "using pre-selected Indeed resume"))


def _handle_demographic(sa, drafted: list[tuple[str, str]], profile=None) -> None:
    """Set the demographic-consent checkboxes per the candidate's prefs: AGREE to
    the required data-processing consent (so the application can proceed), Save and
    Share answers off by default. Matched by label text — save/share are checked
    before the bare 'Agree' because their labels also contain consent verbs."""
    want = {k: _demographic_pref(k, profile) for k in ("save", "share", "consent")}
    try:
        cbs = sa.locator("input[type=checkbox]")
        n = cbs.count()
    except Exception:
        return
    for i in range(n):
        el = cbs.nth(i)
        try:
            label = _lk._field_label(sa, el).lower()
        except Exception:
            label = ""
        if any(t in label for t in ("saving", "pre-fill", "prefill", "save my")):
            kind = "save"
        elif any(t in label for t in ("sharing", "share ", "with indeed")):
            kind = "share"
        elif any(t in label for t in ("agree", "i consent", "i understand", "i certify")):
            kind = "consent"
        else:
            continue
        try:
            _lk._set_checkbox_state(sa, el, want[kind])
            drafted.append((f"EEO {kind}", "checked" if want[kind] else "unchecked"))
        except Exception:
            continue


# ── probes ─────────────────────────────────────────────────────────────────────

def _text(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        return loc.inner_text(timeout=1500) if loc.count() else ""
    except Exception:
        return ""


def _closed(page) -> bool:
    body = _text(page, "body").lower()
    return any(s in body for s in (
        "no longer accepting applications", "this job is no longer available",
        "this job has expired", "no longer available",
    ))


def _already_applied(page) -> bool:
    # Indeed-specific applied markers only — a loose "applied on" would false-match
    # unrelated copy and skip a live job.
    body = _text(page, "body").lower()
    return any(s in body for s in ("you've applied", "you applied to", "application submitted"))


def _login_redirect(page) -> bool:
    u = (page.url or "").lower()
    return "secure.indeed.com/auth" in u or "bot-detection" in u or "/account/login" in u


_APPLY_SEL = ("#indeedApplyButton, button:has-text('Apply now'), "
              "a:has-text('Apply now'), button[aria-label*='Apply' i]")


def _apply_button(page):
    """The 'Apply now' CTA, or None. Indeed renders the SmartApply launcher as a
    button (sometimes inside an iframe-less widget); match text/aria/id."""
    for getter in (
        lambda: page.get_by_role("button", name=re.compile("apply now", re.I)),
        lambda: page.get_by_role("link", name=re.compile("apply now", re.I)),
        lambda: page.locator(_APPLY_SEL),
    ):
        try:
            loc = getter().first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _heading(sa) -> str:
    for sel in ("h1", "h2", "[role='heading']"):
        t = _text(sa, sel)
        if t:
            return " ".join(t.split())[:140]
    return ""


def _button_labels(sa) -> list[str]:
    # One batched read of all button texts (all_inner_texts), not a per-button
    # inner_text loop — avoids the worst-case where many text-less buttons each
    # burn the per-call timeout.
    try:
        texts = sa.locator("button, [role='button']").all_inner_texts()
        return [" ".join(t.split()) for t in texts if t.strip()]
    except Exception:
        return []


# The advance button varies by step: "Continue" / "Continue applying" on early
# steps, "Review your application" on the last pre-review step. All move forward;
# none is the final "Submit application" (handled separately), so matching them as
# "advance" is safe.
_ADVANCE_RE = re.compile(r"\bcontinue\b|review your application|\bnext\b", re.I)


def _continue_button(sa):
    try:
        loc = sa.get_by_role("button", name=_ADVANCE_RE)
        if loc.count() > 0:
            return loc.first
    except Exception:
        pass
    return None


def _click_continue(sa) -> bool:
    btn = _continue_button(sa)
    if btn is None:
        return False
    try:
        btn.click(timeout=8000)
        return True
    except Exception:
        return False


def _click_submit(sa) -> bool:
    try:
        loc = sa.get_by_role("button", name=re.compile("submit (your )?application", re.I)).first
        if loc.count() == 0:
            return False
        loc.click(timeout=8000)
        return True
    except Exception:
        return False


def _debug(page) -> str:
    return f"title='{(page.title() or '')[:50]}' url={(page.url or '')[:70]}"
