"""Contract tests for the deterministic Indeed SmartApply engine
(pipeline/apply/indeed.py).

The page-driving (apply_to, step fills) is verified manually like linkedin.py;
here we pin the pure surface: cookie preparation for session injection, SmartApply
step classification, and primary-action (submit-beats-continue) selection."""

import pytest

from pipeline.apply import indeed
from pipeline.apply import queue as _queue
from pipeline.apply.profile import ApplyProfile
from pipeline.apply.indeed import prepare_indeed_cookies, _classify_step, _primary_action

_MIXED_TRACKER = """# Applications Tracker

| # | Date | Company | Role | Score | Status | PDF | Report | Notes |
|---|------|---------|------|-------|--------|-----|--------|-------|
| 1 | 2026-06-01 | Acme | Eng | 4.2/5 | Evaluated | x | [001](reports/001.md) | https://www.linkedin.com/jobs/view/123 |
| 2 | 2026-06-01 | Globex | Dev | 4.5/5 | Evaluated | x | [002](reports/002.md) | https://www.indeed.com/viewjob?jk=xyz789 |
| 3 | 2026-06-01 | Hooli | SRE | 4.8/5 | Evaluated | x | [003](reports/003.md) | https://boards.greenhouse.io/x/jobs/9 |
"""


class TestQueueSites:
    def test_is_indeed_job(self):
        assert _queue.is_indeed_job("https://www.indeed.com/viewjob?jk=abc")
        assert not _queue.is_indeed_job("https://www.linkedin.com/jobs/view/1")
        assert not _queue.is_indeed_job("https://boards.greenhouse.io/x")

    def test_job_site(self):
        assert _queue.job_site("https://www.indeed.com/viewjob?jk=abc") == "indeed"
        assert _queue.job_site("https://www.linkedin.com/jobs/view/1") == "linkedin"
        # Off-site ATS now routes to the agentic catch-all, not None (see
        # TestJobSite in test_apply.py for the full contract).
        assert _queue.job_site("https://greenhouse.io/x") == "agent"

    def test_select_sites_admits_indeed_excludes_other_ats(self, tmp_path):
        (tmp_path / "data").mkdir(parents=True)
        (tmp_path / "data" / "applications.md").write_text(_MIXED_TRACKER, encoding="utf-8")
        jobs = _queue.select(tmp_path, min_score=4.0, sites=("linkedin", "indeed"))
        assert {j.num for j in jobs} == {"1", "2"}  # greenhouse #3 excluded


class TestPrepareCookies:
    def _raw(self):
        return [
            {"name": "cf_clearance", "value": "x", "domain": ".indeed.com", "path": "/"},
            {"name": "__cf_bm", "value": "y", "domain": ".indeed.com", "path": "/"},
            {"name": "SHOE", "value": "sess", "domain": ".indeed.com", "path": "/",
             "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"},
            {"name": "rememberMe", "value": "rm", "domain": ".indeed.com", "path": "/",
             "expires": 2000000000.0, "sameSite": "None"},
        ]

    def test_excludes_cloudflare_cookies(self):
        out = prepare_indeed_cookies(self._raw(), now=1000.0)
        names = {c["name"] for c in out}
        assert names == {"SHOE", "rememberMe"}  # cf_clearance / __cf_bm dropped

    def test_shape_has_required_fields(self):
        out = prepare_indeed_cookies(self._raw(), now=1000.0)
        for c in out:
            assert set(("name", "value", "domain", "path")) <= set(c)

    def test_session_cookie_gets_persistent_expiry(self):
        out = prepare_indeed_cookies(self._raw(), now=1000.0, persist_days=30)
        shoe = next(c for c in out if c["name"] == "SHOE")
        assert shoe["expires"] == int(1000.0 + 30 * 86400)

    def test_persistent_cookie_keeps_its_expiry(self):
        out = prepare_indeed_cookies(self._raw(), now=1000.0)
        rm = next(c for c in out if c["name"] == "rememberMe")
        assert rm["expires"] == 2000000000.0

    def test_preserves_valid_flags_only(self):
        raw = [{"name": "A", "value": "1", "domain": ".indeed.com", "path": "/",
                "expires": 2000000000.0, "secure": True, "httpOnly": True, "sameSite": "Bogus"}]
        out = prepare_indeed_cookies(raw, now=1000.0)
        c = out[0]
        assert c["secure"] is True and c["httpOnly"] is True
        assert "sameSite" not in c  # invalid sameSite dropped


class TestClassifyStep:
    @pytest.mark.parametrize("url,expected", [
        ("https://smartapply.indeed.com/beta/indeedapply/form/resume-selection-module/resume-selection", "resume-selection"),
        ("https://smartapply.indeed.com/beta/indeedapply/form/questions-module/questions/1", "questions"),
        ("https://smartapply.indeed.com/beta/indeedapply/form/demographic-questions-module/demographic-questions/1", "demographic"),
        ("https://smartapply.indeed.com/beta/indeedapply/form/review-module/review", "review"),
        ("https://smartapply.indeed.com/beta/indeedapply/form/something-else/x", "unknown"),
    ])
    def test_from_url(self, url, expected):
        assert _classify_step(url) == expected

    def test_demographic_beats_questions_substring(self):
        # "demographic-questions" contains "questions" — must classify as demographic
        url = "https://smartapply.indeed.com/x/demographic-questions-module/demographic-questions/1"
        assert _classify_step(url) == "demographic"

    def test_review_from_heading(self):
        assert _classify_step("https://smartapply.indeed.com/x/unknown-mod/y",
                              "Review your application") == "review"


class TestDemographicPref:
    _KEYS = ("APPLY_EEO_DATA_CONSENT", "APPLY_EEO_SAVE_ANSWERS", "APPLY_EEO_SHARE_ANSWERS")

    def test_defaults_consent_on_save_share_off(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        assert indeed._demographic_pref("consent") is True
        assert indeed._demographic_pref("save") is False
        assert indeed._demographic_pref("share") is False

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("APPLY_EEO_DATA_CONSENT", "off")
        monkeypatch.setenv("APPLY_EEO_SHARE_ANSWERS", "true")
        assert indeed._demographic_pref("consent") is False
        assert indeed._demographic_pref("share") is True

    def test_reads_profile_when_no_env(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        prof = ApplyProfile(eeo_data_consent=False, eeo_save_answers=True, eeo_share_answers=False)
        assert indeed._demographic_pref("consent", prof) is False
        assert indeed._demographic_pref("save", prof) is True
        assert indeed._demographic_pref("share", prof) is False

    def test_env_overrides_profile(self, monkeypatch):
        monkeypatch.setenv("APPLY_EEO_DATA_CONSENT", "off")
        prof = ApplyProfile(eeo_data_consent=True)  # profile says yes, env wins
        assert indeed._demographic_pref("consent", prof) is False


class TestPrimaryAction:
    def test_continue(self):
        assert _primary_action(["continue"]) == "continue"

    def test_submit(self):
        assert _primary_action(["submit application"]) == "submit"

    def test_submit_beats_continue(self):
        assert _primary_action(["continue applying", "submit application"]) == "submit"

    def test_none_when_no_primary(self):
        assert _primary_action(["save and close"]) == "none"
        assert _primary_action([]) == "none"
