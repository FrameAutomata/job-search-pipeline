"""Tests for pipeline/work_location.py — can the candidate be where the role is?

The cases that matter here are the ones where a cheaper rule would look right
and be wrong in the expensive direction. This gate REORDERS a queue, so a false
positive buries a role the person could have taken — which is #180's own harm
turned around — and every "unreadable" case must therefore answer False rather
than guess. The asymmetry with `remote_signal.is_local_location` (which errs
strict because it DELETES rows) is the thing most likely to be "simplified"
away by someone reading the two modules side by side, so it is pinned twice:
Fort Worth under a Dallas pass is kept here and dropped there.
"""

import pytest
import yaml

from pipeline import remote_signal
from pipeline.work_location import (
    MODE_HYBRID,
    MODE_ONSITE,
    MODE_REMOTE,
    WorkLocation,
    area_label,
    area_verdict,
    commutable_area,
    commutable_states,
    location_mark,
    out_of_area,
    parse_work_location,
    work_location_from_notes,
)

DFW = ["Dallas, TX"]


def wl(mode="", metro="", state=""):
    return parse_work_location({"work_location": {"mode": mode, "metro": metro, "state": state}})


# ── Reading the model's field ────────────────────────────────────────────────

class TestParse:
    def test_the_documented_shape(self):
        got = wl("onsite", "Chicago, IL", "IL")
        assert got == WorkLocation(mode=MODE_ONSITE, metro="Chicago, IL", state="IL")
        assert got.requires_presence

    @pytest.mark.parametrize("raw,expected", [
        ("onsite", MODE_ONSITE), ("On-Site", MODE_ONSITE), ("on site", MODE_ONSITE),
        ("In-Office", MODE_ONSITE), ("hybrid", MODE_HYBRID), ("Hybrid (2 days)", MODE_HYBRID),
        ("remote", MODE_REMOTE), ("Fully Remote", MODE_REMOTE), ("work from home", MODE_REMOTE),
    ])
    def test_mode_spellings_a_model_actually_writes(self, raw, expected):
        assert wl(raw).mode == expected

    def test_hybrid_wins_a_string_naming_both(self):
        """"Remote/Hybrid" is a commute. Reading it as remote is the whole miss."""
        assert wl("Remote/Hybrid").mode == MODE_HYBRID
        assert wl("Hybrid - 3 days remote").mode == MODE_HYBRID

    def test_a_bare_string_is_the_mode(self):
        assert parse_work_location({"work_location": "remote"}).mode == MODE_REMOTE

    @pytest.mark.parametrize("summary", [
        {}, {"work_location": None}, {"work_location": []}, {"work_location": 7},
        "not a dict", None,
    ])
    def test_anything_unreadable_is_empty_not_an_error(self, summary):
        got = parse_work_location(summary)
        assert got == WorkLocation() and not got.requires_presence

    def test_a_report_written_before_the_field_existed_reads_as_unknown(self):
        """Not as remote: every legacy row would otherwise be silently promoted."""
        assert parse_work_location({"final_decision": "Apply", "hard_stops": []}) == WorkLocation()

    @pytest.mark.parametrize("raw", ["Illinois", "il-ish", "", "  ", "USA"])
    def test_a_state_cell_that_is_not_a_two_letter_code_is_dropped(self, raw):
        assert wl("onsite", "Somewhere", raw).state == ""

    def test_state_is_uppercased(self):
        assert wl("onsite", "Chicago, IL", "il").state == "IL"


# ── The predicate ────────────────────────────────────────────────────────────

class TestOutOfArea:
    def test_another_state_is_out_of_area(self):
        assert out_of_area(wl("onsite", "Chicago, IL", "IL"), DFW)
        assert out_of_area(wl("hybrid", "Westminster, CO", "CO"), DFW)

    def test_remote_is_never_out_of_area(self):
        """Both real copies are "DFW plus fully remote US"; nothing configures that."""
        assert not out_of_area(wl("remote", "", ""), DFW)
        assert not out_of_area(wl("remote", "Chicago, IL", "IL"), DFW)

    def test_hybrid_in_metro_stays_fine(self):
        """The rule is about the METRO, never the number of office days."""
        assert not out_of_area(wl("hybrid", "Dallas, TX", "TX"), DFW)

    def test_a_nearby_metro_in_the_same_state_is_kept(self):
        """The asymmetry with remote_signal, stated as a test. That guard DROPS a
        row, so "Plano, TX" being off-site to a "Dallas, TX" pass is a cheap
        mistake; this one BURIES a role, so the same mistake is the bug #180 is
        about. Sharing the strict rule would bury every DFW suburb."""
        for metro in ("Fort Worth, TX", "Plano, TX", "Irving, TX"):
            assert not out_of_area(wl("onsite", metro, "TX"), DFW), metro
        # ...and the strict guard really would have refused them, so this is a
        # difference between the two modules and not a restatement.
        assert not remote_signal.is_local_location("Fort Worth, TX", DFW)

    def test_a_metro_naming_the_pass_city_is_kept_whatever_the_state_cell_says(self):
        """The city test rescues the state test — a "Dallas-Fort Worth Area"
        metro is local however the model filled `state`."""
        assert not out_of_area(wl("onsite", "Dallas-Fort Worth Area", "IL"), DFW)

    @pytest.mark.parametrize("case", [
        ("", "Chicago, IL", "IL"),         # no mode stated
        ("onsite", "Chicagoland", ""),     # a metro no state can be read from
        ("onsite", "", ""),                # nothing at all
    ])
    def test_an_unreadable_role_is_left_where_it_was(self, case):
        assert not out_of_area(wl(*case), DFW)

    def test_an_empty_state_cell_falls_back_to_the_metro(self):
        """The prompt tells the model to leave `state` blank when the posting
        does not say, so a report with `metro: "Chicago, IL"` and no state is
        the likeliest way an honest evaluation slips the gate — #180's own
        failure surviving inside the fix for it."""
        assert out_of_area(wl("onsite", "Chicago, IL", ""), DFW)
        # ...and the fallback is symmetric, so it cannot invent a demotion.
        assert not out_of_area(wl("onsite", "Fort Worth, TX", ""), DFW)

    @pytest.mark.parametrize("passes", [(), [], ["Texas"], ["Remote"], [""]])
    def test_a_candidate_with_no_city_st_pass_demotes_nothing(self, passes):
        """A state-level or unparseable pass switches the state half OFF rather
        than guessing — better to miss a demotion than to invent one."""
        assert not out_of_area(wl("onsite", "Chicago, IL", "IL"), passes)

    def test_two_commutable_metros_both_count(self):
        assert not out_of_area(wl("onsite", "Denver, CO", "CO"), ["Dallas, TX", "Denver, CO"])
        assert out_of_area(wl("onsite", "Chicago, IL", "IL"), ["Dallas, TX", "Denver, CO"])


class TestCommutableStates:
    def test_reads_the_trailing_code(self):
        assert commutable_states(["Dallas, TX", "Denver, CO"]) == {"TX", "CO"}

    @pytest.mark.parametrize("loc", ["Texas", "Remote", "", None, "United States"])
    def test_anything_without_a_city_st_shape_contributes_nothing(self, loc):
        assert commutable_states([loc]) == set()


class TestCommutableArea:
    """Takes the config PATH, which is the whole composition both readers need."""

    def _cfg(self, tmp_path, doc):
        p = tmp_path / "search.yml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        return p

    def test_it_is_the_non_remote_passes(self, tmp_path):
        """The candidate's own search config is the commutable set — a place they
        have already said they will go to work — rather than a second answer
        parsed out of PROFILE.md prose. The remote pass is excluded by
        remote_signal, which is what makes "open to remote US" need no config."""
        p = self._cfg(tmp_path, {"searches": [
            {"name": "Dallas, TX", "location": "Dallas, TX"},
            {"name": "remote US", "location": "United States", "is_remote": True},
        ]})
        assert commutable_area(p) == ["Dallas, TX"]

    def test_a_quoted_remote_flag_is_still_a_remote_pass(self, tmp_path):
        """normalize_pass' reading, inherited — a truthy "false" string here
        would put "United States" in the commutable set and demote nothing ever."""
        p = self._cfg(tmp_path, {"searches": [{"location": "United States", "is_remote": "true"}]})
        assert commutable_area(p) == []

    def test_an_unreadable_config_demotes_nothing(self, tmp_path):
        """The feature's likeliest silent failure: a missing or malformed config
        makes the whole gate a no-op, so it must degrade rather than raise (and
        handoff logs the empty case for exactly this reason)."""
        assert commutable_area(tmp_path / "nope.yml") == []
        bad = tmp_path / "bad.yml"
        bad.write_text("searches: [oh: no: wait\n", encoding="utf-8")
        assert commutable_area(bad) == []


class TestLabel:
    @pytest.mark.parametrize("mode,metro,state,expected", [
        ("onsite", "Chicago, IL", "IL", "On-site Chicago, IL"),
        ("hybrid", "Westminster, CO", "CO", "Hybrid Westminster, CO"),
        ("onsite", "", "IL", "On-site IL"),
        ("onsite", "", "", "On-site"),
    ])
    def test_it_names_the_mode_and_the_place(self, mode, metro, state, expected):
        assert area_label(wl(mode, metro, state)) == expected


# ── The tracker mark (#180's storage half) ───────────────────────────────────

class TestLocationMark:
    """The evaluation reads the posting once and records WHERE the role is on the
    row; every surface that ranks roles reads that back and applies its own
    commutable area. Storing the FACT rather than the verdict is what lets a
    candidate widen their search and have old rows re-judged, instead of frozen
    verdicts nothing refreshes."""

    @pytest.mark.parametrize("mode,metro,state,mark", [
        ("onsite", "Chicago, IL", "IL", "Work location: On-site Chicago, IL"),
        ("hybrid", "Westminster, CO", "CO", "Work location: Hybrid Westminster, CO"),
        ("remote", "", "", "Work location: Remote"),
        ("onsite", "", "IL", "Work location: On-site IL"),
    ])
    def test_it_round_trips(self, mode, metro, state, mark):
        wl = parse_work_location({"work_location": {"mode": mode, "metro": metro, "state": state}})
        assert location_mark(wl) == mark
        assert work_location_from_notes(mark) == wl

    def test_it_round_trips_inside_the_notes_grammar(self):
        """The cell is `req <id> — <url> — <mark> — <the model's sentence>`, and
        the mark has to survive its neighbours on both sides."""
        wl = parse_work_location(
            {"work_location": {"mode": "onsite", "metro": "Chicago, IL", "state": "IL"}})
        notes = (f"req R_1488728 — https://www.linkedin.com/jobs/view/47 — "
                 f"{location_mark(wl)} — APPLY: strong outreach match")
        assert work_location_from_notes(notes) == wl

    def test_an_unknown_location_writes_no_mark(self):
        """A row with no mark has never been judged, which is different from a
        row judged remote — the whole existing backlog depends on that."""
        assert location_mark(WorkLocation()) == ""
        assert location_mark(parse_work_location({})) == ""

    @pytest.mark.parametrize("notes", [
        "", None, "APPLY: strong match", "https://x.example/1 — CONSIDER",
        "Work location:", "Work location: somewhere vague",
    ])
    def test_no_readable_mark_is_an_empty_location(self, notes):
        assert work_location_from_notes(notes) == WorkLocation()

    def test_a_marked_row_is_judged_against_the_current_area(self, tmp_path):
        """The point of storing the fact: the same row answers differently when
        the candidate's own passes change, with nothing re-evaluated."""
        notes = "https://x.example/1 — Work location: On-site Chicago, IL — APPLY"
        wl = work_location_from_notes(notes)
        assert area_verdict(wl, ["Dallas, TX"]) == "On-site Chicago, IL"
        assert area_verdict(wl, ["Dallas, TX", "Chicago, IL"]) == ""


class TestMarkSafety:
    """The mark carries model-authored text into a markdown cell and into the
    Notes grammar, so both halves of "it survives the round trip" have to hold
    for input nobody vetted."""

    def test_a_metro_without_a_state_still_carries_one(self):
        """`metro: "Chicago"` + `state: "IL"` — writing the metro alone loses the
        one field the predicate tests, so the role would round-trip to
        undemotable. Caught only end-to-end: both halves look right alone."""
        wl = parse_work_location(
            {"work_location": {"mode": "onsite", "metro": "Chicago", "state": "IL"}})
        mark = location_mark(wl)
        assert mark == "Work location: On-site Chicago, IL"
        assert work_location_from_notes(mark).state == "IL"
        assert out_of_area(work_location_from_notes(mark), DFW)

    def test_a_metro_that_already_names_its_state_is_not_doubled(self):
        wl = parse_work_location(
            {"work_location": {"mode": "onsite", "metro": "Chicago, IL", "state": "IL"}})
        assert location_mark(wl) == "Work location: On-site Chicago, IL"

    @pytest.mark.parametrize("metro,banned", [
        ("Chicago | IL", "|"),      # would split the markdown row
        ("Chicago — IL", "—"),      # the Notes grammar's own separator
        ("Chicago\nIL", "\n"),
    ])
    def test_cell_breaking_characters_are_stripped(self, metro, banned):
        wl = parse_work_location(
            {"work_location": {"mode": "onsite", "metro": metro, "state": "IL"}})
        mark = location_mark(wl)
        assert banned not in mark
        # ...and it still round-trips, which is what the em-dash case would break.
        assert work_location_from_notes(f"https://x.example/1 — {mark} — APPLY").state == "IL"

    def test_a_runaway_metro_is_capped(self):
        wl = parse_work_location(
            {"work_location": {"mode": "onsite", "metro": "x" * 400, "state": "IL"}})
        assert len(location_mark(wl)) < 120


class TestNewestMarkWins:
    """merge-tracker's `mergeNotes` keeps the existing cell verbatim and FIRST,
    appending `Re-eval <date> (a→b): <the addition's whole cell>` after it — so a
    re-evaluated row carries every mark it has ever had, oldest first. Reading
    the first one returns the location of the posting this row USED TO BE, in
    both directions, and both are #180 itself. `extract_url` was rewritten under
    #163 for exactly this shape; this is the same rule one cell over."""

    def _merged(self, *marks):
        """The real shape `mergeNotes` produces: `prev. Re-eval …: incoming`."""
        first, *rest = marks
        cell = f"req R_1 — https://x.example/1 — Work location: {first} — APPLY first"
        for i, m in enumerate(rest, start=2):
            cell += (f". Re-eval 2026-09-1{i} (4.1→4.8): https://x.example/{i} — "
                     f"Work location: {m} — APPLY re-eval")
        return cell

    def test_a_role_that_moved_to_another_metro_is_demoted(self):
        """The one that rides the top of the queue if the first mark wins."""
        notes = self._merged("Remote", "On-site Chicago, IL")
        assert area_verdict(work_location_from_notes(notes), DFW) == "On-site Chicago, IL"

    def test_a_role_that_went_remote_is_released(self):
        """The mirror image: it stays buried where nobody scrolls."""
        notes = self._merged("On-site Chicago, IL", "Remote")
        assert area_verdict(work_location_from_notes(notes), DFW) == ""

    def test_the_newest_of_several_wins(self):
        notes = self._merged("On-site Chicago, IL", "Remote", "Hybrid Tucson, AZ")
        assert area_verdict(work_location_from_notes(notes), DFW) == "Hybrid Tucson, AZ"

    def test_an_unreadable_newest_mark_does_not_fall_back_to_an_older_one(self):
        """The older mark describes a posting this row is no longer — unknown is
        the honest answer, and it demotes nothing."""
        notes = self._merged("On-site Chicago, IL", "somewhere vague")
        assert work_location_from_notes(notes) == WorkLocation()


class TestConfidenceLimits:
    """Everything this module declines to judge, and why. The asymmetry it is
    built on — a missed demotion is today's behaviour, a wrong one buries a role
    the candidate could have taken — only holds if the declines are real."""

    @pytest.mark.parametrize("mode", [
        "in-person", "In Person", "office-based", "on location", "on-premises",
    ])
    def test_the_on_site_spellings_a_model_actually_writes(self, mode):
        """A mode we cannot read is a role we cannot judge, so an unlisted
        spelling is not a conservative failure — it is no gate at all."""
        assert parse_work_location({"work_location": {"mode": mode}}).mode == MODE_ONSITE

    def test_one_unreadable_pass_turns_the_state_test_off_for_the_whole_config(self):
        """A non-remote pass this cannot read a state from is BROADER than the
        test — "United States" says the candidate will go anywhere — so
        comparing a Chicago role against the {TX} its sibling contributes would
        demote a role that pass covers. Same answer the module already gives
        when no pass is readable."""
        wl = wl_onsite = parse_work_location(
            {"work_location": {"mode": "onsite", "metro": "Chicago, IL", "state": "IL"}})
        assert out_of_area(wl_onsite, ["Dallas, TX"])                       # confident
        assert not out_of_area(wl, ["Dallas, TX", "United States"])         # one is broader
        assert not out_of_area(wl, ["Dallas, TX", "Texas"])

    def test_the_model_s_own_sentence_cannot_impersonate_the_mark(self):
        """The label is ordinary English and the model writes the Notes verdict.
        An unanchored match meant a sentence like "...flexible work location:
        negotiable" satisfied the idempotence guard — so the real mark was never
        written — and then won the newest-mark read, since it sits after it."""
        notes = ("https://x.example/1 — Work location: On-site Chicago, IL — "
                 "APPLY: flexible work location: negotiable")
        assert work_location_from_notes(notes).metro == "Chicago, IL"

    def test_a_prose_only_collision_is_not_a_mark(self):
        assert work_location_from_notes(
            "https://x.example/1 — APPLY: their work location: policy is unclear"
        ) == WorkLocation()
