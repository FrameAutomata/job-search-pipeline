"""Tests for pipeline/remote_signal.py — the remote-consistency guard's leaf.

Three things are pinned here rather than in the stage tests: the vocabulary
the regex accepts (and the one word it deliberately does not), the one reading
of the two CSV flags every stage shares, and the shape rule search_passes
mirrors from onboard.search_entries — a second copy of that rule is exactly
what the agreement test exists to keep honest.
"""

import csv

import pytest

from pipeline import remote_signal as rs
from pipeline.app import onboard


class TestMentionsRemote:
    @pytest.mark.parametrize("text", [
        "This is a remote position.",
        "REMOTE — US only",
        "Work remotely from anywhere",
        "We are a remote-first company",
        "Work from home available",
        "work-from-home schedule after training",
        "WFH 3 days a week",
        "Telecommute options considered",
        "telecommuting is permitted",
        "Teleworking eligible",
        "teleworkers must have broadband",
        "home-based role covering the region",
        "You will need a dedicated home office",
        "We are a fully distributed company",
        "join our distributed team",
        "Work from anywhere in the US",
        "anywhere in the U.S.",
        "anywhere in the United States",
        "anywhere in the country",
    ])
    def test_vocabulary(self, text):
        assert rs.mentions_remote(text)

    @pytest.mark.parametrize("text", [
        "",
        None,
        "Patient Access Representative — Spartanburg Regional, on-site, full time.",
        "Virtual interview; virtual care experience a plus",     # "virtual" is out
        "Remoteness of the site requires a vehicle",              # not the word
        "home health aide",                                       # not "home-based"
        "Bring your own laptop to the office",
    ])
    def test_not_a_mention(self, text):
        assert not rs.mentions_remote(text)

    def test_hybrid_passes_by_design(self):
        # A hybrid JD says the word; the guard's target is the posting that
        # never does.
        assert rs.mentions_remote("Hybrid: 3 days on-site in Dallas, 2 remote.")


class TestFlagReaders:
    """pandas writes "True"/"False"; older files and fixtures write "true"/"1".
    One reader for the three flags, the shape bridge.is_easy_apply_row set."""

    @pytest.mark.parametrize("cell", ["True", "true", "TRUE", " true ", "1", "yes", "t", True])
    def test_true_cells(self, cell):
        assert rs.is_remote_str({"is_remote": cell})
        assert rs.remote_only_str({"remote_only": cell})

    @pytest.mark.parametrize("cell", ["False", "false", "", None, "0", "no", False, "maybe"])
    def test_false_cells(self, cell):
        assert not rs.is_remote_str({"is_remote": cell})
        assert not rs.remote_only_str({"remote_only": cell})

    def test_absent_columns_read_false(self):
        assert not rs.is_remote_str({})
        assert not rs.remote_only_str({})
        assert not rs.flagged_remote({})

    def test_flagged_is_either(self):
        assert rs.flagged_remote({"is_remote": "True"})
        assert rs.flagged_remote({"remote_only": "True", "is_remote": "False"})
        assert not rs.flagged_remote({"remote_only": "False", "is_remote": "False"})


class TestGuardEnabled:
    def test_default_is_on(self):
        assert rs.guard_enabled({}) is True
        assert rs.guard_enabled({"filter": None}) is True
        assert rs.guard_enabled({"filter": {}}) is True
        assert rs.guard_enabled(None) is True

    @pytest.mark.parametrize("value", [False, "false", "False", "no", "0", "off"])
    def test_off(self, value):
        assert rs.guard_enabled({"filter": {"remote_requires_mention": value}}) is False

    @pytest.mark.parametrize("value", [True, "true", "yes", 1])
    def test_on(self, value):
        assert rs.guard_enabled({"filter": {"remote_requires_mention": value}}) is True

    def test_a_blank_key_keeps_the_default(self):
        # `remote_requires_mention:` with no value loads as None — a stub a
        # hand edit or a template leaves behind, not an answer — and
        # bool(None) would have silently switched the guard off.
        assert rs.guard_enabled({"filter": {"remote_requires_mention": None}}) is True


class TestLocationEligible:
    """The location half of filter.is_eligible, hoisted here so screen can
    re-apply it to a row the guard turns on-site after the JD backfill.
    filter.is_eligible delegates to it (tests/test_filter.py holds the whole
    gate); these pin the helper's own contract."""

    def test_compile_alternation_is_none_for_nothing(self):
        assert rs.compile_alternation([]) is None
        assert rs.compile_alternation(None) is None
        assert rs.compile_alternation([None, ""]) is None

    def test_compile_alternation_is_word_bounded_and_case_insensitive(self):
        pat = rs.compile_alternation(["US", "sc"])
        assert pat.search("Dallas, US")
        assert pat.search("Spartanburg, SC")
        assert not pat.search("Moscow, Russia")

    def test_no_patterns_means_eligible(self):
        assert rs.location_eligible({"location": "Spartanburg, SC"}, None, None) is True

    def test_negative_location_refuses(self):
        neg = rs.compile_alternation(["SC"])
        assert rs.location_eligible({"location": "Spartanburg, SC"}, neg, None) is False
        assert rs.location_eligible({"location": "Dallas, TX"}, neg, None) is True

    def test_eligible_allowlist_refuses_outsiders(self):
        allow = rs.compile_alternation(["TX"])
        assert rs.location_eligible({"location": "Spartanburg, SC"}, None, allow) is False
        assert rs.location_eligible({"location": "Dallas, TX"}, None, allow) is True

    def test_blank_location_is_not_judged(self):
        # No location to match means neither list can refuse it — the same
        # tolerance filter.is_eligible always had for a row the board left blank.
        neg = rs.compile_alternation(["SC"])
        allow = rs.compile_alternation(["TX"])
        assert rs.location_eligible({"location": ""}, neg, allow) is True
        assert rs.location_eligible({}, neg, allow) is True

    def test_filter_shares_the_helper(self):
        from pipeline import filter as filter_mod
        assert filter_mod._compile_alternation is rs.compile_alternation


class TestSearchPasses:
    """The shape rule (`searches:` list vs legacy `search:`) mirrored from
    onboard.search_entries. The agreement test is the drift guard."""

    LIST = {"searches": [{"name": "a", "location": "Dallas, TX"},
                         {"name": "b", "location": "United States", "is_remote": True}]}
    LEGACY = {"search": {"name": "only", "location": "Toronto, ON"}}

    @pytest.mark.parametrize("cfg", [LIST, LEGACY, {}, {"searches": None},
                                     {"searches": "oops"}, {"search": None}, None, "text"])
    def test_agrees_with_onboard_search_entries(self, cfg):
        expected = [e for e in onboard.search_entries(cfg) if isinstance(e, dict)]
        assert rs.search_passes(cfg) == expected

    def test_non_mappings_are_skipped_not_refused(self):
        assert rs.search_passes({"searches": ["scalar", None, {"name": "ok"}]}) == [{"name": "ok"}]


class TestLocalPassLocations:
    def test_non_remote_passes_only_deduped_in_order(self):
        cfg = {"searches": [
            {"name": "remote", "location": "United States", "is_remote": True},
            {"name": "dfw", "location": "Dallas, TX", "hours_old": 24},
            {"name": "easy", "location": "Dallas, TX", "easy_apply": True},
            {"name": "fw", "location": "Fort Worth, TX"},
            {"name": "blank"},
        ]}
        assert rs.local_pass_locations(cfg) == ["Dallas, TX", "Fort Worth, TX"]

    def test_quoted_and_falsy_is_remote_read_as_the_scraper_reads_them(self):
        # normalize_pass: `"true"` is a remote pass, `"false"` is not.
        cfg = {"searches": [
            {"name": "a", "location": "Sacramento, CA", "is_remote": "true"},
            {"name": "b", "location": "Austin, TX", "is_remote": "false"},
            {"name": "c", "location": "Plano, TX", "is_remote": False},
        ]}
        assert rs.local_pass_locations(cfg) == ["Austin, TX", "Plano, TX"]

    def test_legacy_single_search(self):
        assert rs.local_pass_locations({"search": {"location": "Toronto, ON"}}) == ["Toronto, ON"]

    def test_nothing_local(self):
        assert rs.local_pass_locations({"searches": [{"location": "US", "is_remote": True}]}) == []
        assert rs.local_pass_locations({}) == []


class TestIsLocalLocation:
    PASSES = ["Dallas, TX", "Fort Worth, TX"]

    @pytest.mark.parametrize("where", [
        "Dallas, TX", "dallas, texas", "Dallas-Fort Worth Area", "Fort Worth, TX",
        "Downtown Dallas, TX, United States",
    ])
    def test_city_as_a_whole_word(self, where):
        assert rs.is_local_location(where, self.PASSES)

    @pytest.mark.parametrize("where", [
        "Plano, TX",              # a suburb the local pass missed: still dropped
        "El Paso, TX",            # the state code is never enough
        "Spartanburg, SC",
        "Dallastown, PA",         # not a whole word
        "", None,
    ])
    def test_not_local(self, where):
        assert not rs.is_local_location(where, self.PASSES)

    def test_state_or_country_level_pass_location(self):
        assert rs.is_local_location("Austin, Texas", ["Texas"])
        assert rs.is_local_location("Vancouver, BC, Canada", ["Canada"])
        assert not rs.is_local_location("Austin, TX", ["Texas"])

    def test_no_passes_means_nothing_is_local(self):
        assert not rs.is_local_location("Dallas, TX", [])


class TestJudgeRemoteRow:
    def test_unflagged_row_is_left_alone(self):
        row = {"is_remote": "False", "remote_only": "False", "description": "on-site in Maine"}
        assert rs.judge_remote_row(row) is None
        assert row["is_remote"] == "False"

    def test_empty_description_is_left_for_screen(self):
        row = {"is_remote": "True", "remote_only": "True", "description": ""}
        assert rs.judge_remote_row(row) is None
        assert row["is_remote"] == "True"

    def test_jd_that_mentions_remote_confirms_the_claim(self):
        row = {"is_remote": "True", "remote_only": "True", "description": "100% remote"}
        assert rs.judge_remote_row(row) is None
        assert row["is_remote"] == "True"

    def test_remote_only_and_far_away_is_offsite(self):
        row = {"is_remote": "True", "remote_only": "True", "location": "Spartanburg, SC",
               "description": "Patient Access Rep, on-site at the hospital."}
        assert rs.judge_remote_row(row, ["Dallas, TX"]) == rs.OFFSITE
        assert row["is_remote"] == "False"

    def test_remote_only_but_local_is_onsite_and_kept(self):
        row = {"is_remote": "True", "remote_only": "True", "location": "Dallas, TX",
               "description": "Patient Access Rep, on-site at the hospital."}
        assert rs.judge_remote_row(row, ["Dallas, TX"]) == rs.ONSITE
        assert row["is_remote"] == "False"

    def test_not_remote_only_is_onsite_and_falls_through(self):
        # A non-remote pass also returned it: rewrite, but never drop here.
        row = {"is_remote": "True", "remote_only": "False", "location": "Detroit, MI",
               "description": "on-site"}
        assert rs.judge_remote_row(row, ["Dallas, TX"]) == rs.ONSITE
        assert row["is_remote"] == "False"

    def test_remote_only_flag_alone_is_a_claim(self):
        # The board did not flag it remote, but only a remote pass returned it.
        row = {"is_remote": "False", "remote_only": "True", "location": "Burlington, VT",
               "description": "Community outreach coordinator, in our Burlington office."}
        assert rs.judge_remote_row(row, ["Dallas, TX"]) == rs.OFFSITE

    def test_idempotent_across_filter_and_screen(self):
        far = {"is_remote": "True", "remote_only": "True", "location": "Spartanburg, SC",
               "description": "on-site"}
        assert rs.judge_remote_row(far, []) == rs.OFFSITE
        assert rs.judge_remote_row(far, []) == rs.OFFSITE
        shared = {"is_remote": "True", "remote_only": "False", "description": "on-site"}
        assert rs.judge_remote_row(shared) == rs.ONSITE
        assert rs.judge_remote_row(shared) is None      # no longer flagged
        assert shared["is_remote"] == "False"


class TestDroppedReporting:
    ROW = {"company": "Acme", "title": "Rep", "location": "Spartanburg, SC",
           "job_url": "https://a", "is_remote": "False"}

    def test_describe(self):
        assert rs.describe_dropped(self.ROW) == "Acme · Rep · Spartanburg, SC"
        assert rs.describe_dropped({}) == "? · ? · ?"

    def test_report_caps_at_the_limit(self, capsys):
        rows = [{**self.ROW, "job_url": f"https://{i}"} for i in range(30)]
        rs.report_dropped(rows, "filter")
        out = capsys.readouterr().out
        assert out.count("dropped remote-pass on-site posting") == rs._LOG_LIMIT
        assert "and 5 more" in out

    def test_report_nothing_prints_nothing(self, capsys):
        rs.report_dropped([], "filter")
        assert capsys.readouterr().out == ""

    def _read(self):
        with open(rs.DROPPED_PATH, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_write_overwrites_and_tags_the_stage(self):
        rs.write_dropped([self.ROW], "filter")
        rows = self._read()
        assert [r["job_url"] for r in rows] == ["https://a"]
        assert rows[0]["dropped_by"] == "filter"
        rs.write_dropped([{**self.ROW, "job_url": "https://b"}], "filter")
        assert [r["job_url"] for r in self._read()] == ["https://b"]

    def test_write_nothing_truncates(self):
        rs.write_dropped([self.ROW], "filter")
        rs.write_dropped([], "filter")
        assert rs.DROPPED_PATH.read_text(encoding="utf-8") == ""

    def test_extend_keeps_filters_rows_and_unions_columns(self):
        rs.write_dropped([self.ROW], "filter")
        rs.write_dropped([{"company": "Globex", "title": "Dev", "job_url": "https://b",
                           "relevance_score": "7"}], "screen", extend=True)
        rows = self._read()
        assert [(r["job_url"], r["dropped_by"]) for r in rows] == [
            ("https://a", "filter"), ("https://b", "screen")]
        assert rows[0]["relevance_score"] == "" and rows[1]["location"] == ""

    def test_extend_dedupes_by_url(self):
        rs.write_dropped([self.ROW], "filter")
        rs.write_dropped([self.ROW], "screen", extend=True)
        assert len(self._read()) == 1

    def test_extend_with_no_prior_file(self):
        assert not rs.DROPPED_PATH.exists()
        rs.write_dropped([self.ROW], "screen", extend=True)
        assert [r["dropped_by"] for r in self._read()] == ["screen"]
