"""Prompt builder for the agentic apply engine.

Constructs the instruction prompt that drives a claude + Playwright-MCP agent
through an arbitrary job application (Indeed-native, redirected employer ATS, or
anything else the deterministic engines can't handle). All candidate data comes
from the profile + CV — nothing is hardcoded. Adapted from ApplyPilot's
prompt.py, but wired to this repo's ApplyProfile and a proxyless CapSolver flow.

The agent must emit exactly one RESULT:* line; agent.py parses it. See result.py
for the code taxonomy.
"""

from __future__ import annotations

import os
from datetime import datetime

from pipeline.apply.profile import ApplyProfile
from pipeline.apply.queue import ApplyJob

# Stock answers to the boilerplate yes/no screening questions every ATS asks.
_STANDARD_RESPONSES = [
    "Age 18+: Yes",
    "Legally authorized to work (per the work-auth lines above): answer truthfully",
    "Willing to complete a background check: Yes",
    "Previously employed here: No",
    "How did you hear about us: Online job board",
]


def _profile_block(profile: ApplyProfile, cv_text: str) -> str:
    lines = list(profile.summary_lines())
    if profile.salary_target:
        lines.append(f"Salary expectation (state this, or 'negotiable'): "
                     f"${profile.salary_target:,} {profile.salary_currency}")
    lines.append(f"Gender: {profile.eeo_gender or 'Decline to self-identify'}")
    lines.append(f"Race/ethnicity: {profile.eeo_race or 'Decline to self-identify'}")
    lines.append(f"Veteran status: {profile.eeo_veteran or 'Decline to self-identify'}")
    lines.append(f"Disability status: {profile.eeo_disability or 'Decline to self-identify'}")
    lines.extend(_STANDARD_RESPONSES)
    block = "== APPLICANT PROFILE ==\n" + "\n".join(lines)
    if cv_text.strip():
        block += "\n\n== RESUME / EXPERIENCE (source for skills/tenure answers) ==\n" + cv_text.strip()
    return block


def _hard_rules(profile: ApplyProfile) -> str:
    sponsor = "yes" if profile.requires_sponsorship else "no"
    permit = f" Work permit/status: {profile.work_permit_type}." if profile.work_permit_type else ""
    return (
        "== HARD RULES (never break) ==\n"
        "1. Never lie about citizenship, work authorization, criminal history, "
        "education, security clearance, or licenses.\n"
        f"2. Work auth: answer truthfully from the profile. Requires sponsorship: {sponsor}.{permit}\n"
        f"3. Name: use the candidate's legal name ({profile.full_name}) unless a field "
        "explicitly asks for a preferred name."
    )


def _salary_rules(profile: ApplyProfile) -> str:
    cur = profile.salary_currency
    target = f"${profile.salary_target:,} {cur}" if profile.salary_target else "a market rate"
    return (
        "== SALARY (never reveal a walk-away minimum) ==\n"
        f"- If asked for a number, state {target}, or say it is negotiable.\n"
        "- If the posting shows a range, answer with the midpoint of that range.\n"
        "- If asked for a range, give posted-midpoint -10% to +10%.\n"
        "- Hourly? Divide the annual figure by 2080.\n"
        "- Never state a figure below the expectation above."
    )


def _screening_rules() -> str:
    return (
        "== SCREENING QUESTIONS ==\n"
        "- Hard facts (location, work auth, citizenship, clearance, background): "
        "answer truthfully from the profile only.\n"
        "- Skills/tools in the candidate's domain: be confident — if the resume shows "
        "adjacent experience, answer yes. Don't undersell.\n"
        "- Open-ended ('why this role?'): 2-3 specific sentences grounded in the resume "
        "and this posting. Sound human, no generic fluff.\n"
        "- EEO / demographics: decline to self-identify (per the profile)."
    )


def _never_do() -> str:
    return (
        "== NEVER DO (immediate RESULT:FAILED) ==\n"
        "- Never grant camera/microphone/screen/location permissions, or do video/"
        "selfie/ID/biometric verification -> RESULT:FAILED:unsafe_permissions.\n"
        "- Never enter payment info, bank details, or SSN/SIN.\n"
        "- Never sign up for a freelancing/contractor marketplace or set an hourly rate "
        "-> RESULT:FAILED:not_a_job_application.\n"
        "- Never sign in via Google/Microsoft/SSO/OAuth -> RESULT:FAILED:sso_required.\n"
        "- Never install extensions, download executables, or run assessment software."
    )


# The CapSolver solve flow. Kept a NON-f string so the literal JS braces don't
# need escaping; the API key is spliced via .replace.
_CAPTCHA_FLOW = """== CAPTCHA (solve via CapSolver REST — proxyless) ==
API base: https://api.capsolver.com  | key: __CAPSOLVER_KEY__
When ANY CAPTCHA appears (hCaptcha / reCAPTCHA / Turnstile), solve it via the API
(server-side token — you never solve it visually). Do this after navigation and
after Apply/Submit/Login clicks.

DETECT (browser_evaluate): find the widget type + sitekey. Check hCaptcha BEFORE
reCAPTCHA (both use data-sitekey). Cloudflare Turnstile = .cf-turnstile; reCAPTCHA
= .g-recaptcha; hCaptcha = .h-captcha.

SOLVE — createTask then poll then inject (separate browser_evaluate calls):
1. POST https://api.capsolver.com/createTask with
   {"clientKey": "__CAPSOLVER_KEY__", "task": {"type": TASK_TYPE,
    "websiteURL": PAGE_URL, "websiteKey": SITE_KEY}}
   TASK_TYPE (proxyless): hcaptcha->HCaptchaTaskProxyLess,
   recaptchav2->ReCaptchaV2TaskProxyLess, recaptchav3->ReCaptchaV3TaskProxyLess,
   turnstile->AntiTurnstileTaskProxyLess, funcaptcha->FunCaptchaTaskProxyLess.
2. POST /getTaskResult with {"clientKey": "__CAPSOLVER_KEY__", "taskId": TASK_ID}
   every 3s, max 10 polls. status "ready" -> token in solution.token (Turnstile)
   or solution.gRecaptchaResponse (re/hCaptcha).
3. INJECT the token into the widget's response field (e.g. [name="cf-turnstile-response"],
   [name="g-recaptcha-response"], [name="h-captcha-response"]), then click Submit/Verify.
If createTask returns errorId > 0, or after 30s, go to MANUAL FALLBACK.

MANUAL FALLBACK: try the audio challenge or solve a simple text/logic puzzle
yourself. If still blocked -> RESULT:CAPTCHA."""

_CAPTCHA_UNCONFIGURED = """== CAPTCHA ==
CapSolver is NOT CONFIGURED (no CAPSOLVER_API_KEY). If a CAPTCHA blocks you, try
the MANUAL FALLBACK: the audio challenge, or solve a simple text/logic puzzle
yourself. If it's unsolvable, output RESULT:CAPTCHA."""


def _captcha_section(capsolver_key: str) -> str:
    if not capsolver_key:
        return _CAPTCHA_UNCONFIGURED
    return _CAPTCHA_FLOW.replace("__CAPSOLVER_KEY__", capsolver_key)


def _resolve_capsolver_key(capsolver_key: str | None) -> str:
    """The CapSolver key from the arg, else CAPSOLVER_API_KEY env (never baked in
    when absent). Shared by both prompt builders."""
    return capsolver_key if capsolver_key is not None else os.environ.get("CAPSOLVER_API_KEY", "")


def _result_codes() -> str:
    return (
        "== RESULT (output EXACTLY one line) ==\n"
        "RESULT:APPLIED -- submitted successfully\n"
        "RESULT:EXPIRED -- job closed / no longer accepting applications\n"
        "RESULT:CAPTCHA -- blocked by an unsolvable CAPTCHA\n"
        "RESULT:LOGIN_ISSUE -- could not sign in or create an account\n"
        "RESULT:FAILED:reason -- any other failure (brief reason, e.g. "
        "cloudflare_blocked, not_a_job_application, stuck)"
    )


def _login_rules(profile: ApplyProfile, *, ats_password: str,
                 verification_available: bool) -> str:
    """Login-or-signup rules. With a stored ATS password the agent signs in or
    creates an account; with email verification available it fetches the code via
    the read_verification_code tool. SSO is forbidden in _never_do, so it's not
    repeated here."""
    lines = ["== LOGIN & ACCOUNT (many ATS require an account) ==",
             f"- At a login wall, use the candidate's email: {profile.email}."]
    if ats_password:
        # The literal password is needed so the agent can type it into the field;
        # this prompt is built and consumed locally (auto-apply never runs in the
        # cloud), so the secret never leaves the machine.
        lines.append(f"- Sign in with that email and this password: {ats_password}")
        lines.append("- If no account exists yet, CREATE one (sign up) with the same "
                     "email and password.")
    else:
        lines.append("- Try the candidate's email; if it needs a password you don't "
                     "have, do not guess it.")
    if verification_available:
        lines.append("- If the site emails a confirmation link or verification code, "
                     "call the read_verification_code tool to fetch it, then enter it.")
    lines.append("- Cannot sign in or create an account -> RESULT:LOGIN_ISSUE.")
    return "\n".join(lines)


def build_submit_prompt(*, capsolver_key: str | None = None) -> str:
    """Second-turn prompt: the form is already filled and parked at its final
    review step in the open browser; click the final Submit and confirm. The
    answers were drafted in the review turn — do not change them."""
    capsolver_key = _resolve_capsolver_key(capsolver_key)
    return f"""You are resuming a job application already filled out and parked at its final
review step in the open browser. The answers were drafted in a prior turn — do NOT
change them.

== STEP-BY-STEP ==
1. browser_snapshot to confirm you are at the final review / submit step.
2. Click the final Submit/Apply button.
3. Submit often triggers a CAPTCHA — run CAPTCHA DETECT and solve it.
4. Confirm a "thank you / application received" state, then output your RESULT.

{_captcha_section(capsolver_key)}

{_result_codes()}"""


def build_prompt(job: ApplyJob, profile: ApplyProfile, *, resume_pdf: str = "",
                 cv_text: str = "", cover_letter_text: str = "",
                 ats_password: str = "", verification_available: bool = False,
                 dry_run: bool = False, capsolver_key: str | None = None) -> str:
    """The full instruction prompt for the agentic apply engine.

    `job`/`profile` supply the target + candidate; `resume_pdf` is the upload path
    and `cv_text` grounds skill/tenure answers. `dry_run` stops the agent before
    Submit. `capsolver_key` defaults to CAPSOLVER_API_KEY; when absent the CAPTCHA
    section degrades to a manual fallback (no key is ever baked into the prompt)."""
    capsolver_key = _resolve_capsolver_key(capsolver_key)

    if dry_run:
        submit = ("Fill and review EVERY field, then STOP at the final review step. "
                  "Do NOT click the final Submit/Apply button — output RESULT:READY "
                  "(the form is filled and parked for the candidate to submit).")
    else:
        submit = ("Before clicking Submit, snapshot and verify EVERY field against the "
                  "profile and resume; fix anything wrong, then click Submit.")

    cover = cover_letter_text.strip() or (
        "None provided. Skip if optional; if required, write 2 factual sentences from "
        "the resume tying the candidate's experience to this role.")
    phone_digits = "".join(c for c in profile.phone if c.isdigit())

    return f"""You are an autonomous job-application agent. Mission: submit a complete, accurate
application for this candidate. You drive a real browser via Playwright MCP. Think
strategically, act decisively, and finish by outputting one RESULT line.

== JOB ==
URL: {job.url}
Company: {job.company}
Role: {job.role}

== FILES ==
Resume PDF (upload this): {resume_pdf or "N/A"}

== COVER LETTER ==
{cover}

{_profile_block(profile, cv_text)}

{_hard_rules(profile)}

{_never_do()}

{_salary_rules(profile)}

{_screening_rules()}

{_login_rules(profile, ats_password=ats_password, verification_available=verification_available)}

== HAND OFF (check before filling) ==
If the apply flow is one a faster deterministic engine owns, STOP and hand it back
instead of driving it the slow way — recognize this by URL/redirect, before filling:
- LinkedIn Easy Apply modal -> RESULT:DEFER:linkedin
- Indeed SmartApply (smartapply.indeed.com or an "Apply with Indeed" button) -> RESULT:DEFER:indeed

== STEP-BY-STEP ==
1. browser_navigate to the job URL; browser_snapshot to read it. Run CAPTCHA DETECT.
2. If the posting is closed / "no longer accepting applications" -> RESULT:EXPIRED.
3. Click Apply. Many sites trigger a CAPTCHA right after — DETECT and solve.
4. Login wall? Follow the LOGIN & ACCOUNT rules above.
5. Upload the resume PDF (fresh — replace any auto-parsed one).
6. Check ALL pre-filled fields against the profile; fix parser mistakes; fill blanks.
   Phone digits only when a field has a country prefix: {phone_digits}. Dates: {datetime.now().strftime('%m/%d/%Y')}.
7. Answer screening questions per the rules above.
8. {submit}
9. After submit: snapshot, run CAPTCHA DETECT (submit often triggers one), confirm a
   "thank you / application received" state, then output your RESULT.

{_captcha_section(capsolver_key)}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck.
- Cloudflare / bot wall you can't clear -> RESULT:FAILED:cloudflare_blocked.
- Page broken / 500 / blank -> RESULT:FAILED:page_error.
Stop immediately and output your RESULT. Do not loop.

{_result_codes()}"""
