"""Tests for the auto-apply engine's pure logic (no browser).

Covers result classification, profile loading, the deterministic+cache+LLM
answer engine, and tracker candidate selection. The Playwright-driven modules
(browser.py, linkedin.py) are verified manually via `--apply-mode dry-run`."""

import textwrap
from pathlib import Path

import pytest

import pipeline.apply as apply_pkg
from pipeline.apply import linkedin, queue, result
from pipeline.apply.answers import AnswerEngine, _match_option, _sanitize
from pipeline.apply.profile import ApplyProfile, _parse_salary, _parse_salary_target


# ── result.py ────────────────────────────────────────────────────────────────

class TestResult:
    def test_failed_helper_is_not_permanent(self):
        r = result.failed("validation_error")
        assert r.code == "failed" and r.reason == "validation_error"
        assert r.permanent is False
        assert r.applied is False

    def test_known_terminal_codes_are_permanent(self):
        for code in ("not_easy_apply", "expired", "login_issue", "not_eligible"):
            assert result.ApplyResult(code=code).permanent is True

    def test_applied_submitted_flags(self):
        r = result.ApplyResult(code="applied", submitted=True)
        assert r.applied is True and r.submitted is True
        held = result.ApplyResult(code="applied", reason="not submitted (mode=review)")
        assert held.applied is True and held.submitted is False

    def test_str_includes_reason(self):
        assert str(result.failed("boom")) == "failed:boom"
        assert str(result.ApplyResult(code="expired")) == "expired"


# ── profile.py ───────────────────────────────────────────────────────────────

class TestParseSalary:
    @pytest.mark.parametrize("raw,expected", [
        ("$75K", 75000),
        ("130,000", 130000),
        ("$130K-170K", 130000),   # first number wins
        ("$1.2M", 1200000),
        ("", None),
        (None, None),
        (95000, 95000),
    ])
    def test_parse(self, raw, expected):
        assert _parse_salary(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("$130K-$170K", 150000),   # midpoint of a range
        ("$130K - 170K", 150000),
        ("$150K", 150000),         # single value
        ("", None),
        (None, None),
    ])
    def test_parse_target(self, raw, expected):
        assert _parse_salary_target(raw) == expected


class TestApplyProfile:
    def _write(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True)
        (cfg / "profile.yml").write_text(textwrap.dedent(body), encoding="utf-8")
        return tmp_path

    def test_load_full(self, tmp_path):
        co = self._write(tmp_path, """
            candidate:
              full_name: Thomas Thirlwall
              email: t@example.com
              phone: "+1 (956) 525-3015"
              linkedin: linkedin.com/in/x
            location:
              country: United States
              city: Dallas
            work_authorization:
              citizenship: US
              legally_authorized_to_work_in: [United States]
              requires_sponsorship: false
              work_permit_type: Citizen
              eligible_countries: [United States]
            compensation:
              minimum: $75K
              currency: USD
        """)
        p = ApplyProfile.load(co)
        assert p.full_name == "Thomas Thirlwall"
        assert p.first_name == "Thomas" and p.last_name == "Thirlwall"
        assert p.phone_digits == "19565253015"
        assert p.requires_sponsorship is False
        assert p.authorized_regions == ["United States"]
        assert p.salary_floor == 75000

    def test_eligible_countries_default_to_authorized(self, tmp_path):
        co = self._write(tmp_path, """
            work_authorization:
              legally_authorized_to_work_in: [Canada]
              requires_sponsorship: true
        """)
        p = ApplyProfile.load(co)
        assert p.eligible_countries == ["Canada"]
        assert p.requires_sponsorship is True

    def test_missing_file_is_empty_defaults(self, tmp_path):
        p = ApplyProfile.load(tmp_path)
        assert p.full_name == "" and p.salary_floor is None


# ── answers.py ───────────────────────────────────────────────────────────────

@pytest.fixture
def profile():
    return ApplyProfile(
        full_name="Thomas Thirlwall", email="t@example.com", phone="+1 (956) 525-3015",
        city="Dallas", country="United States", linkedin="linkedin.com/in/x",
        citizenship="US", authorized_regions=["United States"],
        requires_sponsorship=False, salary_floor=75000, salary_target=150000,
    )


class TestAnswerEngineDeterministic:
    def _engine(self, profile, tmp_path):
        # Caller that explodes if invoked — deterministic answers must not hit it.
        def boom(system, user):
            raise AssertionError("LLM should not be called for deterministic fields")
        return AnswerEngine(profile, tmp_path / "cache.json", caller=boom)

    def test_contact_fields(self, profile, tmp_path):
        e = self._engine(profile, tmp_path)
        assert e.answer("First name", "text") == "Thomas"
        assert e.answer("Last name", "text") == "Thirlwall"
        assert e.answer("Email address", "text") == "t@example.com"
        assert e.answer("Mobile phone number", "numeric") == "19565253015"

    def test_sponsorship_and_authorization(self, profile, tmp_path):
        e = self._engine(profile, tmp_path)
        assert e.answer("Will you now or in the future require sponsorship?",
                        "select", ["Yes", "No"]) == "No"
        assert e.answer("Are you legally authorized to work in the United States?",
                        "select", ["Yes", "No"]) == "Yes"

    def test_sponsorship_required_flips_answers(self, tmp_path):
        p = ApplyProfile(requires_sponsorship=True)
        def boom(s, u):
            raise AssertionError
        e = AnswerEngine(p, tmp_path / "c.json", caller=boom)
        assert e.answer("Do you require visa sponsorship?", "select", ["Yes", "No"]) == "Yes"
        assert e.answer("Authorized to work without sponsorship?", "select", ["Yes", "No"]) == "No"

    def test_eeo_declines(self, profile, tmp_path):
        e = self._engine(profile, tmp_path)
        assert e.answer("Gender", "select", ["Male", "Female", "Prefer not to say"]) == "Prefer not to say"
        assert e.answer("Veteran status", "text") == "Prefer not to say"

    def test_salary_text_is_negotiable(self, profile, tmp_path):
        # Never reveal the walk-away minimum; a text salary field gets "Negotiable".
        e = self._engine(profile, tmp_path)
        assert e.answer("What are your salary expectations?", "text") == "Negotiable"

    def test_salary_numeric_uses_target_not_floor(self, profile, tmp_path):
        # A numeric salary field can't take "Negotiable" → use the target (150000),
        # NOT the floor (75000).
        e = self._engine(profile, tmp_path)
        assert e.answer("Desired salary", "numeric") == "150000"


class TestAnswerEngineCache:
    def test_llm_called_once_then_cached(self, profile, tmp_path):
        calls = []
        def caller(system, user):
            calls.append(user)
            return "5"
        cache = tmp_path / "cache.json"
        e = AnswerEngine(profile, cache, caller=caller)
        q = "How many years of experience do you have with Kubernetes?"
        assert e.answer(q, "numeric") == "5"
        assert e.answer(q, "numeric") == "5"        # second call → cache
        assert len(calls) == 1                      # LLM hit exactly once
        assert e.cache_hits == 1 and e.llm_calls == 1
        assert cache.exists()                       # persisted

        # A fresh engine reuses the on-disk cache without calling the LLM.
        e2 = AnswerEngine(profile, cache, caller=caller)
        assert e2.answer(q, "numeric") == "5"
        assert len(calls) == 1

    def test_llm_answer_mapped_to_option(self, profile, tmp_path):
        def caller(system, user):
            return "Yes, definitely"
        e = AnswerEngine(profile, tmp_path / "c.json", caller=caller)
        # Novel question (not deterministic) with options → mapped onto an option.
        ans = e.answer("Are you comfortable working night shifts?", "select", ["Yes", "No"])
        assert ans == "Yes"

    def test_no_provider_raises(self, profile, tmp_path, monkeypatch):
        # caller=None forces auto-detect; with no keys it must fail loudly
        # (a missing provider is a setup error, not a per-field fallback).
        monkeypatch.setattr("pipeline.batch_evaluate._detect_provider", lambda: None)
        e = AnswerEngine(profile, tmp_path / "c.json", caller=None)
        with pytest.raises(RuntimeError, match="no LLM provider"):
            e.answer("Describe your ideal team.", "textarea")

    def test_llm_failure_falls_back_and_flags(self, profile, tmp_path):
        # A configured provider that fails the call (after retries) must NOT skip
        # the job — fall back to a best-effort placeholder and flag for review.
        def boom(system, user):
            raise RuntimeError("provider returned empty content")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=boom)
        assert e.answer("Tell us about a project you led.", "textarea") == ""   # blank free text
        assert e.answer("Years of experience with Go?", "numeric") == "0"
        assert e.answer("Are you willing to relocate to Mars?",
                        "select", ["Yes", "No", "Prefer not to say"]) == "Prefer not to say"
        assert e.unanswered == [
            "Tell us about a project you led.",
            "Years of experience with Go?",
            "Are you willing to relocate to Mars?",
        ]


class TestAnswerHelpers:
    def test_sanitize_normalizes(self):
        assert _sanitize("  How  many YEARS?? ") == "how many years"

    def test_match_option_fuzzy(self):
        opts = ["Yes", "No", "Prefer not to say"]
        assert _match_option("yes", opts) == "Yes"
        assert _match_option("I prefer not to say", opts) == "Prefer not to say"
        assert _match_option("totally unrelated", opts) == "Yes"  # falls back to first


# ── queue.py ─────────────────────────────────────────────────────────────────

_TRACKER = """\
# Applications Tracker

| # | Date | Company | Role | Score | Status | PDF | Report | Notes |
|---|------|---------|------|-------|--------|-----|--------|-------|
| 1 | 2026-06-01 | Acme | Engineer | 4.2/5 | Evaluated | ❌ | [001](reports/001.md) | https://www.linkedin.com/jobs/view/123 — strong fit |
| 2 | 2026-06-01 | Globex | Dev | 4.5/5 | Evaluated | ❌ | [002](reports/002.md) | https://boards.greenhouse.io/x/jobs/9 — offsite ATS |
| 3 | 2026-06-01 | Initech | SRE | 4.9/5 | Applied | ❌ | [003](reports/003.md) | https://www.linkedin.com/jobs/view/456 |
| 4 | 2026-06-01 | Umbrella | QA | 2.0/5 | Evaluated | ❌ | [004](reports/004.md) | https://www.linkedin.com/jobs/view/789 |
| 5 | 2026-06-01 | Hooli | Backend | 4.8/5 | Evaluated | ❌ | [005](reports/005.md) | https://www.linkedin.com/jobs/view/999 |
"""


class TestQueueSelect:
    def _career_ops(self, tmp_path: Path) -> Path:
        d = tmp_path / "data"
        d.mkdir(parents=True)
        (d / "applications.md").write_text(_TRACKER, encoding="utf-8")
        return tmp_path

    def test_selects_only_eligible_linkedin_evaluated(self, tmp_path):
        co = self._career_ops(tmp_path)
        jobs = queue.select(co, min_score=4.0, linkedin_only=True)
        nums = [j.num for j in jobs]
        # #1 Acme + #5 Hooli (linkedin, Evaluated, score>=4). #2 non-LI, #3 Applied,
        # #4 below score → all excluded.
        assert nums == ["5", "1"]            # sorted by score desc (4.8, 4.2)
        assert all("linkedin.com/jobs/view" in j.url for j in jobs)

    def test_limit(self, tmp_path):
        co = self._career_ops(tmp_path)
        assert len(queue.select(co, min_score=4.0, limit=1)) == 1

    def test_linkedin_only_false_includes_other_ats(self, tmp_path):
        co = self._career_ops(tmp_path)
        jobs = queue.select(co, min_score=4.0, linkedin_only=False)
        assert "2" in [j.num for j in jobs]  # Globex greenhouse now included

    def test_missing_tracker_returns_empty(self, tmp_path):
        assert queue.select(tmp_path, min_score=4.0) == []

    def test_explicit_applications_md_override(self, tmp_path):
        """applications_md points the queue at a refreshed artifact's tracker,
        not the (possibly absent/stale) local one."""
        co = tmp_path / "career-ops"
        co.mkdir()  # deliberately no data/applications.md here
        art = tmp_path / "artifact" / "data"
        art.mkdir(parents=True)
        (art / "applications.md").write_text(_TRACKER, encoding="utf-8")
        jobs = queue.select(co, min_score=4.0, applications_md=art / "applications.md")
        assert [j.num for j in jobs] == ["5", "1"]
        # Without the override, the empty local career-ops yields nothing.
        assert queue.select(co, min_score=4.0) == []


class TestQueueHelpers:
    def test_is_linkedin_job(self):
        assert queue.is_linkedin_job("https://www.linkedin.com/jobs/view/123") is True
        assert queue.is_linkedin_job("https://linkedin.com/jobs/view/9?x=1") is True
        assert queue.is_linkedin_job("https://boards.greenhouse.io/x") is False
        assert queue.is_linkedin_job("not a url") is False

    def test_extract_url_strips_trailing_punctuation(self):
        assert queue._extract_url("see https://x.com/a/b, fits") == "https://x.com/a/b"


# ── linkedin.py pure helpers (no browser) ────────────────────────────────────

class TestOptoutCheckbox:
    def test_detects_follow_and_marketing(self):
        for lbl in ("Follow Apexon to stay up to date with their page.",
                    "Subscribe to our newsletter",
                    "Receive marketing emails",
                    "Opt-in to promotional updates"):
            assert linkedin._is_optout_checkbox(lbl) is True

    def test_normal_consent_not_optout(self):
        assert linkedin._is_optout_checkbox("I agree to the terms and conditions") is False
        assert linkedin._is_optout_checkbox("I certify the information is accurate") is False


class TestResumeResolution:
    def test_prefers_resume_path_env(self, tmp_path, monkeypatch):
        pdf = tmp_path / "my_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        monkeypatch.setenv("RESUME_PATH", str(pdf))
        assert linkedin._resume_pdf() == pdf

    def test_coerces_txt_path_to_pdf(self, tmp_path, monkeypatch):
        pdf = tmp_path / "r.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        monkeypatch.setenv("RESUME_PATH", str(tmp_path / "r.txt"))  # points at .txt sibling
        assert linkedin._resume_pdf() == pdf

    def test_falls_back_when_resume_path_missing(self, monkeypatch):
        monkeypatch.setenv("RESUME_PATH", str(Path("/nonexistent/x.pdf")))
        r = linkedin._resume_pdf()
        # Either the repo's default resumes/resume.pdf, or None if absent.
        assert r is None or r.name == "resume.pdf"


class TestTailoredResume:
    def _job(self, company):
        return queue.ApplyJob(num="1", company=company, role="Eng",
                              url="https://www.linkedin.com/jobs/view/1", score=4.5)

    def test_finds_by_company_slug(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "CV - Apexon Inc.pdf").write_bytes(b"%PDF-1.4")
        (out / "CV - Globex.pdf").write_bytes(b"%PDF-1.4")
        found = apply_pkg._find_tailored_resume(tmp_path, self._job("Apexon"))
        assert found is not None and found.name == "CV - Apexon Inc.pdf"

    def test_none_when_no_company_match(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "CV - Globex.pdf").write_bytes(b"%PDF-1.4")
        assert apply_pkg._find_tailored_resume(tmp_path, self._job("Apexon")) is None

    def test_none_when_dir_absent(self, tmp_path):
        assert apply_pkg._find_tailored_resume(tmp_path, self._job("Apexon")) is None


class TestCoverLetterLazy:
    """The cover letter is generated only on first request (a form that asks for
    one), then cached — so we never generate for a form that doesn't."""

    def test_generated_once_then_cached(self, profile, tmp_path):
        calls = []
        def gen():
            calls.append(1)
            return "Dear team, I build APIs.\nThomas"
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        e.cover_letter_provider = gen
        assert e.cover_letter().startswith("Dear team")
        assert e.cover_letter().startswith("Dear team")
        assert len(calls) == 1  # provider invoked exactly once

    def test_no_provider_returns_empty(self, profile, tmp_path):
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        assert e.cover_letter() == ""

    def test_provider_failure_returns_empty(self, profile, tmp_path):
        def boom():
            raise RuntimeError("provider down")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        e.cover_letter_provider = boom
        assert e.cover_letter() == ""

    def test_cover_letter_pdf_needs_letter_then_renders(self, profile, tmp_path):
        pdf = tmp_path / "cover.pdf"
        pdf.write_bytes(b"%PDF")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        # No letter yet → no PDF (request-gated, never renders for a form that
        # doesn't ask).
        assert e.cover_letter_pdf() is None
        # Once the letter is available and a pdf provider is set, returns the path.
        e.cover_letter_provider = lambda: "Dear team"
        e.cover_pdf_provider = lambda: pdf
        assert e.cover_letter_pdf() == pdf
