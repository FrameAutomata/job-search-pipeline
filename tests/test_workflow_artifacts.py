"""Every artifact upload declares a retention, and none uploads accumulated state.

Issue #129: the account's Actions storage quota filled up, and an exhausted
storage quota stops GitHub creating workflow runs *at all* — so the Tests
workflow silently stopped running on pull requests, across three separate
trigger events, while its own file was demonstrably fine.

Two properties had to hold and neither was written down anywhere:

  1. **A retention, at or under a ceiling.** The 90 days that caused it was the
     *default* — nobody chose it, nobody reviewed it, and it does not appear in
     the file. That is exactly the shape review misses and a guard catches.
  2. **Nothing that accumulates gets uploaded.** `career-ops/reports/` is
     restored from the state cache every run and only grows, so uploading it
     put the entire report history into every daily artifact. Retention caps
     how many artifacts are alive, never how big each one is; without this half
     the storage still grows without bound, just more slowly.

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

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# A week. Long enough for the delivery mechanism of a DAILY job — the README
# tells users to download the pipeline-output-* artifact — and short enough
# that a copy of this template cannot bank three months of them. Raising this
# is a decision; that is the point of it being a number in a test.
RETENTION_CEILING = 7

# Every path the daily caches, classified. `grows` is the whole point of this
# file: restored at the start of each run and never truncated, so an upload
# naming one carries the accumulated history. `whole` is small and its current
# value is what a reader wants (applications.md IS the tracker), so uploading it
# entire is correct.
#
# A table rather than a blocklist, and asserted EXACT against the workflow
# below: a blocklist lets a cache path added later slip through unclassified,
# which is the same "nobody chose it" failure this file exists to catch.
CACHED_PATHS = {
    "career-ops/data/scan-history.tsv":   "grows",
    "career-ops/data/applications.md":    "whole",
    "career-ops/data/pipeline.md":        "whole",
    "career-ops/data/recheck-state.tsv":  "whole",
    "career-ops/data/easy-apply-urls.txt": "whole",
    "career-ops/batch/batch-input.tsv":   "whole",
    "career-ops/batch/batch-api-state.json": "whole",
    "career-ops/batch/jds":               "grows",
    "career-ops/reports":                 "grows",
}

DAILY = "daily-pipeline.yml"


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(name: str, job: str) -> list[dict]:
    return _doc(name)["jobs"][job]["steps"]


def _step_index(steps: list[dict], predicate) -> int:
    return next(i for i, s in enumerate(steps) if predicate(s))


def _path_entries(with_block: dict) -> list[str]:
    """A step's `path:` value as a list of entries, trailing slashes stripped."""
    return [ln.strip().rstrip("/")
            for ln in str(with_block.get("path") or "").splitlines()
            if ln.strip()]


def _all_upload_steps() -> list[tuple[str, str, dict]]:
    """(workflow, label, with-block) per actions/upload-artifact step, across
    every workflow. Pre-labelled so no assertion has to re-derive it."""
    out = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job.get("steps") or []):
                if str(step.get("uses") or "").startswith("actions/upload-artifact"):
                    out.append((path.name,
                                f"{path.name}:{job_name}:{step.get('name', '?')}",
                                step.get("with") or {}))
    return out


class TestRetentionIsDeclaredAndBounded:
    def test_the_scan_finds_the_upload_steps(self):
        """A collection bug that returned [] would make this file pass forever
        — which is the same failure it exists to prevent, one level up."""
        found = _all_upload_steps()
        assert len(found) >= 2, f"only found {len(found)} upload-artifact steps"
        assert {wf for wf, _, _ in found} >= {DAILY, "edit-tracker.yml"}

    def test_every_upload_sets_retention_days(self):
        missing = [label for _, label, block in _all_upload_steps()
                   if "retention-days" not in block]
        assert not missing, (
            "upload-artifact steps with no retention-days (they inherit the "
            f"repository default, which is 90 days): {missing}"
        )

    def test_no_upload_exceeds_the_ceiling(self):
        over = [(label, block["retention-days"])
                for _, label, block in _all_upload_steps()
                if int(block.get("retention-days", 0)) > RETENTION_CEILING]
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

    def test_no_upload_path_names_a_growing_cache_entry(self):
        growing = [p for p, kind in CACHED_PATHS.items() if kind == "grows"]
        offenders = [
            f"{label} -> {entry}"
            for _, label, block in _all_upload_steps()
            for entry in _path_entries(block)
            if any(entry.startswith(g) for g in growing)
        ]
        assert not offenders, (
            "these upload steps carry state that is restored from the "
            f"pipeline-state cache and grows every run: {offenders}. Stage this "
            "run's own output instead — see pipeline/run_artifact.py and the "
            "'Stage this run's output for upload' step in daily-pipeline.yml."
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
        steps = _steps(DAILY, "scrape-and-evaluate")
        stage = steps[_step_index(
            steps, lambda s: "run_artifact stage" in str(s.get("run") or ""))]["run"]
        assert "--root career-ops" in stage
        assert "--delta reports" in stage
        assert "--whole data/applications.md" in stage


class TestTheGuardsCatchWhatTheyForbid:
    """A guard is only worth having if it fails on the thing it forbids."""

    def _fake(self, monkeypatch, with_block):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_upload_steps",
            lambda: [("fake.yml", "fake.yml:job:Upload", with_block)])

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
        with pytest.raises(AssertionError, match="grows every run"):
            TestNoUploadCarriesAccumulatedState() \
                .test_no_upload_path_names_a_growing_cache_entry()

    def test_an_unclassified_cache_path_is_caught(self, monkeypatch):
        """The exhaustiveness half: a path added to the cache with no entry in
        CACHED_PATHS must fail here rather than quietly skip the check above."""
        monkeypatch.setitem(CACHED_PATHS, "career-ops/data/something-new.tsv", "grows")
        with pytest.raises(AssertionError, match="diverged"):
            TestNoUploadCarriesAccumulatedState().test_every_cached_path_is_classified()
