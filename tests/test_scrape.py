"""Tests for pipeline/scrape.py"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pipeline import scrape as scrape_mod
from pipeline.sites import limitation_conflict


class TestFilterPasses:
    """Test scrape.filter_passes — picks the searches whose names match."""

    SEARCHES = [
        {"name": "recent DFW", "search_terms": ["a"], "sites": ["indeed"]},
        {"name": "remote US",  "search_terms": ["b"], "sites": ["indeed"]},
        {"name": "easy apply", "search_terms": ["c"], "sites": ["indeed"]},
    ]

    def test_none_returns_all(self):
        assert scrape_mod.filter_passes(self.SEARCHES, None) is self.SEARCHES

    def test_empty_list_returns_all(self):
        # An empty selection should not silently drop everything — it's the
        # same as "no filter requested".
        assert scrape_mod.filter_passes(self.SEARCHES, []) is self.SEARCHES

    def test_blank_strings_treated_as_no_filter(self):
        assert scrape_mod.filter_passes(self.SEARCHES, ["", "   "]) is self.SEARCHES

    def test_single_match(self):
        result = scrape_mod.filter_passes(self.SEARCHES, ["easy apply"])
        assert len(result) == 1
        assert result[0]["name"] == "easy apply"

    def test_multi_match_preserves_input_order(self):
        # Output should preserve original search order, regardless of selector order.
        result = scrape_mod.filter_passes(self.SEARCHES, ["remote US", "recent DFW"])
        assert [r["name"] for r in result] == ["recent DFW", "remote US"]

    def test_match_is_case_insensitive(self):
        result = scrape_mod.filter_passes(self.SEARCHES, ["EASY APPLY"])
        assert len(result) == 1
        assert result[0]["name"] == "easy apply"

    def test_match_trims_whitespace(self):
        result = scrape_mod.filter_passes(self.SEARCHES, ["  easy apply  "])
        assert len(result) == 1

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="matched no searches"):
            scrape_mod.filter_passes(self.SEARCHES, ["does not exist"])


class TestFilterPassesEasyApply:
    """Test the easy_apply_only / no_easy_apply selectors used by the cloud
    workflows. These route by JobSpy field instead of pass name so that
    user-renamed passes don't break the workflow."""

    SEARCHES = [
        {"name": "recent local", "search_terms": ["a"], "sites": ["indeed"]},
        {"name": "remote US",    "search_terms": ["b"], "sites": ["indeed"], "is_remote": True},
        {"name": "easy apply",   "search_terms": ["c"], "sites": ["indeed"], "easy_apply": True},
    ]

    def test_easy_apply_only_keeps_only_true(self):
        result = scrape_mod.filter_passes(self.SEARCHES, easy_apply_only=True)
        assert [r["name"] for r in result] == ["easy apply"]

    def test_no_easy_apply_drops_true(self):
        result = scrape_mod.filter_passes(self.SEARCHES, no_easy_apply=True)
        assert [r["name"] for r in result] == ["recent local", "remote US"]

    def test_easy_apply_only_returns_empty_when_no_easy_apply_pass(self):
        # Critical: workflow no-ops cleanly when user has no easy-apply pass.
        searches = [{"name": "x", "search_terms": ["a"], "sites": ["indeed"]}]
        result = scrape_mod.filter_passes(searches, easy_apply_only=True)
        assert result == []

    def test_no_easy_apply_returns_empty_when_only_easy_apply_pass(self):
        searches = [{"name": "x", "easy_apply": True, "search_terms": ["a"], "sites": ["indeed"]}]
        result = scrape_mod.filter_passes(searches, no_easy_apply=True)
        assert result == []

    def test_easy_apply_only_and_no_easy_apply_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            scrape_mod.filter_passes(
                self.SEARCHES, easy_apply_only=True, no_easy_apply=True,
            )

    def test_easy_apply_routing_does_not_match_truthy_strings(self):
        # Only literal Python True should match — protects against accidental
        # `easy_apply: "true"` strings in YAML being interpreted as a flag.
        searches = [
            {"name": "a", "easy_apply": "true",  "search_terms": ["x"], "sites": ["indeed"]},
            {"name": "b", "easy_apply": 1,        "search_terms": ["x"], "sites": ["indeed"]},
            {"name": "c", "easy_apply": True,     "search_terms": ["x"], "sites": ["indeed"]},
        ]
        result = scrape_mod.filter_passes(searches, easy_apply_only=True)
        assert [r["name"] for r in result] == ["c"]

    def test_only_pass_combines_with_easy_apply_filter(self):
        # If both are specified, only_pass narrows first then easy_apply filters.
        # (--only-pass is in a mutually exclusive group at CLI level, but the
        # underlying function supports the combination — useful for tests and
        # ad-hoc callers.)
        result = scrape_mod.filter_passes(
            self.SEARCHES, only_passes=["easy apply", "remote US"], easy_apply_only=True,
        )
        assert [r["name"] for r in result] == ["easy apply"]


class TestLoadSearches:
    """Test scrape.load_searches function."""

    def test_load_searches_with_searches_key(self, tmp_path):
        """Multi-pass format: searches: [dict1, dict2] returns list of length 2."""
        config_file = tmp_path / "config.yml"
        config_file.write_text("""
searches:
  - name: "pass 1"
    search_terms: ["software engineer"]
    sites: [indeed]
  - name: "pass 2"
    search_terms: ["backend engineer"]
    sites: [linkedin]
""")
        result = scrape_mod.load_searches(config_file)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "pass 1"
        assert result[1]["name"] == "pass 2"

    def test_load_searches_with_legacy_search_key(self, tmp_path):
        """Legacy format: search: {...} returns list of length 1."""
        config_file = tmp_path / "config.yml"
        config_file.write_text("""
search:
  search_terms: ["software engineer"]
  sites: [indeed]
""")
        result = scrape_mod.load_searches(config_file)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["search_terms"] == ["software engineer"]

    def test_load_searches_missing_file_raises(self, tmp_path):
        """Missing config file raises FileNotFoundError."""
        nonexistent = tmp_path / "nonexistent.yml"
        with pytest.raises(FileNotFoundError):
            scrape_mod.load_searches(nonexistent)


class TestStripUnsupportedSites:
    """Only indeed + linkedin are supported scrape sites. Glassdoor and
    ZipRecruiter are Cloudflare-walled (403 on every request, zero rows), and
    Google Jobs serves degraded responses then drops the connection mid-body —
    which jobspy's Google scraper doesn't catch, killing the whole run.
    strip_unsupported_sites removes them at load time so stale configs (e.g.
    an old cloud SEARCH_CONFIG_B64 secret) can't crash or waste requests."""

    def test_supported_sites_are_indeed_and_linkedin(self):
        assert set(scrape_mod.SUPPORTED_SITES) == {"indeed", "linkedin"}

    def test_drops_unsupported_sites_preserving_order(self):
        searches = [{
            "name": "p", "search_terms": ["a"],
            "sites": ["glassdoor", "indeed", "zip_recruiter", "linkedin", "google"],
        }]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed", "linkedin"]

    def test_matching_is_case_insensitive(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["LinkedIn", "Google"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["LinkedIn"]

    def test_pass_with_no_supported_sites_is_dropped(self):
        searches = [
            {"name": "dead", "search_terms": ["a"], "sites": ["google", "glassdoor"]},
            {"name": "live", "search_terms": ["a"], "sites": ["indeed"]},
        ]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert [s["name"] for s in result] == ["live"]

    def test_all_supported_passes_come_back_unchanged(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed", "linkedin"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result == searches

    def test_bare_string_sites_is_not_iterated_character_by_character(self):
        # `sites: indeed` is valid YAML and a valid JobSpy site_name; iterating
        # the string would test 'i', 'n', 'd', ... and drop the whole pass.
        searches = [{"name": "p", "search_terms": ["a"], "sites": "indeed"}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]

    def test_surrounding_whitespace_is_stripped_from_kept_sites(self):
        # jobspy resolves the board with Site[name.upper()], which raises on
        # " LINKEDIN " — so a padded entry must not survive verbatim.
        searches = [{"name": "p", "search_terms": ["a"], "sites": [" linkedin "]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["linkedin"]

    def test_case_variant_duplicates_collapse(self):
        # Both map to Site.INDEED; keeping both scrapes the board twice.
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed", "Indeed"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]

    def test_non_string_entries_do_not_crash_the_warning(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["linkedin", 123]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["linkedin"]

    def test_warning_names_the_dropped_sites_and_pass(self, capsys):
        searches = [{"name": "US Remote", "search_terms": ["a"],
                     "sites": ["indeed", "glassdoor", "google"]}]
        scrape_mod.strip_unsupported_sites(searches)
        out = capsys.readouterr().out
        assert "glassdoor" in out and "google" in out
        assert "US Remote" in out

    def test_no_warning_when_nothing_dropped(self, capsys):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed"]}]
        scrape_mod.strip_unsupported_sites(searches)
        assert capsys.readouterr().out == ""

    def test_missing_sites_key_defaults_to_supported(self):
        # Left as None, jobspy's get_site_type() scrapes list(Site) — every
        # retired board included — so the key has to be filled in.
        searches = [{"name": "p", "search_terms": ["a"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == list(scrape_mod.SUPPORTED_SITES)

    def test_explicitly_null_sites_defaults_to_supported(self):
        # `sites:` with nothing after it — a real None in the mapping, which
        # made the mutex check raise TypeError.
        searches = [{"name": "p", "search_terms": ["a"], "sites": None}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == list(scrape_mod.SUPPORTED_SITES)

    def test_comma_separated_scalar_is_split(self):
        # `sites: indeed, linkedin` unbracketed is ONE YAML string, and it is the
        # shape the CLI wizard prompts for. Read whole it matches no board, so
        # naming both supported boards used to drop the pass entirely.
        searches = [{"name": "p", "search_terms": ["a"], "sites": "indeed, linkedin"}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed", "linkedin"]

    def test_comma_separated_scalar_still_drops_unsupported(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": "indeed, glassdoor"}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]

    def test_trailing_comma_does_not_report_a_blank_board(self, capsys):
        searches = [{"name": "p", "search_terms": ["a"], "sites": "indeed,"}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]
        assert capsys.readouterr().out == ""

    def test_non_iterable_scalar_sites_does_not_crash(self):
        # `sites: 5` — list(5) raises TypeError, which aborted the whole scrape
        # stage instead of degrading to the documented warning.
        searches = [{"name": "p", "search_terms": ["a"], "sites": 5}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result == []      # nothing supported, so the pass is skipped

    def test_null_entry_is_not_reported_as_a_board_named_none(self, capsys):
        # `- ~` in the list stringified to "None", sending the user hunting for
        # a board by that name.
        searches = [{"name": "p", "search_terms": ["a"], "sites": [None, "indeed"]}]
        result = scrape_mod.strip_unsupported_sites(searches)
        assert result[0]["sites"] == ["indeed"]
        assert capsys.readouterr().out == ""

    def test_input_list_is_not_mutated(self):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed", "google"]}]
        scrape_mod.strip_unsupported_sites(searches)
        assert searches[0]["sites"] == ["indeed", "google"]


class TestRunStripsUnsupportedSites:
    """run() applies the strip to whatever config it loads, so even a
    hand-edited or stale config never reaches jobspy with a dead site."""

    def test_run_scrapes_only_supported_sites(self, tmp_path, patch_scrape_paths, mocker):
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms: ["software engineer"]
    sites: [indeed, glassdoor, zip_recruiter, linkedin, google]
    results_wanted: 50
filter:
  min_score: 5
""")
        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        assert mock_scrape.call_args[1]["site_name"] == ["indeed", "linkedin"]

    def test_run_with_only_unsupported_sites_noops_cleanly(
        self, tmp_path, patch_scrape_paths, jobs_csv, mocker
    ):
        # All passes stripped away → same clean no-op as "no searches matched":
        # empty jobs.csv, no scrape_jobs call, downstream stages see zero rows.
        # Asserted on content, not just existence, so the previous run's rows
        # can't survive and masquerade as this run's output.
        patch_scrape_paths.write_text(
            jobs_csv.read_text(encoding="utf-8"), encoding="utf-8"
        )
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms: ["software engineer"]
    sites: [google, glassdoor]
    results_wanted: 50
filter:
  min_score: 5
""")
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs")

        result = scrape_mod.run(config)

        mock_scrape.assert_not_called()
        assert result == patch_scrape_paths
        assert patch_scrape_paths.read_text(encoding="utf-8") == ""


def _sets_clause(msg: str) -> str:
    """The options limitation_conflict names as the offenders in `msg`.

    The full message also spells out every option in every group ("only ONE of
    [hours_old | job_type and/or is_remote | easy_apply]"), so a substring test
    against the whole string passes no matter which keys the rule blames.
    """
    # Checked rather than assumed: a regression that returns None here is
    # exactly what these assertions exist to catch, and `None.split` reports it
    # as an AttributeError inside a test helper instead of as the failure it is.
    assert isinstance(msg, str) and "this pass sets " in msg, f"no conflict reported: {msg!r}"
    return msg.split("this pass sets ", 1)[1].removesuffix(". Remove all but one.")


class TestLimitationConflict:
    """pipeline.sites.limitation_conflict — JobSpy's per-pass mutual-exclusion
    rule, returning the message so the scraper can skip the pass and the UI's
    save endpoint (which cannot import this module's jobspy-bound caller) can
    reject the save with it."""

    # Indeed group constraints: only ONE of {hours_old}, {job_type/is_remote}, {easy_apply}
    def test_indeed_hours_old_alone(self):
        assert limitation_conflict({"sites": ["indeed"], "hours_old": 168}) is None

    def test_indeed_is_remote_alone(self):
        assert limitation_conflict({"sites": ["indeed"], "is_remote": True}) is None

    def test_indeed_job_type_alone(self):
        assert limitation_conflict({"sites": ["indeed"], "job_type": "fulltime"}) is None

    def test_indeed_job_type_with_is_remote_is_one_group(self):
        # Both live in Group B, so together they are not a conflict.
        cfg = {"sites": ["indeed"], "job_type": "fulltime", "is_remote": True}
        assert limitation_conflict(cfg) is None

    def test_indeed_easy_apply_alone(self):
        assert limitation_conflict({"sites": ["indeed"], "easy_apply": True}) is None

    def test_indeed_hours_old_and_is_remote_conflicts(self):
        cfg = {"sites": ["indeed"], "hours_old": 168, "is_remote": True}
        assert "Indeed limitation" in limitation_conflict(cfg)

    def test_indeed_hours_old_and_easy_apply_conflicts(self):
        cfg = {"sites": ["indeed"], "hours_old": 168, "easy_apply": True}
        assert "Indeed limitation" in limitation_conflict(cfg)

    def test_indeed_is_remote_and_easy_apply_conflicts(self):
        cfg = {"sites": ["indeed"], "is_remote": True, "easy_apply": True}
        assert "Indeed limitation" in limitation_conflict(cfg)

    def test_message_names_the_options_actually_set(self):
        # "one of these groups" alone leaves the user diffing their config
        # against the rule; name the offending keys. Read the clause rather
        # than the whole message — `allowed` lists every option in every group,
        # so `"is_remote" in msg` holds however the offenders are computed.
        cfg = {"sites": ["indeed"], "hours_old": 168, "is_remote": True}
        assert _sets_clause(limitation_conflict(cfg)) == "hours_old, is_remote"

    def test_explicitly_off_is_not_a_conflict(self):
        # `easy_apply: false` asks for no easy-apply filter, and jobspy reads
        # the value for truthiness — nothing goes on the wire either way. The
        # user asked for one filter (hours_old) and used to be charged the
        # whole pass for saying out loud that they didn't want the other.
        cfg = {"sites": ["indeed"], "hours_old": 168, "easy_apply": False}
        assert limitation_conflict(cfg) is None

    def test_zero_is_not_a_conflict(self):
        # Same rule for the numeric option: `hours_old * 3600 if hours_old`
        # makes 0 a no-op, not "posted in the last zero hours".
        cfg = {"sites": ["indeed"], "hours_old": 0, "is_remote": True}
        assert limitation_conflict(cfg) is None

    def test_an_off_option_is_not_named_among_the_offenders(self):
        # is_remote shares Group B with job_type, so the group is active on
        # job_type alone — but telling the user to remove an option they had
        # already turned off sends them to edit the wrong line.
        cfg = {"sites": ["indeed"], "hours_old": 168, "job_type": "fulltime",
               "is_remote": False}
        assert _sets_clause(limitation_conflict(cfg)) == "hours_old, job_type"

    def test_linkedin_hours_old_and_easy_apply_conflicts(self):
        cfg = {"sites": ["linkedin"], "hours_old": 168, "easy_apply": True}
        assert "LinkedIn limitation" in limitation_conflict(cfg)

    def test_linkedin_hours_old_alone(self):
        assert limitation_conflict({"sites": ["linkedin"], "hours_old": 48}) is None

    def test_linkedin_easy_apply_alone(self):
        assert limitation_conflict({"sites": ["linkedin"], "easy_apply": True}) is None

    def test_linkedin_ignores_the_indeed_only_group(self):
        # is_remote is unrestricted on LinkedIn; only Indeed groups it with job_type.
        cfg = {"sites": ["linkedin"], "hours_old": 48, "is_remote": True}
        assert limitation_conflict(cfg) is None

    def test_retired_site_is_not_checked(self):
        # zip_recruiter is stripped before the scrape, so its options can't
        # conflict with anything — checking it would reject a survivable config.
        cfg = {"sites": ["zip_recruiter"], "hours_old": 168, "is_remote": True,
               "easy_apply": True}
        assert limitation_conflict(cfg) is None

    def test_empty_sites_is_not_checked(self):
        cfg = {"sites": [], "hours_old": 168, "is_remote": True}
        assert limitation_conflict(cfg) is None

    def test_missing_sites_key_inherits_the_supported_boards(self):
        # The repro from the issue's second half: a pass that never names Indeed
        # is still bound by Indeed's rule, because the key is filled in upstream.
        cfg = {"name": "US Remote", "hours_old": 168, "is_remote": True}
        assert "Indeed limitation" in limitation_conflict(cfg)

    def test_null_sites_does_not_crash(self):
        # `sites:` with nothing after it is a real None in the mapping.
        assert "Indeed limitation" in limitation_conflict(
            {"sites": None, "hours_old": 168, "is_remote": True})

    def test_null_sites_without_a_conflict_is_clean(self):
        assert limitation_conflict({"sites": None, "hours_old": 168}) is None

    def test_message_names_the_pass(self):
        cfg = {"name": "US Remote", "hours_old": 168, "is_remote": True,
               "sites": ["indeed", "linkedin"]}
        assert limitation_conflict(cfg).startswith("[US Remote] ")

    def test_case_variant_board_name_is_still_checked(self):
        cfg = {"sites": ["Indeed"], "hours_old": 168, "is_remote": True}
        assert "Indeed limitation" in limitation_conflict(cfg)


class TestOptionalParamForwarding:
    """The other half of the truthiness change, pinned so it can't be "unified".

    limitation_conflict asks "will jobspy act on this?" and tests truthiness;
    OPTIONAL_PARAMS asks "did the user supply a value to forward?" and must keep
    `is not None`. It spans 16 keys whose falsy values are deliberate settings,
    and truthy-filtering them would silently restore jobspy's defaults — for
    linkedin_fetch_description that means the 30+ minute per-JD fetch the
    example config marks KEEP THIS FALSE."""

    def test_explicitly_false_options_are_still_forwarded(self, patch_scrape_paths, tmp_path, mocker):
        cfg = tmp_path / "search.yml"
        cfg.write_text(
            "searches:\n"
            "  - name: 'p'\n"
            "    search_terms: ['nurse']\n"
            "    sites: [indeed]\n"
            "    linkedin_fetch_description: false\n"
            "    enforce_annual_salary: false\n",
            encoding="utf-8",
        )
        mock_scrape = mocker.patch.object(scrape_mod, "scrape_jobs", return_value=pd.DataFrame())

        scrape_mod.run(cfg)

        kwargs = mock_scrape.call_args.kwargs
        assert kwargs["linkedin_fetch_description"] is False
        assert kwargs["enforce_annual_salary"] is False

    def test_zero_valued_options_are_still_forwarded(self, patch_scrape_paths, tmp_path, mocker):
        # `distance: 0` dropped would silently become jobspy's default of 50.
        cfg = tmp_path / "search.yml"
        cfg.write_text(
            "searches:\n"
            "  - name: 'p'\n"
            "    search_terms: ['nurse']\n"
            "    sites: [indeed]\n"
            "    distance: 0\n"
            "    offset: 0\n",
            encoding="utf-8",
        )
        mock_scrape = mocker.patch.object(scrape_mod, "scrape_jobs", return_value=pd.DataFrame())

        scrape_mod.run(cfg)

        kwargs = mock_scrape.call_args.kwargs
        assert kwargs["distance"] == 0
        assert kwargs["offset"] == 0


class TestDropConflictingPasses:
    """A conflicting pass is skipped with a warning, not raised: a config can
    reach a run without passing the UI validator (stale cloud secret, hand-edited
    file), and aborting took every healthy pass down with it."""

    def test_conflicting_pass_is_dropped(self):
        searches = [{"name": "bad", "search_terms": ["a"], "sites": ["indeed"],
                     "hours_old": 168, "is_remote": True}]
        assert scrape_mod.drop_conflicting_passes(searches) == []

    def test_healthy_passes_survive_a_conflicting_neighbour(self):
        searches = [
            {"name": "bad", "search_terms": ["a"], "sites": ["indeed"],
             "hours_old": 168, "is_remote": True},
            {"name": "good", "search_terms": ["a"], "sites": ["indeed"], "hours_old": 168},
        ]
        result = scrape_mod.drop_conflicting_passes(searches)
        assert [s["name"] for s in result] == ["good"]

    def test_warning_names_the_pass_and_the_rule(self, capsys):
        searches = [{"name": "US Remote", "search_terms": ["a"], "sites": ["indeed"],
                     "hours_old": 168, "is_remote": True}]
        scrape_mod.drop_conflicting_passes(searches)
        out = capsys.readouterr().out
        assert "US Remote" in out and "Indeed limitation" in out and "skipping" in out

    def test_no_warning_when_nothing_conflicts(self, capsys):
        searches = [{"name": "p", "search_terms": ["a"], "sites": ["indeed"], "hours_old": 168}]
        assert scrape_mod.drop_conflicting_passes(searches) == searches
        assert capsys.readouterr().out == ""


class TestRunDropsConflictingPasses:
    """run() applies the drop to whatever config it loads, so a conflicting pass
    can no longer abort the stage — and with it the rest of the pipeline."""

    def _config(self, tmp_path, body):
        config = tmp_path / "config.yml"
        config.write_text(body)
        return config

    def test_healthy_pass_still_scrapes(self, tmp_path, patch_scrape_paths, mocker):
        config = self._config(tmp_path, """
searches:
  - name: "conflicting"
    search_terms: ["a"]
    sites: [indeed]
    hours_old: 168
    is_remote: true
  - name: "healthy"
    search_terms: ["b"]
    sites: [indeed]
    hours_old: 168
""")
        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        assert mock_scrape.call_count == 1
        assert mock_scrape.call_args[1]["search_term"] == "b"

    def test_only_conflicting_passes_noops_cleanly(
        self, tmp_path, patch_scrape_paths, jobs_csv, mocker
    ):
        # Same clean no-op as an all-retired-boards config: empty jobs.csv,
        # no jobspy call, no traceback out of the stage. Seeded with stale
        # rows so "empty" is asserted on content, not just existence.
        config = self._config(tmp_path, """
searches:
  - name: "conflicting"
    search_terms: ["a"]
    sites: [indeed]
    hours_old: 168
    is_remote: true
""")
        patch_scrape_paths.write_text(
            jobs_csv.read_text(encoding="utf-8"), encoding="utf-8"
        )
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs")

        result = scrape_mod.run(config)

        mock_scrape.assert_not_called()
        assert result == patch_scrape_paths
        assert patch_scrape_paths.read_text(encoding="utf-8") == ""

    def test_conflict_is_judged_after_retired_boards_are_stripped(
        self, tmp_path, patch_scrape_paths, mocker
    ):
        # zip_recruiter accepts the combination, but it never runs — the pass
        # scrapes Indeed, which does not, so this must still be dropped.
        config = self._config(tmp_path, """
searches:
  - name: "mixed"
    search_terms: ["a"]
    sites: [zip_recruiter, indeed]
    hours_old: 168
    easy_apply: true
""")
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs")

        scrape_mod.run(config)

        mock_scrape.assert_not_called()


class TestRun:
    """Test scrape.run function with mocked JobSpy."""

    def test_run_writes_csv_to_output_path(self, cfg_file, patch_scrape_paths, mocker):
        """run() calls scrape_jobs and writes output CSV to patched OUTPUT_PATH."""
        output_path = patch_scrape_paths

        # Mock scrape_jobs to return a DataFrame with one row
        df = pd.DataFrame(
            {
                "job_url": ["https://indeed.com/job1"],
                "title": ["software engineer"],
                "company": ["acme"],
            }
        )
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        result = scrape_mod.run(cfg_file)

        # Verify file was written
        assert output_path.exists()
        assert result == output_path

        # Verify content
        csv_df = pd.read_csv(output_path)
        assert len(csv_df) == 1
        assert csv_df.iloc[0]["title"] == "software engineer"

    def test_run_deduplicates_on_job_url(self, cfg_file, patch_scrape_paths, mocker):
        """Multiple passes returning same job_url deduplicate to single row."""
        output_path = patch_scrape_paths

        # Two DataFrames with the same job_url
        df1 = pd.DataFrame(
            {
                "job_url": ["https://indeed.com/job1"],
                "title": ["software engineer"],
                "company": ["acme"],
            }
        )
        df2 = pd.DataFrame(
            {
                "job_url": ["https://indeed.com/job1"],
                "title": ["software engineer"],
                "company": ["acme"],
            }
        )

        mocker.patch("pipeline.scrape.scrape_jobs", side_effect=[df1, df2])

        scrape_mod.run(cfg_file)

        csv_df = pd.read_csv(output_path)
        assert len(csv_df) == 1  # Deduped

    def test_run_returns_output_path(self, cfg_file, patch_scrape_paths, mocker):
        """run() returns the OUTPUT_PATH."""
        output_path = patch_scrape_paths

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        result = scrape_mod.run(cfg_file)
        assert result == output_path

    def test_run_empty_results_truncates_stale_output(
        self, cfg_file, patch_scrape_paths, jobs_csv, mocker
    ):
        """A zero-row scrape (rate-limited, network blip) doesn't crash, and
        must not leave the previous run's rows behind — filter/screen/bridge
        would re-process them as if they were today's results."""
        output_path = patch_scrape_paths
        output_path.write_text(
            jobs_csv.read_text(encoding="utf-8"), encoding="utf-8"
        )

        # A fresh empty frame per call — run() tags each one with an easy_apply
        # column, so a shared return_value would stop being "every pass came
        # back empty" the moment this config grows a second term.
        mocker.patch(
            "pipeline.scrape.scrape_jobs", side_effect=lambda *a, **kw: pd.DataFrame()
        )

        result = scrape_mod.run(cfg_file)

        assert result == output_path
        assert output_path.read_text(encoding="utf-8") == ""

    def test_run_calls_scrape_per_term(self, tmp_path, patch_scrape_paths, mocker):
        """scrape_jobs called once per search term."""
        # Config with two search terms
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms:
      - "software engineer"
      - "backend engineer"
    sites: [indeed]
    results_wanted: 50
filter:
  min_score: 5
""")

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        # Called twice (once per search term)
        assert mock_scrape.call_count == 2

    def test_run_passes_optional_params(self, tmp_path, patch_scrape_paths, mocker):
        """Optional params from config are passed through to scrape_jobs."""
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "test"
    search_terms: ["software engineer"]
    sites: [indeed]
    location: "Dallas, TX"
    hours_old: 48
    results_wanted: 100
filter:
  min_score: 5
""")

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mock_scrape = mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(config)

        # Check that location and hours_old were passed
        call_kwargs = mock_scrape.call_args[1]
        assert call_kwargs["location"] == "Dallas, TX"
        assert call_kwargs["hours_old"] == 48
        assert call_kwargs["results_wanted"] == 100

    def test_run_creates_output_directory(self, cfg_file, monkeypatch, tmp_path, mocker):
        """run() creates the output directory if it doesn't exist."""
        output_dir = tmp_path / "nonexistent_dir" / "output"
        output_path = output_dir / "jobs.csv"

        monkeypatch.setattr(scrape_mod, "OUTPUT_PATH", output_path)

        df = pd.DataFrame({"job_url": ["https://indeed.com/job1"]})
        mocker.patch("pipeline.scrape.scrape_jobs", return_value=df)

        scrape_mod.run(cfg_file)

        assert output_dir.exists()

    def test_run_multiple_passes_merged(self, tmp_path, patch_scrape_paths, mocker):
        """Multiple search passes are merged into single output."""
        config = tmp_path / "config.yml"
        config.write_text("""
searches:
  - name: "pass 1"
    search_terms: ["software engineer"]
    sites: [indeed]
    results_wanted: 50

  - name: "pass 2"
    search_terms: ["backend engineer"]
    sites: [indeed]
    results_wanted: 50

filter:
  min_score: 5
""")

        # Pass 1 returns 2 rows, Pass 2 returns 2 different rows
        df1 = pd.DataFrame(
            {
                "job_url": [
                    "https://indeed.com/job1",
                    "https://indeed.com/job2",
                ],
                "title": ["software engineer", "software engineer"],
            }
        )
        df2 = pd.DataFrame(
            {
                "job_url": [
                    "https://indeed.com/job3",
                    "https://indeed.com/job4",
                ],
                "title": ["backend engineer", "backend engineer"],
            }
        )

        mocker.patch("pipeline.scrape.scrape_jobs", side_effect=[df1, df2])

        output_path = patch_scrape_paths
        scrape_mod.run(config)

        csv_df = pd.read_csv(output_path)
        assert len(csv_df) == 4  # All rows merged


class TestMarkEasyApply:
    """scrape.mark_easy_apply collapses the per-pass easy_apply flag to a
    per-URL OR — a job returned by both a broad pass (False) and an easy_apply
    pass (True) must end up True, regardless of which row dedup later keeps."""

    def test_url_in_both_passes_becomes_true(self):
        df = pd.DataFrame({
            "job_url": ["https://a", "https://a", "https://b"],
            "easy_apply": [False, True, False],
        })
        out = scrape_mod.mark_easy_apply(df)
        a_rows = out[out["job_url"] == "https://a"]["easy_apply"]
        assert [bool(v) for v in a_rows] == [True, True]
        assert bool(out[out["job_url"] == "https://b"]["easy_apply"].iloc[0]) is False

    def test_url_only_in_easy_pass_is_true(self):
        df = pd.DataFrame({"job_url": ["https://x"], "easy_apply": [True]})
        out = scrape_mod.mark_easy_apply(df)
        assert bool(out["easy_apply"].iloc[0]) is True

    def test_url_only_in_broad_pass_is_false(self):
        df = pd.DataFrame({"job_url": ["https://y"], "easy_apply": [False]})
        out = scrape_mod.mark_easy_apply(df)
        assert bool(out["easy_apply"].iloc[0]) is False

    def test_missing_column_defaults_false(self):
        df = pd.DataFrame({"job_url": ["https://z"]})
        out = scrape_mod.mark_easy_apply(df)
        assert "easy_apply" in out.columns
        assert bool(out["easy_apply"].iloc[0]) is False
