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
    return _merge(tmp_path_factory.mktemp("merge"), TRACKER, {"123.tsv": ADDITION})


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
    _, career_ops, additions, r = _merge(
        tmp_path_factory.mktemp("refused"), REFUSED_TRACKER,
        {"7.tsv": REFUSED_ADDITION},
        batch_state="id\tx\tstatus\ty\tz\treport\n7\t-\tfailed\t-\t-\t229\n")
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


# The #154 pair, as the pipeline now emits it: a req id leading the Notes cell.
# Both titles fuzzy-match (role-matcher drops tokens of ≤3 chars, so the level
# never participates), so the id is the only thing keeping them apart.
def _row(num, role, report, req, score="4.7/5"):
    return (f"| {num} | 2026-08-01 | UT Southwestern Medical Center | {role} | {score} "
            f"| Evaluated | ❌ | [{report}](../reports/{report}-ut.md) "
            f"| req {req} — https://indeed.com/viewjob?jk=old — CONSIDER |\n")


LEVELLED_TRACKER = ("# Applications Tracker\n\n"
                    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
                    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
                    + _row(10, "INSURANCE SPECIALIST II", 200, "5001", score="4.0/5"))


def _addition(role, report, req):
    return ("11\t2026-09-01\tUT Southwestern Medical Center\t" + role +
            "\tEvaluated\t4.7/5\tnull\t"
            f"[{report}](reports/{report}-ut.md)\t"
            f"req {req} — https://indeed.com/viewjob?jk=new — APPLY\n")


@pytest.fixture(scope="module")
def levelled_apart(tmp_path_factory):
    """Two levels of one title, carrying DIFFERENT req ids."""
    return _merge(tmp_path_factory.mktemp("apart"), LEVELLED_TRACKER,
                  {"123.tsv": _addition("INSURANCE SPECIALIST I", 229, "5002")})


@pytest.fixture(scope="module")
def same_req(tmp_path_factory):
    """A re-evaluation of the SAME requisition — same id, same title."""
    return _merge(tmp_path_factory.mktemp("same"), LEVELLED_TRACKER,
                  {"123.tsv": _addition("INSURANCE SPECIALIST II", 229, "5001")})


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
        """The safety direction. An employer's req id is stable across re-posts,
        which is why it — and not the board's per-posting `jk=` key — is what we
        extract: keying on the posting would add a row every time a listing is
        re-published."""
        tracker, _, _, r = same_req
        assert tracker.count("| INSURANCE SPECIALIST II |") == 1
        assert "🔄 Update" in r.stdout and "➕ Add" not in r.stdout

    def test_the_id_survives_into_the_new_row(self, levelled_apart):
        """The split above only holds for the NEXT merge if the id merge-tracker
        wrote into the row is still readable there — the guard needs it on both
        sides, and the row it just added is one of them."""
        tracker, _, _, _ = levelled_apart
        added = [l for l in tracker.splitlines() if "INSURANCE SPECIALIST I |" in l]
        assert added and "req 5002" in added[0]
