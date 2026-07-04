"""Tests for pipeline/cover_letters.py (pure logic + run with a fake caller)."""

from pipeline import cover_letters
from pipeline import role_select as queue
from pipeline.candidate_profile import ApplyProfile


_TRACKER = """\
# Applications Tracker

| # | Date | Company | Role | Score | Status | PDF | Report | Notes |
|---|------|---------|------|-------|--------|-----|--------|-------|
| 1 | 2026-06-01 | Apexon | Engineer | 4.2/5 | Evaluated | ❌ | [001](reports/001.md) | https://www.linkedin.com/jobs/view/1 — fit |
| 2 | 2026-06-01 | Lowball Inc | Dev | 2.0/5 | Evaluated | ❌ | [002](reports/002.md) | https://example.com/2 — low |
"""


class TestCoverPath:
    def test_naming(self, tmp_path):
        assert cover_letters.cover_path(tmp_path, "Apexon").name == "Apexon - cover.md"

    def test_sanitizes_illegal_chars(self, tmp_path):
        assert cover_letters.cover_path(tmp_path, 'Acme/Globex:Inc*').name == "Acme Globex Inc - cover.md"

    def test_empty_company(self, tmp_path):
        assert cover_letters.cover_path(tmp_path, "").name == "company - cover.md"


class TestBuildPrompt:
    def test_includes_context_and_guardrails(self):
        p = ApplyProfile(full_name="Thomas Thirlwall", email="t@x.com", city="Dallas", country="US")
        job = queue.ApplyJob(num="1", company="Apexon", role="Engineer", url="u", score=4.5,
                             report_path="reports/001.md")
        system, user = cover_letters.build_prompt(p, "MY CV TEXT", job, "REPORT NOTES")
        assert "Apexon" in user and "Engineer" in user
        assert "MY CV TEXT" in user and "REPORT NOTES" in user
        assert "Thomas Thirlwall" in user
        assert "never invent" in system.lower()


class TestFindExisting:
    def test_finds_broad_match(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "Apexon - cover.md").write_text("MY LETTER", encoding="utf-8")
        (out / "Globex - resume.txt").write_text("not a cover", encoding="utf-8")
        assert cover_letters.find_existing(tmp_path, "Apexon") == "MY LETTER"

    def test_requires_cover_in_name(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "Apexon - notes.md").write_text("x", encoding="utf-8")
        assert cover_letters.find_existing(tmp_path, "Apexon") == ""

    def test_empty_when_dir_absent(self, tmp_path):
        assert cover_letters.find_existing(tmp_path, "Apexon") == ""


class TestGenerateForJob:
    def test_returns_existing_without_calling_llm(self, tmp_path, monkeypatch):
        (tmp_path / "output").mkdir(parents=True)
        (tmp_path / "output" / "Apexon - cover.md").write_text("EXISTING", encoding="utf-8")
        monkeypatch.setattr("pipeline.batch_evaluate._build_caller",
                            lambda p, m: (_ for _ in ()).throw(AssertionError("no LLM")))
        job = queue.ApplyJob(num="1", company="Apexon", role="Eng", url="u", score=4.5)
        assert cover_letters.generate_for_job(tmp_path, job, provider="deepinfra") == "EXISTING"

    def test_generates_and_saves_when_missing(self, tmp_path):
        (tmp_path / "cv.md").write_text("CV", encoding="utf-8")
        job = queue.ApplyJob(num="1", company="Apexon", role="Eng", url="u", score=4.5)
        text = cover_letters.generate_for_job(
            tmp_path, job, caller=lambda s, u: "Generated letter\nThomas")
        assert text == "Generated letter\nThomas"
        assert (tmp_path / "output" / "Apexon - cover.md").exists()


class TestRun:
    def _career_ops(self, tmp_path):
        (tmp_path / "data").mkdir(parents=True)
        (tmp_path / "data" / "applications.md").write_text(_TRACKER, encoding="utf-8")
        (tmp_path / "cv.md").write_text("My CV — built APIs at Capital One.", encoding="utf-8")
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "001.md").write_text("Strong API match.", encoding="utf-8")
        return tmp_path

    def _patch_caller(self, monkeypatch, fn):
        monkeypatch.setattr("pipeline.batch_evaluate._detect_provider", lambda: "deepinfra")
        monkeypatch.setattr("pipeline.batch_evaluate._build_caller",
                            lambda provider, model, **kw: fn)

    def test_writes_letter_for_high_fit_only(self, tmp_path, monkeypatch):
        co = self._career_ops(tmp_path)
        calls = []
        def caller(system, user):
            calls.append(user)
            return "Dear Apexon team,\n\nI build APIs.\n\nThomas Thirlwall"
        self._patch_caller(monkeypatch, caller)

        n = cover_letters.run(co, min_score=4.0)
        assert n == 1                                  # only Apexon (4.2), not Lowball (2.0)
        out = co / "output" / "Apexon - cover.md"
        assert out.exists() and "Dear Apexon" in out.read_text(encoding="utf-8")
        assert len(calls) == 1
        assert "Strong API match." in calls[0]         # report context fed in

    def test_skips_existing_unless_force(self, tmp_path, monkeypatch):
        co = self._career_ops(tmp_path)
        (co / "output").mkdir()
        (co / "output" / "Apexon - cover.md").write_text("old letter", encoding="utf-8")
        self._patch_caller(monkeypatch, lambda s, u: "new letter")

        assert cover_letters.run(co, min_score=4.0) == 0          # skipped
        assert (co / "output" / "Apexon - cover.md").read_text(encoding="utf-8") == "old letter"
        assert cover_letters.run(co, min_score=4.0, force=True) == 1  # regenerated
        assert "new letter" in (co / "output" / "Apexon - cover.md").read_text(encoding="utf-8")

    def test_no_jobs_no_calls(self, tmp_path, monkeypatch):
        co = self._career_ops(tmp_path)
        self._patch_caller(monkeypatch, lambda s, u: (_ for _ in ()).throw(AssertionError("no call")))
        assert cover_letters.run(co, min_score=4.9) == 0          # nothing >= 4.9
