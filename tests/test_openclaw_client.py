"""Contract for the OpenClaw browser client (Phase 4b).

OpenClawBrowser turns FillActions into the CLI verb sequences proven live on
Greenhouse. Tests script the CLI through an injected runner that records every
call and replays canned aria output — no subprocess, no browser.
"""

import pytest

from pipeline.ats_fill import SELECT, TEXT, TYPEAHEAD, UPLOAD, FillAction
from pipeline.openclaw_client import OpenClawBrowser

SNAP_BASIC = '- form [ref=e0]:\n  - textbox "First Name" [ref=e1]\n'

# a typeahead dropdown after typing "Dallas": right answer is NOT first
SNAP_OPTIONS = (
    "- form [ref=e0]:\n"
    "  - listbox [ref=e9]:\n"
    '    - option "Dallas, Oregon, United States" [ref=e10]\n'
    '    - option "Dallas, Texas, United States" [ref=e11]\n'
)


class FakeRunner:
    """Records CLI calls; pops scripted stdout per call (default '')."""

    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = list(outputs or [])

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        return self.outputs.pop(0) if self.outputs else ""

    def verbs(self):
        return [c[0] for c in self.calls]


def browser(runner, **kw):
    kw.setdefault("sleep", lambda s: None)
    return OpenClawBrowser("chrome", runner, **kw)


def act(runner, widget, value, ref="e1", **kw):
    b = browser(runner, **kw)
    b.act(FillAction(ref, widget, value, "Label", "key"))
    return b


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_every_call_carries_the_browser_profile():
    r = FakeRunner([SNAP_BASIC])
    browser(r).snapshot()
    assert all("--browser-profile" in c and "chrome" in c for c in r.calls)


def test_snapshot_parses_cli_output():
    r = FakeRunner([SNAP_BASIC])
    idx = browser(r).snapshot()
    assert r.calls[0][0] == "snapshot"
    assert idx.find("textbox", "First Name").ref == "e1"


def _form_with_submit(ref):
    return f'- form [ref=e0]:\n  - button "Submit application" [ref={ref}]\n'


def test_open_navigates_then_settles_until_refs_stable():
    # the form re-mints refs while loading (e1→e2), then stabilizes (e2==e2);
    # open() must not return until it stops moving, so nobody plans stale refs
    # first output is consumed by the `open` call; then the settle snapshots
    r = FakeRunner(["", _form_with_submit("e1"), _form_with_submit("e2"), _form_with_submit("e2")])
    browser(r).open("https://job-boards.greenhouse.io/x/jobs/1")
    assert r.calls[0][:2] == ["open", "https://job-boards.greenhouse.io/x/jobs/1"]
    assert r.verbs() == ["open", "snapshot", "snapshot", "snapshot"]  # settled on the 3rd


def test_settle_gives_up_after_its_budget_on_a_formless_page():
    # no submit button ever appears → don't hang; fall through after the budget
    r = FakeRunner(["", *([SNAP_BASIC] * 10)])
    browser(r, settle_tries=3).open("https://x/jobs/1")
    assert r.verbs() == ["open", "snapshot", "snapshot", "snapshot"]


# ── TEXT ─────────────────────────────────────────────────────────────────────

def test_text_types_into_ref():
    r = FakeRunner()
    act(r, TEXT, "Thomas")
    assert r.calls[0][:3] == ["type", "e1", "Thomas"]


# ── SELECT (react-select: click → type option → Enter) ───────────────────────

def test_select_recipe_click_type_enter():
    r = FakeRunner()
    act(r, SELECT, "Yes")
    assert r.verbs() == ["click", "type", "press"]
    assert r.calls[0][1] == "e1"
    assert r.calls[1][1:3] == ["e1", "Yes"]
    assert r.calls[2][1] == "Enter"


# ── TYPEAHEAD (click → type → wait for options → click the MATCHING one) ─────

def test_typeahead_clicks_matching_option_not_first():
    r = FakeRunner(["", "", SNAP_OPTIONS])  # click, type, then options snapshot
    act(r, TYPEAHEAD, "Dallas, Texas")
    assert r.verbs() == ["click", "type", "snapshot", "click"]
    assert r.calls[-1][1] == "e11"  # Dallas TEXAS, not first-listed Oregon


def test_typeahead_retries_snapshot_until_options_render():
    # async fetch: first post-type snapshot has no options yet
    r = FakeRunner(["", "", SNAP_BASIC, SNAP_OPTIONS])
    act(r, TYPEAHEAD, "Dallas, Texas", typeahead_tries=3)
    assert r.verbs() == ["click", "type", "snapshot", "snapshot", "click"]


def test_typeahead_no_matching_option_raises():
    r = FakeRunner(["", "", SNAP_OPTIONS, SNAP_OPTIONS, SNAP_OPTIONS])
    with pytest.raises(RuntimeError):
        act(r, TYPEAHEAD, "Austin, Texas", typeahead_tries=3)


# ── UPLOAD (stage → arm → click Attach) ──────────────────────────────────────

def test_upload_stages_arms_and_clicks(tmp_path):
    src = tmp_path / "Thomas_Standard.pdf"
    src.write_bytes(b"%PDF")
    inbound = tmp_path / "inbound"
    r = FakeRunner()
    act(r, UPLOAD, str(src), ref="e9", inbound_dir=inbound)
    assert (inbound / "Thomas_Standard.pdf").exists()
    assert r.calls[0][:2] == ["upload", "media://inbound/Thomas_Standard.pdf"]
    assert r.calls[1][:2] == ["click", "e9"]


def test_upload_missing_file_raises(tmp_path):
    r = FakeRunner()
    with pytest.raises(RuntimeError):
        act(r, UPLOAD, str(tmp_path / "nope.pdf"), inbound_dir=tmp_path / "in")
    assert r.calls == []  # nothing armed for a file that doesn't exist


# ── failure semantics ────────────────────────────────────────────────────────

def test_unknown_widget_raises():
    with pytest.raises(RuntimeError):
        act(FakeRunner(), "hologram", "x")


def test_runner_failure_propagates():
    def boom(args, **kw):
        raise RuntimeError("stale ref")

    b = OpenClawBrowser("chrome", boom, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        b.act(FillAction("e1", TEXT, "x", "Label", "key"))
