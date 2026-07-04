"""Tests for pipeline/handoff.py — the browser-agent handoff stage.

This is the signed-off spec. It covers four concerns:
  1. Dedup-key normalization (company suffixes, req-ids, parens, levels, case).
  2. Parsing JOB_LOG.md into a structured status tracker (exact for the tables,
     precision-first best-effort for prose skips).
  3. Reconcile precedence + the agent writeback loop.
  4. Building the work-order (queue minus tracker, ranked, board-filtered,
     resume-base hinted) and the CLI wiring.
"""

import json

import pytest

from pipeline import handoff


# A compact but representative JOB_LOG.md. Exercises every parseable shape:
#   - the ## Applied table (2 rows, different boards)
#   - a Work-at-a-Startup table (one SUBMITTED -> applied, one draft -> drafted)
#   - ## Needs Thomas + ## To finish manually bullets (-> handoff), incl. a
#     struck-through (resolved) bullet that must NOT be emitted
#   - a ### Skipped section with a bold "Company — Role" pair
#   - decoy prose: a company named in passing that must NOT become a skip
SAMPLE_JOB_LOG = """# Job Application Log

## Applied
| Date | Company | Role | Location / Comp | Board | Method |
|------|---------|------|------------------|-------|--------|
| 2026-07-02 | Ryan, LLC | Full Stack AI Engineer | Plano, hybrid | Indeed | Workday |
| 2026-07-02 | Temu | Software Engineer - Ads | Remote, $100K | LinkedIn | Easy Apply |

## Needs Thomas — next-step action required
- **micro1 — Software Engineer, micro Platforms**: submitted, needs a live AI interview.

## To finish manually (external ATS — resume prepped, not yet submitted)
- **JPMorganChase — Experienced Software Engineer, Java/Python**: needs a JPMC account.
- ~~GEICO — Solutions Engineer II - Networking~~ — **SUBMITTED 2026-07-02**, done.

## Work at a Startup
### Drafted — awaiting Thomas's review/send
| Date | Company | Role | Location | Job ID | Note |
|------|---------|------|----------|--------|------|
| 2026-07-03 | Corgi Insurance | Software Engineer | Dallas, TX | 94300 | **SUBMITTED**, confirmed by Thomas. |
| 2026-07-03 | Draftco | Product Engineer | Remote | 999 | Draft saved, awaiting review. |

### Skipped this run
- **Falconer — Software Engineer, Full-Stack** (LinkedIn, San Francisco): explicitly On-site.
- Considered Crogl in passing here, but this sentence is not a bold skip pair.
"""


def _sample_queue_rows():
    """Queue rows covering: 2 applied, 2 handoff, 1 skipped, plus 4 genuinely
    fresh (Curri, Crogl, Oddball, Solugenix)."""
    return [
        {"num": "1", "score": 4.7, "company": "Ryan", "role": "Full Stack AI Engineer",
         "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a1", "report": "", "verdict": ""},
        {"num": "2", "score": 4.2, "company": "Temu", "role": "Software Engineer - Ads",
         "status": "Evaluated", "url": "https://www.linkedin.com/jobs/view/2", "report": "", "verdict": ""},
        {"num": "3", "score": 4.5, "company": "JPMorganChase", "role": "Experienced Software Engineer, Java/Python",
         "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a3", "report": "", "verdict": ""},
        {"num": "4", "score": 4.0, "company": "micro1", "role": "Software Engineer, micro Platforms",
         "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a4", "report": "", "verdict": ""},
        {"num": "5", "score": 3.9, "company": "Falconer", "role": "Software Engineer, Full-Stack",
         "status": "Evaluated", "url": "https://www.linkedin.com/jobs/view/5", "report": "", "verdict": ""},
        {"num": "6", "score": 4.7, "company": "Curri", "role": "Software Engineer",
         "status": "Evaluated", "url": "https://www.linkedin.com/jobs/view/6", "report": "", "verdict": ""},
        {"num": "7", "score": 4.6, "company": "Crogl", "role": "AI Engineer",
         "status": "Evaluated", "url": "https://www.linkedin.com/jobs/view/7", "report": "", "verdict": ""},
        {"num": "8", "score": 4.4, "company": "Oddball", "role": "Backend Engineer",
         "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a8", "report": "", "verdict": ""},
        {"num": "9", "score": 3.8, "company": "Solugenix", "role": "Production Support Engineer",
         "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a9", "report": "", "verdict": ""},
    ]


@pytest.fixture
def queue_file(tmp_path):
    path = tmp_path / "evaluated-roles-by-score.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _sample_queue_rows()) + "\n", encoding="utf-8")
    return path


def _tracked_by_key(tracked):
    return {t.key: t for t in tracked}


# ── 1. Normalization / keys ────────────────────────────────────────────────────
class TestRoleKey:
    def test_company_legal_suffix_stripped(self):
        assert handoff.role_key("Ryan, LLC", "Full Stack AI Engineer") == \
               handoff.role_key("Ryan", "Full Stack AI Engineer")

    def test_company_suffix_variants(self):
        base = handoff.role_key("Post Acute Analytics", "Full Stack Software Engineer")
        for variant in ("Post Acute Analytics Inc", "Post Acute Analytics, Inc.", "POST ACUTE ANALYTICS INC"):
            assert handoff.role_key(variant, "Full Stack Software Engineer") == base

    def test_reqid_in_brackets_stripped(self):
        assert handoff.role_key("Skill", "Software Engineer [210669]") == \
               handoff.role_key("Skill", "Software Engineer")

    def test_reqid_in_parens_stripped(self):
        # A parenthetical that is clearly a req/id is noise.
        assert handoff.role_key("Ryan", "Full Stack AI Engineer (req R0019979)") == \
               handoff.role_key("Ryan", "Full Stack AI Engineer")

    def test_alphabetic_paren_qualifier_preserved(self):
        # A real qualifier like team/scope must NOT collapse two distinct roles
        # at the same company (precision over-merge guard).
        assert handoff.role_key("Acme", "Software Engineer (Backend)") != \
               handoff.role_key("Acme", "Software Engineer (Frontend)")

    def test_level_marker_preserved(self):
        assert handoff.role_key("Acme", "Software Engineer") != \
               handoff.role_key("Acme", "Software Engineer II")

    def test_case_insensitive(self):
        assert handoff.role_key("ACME", "SOFTWARE ENGINEER") == \
               handoff.role_key("acme", "software engineer")

    def test_holdings_suffix_stripped(self):
        # Real miss: JOB_LOG "Sally Beauty Holdings" vs queue "Sally Beauty".
        assert handoff.role_key("Sally Beauty Holdings", "IT AI Engineer") == \
               handoff.role_key("Sally Beauty", "IT AI Engineer")

    def test_trailing_city_state_decoration_stripped(self):
        # Indeed reposts the identical req under per-city titles.
        assert handoff.role_key("Speechify", "Software Engineer, Platform - College Station, TX, USA") == \
               handoff.role_key("Speechify", "Software Engineer, Platform")

    def test_trailing_worktype_decoration_stripped(self):
        assert handoff.role_key("YO IT Consulting", "Node.js Software Engineer - Remote") == \
               handoff.role_key("YO IT Consulting", "Node.js Software Engineer")
        assert handoff.role_key("Alignerr", "Software Engineer (AI Training) - Remote Contract") == \
               handoff.role_key("Alignerr", "Software Engineer (AI Training)")

    def test_meaningful_dash_segment_preserved(self):
        # "- Ads" is a real team/scope, not a decoration — must stay distinct.
        assert handoff.role_key("Temu", "Software Engineer - Ads") != \
               handoff.role_key("Temu", "Software Engineer")


class TestParenSubtitleMatching:
    """A parenthetical SUBTITLE on one side only must not defeat dedup
    (real miss: log 'Software Engineer - Ads (SEM Infrastructure)' vs queue
    'Software Engineer - Ads') — but two CONFLICTING qualifiers still mean
    two distinct roles."""

    def _tracked(self, company, role):
        return handoff.TrackedRole(
            key=handoff.role_key(company, role), company=company, role=role, status="applied")

    def _queue(self, company, role):
        return handoff.QueueRole(num="1", score=4.0, company=company, role=role,
                                 url="https://www.linkedin.com/jobs/view/9")

    def test_subtitle_on_log_side_still_deduped(self):
        tracker = [self._tracked("Temu", "Software Engineer - Ads (SEM Infrastructure)")]
        items = handoff.build_work_order([self._queue("Temu", "Software Engineer - Ads")], tracker)
        assert items == []

    def test_subtitle_on_queue_side_still_deduped(self):
        tracker = [self._tracked("Temu", "Software Engineer - Ads")]
        items = handoff.build_work_order(
            [self._queue("Temu", "Software Engineer - Ads (SEM Infrastructure)")], tracker)
        assert items == []

    def test_conflicting_qualifiers_stay_distinct(self):
        tracker = [self._tracked("Acme", "Software Engineer (Backend)")]
        items = handoff.build_work_order([self._queue("Acme", "Software Engineer (Frontend)")], tracker)
        assert len(items) == 1


class TestBoardOf:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.linkedin.com/jobs/view/123", "linkedin"),
        ("https://www.indeed.com/viewjob?jk=abc", "indeed"),
        ("https://www.workatastartup.com/jobs/94300", "waas"),
        ("https://example.com/careers/1", "other"),
    ])
    def test_board_of(self, url, expected):
        assert handoff.board_of(url) == expected


# ── 2. Parsing JOB_LOG.md ──────────────────────────────────────────────────────
class TestParseJobLog:
    def test_applied_table_rows(self):
        tracked = handoff.parse_job_log(SAMPLE_JOB_LOG)
        by_key = _tracked_by_key(tracked)
        ryan = by_key[handoff.role_key("Ryan", "Full Stack AI Engineer")]
        assert ryan.status == "applied"
        assert ryan.board == "indeed"
        assert ryan.date == "2026-07-02"
        temu = by_key[handoff.role_key("Temu", "Software Engineer - Ads")]
        assert temu.status == "applied"
        assert temu.board == "linkedin"

    def test_waas_submitted_vs_drafted(self):
        by_key = _tracked_by_key(handoff.parse_job_log(SAMPLE_JOB_LOG))
        corgi = by_key[handoff.role_key("Corgi Insurance", "Software Engineer")]
        assert corgi.status == "applied"        # note says SUBMITTED
        assert corgi.board == "waas"
        draftco = by_key[handoff.role_key("Draftco", "Product Engineer")]
        assert draftco.status == "drafted"      # note is not SUBMITTED

    def test_needs_thomas_and_to_finish_are_handoff(self):
        by_key = _tracked_by_key(handoff.parse_job_log(SAMPLE_JOB_LOG))
        assert by_key[handoff.role_key("micro1", "Software Engineer, micro Platforms")].status == "handoff"
        assert by_key[handoff.role_key("JPMorganChase", "Experienced Software Engineer, Java/Python")].status == "handoff"

    def test_struck_through_bullet_not_emitted_as_handoff(self):
        # GEICO is struck through (resolved) in To-finish; it must not show up as
        # an open handoff. (It would be in the Applied table in the real log.)
        by_key = _tracked_by_key(handoff.parse_job_log(SAMPLE_JOB_LOG))
        geico = by_key.get(handoff.role_key("GEICO", "Solutions Engineer II - Networking"))
        assert geico is None or geico.status != "handoff"

    def test_skip_bold_pair_extracted(self):
        by_key = _tracked_by_key(handoff.parse_job_log(SAMPLE_JOB_LOG))
        assert by_key[handoff.role_key("Falconer", "Software Engineer, Full-Stack")].status == "skipped"

    def test_prose_mention_is_not_a_skip(self):
        # "Crogl" is named in a non-bold sentence; it must not be marked skipped.
        tracked = handoff.parse_job_log(SAMPLE_JOB_LOG)
        crogl_key = handoff.role_key("Crogl", "AI Engineer")
        assert all(t.key != crogl_key for t in tracked)

    def test_skip_verdict_bullet_outside_skip_section(self):
        # Real JOB_LOG pattern: session-run sections (heading has no "skip")
        # full of bold-pair bullets that END with an explicit verdict. Only an
        # explicit verdict counts — a plain descriptive bullet stays untracked.
        log = (
            "# Log\n\n"
            "### LinkedIn + Indeed run (2026-07-01, session 3)\n"
            "- **Speechify — Software Engineer, Platform** (Remote): requires GCP. "
            "Skip — GCP specialty gap.\n"
            "- **Rice University — Software Engineer III** (Remote): expired on Indeed "
            "— would've been a great match. Moot.\n"
            "- **Blackhawk Network — Software Engineer** (Coppell TX): JD stub, unable "
            "to evaluate despite the promising location — worth a retry.\n"
        )
        by_key = _tracked_by_key(handoff.parse_job_log(log))
        assert by_key[handoff.role_key("Speechify", "Software Engineer, Platform")].status == "skipped"
        assert by_key[handoff.role_key("Rice University", "Software Engineer III")].status == "skipped"
        # "worth a retry" bullet has no skip/expired verdict → must stay fresh.
        assert handoff.role_key("Blackhawk Network", "Software Engineer") not in by_key


# ── 3. Reconcile + writeback ───────────────────────────────────────────────────
class TestReconcile:
    def test_precedence_applied_beats_skipped(self):
        skipped = handoff.TrackedRole(key="acme::se", company="Acme", role="SE", status="skipped")
        applied = handoff.TrackedRole(key="acme::se", company="Acme", role="SE", status="applied")
        merged = handoff.merge_tracked([skipped], [applied])
        assert len(merged) == 1
        assert merged[0].status == "applied"

    def test_reconcile_seeds_tracker_from_log(self):
        tracked = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        statuses = {t.key: t.status for t in tracked}
        assert statuses[handoff.role_key("Ryan", "Full Stack AI Engineer")] == "applied"
        assert statuses[handoff.role_key("Falconer", "Software Engineer, Full-Stack")] == "skipped"

    def test_writeback_folds_into_tracker(self, tmp_path):
        wo = tmp_path / handoff.WORK_ORDER_JSONL
        wo.write_text(
            json.dumps({"rank": 1, "company": "Crogl", "role": "AI Engineer",
                        "url": "https://www.linkedin.com/jobs/view/7", "status": "applied"}) + "\n" +
            json.dumps({"rank": 2, "company": "Oddball", "role": "Backend Engineer",
                        "url": "https://www.indeed.com/viewjob?jk=a8", "status": "skip:on-site only"}) + "\n",
            encoding="utf-8",
        )
        writeback = handoff.load_writeback(wo)
        by_key = _tracked_by_key(writeback)
        assert by_key[handoff.role_key("Crogl", "AI Engineer")].status == "applied"
        oddball = by_key[handoff.role_key("Oddball", "Backend Engineer")]
        assert oddball.status == "skipped"
        assert "on-site" in oddball.reason

    def test_tracker_roundtrip(self, tmp_path):
        path = tmp_path / handoff.DEFAULT_TRACKER_NAME
        roles = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        handoff.write_tracker(path, roles)
        reloaded = handoff.load_tracker(path)
        assert {t.key for t in reloaded} == {t.key for t in roles}
        assert {t.key: t.status for t in reloaded} == {t.key: t.status for t in roles}

    def test_load_tracker_missing_file_is_empty(self, tmp_path):
        assert handoff.load_tracker(tmp_path / "nope.jsonl") == []


# ── 4. Resume-base hint + work-order ───────────────────────────────────────────
class TestSuggestResumeBase:
    @pytest.mark.parametrize("role", [
        "AI Engineer", "Backend Engineer", "Full Stack Software Engineer",
        "Software Engineer", "Forward Deployed Engineer",
    ])
    def test_ai_fullstack_roles_use_adhoc(self, role):
        assert handoff.suggest_resume_base(role) == handoff.RESUME_BASE_AI

    @pytest.mark.parametrize("role", [
        "Production Support Engineer", "Site Reliability Engineer",
        "DevOps Engineer", "Mainframe Developer",
    ])
    def test_ops_roles_use_standard(self, role):
        assert handoff.suggest_resume_base(role) == handoff.RESUME_BASE_STANDARD


class TestBuildWorkOrder:
    def test_excludes_touched_and_ranks_by_score(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        items = handoff.build_work_order(queue, tracker, board="both")
        companies = [i.company for i in items]
        # Only the four genuinely-fresh roles survive, best score first.
        assert companies == ["Curri", "Crogl", "Oddball", "Solugenix"]
        assert [i.rank for i in items] == [1, 2, 3, 4]
        assert all(i.status == "" for i in items)

    def test_resume_base_hint_attached(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        by_company = {i.company: i for i in handoff.build_work_order(queue, tracker)}
        assert by_company["Crogl"].resume_base == handoff.RESUME_BASE_AI
        assert by_company["Solugenix"].resume_base == handoff.RESUME_BASE_STANDARD

    def test_board_filter(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        li = handoff.build_work_order(queue, tracker, board="linkedin")
        assert {i.company for i in li} == {"Curri", "Crogl"}
        assert all(i.board == "linkedin" for i in li)
        ind = handoff.build_work_order(queue, tracker, board="indeed")
        assert {i.company for i in ind} == {"Oddball", "Solugenix"}

    def test_limit(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        items = handoff.build_work_order(queue, tracker, limit=2)
        assert [i.company for i in items] == ["Curri", "Crogl"]


class TestRender:
    def test_jsonl_lines_parse_with_expected_keys(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        items = handoff.build_work_order(queue, tracker)
        lines = handoff.render_work_order_jsonl(items).strip().splitlines()
        assert len(lines) == 4
        first = json.loads(lines[0])
        assert {"rank", "company", "role", "board", "url", "score", "resume_base", "status"} <= set(first)

    def test_md_is_agent_agnostic_and_lists_roles(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        md = handoff.render_work_order_md(
            handoff.build_work_order(queue, tracker), total_queue=len(queue), touched=len(tracker)
        )
        assert "Curri" in md and "Solugenix" in md
        # Must not hard-code a specific browser agent (template ships to everyone).
        assert "cowork" not in md.lower()


# ── 5. Tailoring enrichment ────────────────────────────────────────────────────
class TestEnrichment:
    """Optional work-order enrichment: pre-tailor a candidate-named resume per
    row (reusing pipeline.resume_tailor) so the hand-off ships ready-to-upload
    files. Tailoring failures must never drop or block a row."""

    def _fresh_items(self, queue_file):
        queue = handoff.load_queue(queue_file)
        tracker = handoff.reconcile(SAMPLE_JOB_LOG, existing=[])
        return handoff.build_work_order(queue, tracker)

    def test_tailors_rows_at_or_above_threshold(self, queue_file):
        items = self._fresh_items(queue_file)
        calls = []

        def fake_tailor(item):
            calls.append(item.company)
            return f"career-ops/output/{item.company} - resume.pdf"

        handoff.enrich_with_resumes(items, fake_tailor, min_score=4.0)
        # Curri 4.7, Crogl 4.6, Oddball 4.4 clear 4.0; Solugenix 3.8 does not.
        assert calls == ["Curri", "Crogl", "Oddball"]
        by_company = {i.company: i for i in items}
        assert by_company["Curri"].resume_pdf.endswith("Curri - resume.pdf")
        assert by_company["Solugenix"].resume_pdf == ""

    def test_tailor_failure_keeps_the_row(self, queue_file):
        items = self._fresh_items(queue_file)

        def flaky_tailor(item):
            if item.company == "Crogl":
                raise RuntimeError("LLM unavailable")
            return None  # tailor declined (e.g. no resume.docx) — also fine

        handoff.enrich_with_resumes(items, flaky_tailor, min_score=4.0)
        assert [i.company for i in items] == ["Curri", "Crogl", "Oddball", "Solugenix"]
        assert all(i.resume_pdf == "" for i in items)

    def test_jsonl_carries_resume_pdf(self, queue_file):
        items = self._fresh_items(queue_file)
        handoff.enrich_with_resumes(items, lambda i: f"{i.company}.pdf", min_score=4.0)
        lines = handoff.render_work_order_jsonl(items).strip().splitlines()
        first = json.loads(lines[0])
        assert first["resume_pdf"] == "Curri.pdf"


# ── 6. CLI ─────────────────────────────────────────────────────────────────────
class TestMain:
    def test_main_writes_tracker_and_work_order(self, tmp_path, queue_file):
        job_log = tmp_path / "JOB_LOG.md"
        job_log.write_text(SAMPLE_JOB_LOG, encoding="utf-8")
        out_dir = tmp_path / "handoff"
        tracker = tmp_path / handoff.DEFAULT_TRACKER_NAME

        rc = handoff.main([
            "--queue", str(queue_file),
            "--job-log", str(job_log),
            "--tracker", str(tracker),
            "--out-dir", str(out_dir),
        ])
        assert rc == 0
        assert tracker.exists()

        wo_jsonl = out_dir / handoff.WORK_ORDER_JSONL
        wo_md = out_dir / handoff.WORK_ORDER_MD
        assert wo_jsonl.exists() and wo_md.exists()

        companies = [json.loads(l)["company"] for l in wo_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert companies == ["Curri", "Crogl", "Oddball", "Solugenix"]
        # Applied/handoff/skipped roles must be absent from the hand-off.
        for gone in ("Ryan", "Temu", "JPMorganChase", "micro1", "Falconer"):
            assert gone not in companies

    def test_main_is_rerunnable_and_idempotent(self, tmp_path, queue_file):
        job_log = tmp_path / "JOB_LOG.md"
        job_log.write_text(SAMPLE_JOB_LOG, encoding="utf-8")
        out_dir = tmp_path / "handoff"
        tracker = tmp_path / handoff.DEFAULT_TRACKER_NAME
        args = ["--queue", str(queue_file), "--job-log", str(job_log),
                "--tracker", str(tracker), "--out-dir", str(out_dir)]
        assert handoff.main(args) == 0
        first = (out_dir / handoff.WORK_ORDER_JSONL).read_text(encoding="utf-8")
        assert handoff.main(args) == 0
        assert (out_dir / handoff.WORK_ORDER_JSONL).read_text(encoding="utf-8") == first


# ── 7. Code-review regressions (2026-07-03 review round) ──────────────────────
class TestQualifierPreservedFromProse:
    """Review bug: _truncate_role cut at " (" for bold-pair bullets, so a logged
    skip of "SE (Backend)" swallowed the sibling "SE (Frontend)" via the fuzzy
    matcher's one-sided rule. Bold already bounds the role — no paren cut."""

    LOG = (
        "# Log\n\n"
        "### Skipped this run\n"
        "- **Acme — Software Engineer (Backend)** (LinkedIn, on-site): office only. Skip.\n"
    )

    def test_bold_pair_keeps_qualifier(self):
        by_key = _tracked_by_key(handoff.parse_job_log(self.LOG))
        assert handoff.role_key("Acme", "Software Engineer (Backend)") in by_key

    def test_sibling_qualifier_role_stays_fresh(self):
        tracker = handoff.parse_job_log(self.LOG)
        queue = [
            handoff.QueueRole(num="1", score=4.0, company="Acme",
                              role="Software Engineer (Backend)",
                              url="https://www.linkedin.com/jobs/view/1"),
            handoff.QueueRole(num="2", score=4.0, company="Acme",
                              role="Software Engineer (Frontend)",
                              url="https://www.linkedin.com/jobs/view/2"),
        ]
        items = handoff.build_work_order(queue, tracker)
        assert [i.role for i in items] == ["Software Engineer (Frontend)"]


class TestDecorationStripIsStateCodeExact:
    """Review bug: the city-decoration rule ran under IGNORECASE, so any
    trailing "- Word, Xy" scope ("- NLP, ML", "- React, UI") peeled like a city
    tag and merged distinct roles. Only real US state codes peel."""

    @pytest.mark.parametrize("role,base", [
        ("Software Engineer - NLP, ML", "Software Engineer"),
        ("Software Engineer - React, UI", "Software Engineer"),
        ("Platform Engineer - Kubernetes, Go", "Platform Engineer"),
    ])
    def test_tech_scope_segments_survive(self, role, base):
        assert handoff.norm_role(role) != handoff.norm_role(base)

    def test_real_state_tags_still_peel(self):
        assert handoff.norm_role("Software Engineer, Platform - Tempe, AZ, USA") == \
               handoff.norm_role("Software Engineer, Platform")
        assert handoff.norm_role("SWE - Fort Collins, CO") == handoff.norm_role("SWE")


class TestEnrichOnePerCompany:
    """Review bug: the tailor cache is company-keyed (<Company> - resume.pdf),
    so tailoring two roles at one company aliases one file (and races under the
    pool). Only the best-ranked row per company gets a pre-tailored resume."""

    def _items(self):
        return [
            handoff.WorkOrderItem(rank=1, num="1", score=4.8, company="JPMC",
                                  role="SWE III - AWS", board="indeed", url="u1",
                                  resume_base="content_adhoc"),
            handoff.WorkOrderItem(rank=2, num="2", score=4.6, company="JPMC",
                                  role="SWE III - Java", board="indeed", url="u2",
                                  resume_base="content_adhoc"),
            handoff.WorkOrderItem(rank=3, num="3", score=4.5, company="Acme",
                                  role="Backend Engineer", board="linkedin", url="u3",
                                  resume_base="content_adhoc"),
        ]

    def test_only_best_row_per_company_tailored(self):
        items = self._items()
        calls = []
        handoff.enrich_with_resumes(items, lambda i: calls.append(i.company) or f"{i.company}.pdf",
                                    min_score=4.0)
        assert calls == ["JPMC", "Acme"]          # one call per company
        assert items[0].resume_pdf == "JPMC.pdf"  # best JPMC row got the file
        assert items[1].resume_pdf == ""          # sibling row left for the agent
        assert items[2].resume_pdf == "Acme.pdf"


class TestActedOnQueueStatusesExcluded:
    """Review bug: only Applied/Rejected were filtered; Interview/Offer/
    Discarded/SKIP queue rows re-entered the work-order."""

    @pytest.mark.parametrize("status", ["Interview", "Offer", "Discarded", "SKIP"])
    def test_acted_on_row_excluded(self, status):
        queue = [handoff.QueueRole(num="1", score=4.5, company="Acme", role="SWE",
                                   url="https://www.linkedin.com/jobs/view/1", status=status)]
        assert handoff.build_work_order(queue, []) == []

    def test_evaluated_row_included(self):
        queue = [handoff.QueueRole(num="1", score=4.5, company="Acme", role="SWE",
                                   url="https://www.linkedin.com/jobs/view/1", status="Evaluated")]
        assert len(handoff.build_work_order(queue, [])) == 1


class TestLateWritebackFold:
    """Review bug: statuses an agent wrote into next-roles.jsonl DURING a long
    enrichment were clobbered by the final overwrite and their roles re-emitted
    status-empty. A second read right before writing folds + drops them."""

    def test_late_statuses_drop_rows_and_reach_tracker(self, tmp_path):
        wo = tmp_path / handoff.WORK_ORDER_JSONL
        wo.write_text(json.dumps({
            "rank": 1, "company": "Acme", "role": "SWE",
            "url": "https://www.linkedin.com/jobs/view/1", "status": "applied",
        }) + "\n", encoding="utf-8")
        items = [
            handoff.WorkOrderItem(rank=1, num="1", score=4.5, company="Acme", role="SWE",
                                  board="linkedin", url="https://www.linkedin.com/jobs/view/1",
                                  resume_base="content_adhoc"),
            handoff.WorkOrderItem(rank=2, num="2", score=4.0, company="Globex", role="Dev",
                                  board="indeed", url="https://www.indeed.com/viewjob?jk=b",
                                  resume_base="content_adhoc"),
        ]
        kept, late = handoff.drop_late_writeback(items, tmp_path)
        assert [i.company for i in kept] == ["Globex"]
        assert [i.rank for i in kept] == [1]      # ranks renumbered
        assert {t.key for t in late} == {handoff.role_key("Acme", "SWE")}

    def test_no_writeback_is_noop(self, tmp_path):
        items = [handoff.WorkOrderItem(rank=1, num="1", score=4.5, company="Acme", role="SWE",
                                       board="linkedin", url="u", resume_base="content_adhoc")]
        kept, late = handoff.drop_late_writeback(items, tmp_path)
        assert kept == items and late == []


class TestQueueFromTracker:
    """Review gap: nothing in the repo produces evaluated-roles-by-score.jsonl,
    so --handoff dead-ended for template users. The tracker (applications.md,
    written by every --evaluate-batch run) is the default queue source when the
    jsonl export is absent."""

    TRACKER = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-06-01 | Acme | Engineer | 4.2/5 | Evaluated | X | [001](reports/001.md) | https://www.linkedin.com/jobs/view/1 — fit |\n"
        "| 2 | 2026-06-01 | Globex | Dev | 3.0/5 | Discarded | X | [002](reports/002.md) | https://example.com/2 — gone |\n"
        "| 3 | 2026-06-01 | Initech | SWE | 4.8/5 | Evaluated | X | [003](reports/003.md) | no url in notes |\n"
    )

    def _career_ops(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "data" / "applications.md").write_text(self.TRACKER, encoding="utf-8")
        return co

    def test_loads_evaluated_rows_with_urls(self, tmp_path):
        co = self._career_ops(tmp_path)
        rows = handoff.load_queue_from_tracker(co)
        assert [(r.company, r.score, r.status) for r in rows] == [("Acme", 4.2, "Evaluated")]
        assert rows[0].url == "https://www.linkedin.com/jobs/view/1"

    def test_run_falls_back_to_tracker_when_jsonl_missing(self, tmp_path):
        co = self._career_ops(tmp_path)
        out = tmp_path / "handoff"
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl", out_dir=out, career_ops=co)
        assert rc == 0
        lines = (out / handoff.WORK_ORDER_JSONL).read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(l)["company"] for l in lines] == ["Acme"]

    def test_run_errors_when_neither_source_exists(self, tmp_path):
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl", out_dir=tmp_path / "o",
                         career_ops=tmp_path / "no-career-ops")
        assert rc == 1


class TestSharedTailorCaller:
    """Review bug: each pooled row resolved its own LLM caller, giving every
    worker a private Gemini rate limiter (free-tier pacing defeated). The
    adapter resolves once, lazily, and shares the caller across rows."""

    def test_caller_resolved_once_and_shared(self, monkeypatch, tmp_path):
        import pipeline.resume_tailor as rt
        resolved = []
        captured = []
        monkeypatch.setattr(rt, "_resolve_caller",
                            lambda p, m: resolved.append(1) or (lambda s, u: "{}"))
        monkeypatch.setattr(rt, "generate_for_job",
                            lambda co, job, caller=None, **kw: captured.append(caller) or None)
        fn = handoff._make_tailor_fn(tmp_path)
        item = handoff.WorkOrderItem(rank=1, num="1", score=4.5, company="A", role="R",
                                     board="linkedin", url="u", resume_base="content_adhoc")
        fn(item); fn(item)
        # Every row shares ONE caller object…
        assert captured[0] is captured[1]
        assert captured[0] is not None
        # …which resolves the provider lazily on first INVOCATION (a fully
        # cached run never invokes it, so a keyless machine still works)…
        assert resolved == []
        assert captured[0]("sys", "user") == "{}"
        assert captured[0]("sys", "user") == "{}"
        # …and exactly once across invocations (one shared rate limiter).
        assert resolved == [1]


class TestOutDirEnv:
    """HANDOFF_OUT_DIR points the work-order at a directory the user's browser
    agent can actually reach (e.g. the folder a Cowork session is connected
    to). Resolved inside run()/default_out_dir() so the CLI, orchestrate, and
    the UI all honor it from one place; an explicit out_dir still wins."""

    TRACKER = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-07-01 | Acme | Engineer | 4.2/5 | Evaluated | X | [001](reports/001.md) | https://www.linkedin.com/jobs/view/1 — fit |\n"
    )

    def _career_ops(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "data" / "applications.md").write_text(self.TRACKER, encoding="utf-8")
        return co

    def test_env_dir_used_when_out_dir_omitted(self, tmp_path, monkeypatch):
        agent_home = tmp_path / "agent-home"
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(agent_home))
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl",
                         career_ops=self._career_ops(tmp_path))
        assert rc == 0
        assert (agent_home / handoff.WORK_ORDER_JSONL).exists()
        assert (agent_home / handoff.DEFAULT_TRACKER_NAME).exists()

    def test_explicit_out_dir_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(tmp_path / "env-dir"))
        explicit = tmp_path / "explicit"
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl",
                         career_ops=self._career_ops(tmp_path),
                         out_dir=explicit)
        assert rc == 0
        assert (explicit / handoff.WORK_ORDER_JSONL).exists()
        assert not (tmp_path / "env-dir").exists()

    def test_default_out_dir_helper_exposed(self, tmp_path, monkeypatch):
        # The UI reads results/prompt paths from the same resolver run() uses.
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(tmp_path / "agent-home"))
        assert handoff.default_out_dir() == tmp_path / "agent-home"
        monkeypatch.delenv("HANDOFF_OUT_DIR")
        assert handoff.default_out_dir() == handoff.ROOT / "output" / "handoff"
