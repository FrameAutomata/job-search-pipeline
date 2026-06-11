"""Tests for pipeline/resume_tailor.py — slot classification, budgets, patching,
the one-page verify loop (render/page-count faked), and threshold gating.

A synthetic docx is built in-test mirroring the real resume's structure:
name, contact, section headers, "Label: values" skills lines (bold label run),
'List Paragraph' bullets, and bold "Company\\tDate" lines."""

from pathlib import Path

import pytest

docx = pytest.importorskip("docx")
from docx import Document  # noqa: E402

from pipeline import resume_tailor as rt
from pipeline.apply import _should_tailor
from pipeline.apply.queue import ApplyJob


def _build_resume(path: Path) -> Path:
    d = Document()
    d.add_paragraph("Jane Dev")                                      # 0 name
    d.add_paragraph("+1 555 | jane@x.com | Dallas, TX")              # 1 contact
    d.add_paragraph("Professional Summary")                          # 2 header
    d.add_paragraph("Engineer with 3 yrs across backend and DevOps " # 3 summary
                    "building production systems.")
    d.add_paragraph("Skills")                                        # 4 header
    p = d.add_paragraph()                                            # 5 skills
    r = p.add_run("Languages: "); r.bold = True
    p.add_run("Java, Python, Go, SQL")
    p2 = d.add_paragraph()                                           # 6 skills
    r2 = p2.add_run("Cloud / DevOps: "); r2.bold = True
    p2.add_run("AWS, Docker, Kubernetes, Terraform")
    d.add_paragraph("Professional Experience")                       # 7 header
    comp = d.add_paragraph()                                         # 8 company line
    rc = comp.add_run("Acme Corp\tJan 2023 – Present"); rc.bold = True
    role = d.add_paragraph()                                         # 9 role line
    rr = role.add_run("Software Engineer\tRemote"); rr.italic = True
    d.add_paragraph("Built REST APIs in Java handling 1M requests "  # 10 bullet
                    "daily with full CI/CD.", style="List Paragraph")
    d.add_paragraph("Deployed Dockerized services to AWS ECS.",      # 11 bullet
                    style="List Paragraph")
    d.add_paragraph("Education & Certifications")                    # 12 header
    edu = d.add_paragraph()                                          # 13 education
    re_ = edu.add_run("State University\t2018 – 2022"); re_.bold = True
    d.save(str(path))
    return path


@pytest.fixture
def resume_docx(tmp_path):
    return _build_resume(tmp_path / "resume.docx")


def _job(company="Acme", score=4.5, report="", role="Backend Engineer"):
    return ApplyJob(num="1", company=company, role=role,
                    url="u", score=score, report_path=report)


class TestExtractSlots:
    def test_classifies_only_editable_slots(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        kinds = {s.id: s.kind for s in slots}
        # summary (3), skills (5, 6), bullets (10, 11) — and NOTHING else.
        assert kinds == {"s3": "summary", "s5": "skills", "s6": "skills",
                         "s10": "bullet", "s11": "bullet"}

    def test_skills_slot_excludes_bold_label(self, resume_docx):
        doc = Document(str(resume_docx))
        slot = next(s for s in rt.extract_slots(doc) if s.id == "s5")
        assert slot.label.strip() == "Languages:"
        assert slot.text == "Java, Python, Go, SQL"

    def test_name_contact_headers_company_lines_protected(self, resume_docx):
        doc = Document(str(resume_docx))
        slot_idxs = {s.para_index for s in rt.extract_slots(doc)}
        for protected in (0, 1, 2, 4, 7, 8, 9, 12, 13):
            assert protected not in slot_idxs

    def test_nothing_editable_before_first_header(self, tmp_path):
        d = Document()
        d.add_paragraph("Stray bullet before any header", style="List Paragraph")
        p = tmp_path / "odd.docx"
        d.save(str(p))
        assert rt.extract_slots(Document(str(p))) == []


class TestApplyReplacements:
    def test_patches_within_budget_and_preserves_protected(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        changed, rejected = rt.apply_replacements(doc, slots, {
            "s10": "Built Java REST APIs at 1M req/day; full CI/CD.",
            "s5": "Go, Java, Python, SQL",
        })
        assert changed == 2 and rejected == []
        assert doc.paragraphs[10].text == "Built Java REST APIs at 1M req/day; full CI/CD."
        # skills: bold label run untouched, values replaced
        assert doc.paragraphs[5].runs[0].text == "Languages: "
        assert doc.paragraphs[5].runs[0].bold is True
        assert doc.paragraphs[5].text == "Languages: Go, Java, Python, SQL"
        # protected paragraphs byte-identical
        assert doc.paragraphs[0].text == "Jane Dev"
        assert doc.paragraphs[8].text == "Acme Corp\tJan 2023 – Present"

    def test_unfittable_replacement_rejected(self, resume_docx):
        # No clause boundary to trim at → whole rewrite rejected, original stays.
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        original = doc.paragraphs[11].text
        too_long = "x" * (next(s.max_chars for s in slots if s.id == "s11") + 50)
        changed, rejected = rt.apply_replacements(doc, slots, {"s11": too_long})
        assert changed == 0 and rejected == ["s11"]
        assert doc.paragraphs[11].text == original

    def test_over_budget_prose_trimmed_at_clause_boundary(self, resume_docx):
        # A summary that overflows its budget is trimmed at a sentence break and
        # APPLIED — not discarded (a rejected summary = no retargeting at all).
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        s3 = next(s for s in slots if s.id == "s3")
        first = "Backend-focused engineer with 3 yrs building production systems."
        new = first + " " + "Extra trailing sentence that pushes well past the budget limit." * 3
        assert len(new) > s3.max_chars
        changed, rejected = rt.apply_replacements(doc, slots, {"s3": new})
        assert changed == 1 and rejected == []
        assert doc.paragraphs[3].text.startswith("Backend-focused engineer")
        assert len(doc.paragraphs[3].text) <= s3.max_chars

    def test_skills_fabricated_tokens_dropped(self, resume_docx):
        # "JavaScript" isn't in the resume → dropped; the rest (grounded) applied.
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        resume_text = "\n".join(p.text for p in doc.paragraphs)
        changed, rejected = rt.apply_replacements(
            doc, slots, {"s5": "Python, JavaScript, Go, Java, SQL"}, resume_text)
        assert changed == 1
        assert doc.paragraphs[5].text == "Languages: Python, Go, Java, SQL"

    def test_skills_all_fabricated_rejected(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        resume_text = "\n".join(p.text for p in doc.paragraphs)
        original = doc.paragraphs[5].text
        changed, rejected = rt.apply_replacements(
            doc, slots, {"s5": "Rust, Elixir, Haskell"}, resume_text)
        assert changed == 0 and rejected == ["s5"]
        assert doc.paragraphs[5].text == original

    def test_skills_paren_compound_semantics(self, resume_docx):
        # A parenthetical enumerates sub-skill CLAIMS: "AWS (ECS)" is grounded
        # (the resume bullet says "AWS ECS"), but "AWS (ECS, DynamoDB)" is not
        # (DynamoDB appears nowhere) — it gets dropped, the grounded rest applied.
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        resume_text = "\n".join(p.text for p in doc.paragraphs)
        changed, _ = rt.apply_replacements(
            doc, slots,
            {"s6": "AWS (ECS), AWS (ECS, DynamoDB), Docker, Kubernetes"}, resume_text)
        assert changed == 1
        assert doc.paragraphs[6].text == "Cloud / DevOps: AWS (ECS), Docker, Kubernetes"

    def test_skills_slash_compound_any_part_grounds(self, resume_docx):
        # '/' is alternative naming: "Java/Kotlin" passes via "Java" alone.
        doc = Document(str(resume_docx))
        resume_text = "\n".join(p.text for p in doc.paragraphs)
        assert rt._skill_in_resume("Java/Kotlin", resume_text) is True
        assert rt._skill_in_resume("Rust/Elixir", resume_text) is False

    def test_unknown_or_unchanged_slots_ignored(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        same = doc.paragraphs[10].text
        changed, rejected = rt.apply_replacements(doc, slots, {"s10": same, "s999": "x", "s10x": 7})
        assert changed == 0 and rejected == []

    def test_bullet_style_survives_patch(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        rt.apply_replacements(doc, slots, {"s10": "Shorter bullet."})
        assert doc.paragraphs[10].style.name == "List Paragraph"

    def test_summary_gets_extra_headroom(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        s3 = next(s for s in slots if s.kind == "summary")
        s10 = next(s for s in slots if s.id == "s10")
        assert s3.max_chars > int(len(s3.text) * 1.25)        # ~+30%
        assert s10.max_chars <= int(len(s10.text) * 1.25) + 5  # bullets ~+20%


class TestParseReplacements:
    def test_plain_json(self):
        assert rt.parse_replacements('{"s3": "new"}') == {"s3": "new"}

    def test_fenced_with_prose(self):
        raw = 'Here you go:\n```json\n{"s3": "new", "s5": "vals"}\n```\nDone.'
        assert rt.parse_replacements(raw) == {"s3": "new", "s5": "vals"}

    def test_garbage_and_non_string_values(self):
        assert rt.parse_replacements("no json here") == {}
        assert rt.parse_replacements('{"s3": 42, "s5": "ok"}') == {"s5": "ok"}


class TestBuildPrompt:
    def test_includes_rules_slots_and_context(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        system, user = rt.build_prompt(slots, "FULL RESUME TEXT", _job(report=""),
                                       "REPORT NOTES")
        assert "Never invent" in system and "max_chars" in system
        # Retargeting is the explicit objective, not optional polish.
        assert "ALWAYS rewrite" in system and "FAILURE" in system
        # Prose-honesty rules (caught live: "3+ years at Bank of America" /
        # "Expert in Java" / opening with the target job title).
        assert "PROSE HONESTY" in system and "NEVER with the target" in system
        assert "Acme — Backend Engineer" in user
        assert "REPORT NOTES" in user and "FULL RESUME TEXT" in user
        assert '"s10"' in user

    def test_jd_text_included_when_present(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        _, user = rt.build_prompt(slots, "cv", _job(), "", jd_text="THE ACTUAL JD")
        assert "JOB DESCRIPTION" in user and "THE ACTUAL JD" in user

    def test_feedback_appended_to_system(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        system, _ = rt.build_prompt(slots, "cv", _job(), "",
                                    feedback=rt._OVERFLOW_FEEDBACK)
        assert "OVERFLOWED" in system
        system2, _ = rt.build_prompt(slots, "cv", _job(), "",
                                     feedback=rt._TITLE_FEEDBACK)
        assert "REJECTED" in system2


class TestProseRules:
    """Deterministic backstop: a summary that retitles the candidate as the
    target job's title is dropped (caught live — the prompt alone didn't hold)."""

    def test_retitled_summary_dropped(self, resume_docx):
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        job = _job(role="Application Support Engineer")
        reps = {"s3": "Application Support Engineer with production experience.",
                "s10": "Fine bullet."}
        out, notes = rt.enforce_prose_rules(reps, slots, job)
        assert "s3" not in out and "s10" in out
        assert notes and "target job title" in notes[0]

    def test_matching_original_opener_allowed(self, resume_docx):
        # Original summary opens "Engineer with 3 yrs..." — a role of "Engineer"
        # appearing the same way isn't retitling. And a rewrite NOT led by the
        # title passes untouched.
        doc = Document(str(resume_docx))
        slots = rt.extract_slots(doc)
        job = _job(role="Backend Engineer")
        reps = {"s3": "Engineer with 3 yrs building backend and DevOps systems for production."}
        out, notes = rt.enforce_prose_rules(reps, slots, job)
        assert "s3" in out and notes == []

    def test_leads_with_role_title_helper(self):
        assert rt._leads_with_role_title(
            "Application Support Engineer with experience...",
            "Software engineer (~3 yrs) across backend...",
            "Application Support Engineer") is True
        assert rt._leads_with_role_title(
            "Software engineer (~3 yrs) focused on support tooling...",
            "Software engineer (~3 yrs) across backend...",
            "Software Engineer") is False


class TestJdText:
    def test_prefers_local_jds_file(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "batch" / "jds").mkdir(parents=True)
        (co / "batch" / "jds" / "7.txt").write_text("CACHED JD", encoding="utf-8")
        job = ApplyJob(num="7", company="X", role="Y", url="u", score=4.0)
        assert rt._jd_text(co, None, job) == "CACHED JD"

    def test_falls_back_to_artifact_then_fetch(self, tmp_path, monkeypatch):
        co = tmp_path / "career-ops"
        co.mkdir()
        art = tmp_path / "artifact"
        (art / "batch" / "jds").mkdir(parents=True)
        (art / "batch" / "jds" / "7.txt").write_text("ARTIFACT JD", encoding="utf-8")
        job = ApplyJob(num="7", company="X", role="Y", url="u", score=4.0)
        assert rt._jd_text(co, art, job) == "ARTIFACT JD"
        # no files anywhere + a LinkedIn URL → guest-endpoint fetch
        job2 = ApplyJob(num="8", company="X", role="Y",
                        url="https://www.linkedin.com/jobs/view/123/", score=4.0)
        monkeypatch.setattr("pipeline.screen.fetch_and_classify",
                            lambda url, timeout=8: ("active", "", "<body>FETCHED JD</body>"))
        assert "FETCHED JD" in rt._jd_text(co, art, job2)

    def test_empty_when_nothing_available(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        job = ApplyJob(num="", company="X", role="Y", url="https://example.com/x", score=4.0)
        assert rt._jd_text(co, None, job) == ""


class TestGenerateForJob:
    """End-to-end with a fake caller and faked render/page-count (no LibreOffice)."""

    def _setup(self, tmp_path, resume_docx, monkeypatch, *, pages_seq, baseline=1):
        co = tmp_path / "career-ops"
        (co / "output").mkdir(parents=True)
        monkeypatch.setattr(rt, "source_docx", lambda: resume_docx)
        monkeypatch.setattr(rt, "_baseline_pages", lambda src: baseline)
        seq = iter(pages_seq)

        def fake_render(docx_path, out_dir):
            pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
            pdf.write_bytes(b"%PDF-fake")
            return pdf
        monkeypatch.setattr(rt, "render_pdf", fake_render)
        monkeypatch.setattr(rt, "page_count", lambda p: next(seq))
        return co

    def test_success_returns_verified_pdf(self, tmp_path, resume_docx, monkeypatch):
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[1])
        out = rt.generate_for_job(
            co, _job(), caller=lambda s, u: '{"s10": "Java APIs, tailored."}')
        assert out is not None and out.suffix == ".pdf" and out.exists()
        assert (co / "output" / "Acme - resume.docx").exists()
        # the tailored edit actually landed in the saved docx
        saved = Document(str(co / "output" / "Acme - resume.docx"))
        assert saved.paragraphs[10].text == "Java APIs, tailored."

    def test_overflow_retries_then_falls_back(self, tmp_path, resume_docx, monkeypatch):
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[2, 2])
        calls = []
        def caller(system, user):
            calls.append(system)
            return '{"s10": "Tailored but somehow too long."}'
        assert rt.generate_for_job(co, _job(), caller=caller) is None
        assert len(calls) == 2 and "OVERFLOWED" in calls[1]   # retry asked to shorten
        assert not (co / "output" / "Acme - resume.docx").exists()  # cleaned up

    def test_overflow_then_fits_on_retry(self, tmp_path, resume_docx, monkeypatch):
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[2, 1])
        out = rt.generate_for_job(
            co, _job(), caller=lambda s, u: '{"s10": "Short."}')
        assert out is not None and out.suffix == ".pdf"

    def test_no_renderer_returns_docx(self, tmp_path, resume_docx, monkeypatch):
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[])
        monkeypatch.setattr(rt, "render_pdf", lambda d, o: None)
        monkeypatch.setattr(rt, "_baseline_pages", lambda src: None)
        out = rt.generate_for_job(
            co, _job(), caller=lambda s, u: '{"s10": "Tailored."}')
        assert out is not None and out.suffix == ".docx"

    def test_existing_reused_without_llm(self, tmp_path, resume_docx, monkeypatch):
        co = tmp_path / "career-ops"
        (co / "output").mkdir(parents=True)
        (co / "output" / "Acme - resume.pdf").write_bytes(b"%PDF-existing")
        def boom(s, u):
            raise AssertionError("LLM must not be called for a cached resume")
        out = rt.generate_for_job(co, _job(), caller=boom)
        assert out is not None and out.name == "Acme - resume.pdf"

    def test_no_source_docx_returns_none(self, tmp_path, monkeypatch):
        co = tmp_path / "career-ops"
        (co / "output").mkdir(parents=True)
        monkeypatch.setattr(rt, "source_docx", lambda: None)
        assert rt.generate_for_job(co, _job(), caller=lambda s, u: "{}") is None

    def test_no_change_returns_none(self, tmp_path, resume_docx, monkeypatch):
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[1])
        assert rt.generate_for_job(co, _job(), caller=lambda s, u: "{}") is None

    def test_retitled_summary_retried_with_feedback_then_applied(
            self, tmp_path, resume_docx, monkeypatch):
        # Call 1 retitles the summary → validator catches it → ONE corrective
        # retry with feedback; call 2 complies and is applied.
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[1])
        job = _job(role="Application Support Engineer")
        systems = []
        def caller(system, user):
            systems.append(system)
            if len(systems) == 1:
                return '{"s3": "Application Support Engineer with production experience."}'
            return '{"s3": "Engineer with 3 yrs of production support across financial systems."}'
        out = rt.generate_for_job(co, job, caller=caller)
        assert out is not None
        assert len(systems) == 2 and "REJECTED" in systems[1]
        saved = Document(str(co / "output" / "Acme - resume.docx"))
        assert saved.paragraphs[3].text.startswith("Engineer with 3 yrs")

    def test_retitled_twice_drops_summary_keeps_rest(
            self, tmp_path, resume_docx, monkeypatch):
        # Both calls retitle → summary dropped (original stays), grounded bullet
        # rewrite still applied.
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[1])
        job = _job(role="Application Support Engineer")
        original_summary = Document(str(resume_docx)).paragraphs[3].text
        def caller(system, user):
            return ('{"s3": "Application Support Engineer with production experience.", '
                    '"s10": "Built Java REST APIs; production support focus."}')
        out = rt.generate_for_job(co, job, caller=caller)
        assert out is not None
        saved = Document(str(co / "output" / "Acme - resume.docx"))
        assert saved.paragraphs[3].text == original_summary
        assert saved.paragraphs[10].text == "Built Java REST APIs; production support focus."

    def test_metadata_stripped(self, tmp_path, resume_docx, monkeypatch):
        co = self._setup(tmp_path, resume_docx, monkeypatch, pages_seq=[1])
        rt.generate_for_job(co, _job(), caller=lambda s, u: '{"s10": "Tailored."}')
        saved = Document(str(co / "output" / "Acme - resume.docx"))
        assert saved.core_properties.title == ""
        assert saved.core_properties.revision == 1


class TestShouldTailor:
    def test_gates_on_score(self):
        assert _should_tailor(_job(score=4.0), 4.0) is True
        assert _should_tailor(_job(score=4.6), 4.0) is True
        assert _should_tailor(_job(score=3.9), 4.0) is False
        assert _should_tailor(_job(score=None), 4.0) is False   # --apply-url one-off


class TestSofficeDiscovery:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        fake = tmp_path / "soffice.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("SOFFICE_PATH", str(fake))
        assert rt.find_soffice() == str(fake)
