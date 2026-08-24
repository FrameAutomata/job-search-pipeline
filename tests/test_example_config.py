"""Guard on config/search.example.yml — the config the repo hands new users.

setup.ps1/setup.sh copy this file to config/search.yml, so it is the first (and
for many users the only) search config a run ever sees. Its three `sites:`
blocks hand-mirror SUPPORTED_SITES the way setup-profile.mjs and onboard.html
do (both guarded in tests/test_app_onboard.py); without a check of its own,
retiring a board leaves the example shipping the dead one, and every first run
prints "dropping unsupported sites" against a config the repo itself wrote.
"""
from pathlib import Path

import pytest

from pipeline.scrape import load_searches
from pipeline.sites import SUPPORTED_SITES, limitation_conflict, resolve_sites

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "search.example.yml"


@pytest.fixture
def passes() -> list[dict]:
    """The example config's search passes, read by the loader a run itself uses."""
    found = load_searches(EXAMPLE)
    assert found, "no search passes parsed out of search.example.yml"
    return found


def test_every_pass_scrapes_exactly_the_supported_boards(passes):
    # Per pass, not just across the file: a union over the three blocks would
    # stay green when a newly supported board reaches the first one and the
    # other two are forgotten, which is how this file gets half-updated.
    for cfg in passes:
        kept, dropped = resolve_sites(cfg)
        label = cfg.get("name") or "<unnamed pass>"
        assert not dropped, \
            f"[{label}] names boards the pipeline strips at load time: {dropped}"
        assert set(kept) == set(SUPPORTED_SITES), \
            f"[{label}] scrapes {sorted(kept)}; supported boards are {sorted(SUPPORTED_SITES)}"


def test_no_pass_breaks_the_mutex_rule(passes):
    """The three passes exist to demonstrate working around JobSpy's mutex rule.
    A pass that breaks it is skipped with a warning at scrape time, so shipping
    one would hand the user a search that silently never runs."""
    conflicts = [c for c in (limitation_conflict(cfg) for cfg in passes) if c]
    assert not conflicts, "\n".join(conflicts)
