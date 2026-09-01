"""Contract test against the installed career-ops `merge-tracker.mjs`.

`_report_key` in [pipeline/_batch_common.py] is a claim about ONE line of that
script: on its guessed match tiers — entry number, fuzzy company+role — a merge
updates the existing row but keeps THAT ROW'S role title, while writing the
addition's report link and score through.

    role: (reportNumMatched || dupReason === 'url') ? addition.role : duplicate.role

The loss guard is built on that reading. If upstream flips it, or adds a tier
that doesn't carry the report number through, the guard either goes back to
reporting intact evaluations as lost (#152, the bug this exists to remove) or —
worse — starts scoring a genuinely lost row as landed. Unit fixtures cannot
catch either: a fixture that simulates the merge by moving files is a third copy
of the behaviour, and it stays green while the real script changes underneath.
So this drives the real one, the way `tests/test_app_onboard.py` drives the real
`setup-profile.mjs` and `tests/test_jobspy_contract.py` reads the installed
library.

Local-only, on the same terms as that round-trip: career-ops is cloned by setup
and `npm install`ed, neither of which CI does. Read-only with respect to the
checkout — the tracker, the additions dir and batch-state all live in tmp_path
via merge-tracker's own env overrides.
"""

import os
import shutil
import subprocess

import pytest

from pipeline._batch_common import _pending_additions, _warn_on_lost_additions
from pipeline.tracker_layout import career_ops_dir

HEADER = ("# Applications Tracker\n\n"
          "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
          "|---|------|---------|------|-------|--------|-----|--------|-------|\n")

# One scenario, used with and without req ids: two levels of one title at one
# company. Both fuzzy-match, because role-matcher drops tokens of ≤3 characters
# and the level never reaches the comparison — so the id is the only thing that
# can tell them apart. Single-sourced deliberately: if upstream ever tightens
# that matcher, this premise must fail in ONE place, not rot in a second copy
# while the first is repaired.
def _notes(req, jk):
    return (f"req {req} — " if req else "") + f"https://indeed.com/viewjob?jk={jk} — APPLY"


def _tracker(role, report, req=None):
    return HEADER + (f"| 10 | 2026-08-01 | UT Southwestern Medical Center | {role} "
                     f"| 4.0/5 | Evaluated | ❌ | [{report}](../reports/{report}-ut.md) "
                     f"| {_notes(req, 'old')} |\n")


def _addition(role, report, req=None):
    return ("11\t2026-09-01\tUT Southwestern Medical Center\t" + role +
            f"\tEvaluated\t4.7/5\tnull\t[{report}](reports/{report}-ut.md)\t"
            f"{_notes(req, 'new')}\n")


def _merge_tracker_runnable() -> bool:
    """True only if node, the career-ops checkout, and the npm packages
    merge-tracker imports all resolve."""
    career_ops = career_ops_dir()
    if shutil.which("node") is None or not (career_ops / "merge-tracker.mjs").exists():
        return False
    try:
        r = subprocess.run(["node", "-e", "require.resolve('js-yaml')"],
                           cwd=str(career_ops), capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        # Broad on purpose: this runs at MODULE SCOPE to build `pytestmark`, so
        # anything it raises — a TimeoutExpired from a wedged node, not an
        # OSError — is a collection error that fails the whole suite instead of
        # skipping this one file. Same reason `test_app_onboard.py` is broad.
        return False


pytestmark = pytest.mark.skipif(
    not _merge_tracker_runnable(),
    reason="needs a career-ops checkout with npm deps (local install; CI does neither)")


def _merge(tmp_path, tracker_text, additions, batch_state=""):
    """Run the real merge-tracker over `additions` ({filename: row}), entirely
    inside tmp_path. Returns (tracker_text_after, career_ops_root, additions_dir,
    completed_process)."""
    career_ops = tmp_path / "co"
    (career_ops / "data").mkdir(parents=True)
    tracker = career_ops / "data" / "applications.md"
    tracker.write_text(tracker_text, encoding="utf-8")
    additions_dir = tmp_path / "adds"
    additions_dir.mkdir()
    for name, row in additions.items():
        (additions_dir / name).write_text(row, encoding="utf-8")
    state = tmp_path / "batch-state.tsv"
    state.write_text(batch_state, encoding="utf-8")

    r = subprocess.run(
        ["node", "merge-tracker.mjs"], cwd=str(career_ops_dir()),
        capture_output=True, text=True, timeout=120,
        # The parent environment PLUS the overrides, never a hand-built dict:
        # node needs SystemRoot/COMSPEC/TEMP to start at all on Windows, which is
        # this repo's primary platform, and a stripped env would turn a
        # local-only test into a hard failure rather than the skip above.
        env={**os.environ,
             "CAREER_OPS_TRACKER": str(tracker),
             "CAREER_OPS_ADDITIONS": str(additions_dir),
             "CAREER_OPS_BATCH_STATE": str(state)},
    )
    assert r.returncode == 0, r.stderr
    return tracker.read_text(encoding="utf-8"), career_ops, additions_dir, r


@pytest.fixture(scope="module")
def merged(tmp_path_factory):
    """One fuzzy-matching addition, no req ids on either side.

    Module-scoped: merge-tracker shells out to sync-pdf-flags.mjs, so each run is
    two node startups plus a tracker-lock acquisition, and every assertion below
    is read-only over one immutable result."""
    return _merge(tmp_path_factory.mktemp("merge"),
                  _tracker("INSURANCE SPECIALIST II", 200),
                  {"123.tsv": _addition("INSURANCE SPECIALIST I", 229)})


class TestFuzzyMergeKeepsTheRowsTitle:
    def test_role_title_is_the_existing_rows_not_the_additions(self, merged):
        """The reading `_report_key` exists for. If this fails, upstream has
        changed which tier may rewrite a title — re-read merge-tracker's
        `reportNumMatched || dupReason === 'url'` ternary before touching the
        guard, and do not relax the test."""
        tracker, _, _, _ = merged
        assert "INSURANCE SPECIALIST II" in tracker
        assert "INSURANCE SPECIALIST I |" not in tracker

    def test_report_and_score_do_write_through(self, merged):
        """The other half: the addition's report number reaches the row, which
        is what makes company + report a usable identity for it."""
        tracker, _, _, _ = merged
        assert "[229]" in tracker and "4.7/5" in tracker
        assert "[200]" not in tracker

    def test_the_tsv_is_archived_so_nothing_retries_it(self, merged):
        """Why a wrong answer here is permanent rather than self-healing."""
        _, _, additions, _ = merged
        assert not list(additions.glob("*.tsv"))
        assert (additions / "merged" / "123.tsv").exists()

    def test_guard_reports_the_retitle_not_a_loss(self, merged, capsys):
        """End to end over the real merge: the #152 run reported three intact
        evaluations as lost. It must now say what actually happened."""
        _, career_ops, additions, _ = merged
        before = _pending_additions(additions / "merged")
        _warn_on_lost_additions(before, career_ops, additions)
        out = capsys.readouterr().out
        assert "WARNING" not in out
        assert "INSURANCE SPECIALIST I" in out and "INSURANCE SPECIALIST II" in out


# A second run: an addition merge-tracker REFUSES (its report number is marked
# `failed` in batch-state.tsv) and archives into merged/ anyway, exit 0. This is
# the loss the guard exists to report, and the reason line is the operator's only
# clue about it.
REFUSED_TRACKER = (HEADER + "| 10 | 2026-08-01 | Initech | SRE | 4.0/5 | Evaluated | ❌ "
                   "| [200](../reports/200-initech-2026-08-01.md) | note |\n")
REFUSED_ADDITION = ("11\t2026-09-01\tAcme Corp\tPlatform Engineer\tEvaluated\t4.7/5\tnull\t"
                    "[229](reports/229-acme-2026-09-01.md)\tAPPLY — note\n")


@pytest.fixture(scope="module")
def refused(tmp_path_factory):
    """A refused row, in `_merge`'s own (tracker, career_ops, additions, proc)
    ordering — a second ordering of one value set is a trap for the next test."""
    return _merge(
        tmp_path_factory.mktemp("refused"), REFUSED_TRACKER,
        {"7.tsv": REFUSED_ADDITION},
        batch_state="id\tx\tstatus\ty\tz\treport\n7\t-\tfailed\t-\t-\t229\n")


class TestRefusalReasonsReachTheOperator:
    """`run_merge_tracker` passes stdout AND stderr to the guard. This is why."""

    def test_a_refusal_is_reported_on_stderr_not_stdout(self, refused):
        """merge-tracker refuses with `console.warn`. Passing only `r.stdout`
        (as this did until #152) left the reasons block empty on every genuine
        loss, while the one refusal it logs to stdout is the deliberately BENIGN
        unscoreable-re-eval case — so the block could only ever print a reason
        belonging to an addition that was not lost.

        If a future release moves this to stdout, this failing is the
        notification; `run_merge_tracker` already reads both, so relax the
        stdout half rather than the combined one."""
        _, _, _, r = refused
        assert "Skipping" in r.stderr
        assert "Skipping" not in r.stdout

    def test_the_guard_prints_that_reason_end_to_end(self, refused, capsys):
        """The wiring, over the real streams: warn about the loss and say why."""
        _, career_ops, additions, r = refused
        before = _pending_additions(additions / "merged")
        _warn_on_lost_additions(before, career_ops, additions, f"{r.stdout}\n{r.stderr}")
        out = capsys.readouterr().out
        assert "WARNING: 1 evaluation(s)" in out
        assert "7.tsv (Acme Corp — Platform Engineer)" in out
        assert "merge-tracker's reasons:" in out and 'marked "failed"' in out


@pytest.fixture(scope="module")
def levelled_apart(tmp_path_factory):
    """Two levels of one title, carrying DIFFERENT req ids."""
    return _merge(tmp_path_factory.mktemp("apart"),
                  _tracker("INSURANCE SPECIALIST II", 200, req="5001"),
                  {"123.tsv": _addition("INSURANCE SPECIALIST I", 229, req="5002")})


@pytest.fixture(scope="module")
def same_req(tmp_path_factory):
    """One requisition re-posted under a re-worded title, same id.

    The title has to DIFFER, or the scenario tests nothing: `roleFuzzyMatch`
    short-circuits on `textA === textB` before tokenizing, and bridge drops an
    identically-titled addition long before the merge — so a same-title pair can
    neither reach the fuzzy tier nor occur in production. `(Remote)` is a wording
    a re-post really picks up, and it still fuzzy-matches, so the merge reaches
    the req comparison and the matching id has to let the fold proceed."""
    return _merge(tmp_path_factory.mktemp("same"),
                  _tracker("INSURANCE SPECIALIST II", 200, req="5001"),
                  {"123.tsv": _addition("Insurance Specialist II (Remote)", 229, req="5001")})


class TestReqIdOverridesTheFuzzyTitleMatch:
    """What `extract_req_id` buys, and what it must not cost. Both directions
    are load-bearing: the id has to split what the title match wrongly folds,
    without splitting a re-post of one requisition into a second row."""

    def test_different_ids_keep_two_rows(self, levelled_apart):
        """The #152/#154 case. Without the ids these fold, and `SPECIALIST I`
        stops existing — invisible to dedup, the handoff and the UI."""
        tracker, _, _, r = levelled_apart
        assert "INSURANCE SPECIALIST I |" in tracker
        assert "INSURANCE SPECIALIST II |" in tracker
        assert "➕ Add" in r.stdout and "🔄 Update" not in r.stdout

    def test_the_same_id_still_folds(self, same_req):
        """The safety direction, and the one that has to survive a re-wording.
        An employer's req id is stable across re-posts, which is why it — and not
        the board's per-posting `jk=` key — is what we extract: keying on the
        posting would add a row every time a listing is re-published."""
        tracker, _, _, r = same_req
        assert tracker.count("| INSURANCE SPECIALIST II |") == 1   # kept its title
        assert "Insurance Specialist II (Remote)" not in tracker   # no second row
        assert "🔄 Update" in r.stdout and "➕ Add" not in r.stdout

    def test_the_id_survives_into_the_new_row(self, levelled_apart):
        """The split above only holds for the NEXT merge if the id merge-tracker
        wrote into the row is still readable there — the guard needs it on both
        sides, and the row it just added is one of them."""
        tracker, _, _, _ = levelled_apart
        added = [l for l in tracker.splitlines() if "INSURANCE SPECIALIST I |" in l]
        assert added and "req 5002" in added[0]


# A row shaped the way an agent CLI writes one on the `--batch` path: the score
# cell as "4.2" rather than "4.2/5". Nothing in Python touched it, because
# career-ops' batch-runner.sh owns tracker-additions/ on that path.
CLI_ROW = ("11\t2026-09-01\tAcme Corp\tPlatform Engineer\tEvaluated\t{score}\tnull\t"
           "[229](reports/229-acme.md)\tAPPLY strong match\n")


@pytest.fixture(scope="module")
def unsanitized(tmp_path_factory):
    return _merge(tmp_path_factory.mktemp("raw"), HEADER,
                  {"7.tsv": CLI_ROW.format(score="4.2")})


@pytest.fixture(scope="module")
def sanitized(tmp_path_factory):
    from pipeline._batch_common import sanitize_addition
    return _merge(tmp_path_factory.mktemp("fixed"), HEADER,
                  {"7.tsv": sanitize_addition(CLI_ROW.format(score="4.2"),
                                              "https://x/j/7", "Job ID: 88214") + "\n"})


class TestUnsanitizedRowsAreRefused:
    """Why `_sanitize_pending_additions` exists, proven against the real script
    rather than asserted. If upstream ever starts accepting a bare `4.2`, the
    first test fails — and that is the notification, not a reason to relax it."""

    def test_a_bare_score_gets_the_row_refused_and_archived(self, unsanitized):
        """Refused, archived into merged/, exit 0 — so nothing ever retries it.
        That is the permanent loss the `--batch` path was exposed to."""
        tracker, _, additions, r = unsanitized
        assert "Platform Engineer" not in tracker            # never reached the tracker
        assert "Skipping" in r.stderr
        assert (additions / "merged" / "7.tsv").exists()      # gone from the queue
        assert r.returncode == 0                             # and no error to notice

    def test_the_same_row_sanitized_merges(self, sanitized):
        tracker, _, _, r = sanitized
        assert "Platform Engineer" in tracker and "4.2/5" in tracker
        assert "➕ Add" in r.stdout

    def test_and_carries_its_url_and_req_id(self, sanitized):
        """The other two things that path lost: the UI's "Open posting" target,
        and the id that keeps two levels of one title apart."""
        tracker, _, _, _ = sanitized
        assert "https://x/j/7" in tracker and "req 88214" in tracker
