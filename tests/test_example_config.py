"""Guard on config/search.example.yml — the config the repo hands new users.

setup.ps1/setup.sh copy this file to config/search.yml, so it is the first (and
for many users the only) search config a run ever sees. Its `sites:` blocks —
one per pass — hand-mirror SUPPORTED_SITES the way setup-profile.mjs and
onboard.html do (both guarded in tests/test_app_onboard.py); without a check of
its own, retiring a board leaves the example shipping the dead one, and every
first run prints "dropping unsupported sites" against a config the repo wrote.
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


def _label(cfg: dict) -> str:
    return cfg.get("name") or "<unnamed pass>"


def test_no_pass_names_a_board_the_pipeline_strips(passes):
    for cfg in passes:
        # resolve_sites hands an omitted key the supported set, so without this
        # the guard would certify an example whose board list had been deleted
        # outright. The example teaches the list by showing it.
        assert cfg.get("sites") is not None, \
            f"[{_label(cfg)}] declares no `sites:` — that block is the mirror this guards"
        _, dropped = resolve_sites(cfg)
        assert not dropped, \
            f"[{_label(cfg)}] names boards the pipeline strips at load time: {dropped}"


def test_the_passes_together_show_every_supported_board(passes):
    # Checked over the union rather than per pass: narrowing one pass to a
    # single board is a real thing to demonstrate (LinkedIn's easy-apply filter
    # no longer works, so Pass 3 may well go Indeed-only), but a board added to
    # SUPPORTED_SITES has to surface somewhere in the file a user starts from.
    shown = {s.lower() for cfg in passes for s in resolve_sites(cfg)[0]}
    assert shown == set(SUPPORTED_SITES), \
        f"example passes scrape {sorted(shown)}; supported boards are {sorted(SUPPORTED_SITES)}"


def test_no_pass_breaks_the_mutex_rule(passes):
    """The passes exist to demonstrate working around JobSpy's mutex rule. One
    that breaks it is skipped with a warning at scrape time, so shipping it
    would hand the user a search that silently never runs."""
    conflicts = [c for c in (limitation_conflict(cfg) for cfg in passes) if c]
    assert not conflicts, "\n".join(conflicts)
