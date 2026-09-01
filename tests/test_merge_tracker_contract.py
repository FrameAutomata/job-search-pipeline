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

TRACKER = ("# Applications Tracker\n\n"
           "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
           "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
           "| 10 | 2026-08-01 | UT Southwestern Medical Center | INSURANCE SPECIALIST II "
           "| 4.0/5 | Evaluated | ❌ | [200](../reports/200-ut-2026-08-01.md) | note |\n")

# Same company, a title that fuzzy-matches the row above, a NEW report number.
ADDITION = ("11\t2026-09-01\tUT Southwestern Medical Center\tINSURANCE SPECIALIST I\t"
            "Evaluated\t4.7/5\tnull\t[229](reports/229-ut-2026-09-01.md)\t"
            "https://indeed.com/viewjob?jk=new — APPLY\n")


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


@pytest.fixture(scope="module")
def merged(tmp_path_factory):
    """Run the real merge-tracker over one fuzzy-matching addition, entirely
    inside a tmp dir. Returns (tracker_text, career_ops_root, additions_dir).

    Module-scoped: merge-tracker shells out to sync-pdf-flags.mjs, so each run is
    two node startups plus a tracker-lock acquisition, and every assertion below
    is read-only over one immutable result."""
    tmp_path = tmp_path_factory.mktemp("merge")
    career_ops = tmp_path / "co"
    (career_ops / "data").mkdir(parents=True)
    tracker = career_ops / "data" / "applications.md"
    tracker.write_text(TRACKER, encoding="utf-8")
    additions = tmp_path / "adds"
    additions.mkdir()
    (additions / "123.tsv").write_text(ADDITION, encoding="utf-8")

    r = subprocess.run(
        ["node", "merge-tracker.mjs"], cwd=str(career_ops_dir()),
        capture_output=True, text=True, timeout=120,
        # The parent environment PLUS the overrides, never a hand-built dict:
        # node needs SystemRoot/COMSPEC/TEMP to start at all on Windows, which is
        # this repo's primary platform, and a stripped env would turn a
        # local-only test into a hard failure rather than the skip above.
        env={**os.environ,
             "CAREER_OPS_TRACKER": str(tracker),
             "CAREER_OPS_ADDITIONS": str(additions),
             "CAREER_OPS_BATCH_STATE": str(tmp_path / "batch-state.tsv")},
    )
    assert r.returncode == 0, r.stderr
    return tracker.read_text(encoding="utf-8"), career_ops, additions, r


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
REFUSED_TRACKER = ("# Applications Tracker\n\n"
                   "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
                   "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
                   "| 10 | 2026-08-01 | Initech | SRE | 4.0/5 | Evaluated | ❌ "
                   "| [200](../reports/200-initech-2026-08-01.md) | note |\n")
REFUSED_ADDITION = ("11\t2026-09-01\tAcme Corp\tPlatform Engineer\tEvaluated\t4.7/5\tnull\t"
                    "[229](reports/229-acme-2026-09-01.md)\tAPPLY — note\n")


@pytest.fixture(scope="module")
def refused(tmp_path_factory):
    """(completed_process, career_ops_root, additions_dir) for a refused row."""
    tmp_path = tmp_path_factory.mktemp("refused")
    career_ops = tmp_path / "co"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "data" / "applications.md").write_text(REFUSED_TRACKER, encoding="utf-8")
    additions = tmp_path / "adds"
    additions.mkdir()
    (additions / "7.tsv").write_text(REFUSED_ADDITION, encoding="utf-8")
    state = tmp_path / "batch-state.tsv"
    state.write_text("id\tx\tstatus\ty\tz\treport\n7\t-\tfailed\t-\t-\t229\n", encoding="utf-8")

    r = subprocess.run(
        ["node", "merge-tracker.mjs"], cwd=str(career_ops_dir()),
        capture_output=True, text=True, timeout=120,
        env={**os.environ,
             "CAREER_OPS_TRACKER": str(career_ops / "data" / "applications.md"),
             "CAREER_OPS_ADDITIONS": str(additions),
             "CAREER_OPS_BATCH_STATE": str(state)},
    )
    assert r.returncode == 0, r.stderr
    return r, career_ops, additions


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
        r, _, _ = refused
        assert "Skipping" in r.stderr
        assert "Skipping" not in r.stdout

    def test_the_guard_prints_that_reason_end_to_end(self, refused, capsys):
        """The wiring, over the real streams: warn about the loss and say why."""
        r, career_ops, additions = refused
        before = _pending_additions(additions / "merged")
        _warn_on_lost_additions(before, career_ops, additions, f"{r.stdout}\n{r.stderr}")
        out = capsys.readouterr().out
        assert "WARNING: 1 evaluation(s)" in out
        assert "7.tsv (Acme Corp — Platform Engineer)" in out
        assert "merge-tracker's reasons:" in out and 'marked "failed"' in out
