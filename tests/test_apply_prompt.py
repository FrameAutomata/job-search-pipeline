"""Contract tests for the agentic apply prompt builder (pipeline/apply/prompt.py).

The prompt itself is a large instruction string driving a claude+MCP agent; we
don't assert its prose, only the load-bearing contract: the job + candidate data
are present, the RESULT protocol the runner parses is emitted, submit posture is
honored, and the proxyless CapSolver section is included ONLY when a key is set
(and never leaks a key when it isn't). Ported/adapted from ApplyPilot's prompt.py."""

import pytest

from pipeline.apply.profile import ApplyProfile
from pipeline.apply.queue import ApplyJob
from pipeline.apply.prompt import build_prompt, build_submit_prompt, build_resume_prompt


def _job():
    return ApplyJob(num="5", company="Globex", role="Backend Engineer",
                    url="https://www.indeed.com/viewjob?jk=abc123", score=4.5)


def _profile(**kw):
    base = dict(full_name="Thomas Thirlwall", email="t@example.com",
                phone="+1 (555) 000-1111", city="Dallas", country="USA",
                requires_sponsorship=False, salary_target=150000)
    base.update(kw)
    return ApplyProfile(**base)


class TestBuildPrompt:
    def test_includes_job_url_company_role(self):
        p = build_prompt(_job(), _profile())
        assert "indeed.com/viewjob?jk=abc123" in p
        assert "Globex" in p and "Backend Engineer" in p

    def test_includes_profile_summary(self):
        p = build_prompt(_job(), _profile())
        assert "Thomas Thirlwall" in p and "t@example.com" in p

    def test_includes_resume_path_and_cv(self):
        p = build_prompt(_job(), _profile(), resume_pdf="C:/x/Thomas_Resume.pdf",
                         cv_text="Built Kubernetes platforms at NewCo (2024-present)")
        assert "C:/x/Thomas_Resume.pdf" in p
        assert "Kubernetes" in p

    def test_result_protocol_present(self):
        p = build_prompt(_job(), _profile())
        for code in ("RESULT:APPLIED", "RESULT:EXPIRED", "RESULT:CAPTCHA",
                     "RESULT:LOGIN_ISSUE", "RESULT:FAILED"):
            assert code in p, code

    def test_captcha_section_included_with_key(self):
        p = build_prompt(_job(), _profile(), capsolver_key="CAPKEY123")
        assert "api.capsolver.com" in p
        assert "CAPKEY123" in p                          # key wired into the solve flow
        assert "AntiTurnstileTaskProxyLess" in p          # proxyless task type

    def test_captcha_section_no_key_no_leak(self):
        p = build_prompt(_job(), _profile(), capsolver_key="")
        assert "NOT CONFIGURED" in p or "manual" in p.lower()
        assert "clientKey: ''" not in p                  # don't bake an empty-key live call

    def test_hcaptcha_fast_paths_to_human_not_capsolver(self):
        """CapSolver discontinued hCaptcha, so the agent must NOT burn a CapSolver
        round-trip on it (the iCIMS dry-run showed it triggering the invisible
        widget into a visible challenge for nothing). An hCaptcha -> NEEDS_HUMAN
        straight away; only reCAPTCHA / Turnstile still go through CapSolver."""
        p = build_prompt(_job(), _profile(), capsolver_key="CAPKEY123")
        assert "HCaptchaTaskProxyLess" not in p           # the dead task type is gone
        assert "no longer supports hcaptcha" in p.lower() # the why, so the agent trusts it
        assert "AntiTurnstileTaskProxyLess" in p          # reCAPTCHA/Turnstile path stays

    def test_salary_never_states_figure_uses_negotiable_or_target(self):
        p = build_prompt(_job(), _profile(salary_target=150000))
        assert "negotiable" in p.lower() or "150" in p

    def test_hard_rules_reflect_sponsorship(self):
        needs = build_prompt(_job(), _profile(requires_sponsorship=True))
        assert "sponsorship" in needs.lower()
        assert "work auth" in needs.lower() or "authorization" in needs.lower()

    def test_never_unsafe_rules_present(self):
        p = build_prompt(_job(), _profile())
        assert "unsafe_permissions" in p or "permission" in p.lower()


class TestReviewVsLiveSubmit:
    """Review mode is the fill-and-STOP turn: the agent parks at the final review
    step and signals RESULT:READY (never a submission). Live mode submits."""

    def test_review_parks_and_emits_ready(self):
        dry = build_prompt(_job(), _profile(), dry_run=True)
        assert "RESULT:READY" in dry            # the hold signal the adapter maps
        assert "do not click" in dry.lower()    # parks before the final submit

    def test_live_submits_not_ready(self):
        live = build_prompt(_job(), _profile(), dry_run=False)
        assert "RESULT:READY" not in live       # live mode never parks
        assert "submit" in live.lower()         # it clicks Submit
        assert "do not click" not in live.lower()  # and doesn't hold


class TestNeedsHumanFallback:
    def test_captcha_fallback_asks_for_a_human(self):
        # An unsolvable CAPTCHA parks for a person, not a hard give-up.
        p = build_prompt(_job(), _profile())
        assert "RESULT:NEEDS_HUMAN" in p


class TestResumePrompt:
    def test_continues_from_here_without_renavigating(self):
        rp = build_resume_prompt()
        low = rp.lower()
        assert "continue" in low
        assert "do not" in low and ("navigate" in low or "reload" in low or "start over" in low)
        assert "RESULT:READY" in rp        # stops at review like the first turn


class TestDeferDetection:
    """The agent should bail to the deterministic engines when it lands on a
    fast-apply flow rather than driving it the slow way."""

    def test_prompt_instructs_defer_on_fast_apply(self):
        p = build_prompt(_job(), _profile())
        assert "RESULT:DEFER" in p
        assert "smartapply" in p.lower() or "easy apply" in p.lower()


class TestSubmitPrompt:
    """The second turn over the parked browser: click the final Submit, don't
    re-fill, report the terminal outcome."""

    def test_clicks_submit_and_reports_applied(self):
        p = build_submit_prompt()
        assert "submit" in p.lower()
        assert "RESULT:APPLIED" in p

    def test_does_not_refill(self):
        # It must not re-fill — the answers were already drafted in the review turn.
        p = build_submit_prompt()
        assert "change them" in p.lower()       # "...do NOT change them"


class TestLoginAndSignup:
    """The login-or-signup variant: with a stored ATS password the agent logs in
    or CREATES an account; with email verification available it fetches the code."""

    def test_with_password_enables_account_creation(self):
        p = build_prompt(_job(), _profile(), ats_password="hunter2")
        assert "hunter2" in p                    # the agent needs the value to type it
        assert "account" in p.lower() and ("create" in p.lower() or "sign up" in p.lower())

    def test_existing_account_signs_in_rather_than_re_registering(self):
        """An account auto-apply (or the user) already made must be signed into, not
        re-created with the same fixed password: try sign-in FIRST, and if a create
        attempt reports the email is already registered, switch to signing in
        instead of looping on registration."""
        p = build_prompt(_job(), _profile(), ats_password="hunter2").lower()
        assert "signing in first" in p           # sign-in is the primary path
        assert "already registered" in p         # already-registered -> sign in, don't re-register

    def test_without_password_routes_to_login_issue(self):
        p = build_prompt(_job(), _profile())     # no creds
        assert "hunter2" not in p
        # No account-creation instruction; an unscalable login wall bails out.
        assert "LOGIN_ISSUE" in p

    def test_login_wall_routes_to_login_issue_not_needs_human(self):
        """A sign-in / account-creation wall the agent can't pass must report
        LOGIN_ISSUE, NOT NEEDS_HUMAN — NEEDS_HUMAN is scoped to CAPTCHA / anti-bot
        challenges. Without this disambiguation the agent picks NEEDS_HUMAN for a
        login wall (it saw "a person can clear it"), so the UI mislabels it as a
        CAPTCHA and the CLI tally is wrong (see the Suffolk/iCIMS dry-run)."""
        p = build_prompt(_job(), _profile())
        assert "not needs_human" in p.lower()

    def test_verification_tool_mentioned_only_when_available(self):
        with_v = build_prompt(_job(), _profile(), verification_available=True)
        without_v = build_prompt(_job(), _profile(), verification_available=False)
        assert "read_verification_code" in with_v
        assert "read_verification_code" not in without_v
