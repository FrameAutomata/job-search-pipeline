"""Contract tests against the installed python-jobspy.

`pipeline.sites.MUTEX_GROUPS` is a claim about this library's filter builders:
Indeed drops the lower-priority filter in silence, LinkedIn drops nothing. That
claim decides which search passes run and which configs the UI refuses to save,
and it is hand-maintained against a third party.

`requirements.txt` pins the version the readings were taken from, but a pin only
stops *accidental* drift — it does nothing about the deliberate bump, which is
the case that matters, because jobspy's whole value is tracking board changes.
These tests assert the readings themselves, so a bump fails here instead of
quietly leaving the constant describing a library that no longer behaves that
way. In both directions: a group that should exist and doesn't means passes
silently don't search what they say; one that shouldn't and does means passes
get discarded.

If one fails after a bump, re-read the builder and update MUTEX_GROUPS. Don't
relax the test.
"""

import pytest

from pipeline.sites import MUTEX_GROUPS

# Imported, not importorskip'd. A skip here would defeat the entire point:
# module reorganisation is the most likely shape of a breaking bump, and
# `importorskip` would turn exactly that into a green build with all of these
# silently gone. CI installs requirements.txt, so jobspy is always present.
from jobspy.model import JobType, ScraperInput, Site
from pydantic import ValidationError

# The Indeed GraphQL filter fragments. Only FULL_TIME_KEY comes from
# _build_filters' job_type_key_mapping; DSQF7 is appended by a separate
# `if self.scraper_input.is_remote:` branch, and the other two are literals
# inside the hours_old and easy_apply filter strings.
DATE_FILTER = "dateOnIndeed"
EASY_APPLY_FILTER = "indeedApplyScope"
FULL_TIME_KEY = "CF3CP"
REMOTE_KEY = "DSQF7"


def _indeed_filters(**kwargs) -> str:
    """Indeed's filter fragment for a ScraperInput carrying `kwargs`.

    `_build_filters` reads nothing but `self.scraper_input`, so the scraper is
    built without `__init__` — that would open a session we have no use for.
    """
    from jobspy.indeed import Indeed

    scraper = Indeed.__new__(Indeed)
    scraper.scraper_input = ScraperInput(site_type=[Site.INDEED], search_term="nurse", **kwargs)
    return scraper._build_filters()


class TestIndeedDropsTheLoserSilently:
    """The reading that keeps `MUTEX_GROUPS["indeed"]` in place."""

    def test_hours_old_wins_over_is_remote(self):
        filters = _indeed_filters(hours_old=168, is_remote=True)
        assert DATE_FILTER in filters
        assert REMOTE_KEY not in filters, (
            "Indeed now honours hours_old with is_remote — Group A and Group B "
            "may no longer be mutually exclusive. Re-read _build_filters."
        )

    def test_hours_old_wins_over_easy_apply(self):
        filters = _indeed_filters(hours_old=168, easy_apply=True)
        assert DATE_FILTER in filters
        assert EASY_APPLY_FILTER not in filters

    def test_hours_old_wins_over_job_type(self):
        filters = _indeed_filters(hours_old=168, job_type=JobType.FULL_TIME)
        assert DATE_FILTER in filters
        assert FULL_TIME_KEY not in filters

    def test_easy_apply_wins_over_job_type(self):
        filters = _indeed_filters(easy_apply=True, job_type=JobType.FULL_TIME)
        assert EASY_APPLY_FILTER in filters
        assert FULL_TIME_KEY not in filters

    def test_easy_apply_wins_over_is_remote(self):
        filters = _indeed_filters(easy_apply=True, is_remote=True)
        assert EASY_APPLY_FILTER in filters
        assert REMOTE_KEY not in filters

    def test_job_type_and_is_remote_are_honoured_together(self):
        # Why those two share one group rather than being a conflict.
        filters = _indeed_filters(job_type=JobType.FULL_TIME, is_remote=True)
        assert FULL_TIME_KEY in filters and REMOTE_KEY in filters

    def test_nothing_set_builds_no_filter(self):
        assert _indeed_filters() == ""


class TestLinkedInDropsNothing:
    """The reading that retired `MUTEX_GROUPS["linkedin"]` (#115)."""

    def test_every_filter_goes_on_the_wire_together(self):
        from jobspy.linkedin import LinkedIn

        captured = {}

        class _StubResponse:
            status_code = 200
            text = ""  # no base-search-card, so scrape() returns after one request

        class _StubSession:
            calls = 0

            def get(self, url, params=None, **kwargs):
                # Bounded deliberately. scrape()'s loop only exits on the
                # empty-job-cards return; if a bump turned that into a
                # `continue`, an unbounded stub would spin forever with a
                # multi-second sleep per pass. For a test whose job is to
                # survive bumps, hanging CI is strictly worse than failing it.
                _StubSession.calls += 1
                assert _StubSession.calls <= 2, (
                    "LinkedIn.scrape did not stop after an empty result page — "
                    "its loop-exit branch has changed; re-read the builder."
                )
                captured.update(params or {})
                return _StubResponse()

        # Built without __init__ for the same reason as _indeed_filters: it
        # opens a rotating requests.Session we would immediately discard
        # unclosed, and it would fail this test for reasons unrelated to the
        # reading if construction ever gains a required argument.
        scraper = LinkedIn.__new__(LinkedIn)
        scraper.session = _StubSession()
        scraper.scrape(ScraperInput(
            site_type=[Site.LINKEDIN], search_term="nurse",
            hours_old=168, easy_apply=True, is_remote=True, job_type=JobType.FULL_TIME,
        ))

        assert captured, "LinkedIn made no request — the stub no longer matches its call shape"
        missing = [k for k in ("f_TPR", "f_AL", "f_WT", "f_JT") if k not in captured]
        assert not missing, (
            f"LinkedIn no longer sends {missing} alongside the rest. It may have "
            "gained a precedence rule, in which case MUTEX_GROUPS needs a "
            '"linkedin" entry again — see #115.'
        )


def test_one_scraper_input_is_built_before_any_board_is_dispatched():
    """The reading that justifies the OTHER degradation — the whole-pass one.

    `pipeline.scrape.resolve_pass_sites` degrades two ways, and the difference
    rests on this: a mutex conflict costs the pass one BOARD, but an option value
    pydantic can't read costs it the whole pass. That is only correct if
    `scrape_jobs` validates once, up front, for every board at once — if it built
    a ScraperInput per board, a bad value would take only the boards that read
    that field and `unreadable_options` would be over-punishing the rest.

    Asserted by construction: a value no board could object to individually
    (`easy_apply` is meaningless to a board that never reads it) still raises
    before a single scraper class is instantiated. Read against the source,
    jobspy builds `ScraperInput(...)` at module scope in `scrape_jobs` and only
    then fans out over `scraper_input.site_type` in a ThreadPoolExecutor.

    If this ever fails, `unreadable_options` should degrade per board too, and
    the asymmetry documented in resolve_pass_sites stops being true.

    Scraping is stubbed rather than trusted to go unreached: the failure mode
    under test is "validation no longer raises", and unstubbed that would send
    this test out to scrape two live job boards instead of failing.

    The stub replaces each scraper's `scrape` METHOD, not the class binding in
    jobspy's namespace. `SCRAPER_MAPPING` is currently rebuilt from module
    globals on every `scrape_jobs` call — so patching the names would work today
    — but jobspy's own source comment invites hoisting that dict to module
    scope, which would silently turn a name patch into a no-op and leave this
    test making live requests on exactly the bump it exists to catch. The
    mapping holds the class object wherever it is built, so patching the method
    survives the move.
    """
    import jobspy
    from jobspy.indeed import Indeed
    from jobspy.linkedin import LinkedIn
    from jobspy.model import JobResponse

    scraped = []

    def stub(self, scraper_input):
        scraped.append(type(self).__name__)
        return JobResponse(jobs=[])

    error = None
    with pytest.MonkeyPatch().context() as m:
        m.setattr(Indeed, "scrape", stub)
        m.setattr(LinkedIn, "scrape", stub)
        try:
            jobspy.scrape_jobs(site_name=["indeed", "linkedin"],
                               search_term="nurse", easy_apply="maybe")
        except Exception as e:  # noqa: BLE001 — classified below, not handled
            error = e

    # Checked before the exception type, because it is the reading that matters
    # and it carries the actionable message.
    assert scraped == [], (
        f"jobspy dispatched to {scraped} before rejecting the value — validation "
        "may now be per board, in which case an unreadable option no longer "
        "costs every board and resolve_pass_sites should degrade per board too."
    )
    assert isinstance(error, ValidationError), (
        f"expected a pydantic ValidationError up front, got {error!r}"
    )


def test_mutex_groups_lists_exactly_the_boards_that_drop_filters():
    """Ties the two readings above back to the constant they justify."""
    assert set(MUTEX_GROUPS) == {"indeed"}


def test_indeeds_groups_match_the_precedence_chain():
    """And ties the partition, which is the load-bearing half.

    Keys alone would let `[("hours_old", "job_type", "is_remote"), ("easy_apply",)]`
    pass every test in this file: the contract tests exercise jobspy, and the
    key check inspects only the dict. This asserts the tuples themselves are the
    branches of `_build_filters` — one group per branch, in precedence order.
    """
    _, groups = MUTEX_GROUPS["indeed"]
    assert [tuple(g) for g in groups] == [
        ("hours_old",),            # if self.scraper_input.hours_old:
        ("job_type", "is_remote"),  # elif job_type or is_remote:  (one branch)
        ("easy_apply",),           # elif easy_apply:
    ], (
        "MUTEX_GROUPS['indeed'] no longer mirrors _build_filters' branches. Each "
        "branch is one group; options sharing a branch share a group."
    )
