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
from pipeline.app import data as app_data


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
        # Every other JobSpy site is now its own board (was "other").
        ("https://www.glassdoor.com/job-listing/abc", "glassdoor"),
        ("https://www.glassdoor.co.uk/job-listing/abc", "glassdoor"),  # any TLD
        ("https://www.ziprecruiter.com/jobs/abc", "zip_recruiter"),    # JobSpy enum spelling
        ("https://www.bayt.com/en/job/abc", "bayt"),
        ("https://www.naukri.com/job-listings-abc", "naukri"),
        ("https://bdjobs.com/jobdetails?id=abc", "bdjobs"),
        ("https://www.workatastartup.com/jobs/94300", "waas"),
        # Unrecognized domains (incl. Google-sourced employer/ATS URLs) → catch-all.
        ("https://boards.greenhouse.io/acme/jobs/1", "other"),
        ("https://example.com/careers/1", "other"),
    ])
    def test_board_of(self, url, expected):
        assert handoff.board_of(url) == expected

    def test_known_boards_covers_jobspy_sites(self):
        # KNOWN_BOARDS is the vocabulary the CLI/UI expose and every value
        # board_of() can emit; it must cover the JobSpy sites we tag by URL.
        assert {"linkedin", "indeed", "glassdoor", "zip_recruiter",
                "bayt", "naukri", "bdjobs"} <= handoff.KNOWN_BOARDS
        assert "other" in handoff.KNOWN_BOARDS          # the catch-all session
        assert "both" not in handoff.KNOWN_BOARDS       # "both" is a selector, not a board

    def test_board_labels_cover_known_boards(self):
        # Every board board_of() can emit needs a human label — else the raw tag
        # (e.g. "zip_recruiter") leaks into work-order headers and kickoff prompts.
        # Guards the label table from drifting when a new site is added.
        missing = [b for b in handoff.KNOWN_BOARDS if b not in handoff._BOARD_LABELS]
        assert not missing, f"KNOWN_BOARDS with no _BOARD_LABELS entry: {missing}"


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


def _multi_site_queue():
    """A fresh (untouched) queue spanning several sites: 2 linkedin, 2 indeed,
    1 glassdoor, 1 zip_recruiter, and 1 unrecognized domain (→ other)."""
    rows = [
        ("Curri", "Software Engineer", 4.7, "https://www.linkedin.com/jobs/view/1"),
        ("Crogl", "AI Engineer", 4.6, "https://www.linkedin.com/jobs/view/2"),
        ("Oddball", "Backend Engineer", 4.4, "https://www.indeed.com/viewjob?jk=a1"),
        ("Solugenix", "Full Stack Engineer", 3.8, "https://www.indeed.com/viewjob?jk=a2"),
        ("Glasso", "Platform Engineer", 4.5, "https://www.glassdoor.com/job-listing/x"),
        ("Zippy", "Software Engineer", 4.1, "https://www.ziprecruiter.com/jobs/z"),
        ("Mystery", "AI Engineer", 4.0, "https://boards.greenhouse.io/mystery/jobs/9"),
    ]
    return [handoff.QueueRole(num=str(i + 1), score=s, company=c, role=r,
                              url=u, status="Evaluated")
            for i, (c, r, s, u) in enumerate(rows)]


class TestBuildSessions:
    """The generalization: instead of one board-filtered work-order, partition
    the fresh queue into one session per site the scraper searches from."""

    def test_partitions_by_site(self):
        sessions = handoff.build_sessions(_multi_site_queue(), [])
        assert set(sessions) == {"linkedin", "indeed", "glassdoor",
                                 "zip_recruiter", "other"}
        for board, items in sessions.items():
            assert all(i.board == board for i in items)

    def test_unknown_domain_lands_in_other(self):
        sessions = handoff.build_sessions(_multi_site_queue(), [])
        assert {i.company for i in sessions["other"]} == {"Mystery"}

    def test_ranks_renumbered_per_session(self):
        sessions = handoff.build_sessions(_multi_site_queue(), [])
        # Each session is independently ranked 1..N, best score first.
        assert [i.rank for i in sessions["linkedin"]] == [1, 2]
        assert [i.company for i in sessions["linkedin"]] == ["Curri", "Crogl"]
        assert [i.rank for i in sessions["indeed"]] == [1, 2]
        assert [i.company for i in sessions["indeed"]] == ["Oddball", "Solugenix"]

    def test_every_fresh_role_in_exactly_one_session(self):
        queue = _multi_site_queue()
        sessions = handoff.build_sessions(queue, [])
        keys = [handoff.role_key(i.company, i.role)
                for items in sessions.values() for i in items]
        assert len(keys) == len(queue)          # nothing dropped
        assert len(set(keys)) == len(keys)      # nothing duplicated across sessions

    def test_limit_is_per_session(self):
        # limit=1 caps EACH session at its single best role, not 1 role total.
        sessions = handoff.build_sessions(_multi_site_queue(), [], limit=1)
        assert all(len(items) == 1 for items in sessions.values())
        assert sessions["linkedin"][0].company == "Curri"   # the site's top score
        assert sessions["indeed"][0].company == "Oddball"

    @pytest.mark.parametrize("limit", [0, -3, None])
    def test_non_positive_limit_means_no_limit(self, limit):
        sessions = handoff.build_sessions(_multi_site_queue(), [], limit=limit)
        assert len(sessions["linkedin"]) == 2

    def test_touched_roles_excluded_from_sessions(self):
        # A role already in the tracker (any site) never appears in a session.
        tracker = [handoff.TrackedRole(
            key=handoff.role_key("Curri", "Software Engineer"),
            company="Curri", role="Software Engineer", status="applied")]
        sessions = handoff.build_sessions(_multi_site_queue(), tracker)
        assert "Curri" not in {i.company for items in sessions.values() for i in items}


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

    def test_md_for_a_site_names_that_sites_file(self):
        items = handoff.build_sessions(_multi_site_queue(), [])["linkedin"]
        md = handoff.render_work_order_md(items, board="linkedin",
                                          total_queue=7, touched=0)
        # The writeback legend must point at THIS site's file, not the generic one.
        assert "next-roles-linkedin.jsonl" in md
        assert "linkedin" in md.lower()
        assert "cowork" not in md.lower()          # still agent-agnostic

    def test_kickoff_prompt_names_site_files(self, tmp_path):
        wo = handoff.work_order_paths(tmp_path, "linkedin")[0]
        assert wo.name == "next-roles-linkedin.jsonl"
        prompt = handoff.kickoff_prompt(wo, board="linkedin")
        assert "next-roles-linkedin.jsonl" in prompt
        assert "next-roles-linkedin.md" in prompt   # human-readable sibling
        assert "cowork" not in prompt.lower()


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
    def test_main_writes_tracker_and_per_site_work_orders(self, tmp_path, queue_file):
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

        def companies(board):
            jsonl, md = handoff.work_order_paths(out_dir, board)
            assert jsonl.exists() and md.exists()
            return [json.loads(l)["company"]
                    for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]

        # One session per site, each ranked best-first (queue_file: Curri/Crogl
        # are LinkedIn, Oddball/Solugenix are Indeed).
        assert companies("linkedin") == ["Curri", "Crogl"]
        assert companies("indeed") == ["Oddball", "Solugenix"]
        # Applied/handoff/skipped roles must be absent from every session.
        allc = companies("linkedin") + companies("indeed")
        for gone in ("Ryan", "Temu", "JPMorganChase", "micro1", "Falconer"):
            assert gone not in allc

    def test_main_is_rerunnable_and_idempotent(self, tmp_path, queue_file):
        job_log = tmp_path / "JOB_LOG.md"
        job_log.write_text(SAMPLE_JOB_LOG, encoding="utf-8")
        out_dir = tmp_path / "handoff"
        tracker = tmp_path / handoff.DEFAULT_TRACKER_NAME
        args = ["--queue", str(queue_file), "--job-log", str(job_log),
                "--tracker", str(tracker), "--out-dir", str(out_dir)]
        li = handoff.work_order_paths(out_dir, "linkedin")[0]
        assert handoff.main(args) == 0
        first = li.read_text(encoding="utf-8")
        assert handoff.main(args) == 0
        assert li.read_text(encoding="utf-8") == first

    def test_main_bootstrap_dir_seeds_without_a_queue(self, tmp_path):
        # The setup-script entrypoint: create + seed the handoff dir with no queue,
        # no career-ops, no work-order build.
        out = tmp_path / "handoff"
        rc = handoff.main(["--bootstrap-dir", "--out-dir", str(out)])
        assert rc == 0
        assert (out / handoff.HANDOFF_README).exists()
        # It bootstraps only — no work-order files written.
        assert not handoff.work_order_paths(out, "linkedin")[0].exists()


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

    def test_late_status_in_per_site_file_is_caught(self, tmp_path):
        # The status now lands in a per-site file (next-roles-<board>.jsonl), not
        # the legacy combined file — drop_late_writeback must read them all.
        handoff.work_order_paths(tmp_path, "linkedin")[0].write_text(json.dumps({
            "company": "Acme", "role": "SWE",
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


class TestLoadAllWriteback:
    """Writeback can arrive in any per-site file — and, right after the upgrade,
    still in the legacy combined next-roles.jsonl an agent hasn't finished.
    load_all_writeback unions them all so no status is lost."""

    def test_unions_per_site_and_legacy_files(self, tmp_path):
        handoff.work_order_paths(tmp_path, "linkedin")[0].write_text(json.dumps({
            "company": "Acme", "role": "SWE",
            "url": "https://www.linkedin.com/jobs/view/1", "status": "applied"}) + "\n",
            encoding="utf-8")
        handoff.work_order_paths(tmp_path, "indeed")[0].write_text(json.dumps({
            "company": "Globex", "role": "Dev",
            "url": "https://www.indeed.com/viewjob?jk=b", "status": "skip:onsite"}) + "\n",
            encoding="utf-8")
        # A pre-upgrade combined file an agent is still working.
        (tmp_path / handoff.WORK_ORDER_JSONL).write_text(json.dumps({
            "company": "Initech", "role": "Engineer",
            "url": "https://www.linkedin.com/jobs/view/3", "status": "handoff"}) + "\n",
            encoding="utf-8")
        wb = {t.key: t for t in handoff.load_all_writeback(tmp_path)}
        assert set(wb) == {
            handoff.role_key("Acme", "SWE"),
            handoff.role_key("Globex", "Dev"),
            handoff.role_key("Initech", "Engineer"),
        }
        assert wb[handoff.role_key("Globex", "Dev")].status == "skipped"  # skip:<reason> parsed

    def test_empty_dir_is_empty(self, tmp_path):
        assert handoff.load_all_writeback(tmp_path) == []


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
        # Acme's posting is a LinkedIn URL → it lands in the LinkedIn session.
        lines = handoff.work_order_paths(out, "linkedin")[0].read_text(
            encoding="utf-8").strip().splitlines()
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
        assert handoff.work_order_paths(agent_home, "linkedin")[0].exists()  # Acme → LinkedIn
        assert (agent_home / handoff.DEFAULT_TRACKER_NAME).exists()

    def test_explicit_out_dir_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(tmp_path / "env-dir"))
        explicit = tmp_path / "explicit"
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl",
                         career_ops=self._career_ops(tmp_path),
                         out_dir=explicit)
        assert rc == 0
        assert handoff.work_order_paths(explicit, "linkedin")[0].exists()  # Acme → LinkedIn
        assert not (tmp_path / "env-dir").exists()

    def test_default_out_dir_helper_exposed(self, tmp_path, monkeypatch):
        # The UI reads results/prompt paths from the same resolver run() uses.
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(tmp_path / "agent-home"))
        assert handoff.default_out_dir() == tmp_path / "agent-home"
        monkeypatch.delenv("HANDOFF_OUT_DIR")
        assert handoff.default_out_dir() == handoff.ROOT / "output" / "handoff"


class TestTrackerStatusSync:
    """Bridge: agent writeback surfaces in career-ops' applications.md — the
    tracker the UI renders and the cloud maintains. applied->Applied, skip->SKIP;
    handoff/claimed stay dedup-only. Anchored by company/role identity (cloud
    row-numbers are minted independently), reusing the UI's record_status_changes
    so a pending cloud override is queued for the edit-tracker push. The autouse
    conftest fixture isolates the override file to tmp."""

    APPS = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 7 | 2026-07-01 | Acme | AI Engineer | 4.5/5 | Evaluated | X | [007](reports/007.md) | https://www.linkedin.com/jobs/view/7 |\n"
        "| 8 | 2026-07-01 | Globex | Backend Engineer | 4.2/5 | Evaluated | X | [008](reports/008.md) | https://www.indeed.com/viewjob?jk=8 |\n"
    )

    def _apps(self, tmp_path, text=None):
        p = tmp_path / "applications.md"
        p.write_text(text or self.APPS, encoding="utf-8")
        return p

    def _wb(self, company, role, status):
        return handoff.TrackedRole(key=handoff.role_key(company, role),
                                   company=company, role=role, status=status)

    def _status(self, apps_md, company, role):
        want = handoff.role_key(company, role)
        for row in app_data.parse_applications(apps_md):
            if handoff.role_key(row["company"], row["role"]) == want:
                return row.get("status_canonical")
        return None

    def _apps_status(self, tmp_path, status):
        # The APPS fixture with Acme's row set to `status`.
        return self._apps(tmp_path, self.APPS.replace(
            "| Acme | AI Engineer | 4.5/5 | Evaluated",
            f"| Acme | AI Engineer | 4.5/5 | {status}"))

    def test_applied_marks_applied_and_queues_cloud_override(self, tmp_path):
        apps = self._apps(tmp_path)
        n = handoff.sync_tracker_statuses([self._wb("Acme", "AI Engineer", "applied")], apps)
        assert n == 1
        assert self._status(apps, "Acme", "AI Engineer") == "Applied"
        # A pending, identity-anchored cloud override is queued for edit-tracker.
        vals = [v for v in app_data.load_status_overrides().values()
                if app_data.override_identity(v) == ("Acme", "AI Engineer")]
        assert vals and app_data.override_status(vals[0]) == "Applied"

    def test_skip_marks_skip(self, tmp_path):
        apps = self._apps(tmp_path)
        handoff.sync_tracker_statuses([self._wb("Globex", "Backend Engineer", "skipped")], apps)
        assert self._status(apps, "Globex", "Backend Engineer") == "SKIP"

    def test_handoff_and_claimed_not_synced(self, tmp_path):
        apps = self._apps(tmp_path)
        n = handoff.sync_tracker_statuses(
            [self._wb("Acme", "AI Engineer", "handoff"),
             self._wb("Globex", "Backend Engineer", "claimed")], apps)
        assert n == 0
        assert self._status(apps, "Acme", "AI Engineer") == "Evaluated"
        assert self._status(apps, "Globex", "Backend Engineer") == "Evaluated"

    def test_already_applied_row_is_idempotent(self, tmp_path):
        # A row already reflected (not Evaluated) is left untouched — no re-write,
        # no re-queued cloud override.
        apps = self._apps_status(tmp_path, "Applied")
        assert handoff.sync_tracker_statuses([self._wb("Acme", "AI Engineer", "applied")], apps) == 0
        assert app_data.load_status_overrides() == {}

    def test_does_not_downgrade_a_further_along_status(self, tmp_path):
        # M1: a stale `applied` writeback must not revert a manually-advanced row.
        apps = self._apps_status(tmp_path, "Responded")
        assert handoff.sync_tracker_statuses([self._wb("Acme", "AI Engineer", "applied")], apps) == 0
        assert self._status(apps, "Acme", "AI Engineer") == "Responded"

    def test_skip_does_not_clobber_applied(self, tmp_path):
        # M1: `skip:already-applied` on an already-Applied row must not downgrade it.
        apps = self._apps_status(tmp_path, "Applied")
        assert handoff.sync_tracker_statuses([self._wb("Acme", "AI Engineer", "skipped")], apps) == 0
        assert self._status(apps, "Acme", "AI Engineer") == "Applied"

    def test_resolves_across_legal_suffix_and_decoration_variance(self, tmp_path):
        # M2: the writeback identity differs from the tracker's by a legal suffix
        # AND a "- Remote" decoration, but role_key normalizes both, so the row
        # still resolves (and the override anchors on the tracker's clean identity).
        apps = self._apps(tmp_path,
            "# Applications Tracker\n\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 4 | 2026-07-01 | Ryan | Backend Engineer | 4.3/5 | Evaluated | X | [004](reports/004.md) | https://www.indeed.com/viewjob?jk=4 |\n")
        n = handoff.sync_tracker_statuses(
            [self._wb("Ryan, LLC", "Backend Engineer - Remote", "applied")], apps)
        assert n == 1
        assert self._status(apps, "Ryan", "Backend Engineer") == "Applied"
        vals = list(app_data.load_status_overrides().values())
        assert vals and app_data.override_identity(vals[0]) == ("Ryan", "Backend Engineer")

    def test_queued_override_reanchors_on_a_renumbered_tracker(self, tmp_path):
        # The point of identity anchoring: the queued override carries
        # company/role, so a push re-resolves it onto the correct row even when
        # the cloud tracker numbers that role differently (8 locally -> 42 cloud).
        apps = self._apps(tmp_path)
        handoff.sync_tracker_statuses([self._wb("Globex", "Backend Engineer", "applied")], apps)
        cloud = self.APPS.replace("| 8 |", "| 42 |")
        _, payload, unresolved = app_data.resolve_overrides_for_push(
            cloud, app_data.load_status_overrides())
        assert not unresolved
        assert payload.get("42") == "Applied"      # re-anchored onto the cloud's num

    def test_role_absent_from_tracker_is_skipped(self, tmp_path):
        apps = self._apps(tmp_path)
        assert handoff.sync_tracker_statuses(
            [self._wb("Nowhere", "Ghost Engineer", "applied")], apps) == 0

    def test_missing_applications_md_is_noop(self, tmp_path):
        assert handoff.sync_tracker_statuses(
            [self._wb("Acme", "AI Engineer", "applied")], tmp_path / "nope.md") == 0


class TestHandoffReadme:
    """The seeded agent-instructions README — generated from WRITEBACK_STATUSES so
    its status legend can't drift, and agent-agnostic (ships to every user)."""

    def test_covers_work_order_format_and_writeback_vocab(self):
        md = handoff.render_handoff_readme()
        assert "next-roles-" in md              # the per-site work-order files
        assert "role-status.jsonl" in md        # the tracker (don't hand-edit)
        for token, _ in handoff.WRITEBACK_STATUSES:
            assert token in md                  # the full writeback legend
        assert "cowork" not in md.lower()       # agent-agnostic

    def test_points_agent_at_the_living_profile(self):
        # The README must send the agent to the living master (PROFILE.md) and
        # say it's a document to grow — the whole point of Commit 2.
        md = handoff.render_handoff_readme()
        assert handoff.HANDOFF_PROFILE in md
        assert "grow" in md.lower() or "living" in md.lower()


class TestBootstrapHandoffDir:
    """Create + seed the handoff directory. Non-clobbering (the folder accumulates
    the user's own files) and idempotent (safe to call every run)."""

    def test_creates_dir_and_seeds_readme(self, tmp_path):
        out = tmp_path / "agent-home"
        readme = handoff.bootstrap_handoff_dir(out)
        assert out.is_dir()
        assert readme == out / handoff.HANDOFF_README
        assert readme.read_text(encoding="utf-8") == handoff.render_handoff_readme()

    def test_does_not_clobber_existing_readme(self, tmp_path):
        out = tmp_path / "agent-home"
        out.mkdir()
        (out / handoff.HANDOFF_README).write_text("my own notes", encoding="utf-8")
        handoff.bootstrap_handoff_dir(out)
        assert (out / handoff.HANDOFF_README).read_text(encoding="utf-8") == "my own notes"

    def test_idempotent(self, tmp_path):
        out = tmp_path / "agent-home"
        handoff.bootstrap_handoff_dir(out)
        handoff.bootstrap_handoff_dir(out)      # no error, still seeded
        assert (out / handoff.HANDOFF_README).exists()


# ── Living PROFILE.md — the browser agent's master, seeded from career-ops ─────
# A representative cv.md + profile.yml subset. The metrics are load-bearing: the
# entire point of the seeded fact bank is that quantitative results survive into
# the profile VERBATIM — dropping them was the #1 complaint about the old resume
# tailor, so the seed must never trim them.
_FIXTURE_CV = """# Jane Doe

## Professional Summary
Engineer who shipped an 840-star observability platform via an agentic workflow.

## Skills
Languages: Python · Go · SQL
Cloud & CI/CD: AWS · Docker · GitHub Actions

PROFESSIONAL EXPERIENCE
Capital One  Aug 2022 – Sep 2023
Software Engineer  Chicago, IL
• Built Java/Spring APIs powering two launches — BJ's opt-out (1M+ cardholders) and Kohl's (millions); shipped to AWS ECS via CI/CD.
Bank of America  Apr 2025 – Present
Production Support Engineer  Plano, TX
• Kept mission-critical financial systems at 99.999% uptime on a 24/7 on-call rotation.
"""

# Distinctive, quantified strings that MUST reach the profile untouched.
_FIXTURE_CV_METRICS = ["840-star", "1M+ cardholders", "99.999% uptime", "24/7"]

_FIXTURE_PROFILE = {
    "candidate": {
        "full_name": "Jane Doe", "email": "jane@example.com",
        "phone": "+1 (555) 010-2030", "location": "Dallas, TX",
        "linkedin": "linkedin.com/in/jane-doe", "github": "github.com/janedoe",
    },
    "narrative": {
        "headline": "Software engineer building impactful products",
        "exit_story": "Passionate about shipping quality software.",
        "superpowers": ["Full-stack development", "Problem-solving"],
    },
    "target_roles": {"primary": ["AI Engineer", "Applied AI Engineer"]},
    "compensation": {"target_range": "$75K-$500K", "minimum": "$75K",
                     "location_flexibility": "Remote preferred"},
    "location": {"country": "United States", "city": "Dallas", "state": "Texas",
                 "timezone": "CST", "visa_status": "No sponsorship needed"},
    "work_authorization": {"citizenship": "US", "requires_sponsorship": False,
                           "work_permit_type": "Citizen"},
    "voluntary_disclosures": {
        "gender": "Male", "race_ethnicity": "Hispanic",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "Yes, I have a disability (or previously had one)",
    },
}

# The sections the living master must always carry (case-insensitive substrings).
_PROFILE_SECTIONS = ["Identity", "Positioning", "fact bank", "Skills",
                     "Standing answers", "Tailoring", "grow"]


class TestRenderProfileMd:
    """render_profile_md assembles the seed for the browser agent's living master
    from career-ops data (cv.md + profile.yml). Contract: every required section
    is present; metrics survive verbatim (the anti-regression core); the standing
    form-answers are filled from profile.yml; it degrades to a usable scaffold
    when a source is missing."""

    def _md(self, **kw):
        return handoff.render_profile_md(**kw)

    def test_has_all_master_sections(self):
        md = self._md(cv_md=_FIXTURE_CV, profile=_FIXTURE_PROFILE).lower()
        for section in _PROFILE_SECTIONS:
            assert section.lower() in md, f"missing section: {section}"

    @pytest.mark.parametrize("metric", _FIXTURE_CV_METRICS)
    def test_metrics_survive_verbatim(self, metric):
        # The user's #1 quality requirement: quantitative results are copied in
        # verbatim and never trimmed.
        assert metric in self._md(cv_md=_FIXTURE_CV, profile=_FIXTURE_PROFILE)

    def test_skills_seeded_from_cv(self):
        md = self._md(cv_md=_FIXTURE_CV, profile=_FIXTURE_PROFILE)
        assert "Python" in md and "AWS" in md

    def test_identity_from_profile(self):
        md = self._md(cv_md=_FIXTURE_CV, profile=_FIXTURE_PROFILE)
        assert "Jane Doe" in md and "jane@example.com" in md

    def test_standing_answers_from_profile(self):
        # The form-fill answers the agent needs on every application — work auth,
        # comp, location, and the EEO/voluntary disclosures — sourced from
        # profile.yml so the agent never has to re-ask the user.
        md = self._md(cv_md=_FIXTURE_CV, profile=_FIXTURE_PROFILE)
        assert "$75K-$500K" in md                                    # comp target
        assert "CST" in md                                           # location/tz
        assert "Citizen" in md or "no sponsorship" in md.lower()     # work auth
        assert "I am not a protected veteran" in md                  # EEO verbatim
        assert "Yes, I have a disability (or previously had one)" in md

    def test_scaffold_without_sources(self):
        # A fresh install with no cv.md / profile.yml still gets every heading +
        # the growth protocol, so the agent has a frame to fill in.
        md = self._md().lower()
        for section in _PROFILE_SECTIONS:
            assert section.lower() in md

    def test_partial_profile_does_not_crash(self):
        # A profile.yml missing most keys must still render (defensive .get).
        md = self._md(cv_md=_FIXTURE_CV, profile={"candidate": {"full_name": "Jane Doe"}})
        assert "Jane Doe" in md

    def test_growth_protocol_present(self):
        # The living-document instruction: the agent appends the facts/answers it
        # learns so the next run is smarter (the user's explicit ask).
        md = self._md(cv_md=_FIXTURE_CV, profile=_FIXTURE_PROFILE).lower()
        assert "grow" in md or "update this" in md or "living" in md


class TestProfileFactBankNeverDrops:
    """Regression: the fact bank must carry experience + metrics VERBATIM for any
    résumé layout — order of sections, heading style, all-caps skill labels. A
    structural-divider parser once dropped the whole experience block for these
    (metric loss is the exact failure the fact bank exists to prevent)."""

    def _md(self, cv):
        return handoff.render_profile_md(cv_md=cv, profile=_FIXTURE_PROFILE)

    def test_experience_first_then_trailing_section(self):
        # Experience above Skills, with a section (Certifications) AFTER Skills.
        cv = ("# Jane\n\n## Summary\nDid things.\n\n## Experience\n### Acme\n"
              "- Cut latency 40% and saved $2M\n\n## Skills\nPython · Go\n\n"
              "## Certifications\nAWS Certified\n")
        assert "Cut latency 40% and saved $2M" in self._md(cv)

    def test_skills_last_layout(self):
        cv = ("# Jane\n\n## Experience\n### Acme\n- Shipped X to 5M users\n\n"
              "## Skills\nPython · Go · AWS\n")
        md = self._md(cv)
        assert "Shipped X to 5M users" in md and "AWS" in md

    def test_allcaps_skill_labels_preserved(self):
        # Category labels ('BACKEND') / lone tokens are common in Skills blocks;
        # they must not be mistaken for a section boundary that eats the skills.
        cv = ("# Jane\n\n## Skills\nBACKEND\nPython · Go\nFRONTEND\nReact\n\n"
              "PROFESSIONAL EXPERIENCE\nAcme\n- Shipped X to 5M users\n")
        md = self._md(cv)
        assert "Python · Go" in md and "React" in md       # skills survive
        assert "Shipped X to 5M users" in md               # experience survives


class TestProfileRobustToMalformedYaml:
    """render_profile_md must not crash or emit garbage on a hand-edited /
    alternate-schema profile.yml (the docstring promises graceful degradation)."""

    def test_non_dict_section_does_not_crash(self):
        # target_roles authored as a bare list (not the {primary: [...]} dict).
        p = {"candidate": {"full_name": "Jane Doe"},
             "target_roles": ["AI Engineer", "Backend Engineer"]}
        md = handoff.render_profile_md(cv_md=_FIXTURE_CV, profile=p)
        assert "Jane Doe" in md

    def test_scalar_superpowers_not_split_per_character(self):
        p = {"candidate": {"full_name": "Jane Doe"},
             "narrative": {"superpowers": "Full-stack development"}}
        md = handoff.render_profile_md(cv_md=_FIXTURE_CV, profile=p)
        assert "Full-stack development" in md
        assert "F; u; l; l" not in md                       # not char-joined

    def test_string_requires_sponsorship_not_inverted(self):
        # A quoted "false" (vs the bool false) must NOT flip the legal work-auth
        # answer to "requires sponsorship".
        p = {**_FIXTURE_PROFILE,
             "work_authorization": {"citizenship": "US", "work_permit_type": "Citizen",
                                    "requires_sponsorship": "false"}}
        md = handoff.render_profile_md(cv_md=_FIXTURE_CV, profile=p)
        assert "US Citizen — no sponsorship required" in md


class TestBootstrapSeedsProfile:
    """bootstrap_handoff_dir also seeds the living PROFILE.md next to the README —
    non-clobber (the agent grows it) and sourced from career-ops."""

    def _career_ops(self, tmp_path):
        import yaml
        co = tmp_path / "career-ops"
        (co / "config").mkdir(parents=True)
        (co / "cv.md").write_text(_FIXTURE_CV, encoding="utf-8")
        (co / "config" / "profile.yml").write_text(
            yaml.safe_dump(_FIXTURE_PROFILE), encoding="utf-8")
        return co

    def test_seeds_profile_from_career_ops(self, tmp_path):
        co = self._career_ops(tmp_path)
        out = tmp_path / "agent-home"
        handoff.bootstrap_handoff_dir(out, career_ops=co)
        text = (out / handoff.HANDOFF_PROFILE).read_text(encoding="utf-8")
        assert "Jane Doe" in text
        assert "99.999% uptime" in text                    # metric survived the seed
        assert "I am not a protected veteran" in text      # standing answer

    def test_does_not_clobber_existing_profile(self, tmp_path):
        co = self._career_ops(tmp_path)
        out = tmp_path / "agent-home"
        out.mkdir()
        (out / handoff.HANDOFF_PROFILE).write_text(
            "# my grown profile\nlearned facts", encoding="utf-8")
        handoff.bootstrap_handoff_dir(out, career_ops=co)
        assert (out / handoff.HANDOFF_PROFILE).read_text(encoding="utf-8") == \
            "# my grown profile\nlearned facts"

    def test_seeds_scaffold_when_career_ops_absent(self, tmp_path):
        # No cv.md / profile.yml anywhere → still seed a scaffold PROFILE.md.
        out = tmp_path / "agent-home"
        handoff.bootstrap_handoff_dir(out, career_ops=tmp_path / "nope")
        assert (out / handoff.HANDOFF_PROFILE).exists()


class TestProfileReferencedInPrompts:
    """The kickoff (batch) and per-role prompts must both point the browser agent
    at the living PROFILE.md so it qualifies + tailors against it."""

    def test_kickoff_prompt_names_the_profile(self, tmp_path):
        wo = handoff.work_order_paths(tmp_path, "linkedin")[0]
        assert handoff.HANDOFF_PROFILE in handoff.kickoff_prompt(wo, board="linkedin")

    def test_role_prompt_names_the_profile(self):
        p = handoff.role_prompt("Acme", "SWE", "https://www.linkedin.com/jobs/view/1")
        assert handoff.HANDOFF_PROFILE in p


class TestRunPerSiteSessions:
    """Integration: run() writes one next-roles-<site>.{jsonl,md} per site, a
    single shared role-status.jsonl, and folds agent writeback from the per-site
    files back into the tracker on the next run."""

    def test_run_seeds_readme_even_with_no_queue(self, tmp_path):
        # The dir is bootstrapped before the queue check, so a browser agent gets
        # the instructions even on the very first (empty) run.
        out = tmp_path / "handoff"
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl", out_dir=out,
                         career_ops=tmp_path / "none")
        assert rc == 1                          # no queue
        assert (out / handoff.HANDOFF_README).exists()

    def test_run_survives_unbootstrappable_out_dir(self, tmp_path):
        # A misconfigured out_dir (a file, not a dir) must not crash a no-queue run —
        # the bootstrap is best-effort; run() still returns 1 cleanly.
        bad = tmp_path / "handoff"
        bad.write_text("i am a file, not a dir", encoding="utf-8")
        rc = handoff.run(queue_path=tmp_path / "missing.jsonl", out_dir=bad,
                         career_ops=tmp_path / "none")
        assert rc == 1

    def _queue_file(self, tmp_path, rows):
        p = tmp_path / "evaluated-roles-by-score.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return p

    def _rows(self):
        return [
            {"num": "1", "score": 4.7, "company": "Curri", "role": "Software Engineer",
             "status": "Evaluated", "url": "https://www.linkedin.com/jobs/view/1"},
            {"num": "2", "score": 4.4, "company": "Oddball", "role": "Backend Engineer",
             "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a1"},
            {"num": "3", "score": 4.5, "company": "Glasso", "role": "Platform Engineer",
             "status": "Evaluated", "url": "https://www.glassdoor.com/job-listing/x"},
        ]

    def _companies(self, out, board):
        p = handoff.work_order_paths(out, board)[0]
        return [json.loads(l)["company"]
                for l in p.read_text(encoding="utf-8").strip().splitlines()]

    def test_writes_one_file_per_site_and_shared_tracker(self, tmp_path):
        out = tmp_path / "handoff"
        rc = handoff.run(queue_path=self._queue_file(tmp_path, self._rows()),
                         out_dir=out, career_ops=tmp_path / "no-career-ops")
        assert rc == 0
        assert self._companies(out, "linkedin") == ["Curri"]
        assert self._companies(out, "indeed") == ["Oddball"]
        assert self._companies(out, "glassdoor") == ["Glasso"]
        # Each site also gets its human-readable .md.
        assert handoff.work_order_paths(out, "linkedin")[1].exists()
        # One shared tracker; NO legacy combined next-roles.jsonl is produced.
        assert (out / handoff.DEFAULT_TRACKER_NAME).exists()
        assert not (out / handoff.WORK_ORDER_JSONL).exists()

    def test_empties_stale_site_file(self, tmp_path):
        out = tmp_path / "handoff"
        out.mkdir()
        # A leftover Indeed session from a previous run whose role is no longer
        # in the queue. This run's queue has only a LinkedIn role.
        stale = handoff.work_order_paths(out, "indeed")[0]
        stale.write_text(json.dumps({
            "rank": 1, "company": "GoneCorp", "role": "Old Role",
            "url": "https://www.indeed.com/viewjob?jk=old", "status": ""}) + "\n",
            encoding="utf-8")
        rows = [{"num": "1", "score": 4.7, "company": "Curri", "role": "Software Engineer",
                 "status": "Evaluated", "url": "https://www.linkedin.com/jobs/view/1"}]
        rc = handoff.run(queue_path=self._queue_file(tmp_path, rows),
                         out_dir=out, career_ops=tmp_path / "none")
        assert rc == 0
        assert self._companies(out, "linkedin") == ["Curri"]
        # The stale Indeed session is emptied so the agent won't re-work it.
        assert stale.read_text(encoding="utf-8").strip() == ""

    def test_narrow_board_writes_only_that_site(self, tmp_path):
        out = tmp_path / "handoff"
        out.mkdir()
        # A LinkedIn session the user built separately must survive a narrowed
        # Indeed-only build.
        li = handoff.work_order_paths(out, "linkedin")[0]
        li.write_text(json.dumps({"rank": 1, "company": "Keep", "role": "Me",
                                  "url": "https://www.linkedin.com/jobs/view/9",
                                  "status": ""}) + "\n", encoding="utf-8")
        rows = [{"num": "1", "score": 4.4, "company": "Oddball", "role": "Backend Engineer",
                 "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a1"}]
        rc = handoff.run(queue_path=self._queue_file(tmp_path, rows),
                         out_dir=out, board="indeed", career_ops=tmp_path / "none")
        assert rc == 0
        assert self._companies(out, "indeed") == ["Oddball"]
        assert "Keep" in li.read_text(encoding="utf-8")   # LinkedIn file untouched

    def test_session_summaries_lists_nonempty_sessions(self, tmp_path):
        out = tmp_path / "handoff"
        assert handoff.run(queue_path=self._queue_file(tmp_path, self._rows()),
                           out_dir=out, career_ops=tmp_path / "none") == 0
        summ = {s["board"]: s for s in handoff.session_summaries(out)}
        assert set(summ) == {"linkedin", "indeed", "glassdoor"}
        assert summ["linkedin"]["label"] == "LinkedIn"
        assert summ["linkedin"]["fresh"] == 1
        assert summ["linkedin"]["work_order"].endswith("next-roles-linkedin.jsonl")
        assert "next-roles-linkedin.jsonl" in summ["linkedin"]["kickoff"]
        # Only non-empty per-site sessions — never the legacy combined file (its
        # board tag "both" is absent from the set above), never an emptied site.
        assert all(s["fresh"] > 0 for s in summ.values())

    def test_agent_writeback_from_site_file_folds_into_tracker(self, tmp_path):
        out = tmp_path / "handoff"
        qf = self._queue_file(tmp_path, self._rows())
        assert handoff.run(queue_path=qf, out_dir=out, career_ops=tmp_path / "none") == 0
        # Agent applies to Curri and records it in the LinkedIn session file.
        li = handoff.work_order_paths(out, "linkedin")[0]
        obj = json.loads(li.read_text(encoding="utf-8").strip())
        obj["status"] = "applied"
        li.write_text(json.dumps(obj) + "\n", encoding="utf-8")
        # Next run folds that status into the shared tracker and drops the role.
        assert handoff.run(queue_path=qf, out_dir=out, career_ops=tmp_path / "none") == 0
        assert li.read_text(encoding="utf-8").strip() == ""   # Curri no longer fresh
        tracker = handoff.load_tracker(out / handoff.DEFAULT_TRACKER_NAME)
        curri = [t for t in tracker if t.key == handoff.role_key("Curri", "Software Engineer")]
        assert curri and curri[0].status == "applied"

    def test_narrow_build_empties_legacy_combined_file(self, tmp_path):
        out = tmp_path / "handoff"
        out.mkdir()
        # A pre-upgrade combined next-roles.jsonl on disk, plus a separate
        # LinkedIn session the user built earlier.
        legacy = out / handoff.WORK_ORDER_JSONL
        legacy.write_text(json.dumps({"rank": 1, "company": "OldCo", "role": "Old Role",
                                      "url": "https://www.indeed.com/viewjob?jk=old",
                                      "status": ""}) + "\n", encoding="utf-8")
        li = handoff.work_order_paths(out, "linkedin")[0]
        li.write_text(json.dumps({"rank": 1, "company": "Keep", "role": "Me",
                                  "url": "https://www.linkedin.com/jobs/view/9",
                                  "status": ""}) + "\n", encoding="utf-8")
        rows = [{"num": "1", "score": 4.4, "company": "Oddball", "role": "Backend Engineer",
                 "status": "Evaluated", "url": "https://www.indeed.com/viewjob?jk=a1"}]
        rc = handoff.run(queue_path=self._queue_file(tmp_path, rows),
                         out_dir=out, board="indeed", career_ops=tmp_path / "none")
        assert rc == 0
        assert self._companies(out, "indeed") == ["Oddball"]
        # The legacy combined file is never a valid session — emptied even on a
        # narrowed build (else a pre-upgrade file lingers and gets re-worked)...
        assert legacy.read_text(encoding="utf-8").strip() == ""
        # ...but the OTHER site's session the user built separately survives.
        assert "Keep" in li.read_text(encoding="utf-8")

    def test_empty_board_builds_all_sessions(self, tmp_path):
        # board="" is the same "all sites" sentinel as "both" (via _is_combined),
        # not a narrowed session literally named "".
        out = tmp_path / "handoff"
        rc = handoff.run(queue_path=self._queue_file(tmp_path, self._rows()),
                         out_dir=out, board="", career_ops=tmp_path / "none")
        assert rc == 0
        assert self._companies(out, "linkedin") == ["Curri"]
        assert self._companies(out, "indeed") == ["Oddball"]
        assert self._companies(out, "glassdoor") == ["Glasso"]
        assert not (out / handoff.WORK_ORDER_JSONL).exists()   # no stray "" session file

    def _career_ops_with_row(self, tmp_path, status="Evaluated"):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "data" / "applications.md").write_text(
            "# Applications Tracker\n\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            f"| 3 | 2026-07-01 | Acme | AI Engineer | 4.6/5 | {status} | X | [003](reports/003.md) "
            "| https://www.linkedin.com/jobs/view/3 |\n", encoding="utf-8")
        return co

    def _seed_applied_writeback(self, out):
        out.mkdir(exist_ok=True)
        handoff.work_order_paths(out, "linkedin")[0].write_text(json.dumps({
            "rank": 1, "company": "Acme", "role": "AI Engineer",
            "url": "https://www.linkedin.com/jobs/view/3", "status": "applied"}) + "\n",
            encoding="utf-8")

    def test_run_reflects_applied_writeback_into_applications_md(self, tmp_path):
        # The agent applied to a tracker role (status in a per-site work-order).
        # run() folds it into role-status.jsonl AND marks the tracker Applied +
        # queues a cloud override.
        co = self._career_ops_with_row(tmp_path)
        out = tmp_path / "handoff"
        self._seed_applied_writeback(out)
        assert handoff.run(queue_path=tmp_path / "missing.jsonl", out_dir=out, career_ops=co) == 0
        rows = {r["company"]: r for r in app_data.parse_applications(co / "data" / "applications.md")}
        assert rows["Acme"]["status_canonical"] == "Applied"                    # UI Kanban
        tracker = handoff.load_tracker(out / handoff.DEFAULT_TRACKER_NAME)
        assert any(t.key == handoff.role_key("Acme", "AI Engineer") and t.status == "applied"
                   for t in tracker)                                             # dedup ledger
        assert any(app_data.override_identity(v) == ("Acme", "AI Engineer")
                   for v in app_data.load_status_overrides().values())          # cloud push queue

    def test_run_tracker_sync_is_idempotent(self, tmp_path):
        co = self._career_ops_with_row(tmp_path)
        out = tmp_path / "handoff"
        self._seed_applied_writeback(out)
        # A scored-export queue so the re-run still has a queue after the tracker's
        # only row flips to Applied (the tracker-sourced queue would then be empty).
        q = tmp_path / "q.jsonl"
        q.write_text(json.dumps({"num": "3", "score": 4.6, "company": "Acme", "role": "AI Engineer",
                                 "url": "https://www.linkedin.com/jobs/view/3", "status": "Evaluated"}) + "\n",
                     encoding="utf-8")
        assert handoff.run(queue_path=q, out_dir=out, career_ops=co) == 0
        # Simulate a push clearing the override queue, then re-run: the role is
        # already Applied in role-status.jsonl, so nothing is re-queued.
        app_data.save_status_overrides({})
        assert handoff.run(queue_path=q, out_dir=out, career_ops=co) == 0
        assert app_data.load_status_overrides() == {}


# ── 8. Code-review fixes (2026-07-04, PR #86 high-recall round) ────────────────
class TestActionableIsAllowlist:
    """M2: a denylist of acted-on statuses failed OPEN — it omitted "Responded"
    (a real canonical state), so a role the employer already replied to came
    back as fresh. Only "Evaluated" is actionable (allowlist, matching
    role_select._PENDING_STATUSES)."""

    @pytest.mark.parametrize("status", ["Responded", "Interview", "Offer",
                                        "Discarded", "SKIP", "Applied", "Rejected"])
    def test_non_evaluated_queue_row_excluded(self, status):
        q = handoff.QueueRole(num="1", score=4.5, company="Acme", role="SWE",
                              url="https://www.linkedin.com/jobs/view/1", status=status)
        assert handoff.build_work_order([q], []) == []

    def test_evaluated_row_included(self):
        q = handoff.QueueRole(num="1", score=4.5, company="Acme", role="SWE",
                              url="https://www.linkedin.com/jobs/view/1", status="Evaluated")
        assert len(handoff.build_work_order([q], [])) == 1

    def test_tracker_responded_row_excluded(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "data" / "applications.md").write_text(
            "# Applications Tracker\n\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-07-01 | Acme | SWE | 4.5/5 | Responded | X | [001](reports/001.md) | https://www.linkedin.com/jobs/view/1 |\n"
            "| 2 | 2026-07-01 | Globex | Dev | 4.2/5 | Evaluated | X | [002](reports/002.md) | https://www.linkedin.com/jobs/view/2 |\n",
            encoding="utf-8")
        rows = handoff.load_queue_from_tracker(co)
        assert [r.company for r in rows] == ["Globex"]     # Responded excluded


class TestReportPathThreaded:
    """M1: --handoff-tailor built resumes from JD text only because the tailor
    adapter dropped report_path — the evaluation report's proof-points that
    generate_for_job consumes were never passed. report flows queue -> item ->
    ApplyJob.report_path (report_base defaults to career_ops)."""

    def test_load_queue_reads_report(self, tmp_path):
        p = tmp_path / "q.jsonl"
        p.write_text(json.dumps({"num": "1", "score": 4.8, "company": "Acme",
                                 "role": "AI Engineer", "url": "u", "status": "Evaluated",
                                 "report": "reports/001-acme.md"}) + "\n", encoding="utf-8")
        assert handoff.load_queue(p)[0].report == "reports/001-acme.md"

    def test_report_flows_into_work_order_item(self):
        q = handoff.QueueRole(num="1", score=4.8, company="Acme", role="AI Engineer",
                              url="https://www.linkedin.com/jobs/view/1", status="Evaluated",
                              report="reports/001-acme.md")
        assert handoff.build_work_order([q], [])[0].report == "reports/001-acme.md"

    def test_tailor_passes_report_path_to_generate(self, monkeypatch, tmp_path):
        import pipeline.resume_tailor as rt
        seen = {}
        monkeypatch.setattr(rt, "_resolve_caller", lambda p, m: (lambda s, u: "{}"))
        monkeypatch.setattr(rt, "generate_for_job",
                            lambda co, job, **kw: seen.update(report_path=job.report_path) or None)
        fn = handoff._make_tailor_fn(tmp_path)
        fn(handoff.WorkOrderItem(rank=1, num="1", score=4.5, company="Acme", role="AI Engineer",
                                 board="linkedin", url="u", resume_base="content_adhoc",
                                 report="reports/001-acme.md"))
        assert seen["report_path"] == "reports/001-acme.md"


class TestLimitGuard:
    """L1: fresh[:limit] with no positivity check — --limit 0 emptied the
    work-order, --limit -3 dropped the 3 lowest. Non-positive means "no limit"
    (matching the UI-JS guard)."""

    def _queue(self, n):
        return [handoff.QueueRole(num=str(i), score=float(9 - i), company=f"C{i}",
                                  role="R", url=f"https://www.linkedin.com/jobs/view/{i}",
                                  status="Evaluated") for i in range(n)]

    @pytest.mark.parametrize("limit", [0, -3, None])
    def test_non_positive_limit_means_no_limit(self, limit):
        assert len(handoff.build_work_order(self._queue(3), [], limit=limit)) == 3

    def test_positive_limit_applies(self):
        assert len(handoff.build_work_order(self._queue(3), [], limit=2)) == 2
