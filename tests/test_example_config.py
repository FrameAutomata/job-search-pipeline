"""Guard on config/search.example.yml — the config the repo hands new users.

setup.ps1/setup.sh copy this file to config/search.yml, so it is the first (and
for many users the only) search config a run ever sees. Its `sites:` blocks —
one per pass — hand-mirror SUPPORTED_SITES the way setup-profile.mjs and
onboard.html do (both guarded in tests/test_app_onboard.py); without a check of
its own, retiring a board leaves the example shipping the dead one, and every
first run prints "dropping unsupported sites" against a config the repo wrote.
"""
import re
from pathlib import Path

import pytest

from pipeline.scrape import load_searches
from pipeline.sites import MUTEX_GROUPS, SUPPORTED_SITES, limitation_conflict, resolve_sites

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
    that breaks it loses the offending board at scrape time — and, being
    `[indeed, linkedin]` like every pass here, loses the Indeed half of a search
    the user believes they configured. A single-board one would be skipped
    outright. Either way, shipping it teaches the wrong shape."""
    conflicts = [c for c in (limitation_conflict(cfg) for cfg in passes) if c]
    assert not conflicts, "\n".join(conflicts)


def test_limitation_headings_match_the_boards_that_have_a_rule():
    """The example config's prose is a mirror of MUTEX_GROUPS; guard it.

    `test_no_pass_breaks_the_mutex_rule` runs the rule over the parsed passes,
    so the YAML keys are covered — but the "⚠ <BOARD> LIMITATION" blocks a user
    actually reads are invisible to it. That is exactly how a retired LinkedIn
    rule survived in this file after the code stopped enforcing it (#115), in a
    file setup copies to config/search.yml, where it contradicted a comment
    twenty lines below.

    Same treatment SUPPORTED_SITES already gets for its three hand-mirrors.
    """
    # [\w ]+ rather than \w+: a multi-word display label ("Google Jobs") would
    # otherwise fail this assertion with a message blaming the config.
    headings = set(re.findall(r"⚠ ([\w ]+?) LIMITATION", EXAMPLE.read_text(encoding="utf-8")))
    expected = {label.upper() for label, _ in MUTEX_GROUPS.values()}
    assert headings == expected, (
        f"config/search.example.yml documents limitations for {sorted(headings)}, "
        f"but MUTEX_GROUPS enforces them for {sorted(expected)}."
    )


# Words that mark a sentence as describing a mutual-exclusion rule.
_MUTEX_WORDS = ("exclusiv", "limitation", "mutually", "only one")


def _comment_sentences(path) -> list[str]:
    """The example config's comment prose, unwrapped, split into sentences.

    Comments are joined before splitting because the statements that matter wrap
    across lines — "Indeed/LinkedIn\n# group-exclusivity constraints" is one
    claim, and a line-at-a-time scan sees neither half of it.
    """
    text = " ".join(
        line.split("#", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()
        if "#" in line
    )
    return re.split(r"(?<=[.:])\s+", " ".join(text.split()))


def test_no_retired_board_is_described_as_having_a_mutex_rule():
    """The prose guard the heading check should have been.

    Matching only "⚠ <BOARD> LIMITATION" catches the headline block and nothing
    else — which is how two plain-prose statements of the retired LinkedIn rule
    ("Use multiple passes to work around Indeed/LinkedIn group-exclusivity
    constraints") survived in this file *after* the headline was fixed, in the
    config setup copies to search.yml. A user reading them splits a LinkedIn
    pass to dodge a constraint that no longer exists.

    So: a board with no entry in MUTEX_GROUPS must not be named in the same
    sentence as an exclusivity word.
    """
    unruled = {b for b in SUPPORTED_SITES if b not in MUTEX_GROUPS}
    assert unruled, "every supported board has a mutex rule — this guard is inert"

    offenders = [
        f"{board}: {sentence.strip()[:90]}"
        for sentence in _comment_sentences(EXAMPLE)
        if any(w in sentence.lower() for w in _MUTEX_WORDS)
        for board in unruled
        if board in sentence.lower()
    ]
    assert not offenders, (
        "config/search.example.yml describes a mutual-exclusion rule for a board "
        f"that has none in MUTEX_GROUPS: {offenders}"
    )


class TestCareerOpsInstallFlags:
    """career-ops ships a `postinstall` that runs
    `npx playwright install chromium --with-deps`. Every place we install its
    node deps must pass --ignore-scripts: setup installs Chromium deliberately
    on its own terms (the Nix path needs a pinned driver, not whatever
    postinstall fetches, and `--with-deps` shells out to the system package
    manager, which needs root and aborts setup under `set -euo pipefail`), and
    the daily workflow only ever runs merge-tracker.mjs, so it would pay minutes
    of runner time for a browser nothing there launches.

    Guards the rule rather than the copies, in the spirit of tests/test_stdio.py
    — the flag lives in three host languages with no shared install step, so
    nothing stops a fourth site being added by pasting an older line."""

    # How far back to look for the career-ops context. setup.sh names the
    # directory on the `npm install` line itself; setup.ps1 sets it with a
    # Push-Location several lines earlier, so a line-local match sees only two
    # of the three sites — which is exactly the silent gap this guards.
    _CONTEXT_LINES = 6

    def _career_ops_installs(self, text):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "npm install" not in line or line.lstrip().startswith("#"):
                continue
            # Whichever directory was named MOST RECENTLY wins — setup.sh
            # installs the pipeline's own deps a few lines after career-ops',
            # and a plain "is career-ops anywhere above" test claims that one too.
            for prev in reversed(lines[max(0, i - self._CONTEXT_LINES):i + 1]):
                low = prev.lower()
                if "careerops" in low or "career-ops" in low:
                    yield i + 1, line.strip()
                    break
                if "$root" in low or "pop-location" in low:
                    break

    def test_career_ops_installs_ignore_scripts(self):
        root = Path(__file__).resolve().parent.parent
        sources = [root / "setup.sh", root / "setup.ps1"]
        sources += sorted((root / ".github" / "workflows").glob("*.yml"))
        seen, offenders = 0, []
        for path in sources:
            for lineno, line in self._career_ops_installs(path.read_text(encoding="utf-8")):
                seen += 1
                if "--ignore-scripts" not in line:
                    offenders.append(f"{path.name}:{lineno}: {line}")
        # A guard that matches nothing would pass forever.
        assert seen >= 3, f"expected to find the career-ops installs, saw {seen}"
        assert not offenders, (
            "npm install targeting career-ops without --ignore-scripts:\n  "
            + "\n  ".join(offenders)
        )

    def test_guard_catches_a_missing_flag(self):
        """The guard is only worth having if it fails on the thing it forbids."""
        bad = 'Push-Location $careerOps\nnpm install\nPop-Location\n'
        assert [l for _, l in self._career_ops_installs(bad) if "--ignore-scripts" not in l]
