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
from types import SimpleNamespace

import pytest

from pipeline._batch_common import (
    _liveness_closed_rows, _pending_additions, _recover_refused_additions,
    _reopen_reposted, _sanitize_pending_additions, _warn_on_lost_additions,
    closed_by_recheck,
    liveness_closed_mark,
)
from pipeline.tracker_layout import career_ops_dir
from tests.conftest import ADDITION_LABEL_LINE, ADDITION_LABELS, tracker_row

HEADER = ("# Applications Tracker\n\n"
          "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
          "|---|------|---------|------|-------|--------|-----|--------|-------|\n")

# One scenario, used with and without req ids: one title at one company, and a
# re-wording of it that still fuzzy-matches — `(Remote)` is a suffix a re-post
# really picks up. NOT two levels of one title, deliberately: that was the
# original premise, and career-ops#3651 (merged 2026-09-02) made a stated level a
# disagreement, so `SPECIALIST I` vs `… II` stopped fuzzy-matching and the three
# tests on this fixture failed by design. This pair matches under both matchers,
# so the suite is honest against a checkout on either side of that release.
# Single-sourced, so if upstream tightens the matcher again the premise fails in
# ONE place rather than rotting in a second copy while the first is repaired.
BASE, VARIANT = "INSURANCE SPECIALIST II", "Insurance Specialist II (Remote)"
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


_RUNNABLE = _merge_tracker_runnable()

pytestmark = pytest.mark.skipif(
    not _RUNNABLE,
    reason="needs a career-ops checkout with npm deps (local install; CI does neither)")


def _stage(tmp_path, tracker_text, additions, batch_state=""):
    """Lay out a tracker, an additions dir ({filename: row}) and a batch-state
    file inside tmp_path. Returns (career_ops_root, additions_dir, batch_state)."""
    career_ops = tmp_path / "co"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "data" / "applications.md").write_text(tracker_text, encoding="utf-8")
    additions_dir = tmp_path / "adds"
    additions_dir.mkdir()
    for name, row in additions.items():
        (additions_dir / name).write_text(row, encoding="utf-8")
    state = tmp_path / "batch-state.tsv"
    state.write_text(batch_state, encoding="utf-8")
    return career_ops, additions_dir, state


def _node_merge(career_ops, additions_dir, state):
    """One run of the real merge-tracker over a `_stage`d layout, which it
    reads and writes through its own env overrides — the checkout is only its
    cwd. Returns the completed process."""
    r = subprocess.run(
        ["node", "merge-tracker.mjs"], cwd=str(career_ops_dir()),
        capture_output=True, text=True, timeout=120,
        # The parent environment PLUS the overrides, never a hand-built dict:
        # node needs SystemRoot/COMSPEC/TEMP to start at all on Windows, which is
        # this repo's primary platform, and a stripped env would turn a
        # local-only test into a hard failure rather than the skip above.
        env={**os.environ,
             "CAREER_OPS_TRACKER": str(career_ops / "data" / "applications.md"),
             "CAREER_OPS_ADDITIONS": str(additions_dir),
             "CAREER_OPS_BATCH_STATE": str(state)},
    )
    assert r.returncode == 0, r.stderr
    return r


def _tracker_after(career_ops):
    return (career_ops / "data" / "applications.md").read_text(encoding="utf-8")


def _merge(tmp_path, tracker_text, additions, batch_state=""):
    """Run the real merge-tracker over `additions` ({filename: row}), entirely
    inside tmp_path. Returns (tracker_text_after, career_ops_root, additions_dir,
    completed_process)."""
    career_ops, additions_dir, state = _stage(tmp_path, tracker_text, additions, batch_state)
    r = _node_merge(career_ops, additions_dir, state)
    return _tracker_after(career_ops), career_ops, additions_dir, r


@pytest.fixture(scope="module")
def merged(tmp_path_factory):
    """One fuzzy-matching addition, no req ids on either side.

    Module-scoped: merge-tracker shells out to sync-pdf-flags.mjs, so each run is
    two node startups plus a tracker-lock acquisition, and every assertion below
    is read-only over one immutable result."""
    return _merge(tmp_path_factory.mktemp("merge"),
                  _tracker(BASE, 200), {"123.tsv": _addition(VARIANT, 229)})


class TestFuzzyMergeKeepsTheRowsTitle:
    def test_role_title_is_the_existing_rows_not_the_additions(self, merged):
        """The reading `_report_key` exists for. If this fails, upstream has
        changed which tier may rewrite a title — re-read merge-tracker's
        `reportNumMatched || dupReason === 'url'` ternary before touching the
        guard, and do not relax the test."""
        tracker, _, _, _ = merged
        assert f"| {BASE} |" in tracker
        assert VARIANT not in tracker

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
        assert VARIANT in out and BASE in out


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
def distinct_reqs(tmp_path_factory):
    """The same fuzzy-matching pair, carrying DIFFERENT req ids — two openings
    that happen to share a title's wording."""
    return _merge(tmp_path_factory.mktemp("apart"),
                  _tracker(BASE, 200, req="5001"),
                  {"123.tsv": _addition(VARIANT, 229, req="5002")})


@pytest.fixture(scope="module")
def same_req(tmp_path_factory):
    """The same pair, carrying the SAME id — one requisition re-posted under a
    re-worded title. The wording has to differ or the scenario tests nothing:
    `roleFuzzyMatch` short-circuits on identical strings, and bridge drops an
    identically-titled addition long before the merge."""
    return _merge(tmp_path_factory.mktemp("same"),
                  _tracker(BASE, 200, req="5001"),
                  {"123.tsv": _addition(VARIANT, 229, req="5001")})


class TestReqIdOverridesTheFuzzyTitleMatch:
    """What `extract_req_id` buys, and what it must not cost. Both directions
    are load-bearing: the id has to split what the title match wrongly folds,
    without splitting a re-post of one requisition into a second row."""

    def test_different_ids_keep_two_rows(self, distinct_reqs):
        """The #154 case. Without the ids these fold, and the second opening
        stops existing — invisible to dedup, the handoff and the UI."""
        tracker, _, _, r = distinct_reqs
        assert f"| {VARIANT} |" in tracker and f"| {BASE} |" in tracker
        assert "➕ Add" in r.stdout and "🔄 Update" not in r.stdout

    def test_the_same_id_still_folds(self, same_req):
        """The safety direction, and the one that has to survive a re-wording.
        An employer's req id is stable across re-posts, which is why it — and not
        the board's per-posting `jk=` key — is what we extract: keying on the
        posting would add a row every time a listing is re-published."""
        tracker, _, _, r = same_req
        assert tracker.count(f"| {BASE} |") == 1       # kept its title
        assert VARIANT not in tracker                   # no second row
        assert "🔄 Update" in r.stdout and "➕ Add" not in r.stdout

    def test_the_id_survives_into_the_new_row(self, distinct_reqs):
        """The split above only holds for the NEXT merge if the id merge-tracker
        wrote into the row is still readable there — the guard needs it on both
        sides, and the row it just added is one of them."""
        tracker, _, _, _ = distinct_reqs
        added = [l for l in tracker.splitlines() if f"| {VARIANT} |" in l]
        assert added and "req 5002" in added[0]


# A row shaped the way an agent CLI writes one on the `--batch` path: the score
# cell as "4.2" rather than "4.2/5". Nothing in Python touched it, because
# career-ops' batch-runner.sh owns tracker-additions/ on that path.
CLI_ROW = ("11\t2026-09-01\tAcme Corp\tPlatform Engineer\tEvaluated\t{score}\tnull\t"
           "[229](reports/229-acme.md)\tAPPLY strong match\n")


def _queue_url(career_ops):
    """A batch-input.tsv queuing job 7 at https://x/j/7 — where the merges this
    repo runs find the URL a queued job's row did not carry."""
    (career_ops / "batch").mkdir(exist_ok=True)
    (career_ops / "batch" / "batch-input.tsv").write_text(
        "id\turl\tsource\tnotes\n7\thttps://x/j/7\tAcme Corp\tPlatform Engineer\n",
        encoding="utf-8")


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


@pytest.fixture(scope="module")
def recovered(tmp_path_factory):
    """The `--batch` case end to end, against the real script: the runner's
    own merge refuses a bare-score row and archives it; `run_merge_tracker` —
    which the wrappers now run after the runner — pulls it back, repairs it,
    and the second merge lands it."""
    tmp_path = tmp_path_factory.mktemp("recover")
    # First merge: exactly what batch-runner.sh's last step does.
    tracker, career_ops, additions, first = _merge(
        tmp_path, HEADER, {"7.tsv": CLI_ROW.format(score="4.2")})
    assert "Platform Engineer" not in tracker           # refused …
    assert (additions / "merged" / "7.tsv").exists()     # … and archived
    # What the wrapper runs next. batch-input.tsv supplies the URL, as it
    # would for a queued job.
    _queue_url(career_ops)
    _recover_refused_additions(career_ops, additions)
    assert (additions / "7.tsv").exists()                # back in the queue
    # Second merge, same tracker and additions dir.
    second = _node_merge(career_ops, additions, tmp_path / "batch-state.tsv")
    return _tracker_after(career_ops), second

class TestRecoveryAfterTheRunnersOwnMerge:
    def test_the_evaluation_lands_on_the_second_merge(self, recovered):
        tracker, second = recovered
        assert "Platform Engineer" in tracker and "4.2/5" in tracker
        assert "➕ Add" in second.stdout

    def test_with_the_url_it_was_missing(self, recovered):
        tracker, _ = recovered
        assert "https://x/j/7" in tracker


# ── Headed additions (career-ops#3706) ───────────────────────────────────────
#
# Since #3706 (merged 2026-09-04) career-ops' batch worker writes a row of column
# LABELS above its one data row, and merge-tracker resolves that file by name.
# Our readers took the two-line file for one malformed row: the loss guard read
# the label line as a job called "role" at a company called "company", found no
# such row in the tracker and warned about an evaluation that had landed intact —
# the #152 false alarm by a new route — and recovery could not put a refused one
# back. So the two scenarios above are re-proven here on the form the worker now
# writes, against the script that reads it.
#
# The label line is `ADDITION_LABEL_LINE` (tests/conftest.py), verbatim from Step 5
# of career-ops' batch/batch-prompt.md, which tells the worker to write it "exactly
# as shown". Every row below is built from it by label, so a relabelling upstream
# is one edit there and the rows follow.
#
# No alias-table pin is needed, unlike the unit tests: merge-tracker and our
# readers both resolve these labels through the same checkout's
# tracker-aliases.json — the pairing a real merge has — and every label in the
# worker's line is in `_FALLBACK_ALIASES` too, so the Python reading is the same
# either way.
def _headed_cells(score):
    """`CLI_ROW`'s cells by label, plus the URL a headed row carries in a column
    of its own — the same job, so each headed scenario below differs from its
    headerless twin only in the form."""
    values = CLI_ROW.format(score=score).rstrip("\n").split("\t") + ["https://x/j/7"]
    assert len(values) == len(ADDITION_LABELS), "a label with no value, or a value with no label"
    return dict(zip(ADDITION_LABELS, values))


def _headed_addition(score):
    """A worker-written headed addition of `_headed_cells`."""
    return f"{ADDITION_LABEL_LINE}\n" + "\t".join(_headed_cells(score).values()) + "\n"


def _merge_tracker_reads_headed() -> bool:
    """True when the checkout's merge-tracker.mjs carries #3706's headed parser.

    A text probe, and it gates a SKIP only — it asserts nothing; the tests below
    ask the running script. A merge-tracker from before #3706 misreads a headed
    file entirely: it takes the label line for the data row and refuses the whole
    file as a score/status swap (`"status" | "score"`), whatever the data row
    holds, so neither case below has a meaning there. Broad `except` for
    `_merge_tracker_runnable`'s reason: this runs at import to build a mark."""
    try:
        return "parseHeadedAddition" in (
            career_ops_dir() / "merge-tracker.mjs").read_text(encoding="utf-8")
    except Exception:
        return False


# `_RUNNABLE and`: pytest reports the NEAREST skip mark's reason, so without it a
# machine with no checkout at all — CI — would be told its checkout is too old.
needs_headed_additions = pytest.mark.skipif(
    _RUNNABLE and not _merge_tracker_reads_headed(),
    reason="this career-ops checkout's merge-tracker.mjs predates headed additions "
           "(career-ops#3706, no parseHeadedAddition) — point CAREER_OPS_PATH at a "
           "newer checkout to run these")


@pytest.fixture(scope="module")
def headed_merged(tmp_path_factory):
    """A headed addition with a readable score. The guard's snapshot is taken
    BEFORE the merge, where `run_merge_tracker` takes it: the guard's question is
    what became of the queue as it stood."""
    career_ops, additions, state = _stage(tmp_path_factory.mktemp("headed"), HEADER,
                                          {"7.tsv": _headed_addition("4.7/5")})
    before = _pending_additions(additions)
    r = _node_merge(career_ops, additions, state)
    return SimpleNamespace(tracker=_tracker_after(career_ops), career_ops=career_ops,
                           additions=additions, before=before, proc=r)


@needs_headed_additions
class TestHeadedAdditionLands:
    def test_it_merges_and_is_archived(self, headed_merged):
        h = headed_merged
        row = tracker_row(h.tracker, "11")
        assert (row["company"], row["role"], row["score"]) == (
            "Acme Corp", "Platform Engineer", "4.7/5")
        assert "➕ Add" in h.proc.stdout
        assert not list(h.additions.glob("*.tsv"))
        assert (h.additions / "merged" / "7.tsv").exists()

    def test_the_guard_does_not_report_it_lost(self, headed_merged, capsys):
        """The #152 false alarm this fix removes, over the real merge. The first
        assertion is what keeps the second from being vacuous: a snapshot that
        dropped the headed file warns about nothing too, and one that read its
        label line as a job warns about a role called "role"."""
        h = headed_merged
        assert [(a["company"], a["role"], a["report"]) for a in h.before] == [
            ("Acme Corp", "Platform Engineer", "229")]
        _warn_on_lost_additions(h.before, h.career_ops, h.additions,
                                f"{h.proc.stdout}\n{h.proc.stderr}")
        assert "WARNING" not in capsys.readouterr().out


@pytest.fixture(scope="module")
def headed_recovered(tmp_path_factory):
    """`recovered` on the headed form: the runner's own merge refuses a
    bare-score headed row and archives it, recovery re-queues it, a second merge
    lands it. Each stage's evidence is kept rather than asserted here, so a
    failure names the stage. No batch-input.tsv: the headed row brings its URL
    in its own column."""
    tmp_path = tmp_path_factory.mktemp("headed-recover")
    career_ops, additions, state = _stage(tmp_path, HEADER,
                                          {"7.tsv": _headed_addition("4.2")})
    first = _node_merge(career_ops, additions, state)
    after_first = _tracker_after(career_ops)
    archived = (additions / "merged" / "7.tsv").exists()
    _recover_refused_additions(career_ops, additions)
    queued = additions / "7.tsv"
    requeued = queued.read_text(encoding="utf-8") if queued.exists() else None
    second = _node_merge(career_ops, additions, state)
    return SimpleNamespace(first=first, after_first=after_first, archived=archived,
                           requeued=requeued, second=second,
                           tracker=_tracker_after(career_ops))


@needs_headed_additions
class TestHeadedRecoveryAfterTheRunnersOwnMerge:
    def test_a_bare_score_is_refused_by_label_and_archived(self, headed_recovered):
        """The refusal has to name the column LABELLED score. A pre-#3706 script
        refuses this file too, on the label line, so a bare "Skipping" would pass
        even if the header were never read as one."""
        h = headed_recovered
        assert "Platform Engineer" not in h.after_first
        assert 'the column labelled "score" reads "4.2"' in h.first.stderr
        assert h.archived

    def test_recovery_keeps_the_label_line_and_the_second_merge_lands_it(
            self, headed_recovered):
        """Re-queued as a headed file, not flattened into a headerless row —
        merge-tracker takes a labelled file by name, and that is the one form
        in which a never-scored `—`/`—` row has an order at all (#3517)."""
        h = headed_recovered
        assert h.requeued is not None                        # back in the queue
        label, data = h.requeued.rstrip("\n").split("\n")    # one data row
        assert label == ADDITION_LABEL_LINE
        assert dict(zip(ADDITION_LABELS, data.split("\t")))["score"] == "4.2/5"
        assert "➕ Add" in h.second.stdout
        row = tracker_row(h.tracker, "11")
        assert (row["role"], row["score"]) == ("Platform Engineer", "4.2/5")


@pytest.fixture(scope="module")
def headed_short(tmp_path_factory):
    """A headed row that stops before its `url` cell with its Notes empty — both
    of which merge-tracker allows — and a queued URL for the sanitizer to write
    into Notes. A bare URL in Notes with no `url` cell is the shape merge-tracker
    refuses as every optional cell shifted one left."""
    tmp_path = tmp_path_factory.mktemp("headed-short")
    cells = _headed_cells("4.7/5")
    row = "\t".join(cells[label] for label in ADDITION_LABELS[:-2]) + "\t"   # empty notes, no url
    career_ops, additions, state = _stage(tmp_path, HEADER,
                                          {"7.tsv": f"{ADDITION_LABEL_LINE}\n{row}\n"})
    _queue_url(career_ops)
    _sanitize_pending_additions(career_ops, additions)
    return SimpleNamespace(proc=_node_merge(career_ops, additions, state),
                           tracker=_tracker_after(career_ops))


@needs_headed_additions
class TestHeadedRowShortOfItsUrlCell:
    def test_the_repaired_row_still_merges_with_its_url(self, headed_short):
        """Unpadded, the sanitizer turned a row merge-tracker accepts into one it
        refuses — `a URL sits under "notes" while the "url" cell is absent` —
        and, its score being readable, recovery never retried it."""
        h = headed_short
        assert "Skipping" not in h.proc.stderr, h.proc.stderr
        row = tracker_row(h.tracker, "11")
        assert row["role"] == "Platform Engineer" and "https://x/j/7" in row["notes"]


@needs_headed_additions
def test_the_label_line_is_the_one_the_prompt_prescribes():
    """`ADDITION_LABEL_LINE` is a copy — CI has no checkout to read it from — so
    here, where the checkout is, it is held to the line career-ops' batch prompt
    tells the worker to write "exactly as shown". A relabelling upstream fails
    this, rather than leaving every headed test driving a line no worker writes."""
    prompt = (career_ops_dir() / "batch" / "batch-prompt.md").read_text(encoding="utf-8")
    shown = [line.strip() for line in prompt.splitlines()
             if line.strip().startswith("num\\tdate\\t")]
    assert shown, "batch-prompt.md no longer shows a `num\\tdate\\t…` label line"
    assert shown[0].replace("\\t", "\t") == ADDITION_LABEL_LINE


class TestRecheckDiscardIsReopenedOnRepost:
    """The premise of #163 against the real script, then our half of the fix.

    merge-tracker's update tier writes a re-eval's score, report, date and
    notes through and KEEPS the row's status — so a re-post of a role the
    liveness re-check had Discarded was folded onto the Discarded row and never
    surfaced again. `_reopen_reposted` resets such a row (its report number
    changed in the merge, and the snapshot taken before the merge saw the
    re-check's mark) to Evaluated, and `extract_url` then reads the new
    posting's URL — the one the next re-check must verify. A higher score is
    used so the case holds on a checkout on either side of upstream's #2411
    (the older script skips a lower one)."""

    @pytest.fixture
    def reopened(self, tmp_path):
        text = (_tracker(BASE, 200)
                .replace("| Evaluated |", "| Discarded |")
                .replace("— APPLY |", f"— APPLY — {liveness_closed_mark('2026-08-20', 'HTTP 404')} |"))
        pre = tmp_path / "pre.md"
        pre.write_text(text, encoding="utf-8")
        closed = _liveness_closed_rows(pre)
        merged_text, career_ops, _, _ = _merge(tmp_path, text, {"123.tsv": _addition(VARIANT, 229)})
        _reopen_reposted(closed, career_ops)
        after = (career_ops / "data" / "applications.md").read_text(encoding="utf-8")
        return closed, merged_text, after

    @staticmethod
    def _row(text):
        return tracker_row(text, "10")

    def test_upstream_writes_through_and_keeps_the_status(self, reopened):
        closed, merged_text, _ = reopened
        assert set(closed) == {"10"}
        row = self._row(merged_text)
        assert row["report_num"] == "229" and row["status_canonical"] == "Discarded"

    def test_reopen_resets_to_evaluated_and_points_at_the_new_posting(self, reopened):
        from pipeline.app import data
        _, _, after = reopened
        row = self._row(after)
        assert row["status_canonical"] == "Evaluated"
        assert data.extract_url(row["notes"]) == "https://indeed.com/viewjob?jk=new"
        assert not closed_by_recheck(row["notes"])          # a later Discard is a person's
