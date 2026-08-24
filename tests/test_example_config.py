"""Guards on config/search.example.yml — the config the repo hands new users.

setup.ps1/setup.sh copy this file to config/search.yml, so it is the first (and
for many users the only) search config a run ever sees. It hand-mirrors
SUPPORTED_SITES in three separate `sites:` blocks, the way setup-profile.mjs
and onboard.html do (both guarded in tests/test_app_onboard.py); without a
check of its own, retiring a board leaves the example shipping the dead one and
every first run printing "dropping unsupported sites" against a config the repo
itself wrote.
"""

from pathlib import Path

import pytest
import yaml

from pipeline.sites import SUPPORTED_SITES, limitation_conflict, partition_sites

EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "search.example.yml"


@pytest.fixture(scope="module")
def passes() -> list[dict]:
    """The example config's search passes, read the way scrape.load_searches does."""
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    found = raw.get("searches") or ([raw["search"]] if "search" in raw else [])
    assert found, "no search passes parsed out of search.example.yml"
    return found


class TestExampleSites:
    def test_every_pass_lists_exactly_the_supported_boards(self, passes):
        # Per pass, not just across the file: the union alone would stay green
        # when a newly supported board is added to the first block and the other
        # two are forgotten, which is the likely way this file gets half-updated.
        for cfg in passes:
            kept, dropped = partition_sites(cfg.get("sites"))
            label = cfg.get("name") or "<unnamed pass>"
            assert not dropped, \
                f"[{label}] names boards the pipeline strips at load time: {dropped}"
            assert set(kept) == set(SUPPORTED_SITES), \
                f"[{label}] lists {sorted(kept)}; supported boards are {sorted(SUPPORTED_SITES)}"


class TestExamplePassesAreRunnable:
    """The three passes exist to demonstrate working around JobSpy's mutex rule.
    A pass that breaks it is skipped with a warning at scrape time, so shipping
    one would hand the user a search that silently never runs."""

    def test_no_pass_breaks_the_mutex_rule(self, passes):
        conflicts = [c for c in (limitation_conflict(cfg) for cfg in passes) if c]
        assert not conflicts, "\n".join(conflicts)
