"""Tests for the LLM build/tailor step (Commit 3c-1).

resume_content turns PROFILE.md + a JD into a GROUNDED content-JSON for the 3b
renderer/fit. The LLM is injected as a fake caller so these run with no provider;
fit_to_page is patched so they need no LibreOffice.
"""
import json
from pathlib import Path

import pytest

from pipeline import resume_content


_PROFILE = """# Jane Doe — candidate profile

## Role fact bank
Acme Corp — Senior Engineer (2022–Present) — Cut p99 latency 40% and saved $2M/yr.

## Skills inventory
Python (Strong) · Go (Solid) · COBOL (Lighter / older — don't over-claim)
"""

_JD = "Senior Python Engineer. Must have Go and AWS. Backend systems at scale."

_CONTENT = {
    "name": "Jane Doe", "contact": "jane@example.com",
    "summary": "Senior engineer who ships backend systems at scale.",
    "skills": [{"label": "Languages", "items": "Python · Go"}],
    "experience": [{"org": "Acme Corp", "dates": "2022 – Present", "role": "Senior Engineer",
                    "loc": "Remote", "bullets": ["Cut p99 latency 40% and saved $2M/yr"]}],
    "projects_heading": "Projects", "projects": [],
    "education": ["B.S. Computer Science"],
}


class TestBuildPrompt:
    """The grounding contract: only PROFILE facts, keep metrics, honesty tiers,
    add/remove skills per JD, never invent, strict-JSON output."""

    def test_system_states_the_grounding_rules(self):
        system, _ = resume_content.build_prompt(_PROFILE, _JD)
        low = system.lower()
        assert "only" in low and ("profile" in low or "fact" in low)   # ground in PROFILE
        assert "invent" in low                                          # never invent
        assert "metric" in low                                          # keep metrics
        assert "json" in low                                            # strict JSON out

    def test_system_lists_the_schema_keys(self):
        system, _ = resume_content.build_prompt(_PROFILE, _JD)
        for key in ("name", "summary", "skills", "experience", "projects", "education"):
            assert key in system

    def test_user_carries_profile_jd_and_report(self):
        _, user = resume_content.build_prompt(_PROFILE, _JD, report="PROOF: shipped X to 5M")
        assert "Cut p99 latency 40%" in user        # PROFILE fact bank
        assert "Senior Python Engineer" in user     # the JD
        assert "PROOF: shipped X to 5M" in user     # eval-report proof-points


class TestParseContentJson:
    def test_parses_bare_json(self):
        assert resume_content.parse_content_json(json.dumps(_CONTENT))["name"] == "Jane Doe"

    def test_strips_code_fences(self):
        raw = "```json\n" + json.dumps(_CONTENT) + "\n```"
        assert resume_content.parse_content_json(raw)["experience"][0]["org"] == "Acme Corp"

    def test_strips_surrounding_prose(self):
        raw = "Sure, here is the tailored résumé:\n\n" + json.dumps(_CONTENT) + "\n\nLet me know!"
        assert resume_content.parse_content_json(raw)["name"] == "Jane Doe"

    def test_rejects_non_json(self):
        with pytest.raises(ValueError):
            resume_content.parse_content_json("I'm sorry, I can't help with that.")

    def test_rejects_json_that_is_not_an_object(self):
        with pytest.raises(ValueError):
            resume_content.parse_content_json("[1, 2, 3]")


class TestBuildForJob:
    """build_for_job: caller → parse → fit_to_page. The caller is injected and
    fit_to_page is patched, so no provider and no LibreOffice are needed."""

    def _caller(self, raw):
        return lambda system, user: raw

    def test_builds_grounded_content_and_fits(self, tmp_path, monkeypatch):
        captured = {}
        sentinel = object()
        monkeypatch.setattr("pipeline.resume_build.fit_to_page",
                            lambda content, out_dir, **kw: captured.update(content=content) or sentinel)
        out = resume_content.build_for_job(_PROFILE, _JD, tmp_path,
                                           caller=self._caller(json.dumps(_CONTENT)))
        assert out is sentinel
        assert captured["content"]["experience"][0]["org"] == "Acme Corp"

    def test_returns_none_on_unparseable_output(self, tmp_path, monkeypatch):
        # Garbage LLM output must NOT reach the renderer — falls back to None so
        # the agent tailors the row itself (same contract as the old tailor).
        monkeypatch.setattr("pipeline.resume_build.fit_to_page",
                            lambda *a, **k: pytest.fail("garbage must not be rendered"))
        assert resume_content.build_for_job(_PROFILE, _JD, tmp_path,
                                            caller=self._caller("no JSON here, sorry")) is None

    def test_returns_none_when_caller_fails(self, tmp_path):
        def boom(system, user):
            raise RuntimeError("provider down")
        # max_attempts=1 / base_delay=0 so the failure path doesn't sleep/retry.
        assert resume_content.build_for_job(_PROFILE, _JD, tmp_path, caller=boom,
                                            max_attempts=1, base_delay=0.0) is None


class TestGenerateForJob:
    """generate_for_job: read PROFILE.md from the handoff dir → build → cache the
    PDF at the company path the UI already looks in. build_for_job is patched so
    these need no LLM."""

    def _job(self):
        from pipeline.role_select import ApplyJob
        return ApplyJob(num="1", company="Acme", role="Engineer", url="u", score=4.5)

    def _dirs(self, tmp_path, profile=True):
        co = tmp_path / "career-ops"; (co / "output").mkdir(parents=True)
        pd = tmp_path / "handoff"; pd.mkdir()
        if profile:
            (pd / "PROFILE.md").write_text("# Jane\n## Role fact bank\nAcme — cut cost 40%.",
                                           encoding="utf-8")
        return co, pd

    def test_missing_profile_returns_none(self, tmp_path, monkeypatch):
        co, pd = self._dirs(tmp_path, profile=False)
        monkeypatch.setattr(resume_content, "build_for_job",
                            lambda *a, **k: pytest.fail("must not build without a PROFILE"))
        assert resume_content.generate_for_job(co, self._job(), profile_dir=pd) is None

    def test_builds_and_caches_the_pdf_at_company_path(self, tmp_path, monkeypatch):
        from pipeline import resume_build
        co, pd = self._dirs(tmp_path)
        winner = tmp_path / "winner.pdf"; winner.write_bytes(b"%PDF-1.4 built")
        seen = {}
        def fake_build(profile_md, jd, out_dir, **kw):
            seen["profile_md"] = profile_md
            return resume_build.BuildResult(pdf=winner, scale=1.1, fit=None)
        monkeypatch.setattr(resume_content, "build_for_job", fake_build)
        out = resume_content.generate_for_job(co, self._job(), profile_dir=pd,
                                              caller=lambda s, u: "")
        assert out is not None and Path(out).exists()
        assert "Acme" in Path(out).name                       # cached at the company path
        assert "cut cost 40%" in seen["profile_md"]           # PROFILE.md fed to the builder
        assert Path(str(out) + ".role").read_text(encoding="utf-8") == "Engineer"  # role recorded

    def test_overflowing_build_is_not_cached(self, tmp_path, monkeypatch):
        from pipeline import resume_build, resume_fit, resume_tailor
        co, pd = self._dirs(tmp_path)
        winner = tmp_path / "big.pdf"; winner.write_bytes(b"%PDF two pages")
        overflow = resume_fit.FitResult(ok=False, code=3, verdict="OVERFULL",
                                        fill=0.99, pages=2, notes=[])
        monkeypatch.setattr(resume_content, "build_for_job",
                            lambda *a, **k: resume_build.BuildResult(pdf=winner, scale=0.9, fit=overflow))
        _, pdf_out = resume_tailor.resume_paths(co, "Acme")
        assert resume_content.generate_for_job(co, self._job(), profile_dir=pd) is None
        assert not pdf_out.exists()                           # a 2-page résumé is never cached

    def test_reuses_cached_pdf_newer_than_profile_and_role(self, tmp_path, monkeypatch):
        from pipeline import resume_tailor
        co, pd = self._dirs(tmp_path)
        _, pdf_out = resume_tailor.resume_paths(co, "Acme")
        pdf_out.parent.mkdir(parents=True, exist_ok=True)
        pdf_out.write_bytes(b"%PDF cached")                   # cache newer than PROFILE.md…
        Path(str(pdf_out) + ".role").write_text("Engineer", encoding="utf-8")  # …and tailored for this role
        monkeypatch.setattr(resume_content, "build_for_job",
                            lambda *a, **k: pytest.fail("a matching fresh cache must be reused, not rebuilt"))
        assert Path(resume_content.generate_for_job(co, self._job(), profile_dir=pd)) == pdf_out

    def test_rebuilds_when_cached_role_differs(self, tmp_path, monkeypatch):
        # Company-keyed cache tailored for a DIFFERENT role must NOT be served for
        # this role (else the agent uploads a résumé aimed at the wrong job).
        from pipeline import resume_build, resume_tailor
        co, pd = self._dirs(tmp_path)
        _, pdf_out = resume_tailor.resume_paths(co, "Acme")
        pdf_out.parent.mkdir(parents=True, exist_ok=True)
        pdf_out.write_bytes(b"%PDF for a different role")
        Path(str(pdf_out) + ".role").write_text("Backend Engineer", encoding="utf-8")
        winner = tmp_path / "new.pdf"; winner.write_bytes(b"%PDF fresh")
        built = []
        monkeypatch.setattr(resume_content, "build_for_job",
                            lambda *a, **k: built.append(1) or resume_build.BuildResult(pdf=winner, scale=1.0, fit=None))
        resume_content.generate_for_job(co, self._job(), profile_dir=pd, caller=lambda s, u: "")
        assert built == [1]                                   # rebuilt (role mismatch), not reused
        assert Path(str(pdf_out) + ".role").read_text(encoding="utf-8") == "Engineer"  # marker updated
