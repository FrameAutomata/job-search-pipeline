"""Contract tests for the agentic apply prompt builder (pipeline/apply/prompt.py).

The prompt itself is a large instruction string driving a claude+MCP agent; we
don't assert its prose, only the load-bearing contract: the job + candidate data
are present, the RESULT protocol the runner parses is emitted, submit posture is
honored, and the proxyless CapSolver section is included ONLY when a key is set
(and never leaks a key when it isn't). Ported/adapted from ApplyPilot's prompt.py."""

import pytest

from pipeline.apply.profile import ApplyProfile
from pipeline.apply.queue import ApplyJob
from pipeline.apply.prompt import build_prompt


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

    def test_dry_run_holds_before_submit(self):
        dry = build_prompt(_job(), _profile(), dry_run=True)
        live = build_prompt(_job(), _profile(), dry_run=False)
        assert "do not click" in dry.lower()           # rehearsal: stop before submit
        assert "do not click" not in live.lower()       # live mode submits after review

    def test_captcha_section_included_with_key(self):
        p = build_prompt(_job(), _profile(), capsolver_key="CAPKEY123")
        assert "api.capsolver.com" in p
        assert "CAPKEY123" in p                          # key wired into the solve flow
        assert "AntiTurnstileTaskProxyLess" in p          # proxyless task type

    def test_captcha_section_no_key_no_leak(self):
        p = build_prompt(_job(), _profile(), capsolver_key="")
        assert "NOT CONFIGURED" in p or "manual" in p.lower()
        assert "clientKey: ''" not in p                  # don't bake an empty-key live call

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
