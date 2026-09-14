"""Tests for pipeline/backfill_work_location.py — recovering #180's field from
reports written before it existed.

This module parses prose, which #180 rejected for the permanent path, so the
cases that matter are the ones where a wrong read costs the candidate a role.
Every regex here was added because a real corpus defeated the previous one, and
each of those four rounds is pinned below: the bare parse, full state names, the
"the evaluator did not know" guard, and the report template's SPANISH heading.
"""

import pytest

from pipeline import backfill_work_location as bf
from pipeline.work_location import MODE_HYBRID, MODE_ONSITE, MODE_REMOTE, WorkLocation

DFW = ["Dallas, TX"]

HEADER = ("| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
          "|---|------|---------|------|-------|--------|-----|--------|-------|\n")


def _report(policy: str, *, spanish: bool = False, table: bool = False) -> str:
    label = "Politica Remota" if spanish else "Remote policy"
    row = (f"| **{label}** | {policy} |" if table else f"- **{label}:** {policy}")
    return f"# Evaluacion: Acme - Role\n\n## A) Resumen del Rol\n\n{row}\n\nmore prose\n"


class TestReadPolicy:
    """Block A's row, in every shape the real corpus uses."""

    @pytest.mark.parametrize("kw", [
        {}, {"table": True}, {"spanish": True}, {"spanish": True, "table": True},
    ])
    def test_every_layout_and_language(self, kw):
        assert bf.read_policy(_report("On-site (Chicago, IL)", **kw)).startswith("On-site")

    def test_the_spanish_heading_is_not_an_edge_case_we_invented(self):
        """The report template IS Spanish (`# Evaluacion`, `## A) Resumen del
        Rol`) and the model sometimes carries that into this row. Only 9 of 689
        reports in the first real corpus — one of which was a 4.8 in-person role
        in Portland, MAINE sitting in the top ten of the queue."""
        got = bf.location_from_policy(bf.read_policy(
            _report("In-person (Portland, ME)", spanish=True, table=True)))
        assert got.mode == MODE_ONSITE and got.state == "ME"

    def test_no_policy_line_is_empty(self):
        assert bf.read_policy("# Evaluacion\n\nnothing here\n") == ""


class TestLocationFromPolicy:
    @pytest.mark.parametrize("prose,mode,state", [
        ("On-site (Chicago, IL - Out of the Closet)", MODE_ONSITE, "IL"),
        ("Hybrid (1-2 days per week in office in Westminster, CO)", MODE_HYBRID, "CO"),
        ("On-Site (Los Angeles, CA) with up to 75% local travel", MODE_ONSITE, "CA"),
        ("In-person (Portland, ME)", MODE_ONSITE, "ME"),
    ])
    def test_the_common_shape(self, prose, mode, state):
        got = bf.location_from_policy(prose)
        assert got.mode == mode and got.state == state

    def test_remote_needs_no_place(self):
        assert bf.location_from_policy("Remote (Work from home)").mode == MODE_REMOTE
        assert bf.location_from_policy("Remote (Work from home)").metro == ""

    @pytest.mark.parametrize("prose,state", [
        ("Field-based / On-site (Essex County, New Jersey)", "NJ"),
        ("On-site / Community-based (Lebanon, Oregon)", "OR"),
        ("On-site (Burlington, Vermont)", "VT"),
    ])
    def test_a_spelled_out_state_counts(self, prose, state):
        """Recovered six demotions on the first real corpus, two at the top of
        the queue. The live module validates against no state table on purpose;
        that rule is about cells we wrote, and this reads prose nobody wrote to
        a schema."""
        assert bf.location_from_policy(prose).state == state

    def test_west_virginia_is_not_virginia(self):
        assert bf.location_from_policy("On-site (Charleston, West Virginia)").state == "WV"

    @pytest.mark.parametrize("prose", [
        "Unspecified in truncated snippet (University of Miami typically on-site)",
        "Not explicitly stated (likely hybrid/remote based on Ohio policy)",
        "On-site presence unclear; presumably Illinois",
    ])
    def test_the_evaluator_saying_it_does_not_know_is_not_a_location(self, prose):
        """A line whose whole content is "there is no location here" would
        otherwise be turned into a confident mark by the state-name match — one
        real report reads exactly the first of these, and it would have become
        "On-site FL"."""
        assert bf.location_from_policy(prose) == WorkLocation()

    def test_a_mode_with_no_place_is_left_alone(self):
        """61 of 210 rows on the first corpus, almost all of them local
        ("On-site (Dallas office)"), where no mark IS the right answer."""
        for prose in ("Hybrid (3 days in office, 2 days remote)", "On-site (Dallas office)",
                      "Hybrid (US-based)"):
            assert bf.location_from_policy(prose) == WorkLocation(), prose

    def test_the_mode_word_is_not_repeated_into_the_place(self):
        """"On-site / Hybrid in Brooklyn, NY" captured "Hybrid in Brooklyn, NY",
        which rendered as "Hybrid Hybrid in Brooklyn, NY"."""
        got = bf.location_from_policy("On-site / Hybrid in Brooklyn, NY")
        assert got.metro == "Brooklyn, NY"

    def test_unreadable_prose_is_empty(self):
        for prose in ("", "   ", "See the JD", "TBD"):
            assert bf.location_from_policy(prose) == WorkLocation()


class TestPlan:
    def _world(self, tmp_path, rows, reports):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "reports").mkdir(parents=True)
        (co / "data" / "applications.md").write_text(HEADER + "".join(rows), encoding="utf-8")
        for name, text in reports.items():
            (co / "reports" / name).write_text(text, encoding="utf-8")
        return co

    def _row(self, num, company, status, notes, rep="001"):
        link = f"[{rep}](reports/{rep}-{company.lower()}-2026-09-14.md)" if rep else ""
        return (f"| {num} | 2026-09-14 | {company} | Role | 4.5/5 | {status} |  | "
                f"{link} | {notes} |\n")

    def test_it_plans_only_open_unmarked_rows(self, tmp_path):
        co = self._world(tmp_path, [
            self._row(1, "Acme", "Evaluated", "https://x.example/1 — APPLY"),
            self._row(2, "Globex", "Applied", "https://x.example/2 — APPLY", rep="002"),
            self._row(3, "Initech", "Evaluated",
                      "https://x.example/3 — Work location: Remote — APPLY", rep="003"),
        ], {
            "001-acme-2026-09-14.md": _report("On-site (Chicago, IL)"),
            "002-globex-2026-09-14.md": _report("On-site (Chicago, IL)"),
            "003-initech-2026-09-14.md": _report("On-site (Chicago, IL)"),
        })
        planned, tally = bf.plan(co, DFW)
        assert [p["company"] for p in planned] == ["Acme"]
        assert tally["open"] == 2 and tally["marked"] == 1
        assert sum(1 for x in planned if x["verdict"]) == 1

    def test_a_row_whose_report_is_missing_is_counted_not_guessed(self, tmp_path):
        co = self._world(tmp_path, [
            self._row(1, "Acme", "Evaluated", "https://x.example/1 — APPLY")], {})
        planned, tally = bf.plan(co, DFW)
        assert planned == [] and tally["no_report"] == 1


class TestApply:
    def test_it_writes_the_mark_and_leaves_every_other_cell_alone(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        t = co / "data" / "applications.md"
        before = HEADER + ("| 1 | 2026-09-14 | Acme | Role | 4.5/5 | Evaluated |  | "
                           "[001](reports/001-acme-2026-09-14.md) | https://x.example/1 — APPLY |\n")
        t.write_text(before, encoding="utf-8")
        n = bf.apply_marks(co, [{"num": "1", "mark": "Work location: On-site Chicago, IL"}])
        after = t.read_text(encoding="utf-8")
        assert n == 1
        # Appended, because that is what the shared writer does — the reader
        # takes the LAST mark (newest wins) and extract_url the first URL, so
        # neither cares, and one writer for the Notes grammar beats two.
        assert "https://x.example/1 — APPLY — Work location: On-site Chicago, IL" in after
        assert len(after.splitlines()) == len(before.splitlines())
        cells_before = before.splitlines()[-1].split("|")
        cells_after = after.splitlines()[-1].split("|")
        assert cells_before[:-2] == cells_after[:-2]

    def test_it_is_idempotent(self, tmp_path):
        """The tool is run repeatedly as its parser improves — a second pass must
        add marks for newly-readable rows without touching the rest."""
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        t = co / "data" / "applications.md"
        t.write_text(HEADER + ("| 1 | 2026-09-14 | Acme | Role | 4.5/5 | Evaluated |  | "
                               "[001](x.md) | https://x.example/1 — APPLY |\n"), encoding="utf-8")
        row = [{"num": "1", "mark": "Work location: On-site Chicago, IL"}]
        assert bf.apply_marks(co, row) == 1
        once = t.read_text(encoding="utf-8")
        assert bf.apply_marks(co, row) == 0
        assert t.read_text(encoding="utf-8") == once

    def test_an_empty_notes_cell_gets_a_bare_mark(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        t = co / "data" / "applications.md"
        t.write_text(HEADER + "| 1 | 2026-09-14 | Acme | Role | 4.5/5 | Evaluated |  | [001](x.md) |  |\n",
                     encoding="utf-8")
        bf.apply_marks(co, [{"num": "1", "mark": "Work location: Remote"}])
        assert "| Work location: Remote |" in t.read_text(encoding="utf-8")


class TestMainRefusesToMarkBlind:
    def test_no_commutable_metros_aborts(self, tmp_path, capsys, monkeypatch):
        """Everything would read as reachable, so the run would write 100+ marks
        whose verdict was never actually computed."""
        cfg = tmp_path / "search.yml"
        cfg.write_text("searches:\n  - location: United States\n    is_remote: true\n",
                       encoding="utf-8")
        rc = bf.main(["--career-ops", str(tmp_path), "--config", str(cfg)])
        assert rc == 1 and "Aborting" in capsys.readouterr().out


class TestModeFromProse:
    """The live module reads a three-token cell the model was ASKED for; this
    reads a sentence it wrote freely, where both signals routinely appear."""

    def test_hybrid_wins_outright(self):
        """A hybrid role requires presence whatever else the sentence says.
        "eligible to telework 2 days per week" is a real, correct demotion that
        a "mentions remote, so skip" rule would throw away."""
        assert bf.mode_from_prose(
            "Hybrid (Tumwater, WA duty station, eligible to telework 2 days per week)"
        ) == MODE_HYBRID

    @pytest.mark.parametrize("prose,mode", [
        ("Fully Remote (< 10% on-site)", MODE_REMOTE),
        ("Remote: Not on-site or < 10% of hours worked on-site", MODE_REMOTE),
        ("On-site (Dallas, TX) with some remote flexibility", MODE_ONSITE),
        ("On-site (Chicago, IL - work from home one day)", MODE_ONSITE),
    ])
    def test_otherwise_the_earlier_signal_wins(self, prose, mode):
        assert bf.mode_from_prose(prose) == mode

    def test_the_order_rule_prevents_a_wrong_demotion(self):
        """The failure it exists for: a remote role whose sentence happens to
        name an office would otherwise be marked on-site THERE and buried."""
        wl = bf.location_from_policy("Fully Remote (< 10% on-site at the Chicago, IL office)")
        assert wl.mode == MODE_REMOTE
        from pipeline.work_location import area_verdict
        assert area_verdict(wl, DFW) == ""

    def test_neither_signal_is_unreadable(self):
        assert bf.mode_from_prose("See the job description") == ""


class TestItRefusesRatherThanGuesses:
    """Every rule here is a refusal, and each was a wrong mark first. A missed
    mark leaves a row as it is today; a wrong one buries a role the candidate
    could have taken, on evidence they never see."""

    @pytest.mark.parametrize("prose,would_have_been", [
        ("On-site (George Washington University Hospital)", "WA"),
        ("In-person (Virginia Mason Medical Center)", "VA"),
        ("On-site (Indiana University Health, Dallas clinic)", "IN"),
        ("On-site (Delaware North, Arlington TX venue)", "DE"),
    ])
    def test_an_employer_name_is_not_a_work_state(self, prose, would_have_been):
        """This corpus is health, education and outreach employers whose NAMES
        are state names. Matching a bare state name anywhere in the sentence
        produced a bare-state mark — the guaranteed-demotion shape, since
        out_of_area compares the state and is_local_location cannot rescue an
        empty metro — so a Dallas role was buried on its employer's name."""
        assert bf.location_from_policy(prose) == WorkLocation(), would_have_been

    @pytest.mark.parametrize("prose", [
        "Not on-site; fully remote (HQ Chicago, IL)",
        "No on-site requirement - 100% remote, company based in Chicago, IL",
        "Remote (this is not a hybrid role; HQ Chicago, IL)",
    ])
    def test_a_negated_mode_is_declined(self, prose):
        """These put the ON-SITE word first, so earlier-signal-wins read them as
        on-site and demoted a remote role to its headquarters' metro — the very
        failure that rule was added to prevent, through the other door."""
        assert bf.location_from_policy(prose) == WorkLocation()

    @pytest.mark.parametrize("prose", [
        "On-site parking provided; position is fully remote (Boston, MA HQ)",
        "Remote role; on-site gym available at the Dallas, TX HQ",
    ])
    def test_an_on_site_AMENITY_is_not_a_work_mode(self, prose):
        """Postings advertise on-site parking, an on-site gym, an on-site clinic.
        Nothing negates and nothing hedges, so neither of those guards sees it —
        the on-site token simply comes FIRST and won on position, reading a
        remote role as on-site at the company's headquarters. Here the role is
        correctly read as remote rather than merely declined."""
        assert bf.location_from_policy(prose).mode == MODE_REMOTE

    def test_a_genuine_on_site_role_keeps_its_amenities(self):
        got = bf.location_from_policy("On-site (Chicago, IL) with on-site parking")
        assert got.mode == MODE_ONSITE and got.state == "IL"

    def test_two_states_in_one_sentence_is_ambiguity(self):
        """"HQ in Chicago, IL; role based in Dallas, TX" — taking the first match
        demoted a Dallas role to Chicago."""
        assert bf.location_from_policy(
            "On-site (HQ in Chicago, IL; role based in Dallas, TX)") == WorkLocation()

    @pytest.mark.parametrize("prose", [
        "Hybrid (Tuesdays, IT department on-site)",
        "On-site (Mon-Fri, AM and PM shifts at the Dallas clinic)",
    ])
    def test_two_uppercase_letters_after_a_comma_is_not_a_state(self, prose):
        assert bf.location_from_policy(prose) == WorkLocation()

    def test_a_real_two_state_free_sentence_still_reads(self):
        """The refusals must not swallow the cases the module exists for."""
        got = bf.location_from_policy("On-site (Chicago, IL - Out of the Closet Thrift Store)")
        assert got.mode == MODE_ONSITE and got.state == "IL"


class TestStructuredFieldWins:
    def test_a_report_that_states_work_location_is_not_prose_parsed(self):
        """plan() selects on an unmarked tracker ROW, which is not the same set
        as a pre-#180 report — a `--batch` row merged before the mark shipped has
        the field already. _report_location_mark is "the one place a report is
        turned into that mark"; this must not become a third shape."""
        report = ("# Evaluacion\n\n## Machine Summary\n\n```yaml\n"
                  'final_decision: "Apply"\nwork_location:\n  mode: "hybrid"\n'
                  '  metro: "Austin, TX"\n  state: "TX"\n```\n\n'
                  "- **Remote policy:** On-site (Chicago, IL)\n")
        # The structured field says Austin; the prose says Chicago. Structured wins.
        assert bf.mark_for_report(report) == "Work location: Hybrid Austin, TX"

    def test_prose_is_the_fallback(self):
        report = _report("On-site (Chicago, IL)")
        assert bf.mark_for_report(report) == "Work location: On-site Chicago, IL"


class TestCandidateAside:
    """The evaluator annotates the job's location line with the CANDIDATE's own
    location — "On-site / Tucson, AZ (Note: Candidate is in Dallas, TX)". That
    second state is the person's, not the job's, and reading it made nine
    correct demotions look like two-state ambiguity and decline."""

    @pytest.mark.parametrize("prose,state", [
        ("On-site / Tucson, AZ (Note: Candidate is in Dallas, TX; relocation needed)", "AZ"),
        ("On-site (Springfield, MA - note: candidate is in Dallas, TX; requires relocation)", "MA"),
        ("On-site (Chicago, IL - Out of the Closet) *Note: Candidate is based in Dallas, TX", "IL"),
    ])
    def test_the_job_state_survives_the_aside(self, prose, state):
        assert bf.location_from_policy(prose).state == state

    def test_the_aside_inside_the_same_parenthetical_is_removed_narrowly(self):
        """Removing the whole group would take the job's own metro with it, which
        is why the narrow clause rule runs before the broad one."""
        assert bf.role_prose(
            "On-site (Springfield, MA - note: candidate is in Dallas, TX)"
        ).startswith("On-site (Springfield, MA")

    def test_a_genuine_second_job_state_is_still_ambiguity(self):
        """The aside rule must not become a way to ignore a real second place."""
        assert bf.location_from_policy(
            "On-site (HQ in Chicago, IL; role based in Dallas, TX)") == WorkLocation()


class TestAStateCodeIsNotEveryTwoLetters:
    """Health and education postings are full of two-letter tokens that ARE
    state codes: PA-C, and bare ID / IN / ME / AM after a comma."""

    @pytest.mark.parametrize("prose", [
        "On-site (Mon-Fri, PA-C and NP staff on site at the Dallas clinic)",
        "On-site (Mon-Fri, ID badge required at the Dallas office)",
    ])
    def test_a_credential_or_schedule_is_not_a_place(self, prose):
        assert bf.location_from_policy(prose) == WorkLocation()

    def test_an_unambiguous_place_beside_a_schedule_still_reads(self):
        """The refusal must not swallow the sentence it shares a comma with —
        taking merely the FIRST match declined this one."""
        got = bf.location_from_policy("On-site (Mon-Fri, AM shifts; Dallas, TX office)")
        assert got.metro == "Dallas, TX" and got.state == "TX"


class TestThePlaceIsNotTheWholeClause:
    def test_a_greedy_capture_cannot_drag_in_another_city(self):
        """"reports to the Dallas hub but based in Chicago, IL" put "Dallas" in
        the metro, where is_local_location falsely rescued it — so the Chicago
        role was NOT demoted, and the garbage went into Notes permanently."""
        got = bf.location_from_policy(
            "On-site - reports to the Dallas hub but based in Chicago, IL")
        assert got.metro == "Chicago, IL"
        from pipeline.work_location import area_verdict
        assert area_verdict(got, DFW) == "On-site Chicago, IL"

    @pytest.mark.parametrize("prose,metro", [
        ("On-site (Fort Worth, TX)", "Fort Worth, TX"),
        ("On-site / Field-based (visits to homes in Lake County, IL)", "Lake County, IL"),
        ("Field-based / On-site (Essex County, New Jersey)", "Essex County, NJ"),
    ])
    def test_multi_word_places_survive(self, prose, metro):
        assert bf.location_from_policy(prose).metro == metro


class TestAnOptionalHybridIsNotACommute:
    @pytest.mark.parametrize("prose", [
        "Remote (optional hybrid access to the Chicago, IL office)",
        "Remote-first; hybrid available for those near Boston, MA",
    ])
    def test_hybrid_offered_inside_a_remote_role(self, prose):
        """Hybrid wins outright everywhere else, which marked these a commute at
        an office the candidate never has to visit."""
        assert bf.location_from_policy(prose).mode != MODE_HYBRID

    def test_a_required_hybrid_still_wins(self):
        assert bf.location_from_policy(
            "Hybrid (Tumwater, WA duty station, eligible to telework 2 days per week)"
        ).mode == MODE_HYBRID


class TestApplyKeepsTheFileWellFormed:
    def test_the_trailing_newline_survives(self, tmp_path):
        """`read_text` strips, so a naive write drops it and every later diff of
        the tracker shows a spurious last-line change."""
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        t = co / "data" / "applications.md"
        t.write_text(HEADER + "| 1 | 2026-09-14 | Acme | Role | 4.5/5 | Evaluated |  | [001](x.md) | n |\n",
                     encoding="utf-8")
        bf.apply_marks(co, [{"num": "1", "mark": "Work location: Remote"}])
        assert t.read_text(encoding="utf-8").endswith("\n")


class TestItDoesNotTellYouToPressPush:
    def test_the_printed_advice_names_the_mechanism_that_works(self, tmp_path, capsys):
        """The UI's Push carries pending STATUS overrides, which this writes
        none of — it answers 400, and the next Refresh (cloud rows verbatim)
        would erase every mark. Getting this wrong loses the whole run."""
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "data" / "applications.md").write_text(HEADER, encoding="utf-8")
        cfg = tmp_path / "search.yml"
        cfg.write_text("searches:\n  - location: Dallas, TX\n", encoding="utf-8")
        bf.main(["--career-ops", str(co), "--config", str(cfg), "--apply"])
        out = capsys.readouterr().out
        assert "NOT pushed by the UI's Push button" in out
        assert "edit-tracker.yml" in out and "applications_md_release_tag" in out
