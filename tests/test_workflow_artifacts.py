"""Every artifact upload declares a retention, and none uploads accumulated state.

Issue #129: the account's Actions storage quota filled up, and an exhausted
storage quota stops GitHub creating workflow runs *at all* — so the Tests
workflow silently stopped running on pull requests, across three separate
trigger events, while its own file was demonstrably fine.

Two properties had to hold and neither was written down anywhere:

  1. **A retention, at or under a ceiling.** The 90 days that caused it was the
     *default* — nobody chose it, nobody reviewed it, and it does not appear in
     the file. That is exactly the shape review misses and a guard catches.
  2. **No SCHEDULED upload names cached state.** `career-ops/reports/` is
     restored from the state cache every run and only grows, so uploading it
     put the entire report history into every daily artifact. Retention caps
     how many artifacts are alive, never how big each one is; without this half
     the storage still grows without bound, just more slowly. Scheduled, not
     all: export-reports.yml exports the whole history on purpose, and a human
     asking for it once is bounded by the asking in a way a cron job is not.

Guarding the rule rather than today's two upload steps, in the spirit of
tests/test_stdio.py: the failure mode is a *new* upload added later by pasting
an older one, and both halves of the rule are invisible in the paste.

This is also the only place either property can be checked. The repository that
holds the code is public — public repos don't consume the account's storage
quota — and it is the template, where daily-pipeline.yml skips by design. The
daily only really runs in the private copies made via "Use this template", so
the bug is structurally unobservable from here.
"""

from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# A week. Long enough for the delivery mechanism of a DAILY job — the README
# tells users to download the pipeline-output-* artifact — and short enough
# that a copy of this template cannot bank three months of them. Raising this
# is a decision; that is the point of it being a number in a test.
RETENTION_CEILING = 7

# Every path the daily caches, and what an artifact may do with it.
#
#   "delta" — restored at the start of every run and never truncated. Only the
#             files THIS run added may ship; uploading the directory is the bug.
#   "whole" — must ship entire because a consumer needs its current value, and
#             the growth that comes with that is accepted and named here.
#   "local" — never leaves the cache.
#
# A table rather than a blocklist, and asserted EXACT against the workflow
# below: a blocklist lets a cache path added later slip through unclassified,
# which is the same "nobody chose it" failure this file exists to catch.
#
# Note applications.md is "whole" even though it grows: the UI's Refresh MERGES
# it, so a diff is useless to it. That is irreducible, and it is ~200 bytes per
# evaluated role against ~4KB per report. pipeline.md is "local" for the
# opposite reason — it is append-only at ~600 bytes for EVERY bridged offer,
# many more than are ever evaluated, and nothing reads it out of the artifact.
CACHED_PATHS = {
    "career-ops/reports":                    "delta",
    "career-ops/data/applications.md":       "whole",
    "career-ops/data/scan-history.tsv":      "local",
    "career-ops/data/pipeline.md":           "local",
    "career-ops/data/recheck-state.tsv":     "local",
    "career-ops/data/easy-apply-urls.txt":   "local",
    "career-ops/batch/batch-input.tsv":      "local",
    "career-ops/batch/batch-api-state.json": "local",
    "career-ops/batch/jds":                  "local",
}

DAILY = "daily-pipeline.yml"
CAREER_OPS = "career-ops"


class Upload(NamedTuple):
    workflow: str
    label: str          # "<workflow>:<job>:<step name>", pre-built for messages
    block: dict         # the step's `with:` mapping
    scheduled: bool     # does its workflow have a `schedule:` trigger?


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """A workflow's `on:` block. PyYAML resolves the bare key `on` to the
    boolean True (YAML 1.1), so reading doc["on"] finds nothing — and a
    scheduled workflow would then look manual to the rule below."""
    trig = doc.get("on", doc.get(True))
    return trig if isinstance(trig, dict) else {}


def _steps(name: str, job: str) -> list[dict]:
    return _doc(name)["jobs"][job]["steps"]


def _step_index(steps: list[dict], predicate) -> int:
    return next(i for i, s in enumerate(steps) if predicate(s))


def _norm(entry: str) -> str:
    """A path entry as the repo writes it: no `./` prefix, no trailing slash."""
    entry = entry.strip().rstrip("/")
    while entry.startswith("./"):
        entry = entry[2:]
    return entry


def _path_entries(with_block: dict) -> list[str]:
    """A step's `path:` value as a list of normalized entries."""
    return [_norm(ln) for ln in str(with_block.get("path") or "").splitlines()
            if ln.strip()]


def _covers(entry: str, target: str) -> bool:
    """Does uploading `entry` put `target` in the artifact?

    Both directions, because both are ways to reintroduce the bug: naming the
    path itself, and naming a PARENT of it (`path: career-ops` ships reports/
    just as surely as `path: career-ops/reports`). A one-directional startswith
    catches only the first, which is the one nobody would write by accident."""
    entry, target = _norm(entry).rstrip("*").rstrip("/"), _norm(target)
    return entry == target or target.startswith(entry + "/") \
        or entry.startswith(target + "/")


def _run_artifact_args(mode: str) -> dict[str, list[str]]:
    """The daily's `python -m pipeline.run_artifact <mode>` invocation, parsed
    into {flag: [values]}. Reads the parsed step's `run` rather than splitting
    the raw YAML, so indentation and step order can change freely."""
    steps = _steps(DAILY, "scrape-and-evaluate")
    run = steps[_step_index(
        steps, lambda s: f"run_artifact {mode}" in str(s.get("run") or ""))]["run"]
    tokens = run.replace("\\\n", " ").split()
    args: dict[str, list[str]] = {}
    flag = None
    for tok in tokens:
        if tok.startswith("--"):
            flag = tok[2:]
            args.setdefault(flag, [])
        elif flag is not None:
            args[flag].append(tok)
    return args


def _stage_args() -> dict[str, list[str]]:
    return _run_artifact_args("stage")


def _all_upload_steps() -> list[Upload]:
    """Every actions/upload-artifact step across every workflow, pre-labelled
    and tagged with whether its workflow runs on a schedule."""
    out = []
    # Both extensions: GitHub honours .yaml as readily as .yml, and a workflow
    # this scan cannot see is a workflow exempt from every rule in this file.
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        scheduled = "schedule" in _triggers(doc)
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job.get("steps") or []):
                if str(step.get("uses") or "").startswith("actions/upload-artifact"):
                    out.append(Upload(
                        path.name,
                        f"{path.name}:{job_name}:{step.get('name', '?')}",
                        step.get("with") or {},
                        scheduled))
    return out


class TestRetentionIsDeclaredAndBounded:
    def test_the_scan_finds_the_upload_steps(self):
        """A collection bug that returned [] would make this file pass forever
        — which is the same failure it exists to prevent, one level up."""
        found = _all_upload_steps()
        assert len(found) >= 2, f"only found {len(found)} upload-artifact steps"
        assert {u.workflow for u in found} >= {DAILY, "edit-tracker.yml"}

    def test_every_upload_sets_retention_days(self):
        missing = [u.label for u in _all_upload_steps()
                   if "retention-days" not in u.block]
        assert not missing, (
            "upload-artifact steps with no retention-days (they inherit the "
            f"repository default, which is 90 days): {missing}"
        )

    def test_no_upload_exceeds_the_ceiling(self):
        over = [(u.label, u.block["retention-days"]) for u in _all_upload_steps()
                if int(u.block.get("retention-days", 0)) > RETENTION_CEILING]
        assert not over, (
            f"upload-artifact steps above the {RETENTION_CEILING}-day ceiling: {over}. "
            "Raising it is a decision about a copy's Actions storage quota — "
            "change RETENTION_CEILING here deliberately if that is what you mean."
        )


class TestNoUploadCarriesAccumulatedState:
    def test_every_cached_path_is_classified(self):
        """CACHED_PATHS must name the daily's cache list exactly. A path added
        to the cache but not here would be unclassified — and so exempt from the
        check below, silently, which is the shape of the original bug."""
        steps = _steps(DAILY, "scrape-and-evaluate")
        restore = steps[_step_index(
            steps, lambda s: str(s.get("uses") or "").startswith("actions/cache/restore"))]
        assert sorted(_path_entries(restore.get("with") or {})) == sorted(CACHED_PATHS), (
            "the daily's cached paths and CACHED_PATHS have diverged. Classify "
            "the new one as 'grows' (restored and never truncated — must not be "
            "uploaded whole) or 'whole' (small, current value is the point). "
            "Making that call is the point of this test."
        )

    def test_no_scheduled_upload_names_a_cached_path(self):
        """Cached state reaches a scheduled artifact through the staging step or
        not at all — that is what bounds the artifact to one run's work. A
        manual export is exempt and says so in its own header."""
        offenders = [
            f"{u.label} -> {entry}"
            for u in _all_upload_steps() if u.scheduled
            for entry in _path_entries(u.block)
            for cached in CACHED_PATHS
            if _covers(entry, cached)
        ]
        assert not offenders, (
            "these SCHEDULED upload steps name state restored from the "
            f"pipeline-state cache: {offenders}. Stage this run's own output "
            "instead — see pipeline/run_artifact.py and the 'Stage this run's "
            "output for upload' step in daily-pipeline.yml. If the whole history "
            "really is wanted, it belongs in a workflow_dispatch-only workflow "
            "like export-reports.yml, not on a cron."
        )

    def test_the_stage_step_matches_the_classification(self):
        """CACHED_PATHS is only a claim unless the staging step obeys it: every
        "delta" path deltaed, every "whole" path shipped entire, and nothing
        classified "local" named at all."""
        stage = _stage_args()
        deltaed = {f"{CAREER_OPS}/{r}" for r in stage["delta"]}
        wholed = {f"{CAREER_OPS}/{r}" for r in stage["whole"]}

        assert deltaed == {p for p, k in CACHED_PATHS.items() if k == "delta"}
        assert wholed & set(CACHED_PATHS) == {
            p for p, k in CACHED_PATHS.items() if k == "whole"}
        leaked = sorted(p for p in wholed if CACHED_PATHS.get(p) == "local")
        assert not leaked, (
            f"{leaked} are classified 'local' but the daily ships them whole. "
            "Both pipeline.md and easy-apply-urls.txt are append-only and were "
            "removed for exactly that reason (issue #129)."
        )

    def test_the_daily_stages_before_it_uploads(self):
        """The staging step and the manifest it needs are what make the daily
        artifact O(one run) instead of O(all history), and the upload has to be
        pointed at the staged tree for that to mean anything."""
        steps = _steps(DAILY, "scrape-and-evaluate")

        def at(predicate):
            return _step_index(steps, predicate)

        def runs(needle):
            return lambda s: needle in str(s.get("run") or "")

        def uses(action):
            return lambda s: str(s.get("uses") or "").startswith(action)

        restore = at(uses("actions/cache/restore"))
        snapshot = at(runs("run_artifact snapshot"))
        pipeline = at(runs("orchestrate.py"))
        stage = at(runs("run_artifact stage"))
        upload = at(uses("actions/upload-artifact"))

        # The snapshot has exactly one correct slot. Before the restore it sees
        # an empty tree, so every restored report looks new and the artifact is
        # unbounded again; after the pipeline it sees this run's reports too and
        # the artifact is empty. Matched by content and asserted as relative
        # order, so renaming or inserting a step doesn't churn this.
        assert restore < snapshot < pipeline < stage < upload, (
            "step order is wrong for the snapshot/stage pair: "
            f"{[s.get('name', '') for s in steps]}")

        upload_path = str(steps[upload]["with"]["path"])
        assert "pipeline-artifact" in upload_path, (
            f"the daily must upload the staged tree, not {upload_path!r}")

    def test_the_staged_tree_keeps_the_layout_the_ui_reads(self):
        """`gh.download_artifact` looks for reports/ or data/ at the artifact
        root and `data.sync_pulled_tracker` reads data/applications.md from
        there. The staging step's arguments are what decide that now."""
        stage = _stage_args()
        assert stage["root"] == [CAREER_OPS]
        assert "reports" in stage["delta"]
        assert "data/applications.md" in stage["whole"]

    def test_snapshot_and_stage_agree_on_what_the_manifest_covers(self):
        """Different --root or --delta between the two steps and the manifest's
        keys stop lining up: every restored report reads as new and the whole
        history goes back into the artifact. run_artifact refuses a mismatch at
        runtime; this catches it at review time, when it is still free."""
        snap = _run_artifact_args("snapshot")
        stage = _stage_args()
        assert snap["root"] == stage["root"], (snap["root"], stage["root"])
        assert snap["delta"] == stage["delta"], (snap["delta"], stage["delta"])
        assert snap["manifest"] == stage["manifest"], "same manifest file, too"


class TestTheGuardsCatchWhatTheyForbid:
    """A guard is only worth having if it fails on the thing it forbids."""

    def _fake(self, monkeypatch, with_block, *, scheduled=True):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_upload_steps",
            lambda: [Upload("fake.yml", "fake.yml:job:Upload", with_block, scheduled)])

    def test_missing_retention_is_caught(self, monkeypatch):
        self._fake(monkeypatch, {"path": "out/"})
        with pytest.raises(AssertionError, match="no retention-days"):
            TestRetentionIsDeclaredAndBounded().test_every_upload_sets_retention_days()

    def test_over_ceiling_retention_is_caught(self, monkeypatch):
        self._fake(monkeypatch, {"path": "out/", "retention-days": 90})
        with pytest.raises(AssertionError, match="ceiling"):
            TestRetentionIsDeclaredAndBounded().test_no_upload_exceeds_the_ceiling()

    def test_uploading_the_reports_dir_is_caught(self, monkeypatch):
        self._fake(monkeypatch, {
            "path": "career-ops/reports/\ncareer-ops/data/applications.md\n",
            "retention-days": 7,
        })
        with pytest.raises(AssertionError, match="restored from the"):
            TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_uploading_a_parent_of_the_reports_dir_is_caught(self, monkeypatch):
        """`path: career-ops` ships reports/ just as surely, and is what someone
        reaching for "upload everything" would actually write."""
        self._fake(monkeypatch, {"path": "career-ops", "retention-days": 7})
        with pytest.raises(AssertionError, match="restored from the"):
            TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_a_dot_slash_prefix_does_not_evade_the_check(self, monkeypatch):
        self._fake(monkeypatch, {"path": "./career-ops/reports/", "retention-days": 7})
        with pytest.raises(AssertionError, match="restored from the"):
            TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_a_manual_export_of_the_same_path_is_allowed(self, monkeypatch):
        """The rule is about crons, not about the path. export-reports.yml
        uploads exactly this, on purpose, and must not trip the guard."""
        self._fake(monkeypatch, {"path": "career-ops", "retention-days": 7},
                   scheduled=False)
        TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_an_unclassified_cache_path_is_caught(self, monkeypatch):
        """The exhaustiveness half: a path added to the cache with no entry in
        CACHED_PATHS must fail here rather than quietly skip the checks above."""
        monkeypatch.setitem(CACHED_PATHS, "career-ops/data/something-new.tsv", "local")
        with pytest.raises(AssertionError, match="diverged"):
            TestNoUploadCarriesAccumulatedState().test_every_cached_path_is_classified()
