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

# Paths that are restored from the pipeline-state cache and therefore grow
# forever. An upload step naming one of these is the original bug, whatever its
# retention says. Written as the repo-relative prefixes the workflows use.
ACCUMULATING_PATHS = (
    "career-ops/reports",
    "career-ops/batch/jds",
    "career-ops/data/scan-history.tsv",
)


def _workflows():
    return sorted(WORKFLOWS.glob("*.yml"))


def _upload_steps(doc: dict):
    """(job name, step) for every actions/upload-artifact step in a workflow."""
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in (job.get("steps") or []):
            uses = str(step.get("uses") or "")
            if uses.startswith("actions/upload-artifact"):
                yield job_name, step


def _all_upload_steps():
    out = []
    for path in _workflows():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, step in _upload_steps(doc):
            out.append((path.name, job_name, step))
    return out


class TestRetentionIsDeclaredAndBounded:
    def test_the_scan_finds_the_upload_steps(self):
        """A collection bug that returned [] would make this file pass forever
        — which is the same failure it exists to prevent, one level up."""
        found = _all_upload_steps()
        assert len(found) >= 2, f"only found {len(found)} upload-artifact steps"
        assert {name for name, _, _ in found} >= {
            "daily-pipeline.yml", "edit-tracker.yml"
        }

    def test_every_upload_sets_retention_days(self):
        missing = [
            f"{wf}:{job}:{step.get('name', '?')}"
            for wf, job, step in _all_upload_steps()
            if "retention-days" not in (step.get("with") or {})
        ]
        assert not missing, (
            "upload-artifact steps with no retention-days (they inherit the "
            f"repository default, which is 90 days): {missing}"
        )

    def test_no_upload_exceeds_the_ceiling(self):
        over = [
            (f"{wf}:{job}:{step.get('name', '?')}", (step.get("with") or {}).get("retention-days"))
            for wf, job, step in _all_upload_steps()
            if int((step.get("with") or {}).get("retention-days", 0)) > RETENTION_CEILING
        ]
        assert not over, (
            f"upload-artifact steps above the {RETENTION_CEILING}-day ceiling: {over}. "
            "Raising it is a decision about a copy's Actions storage quota — "
            "change RETENTION_CEILING here deliberately if that is what you mean."
        )


class TestNoUploadCarriesAccumulatedState:
    def test_no_upload_path_names_a_cache_restored_directory(self):
        offenders = []
        for wf, job, step in _all_upload_steps():
            paths = str((step.get("with") or {}).get("path") or "")
            for line in paths.splitlines():
                entry = line.strip().rstrip("/")
                if any(entry.startswith(acc) for acc in ACCUMULATING_PATHS):
                    offenders.append(f"{wf}:{job}:{step.get('name', '?')} -> {entry}")
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
        doc = yaml.safe_load(
            (WORKFLOWS / "daily-pipeline.yml").read_text(encoding="utf-8"))
        steps = doc["jobs"]["scrape-and-evaluate"]["steps"]
        names = [s.get("name", "") for s in steps]

        snapshot = next(i for i, s in enumerate(steps)
                        if "run_artifact snapshot" in str(s.get("run") or ""))
        stage = next(i for i, s in enumerate(steps)
                     if "run_artifact stage" in str(s.get("run") or ""))
        restore = next(i for i, s in enumerate(steps)
                       if str(s.get("uses") or "").startswith("actions/cache/restore"))
        pipeline = next(i for i, s in enumerate(steps)
                        if "orchestrate.py" in str(s.get("run") or ""))
        upload = next(i for i, s in enumerate(steps)
                      if str(s.get("uses") or "").startswith("actions/upload-artifact"))

        # The snapshot has exactly one correct slot. Before the restore it sees
        # an empty tree, so every restored report looks new and the artifact is
        # unbounded again; after the pipeline it sees this run's reports too and
        # the artifact is empty.
        assert restore < snapshot < pipeline < stage < upload, (
            f"step order is wrong for the snapshot/stage pair: {names}")

        upload_path = str(steps[upload]["with"]["path"])
        assert "pipeline-artifact" in upload_path, (
            f"the daily must upload the staged tree, not {upload_path!r}")

    def test_the_staged_tree_keeps_the_layout_the_ui_reads(self):
        """`gh.download_artifact` looks for reports/ or data/ at the artifact
        root and `data.sync_pulled_tracker` reads data/applications.md from
        there. The staging step's arguments are what decide that now."""
        text = (WORKFLOWS / "daily-pipeline.yml").read_text(encoding="utf-8")
        stage = text.split("run_artifact stage", 1)[1].split("- name:", 1)[0]
        assert "--root career-ops" in stage
        assert "--delta reports" in stage
        assert "--whole data/applications.md" in stage


class TestTheGuardsCatchWhatTheyForbid:
    """A guard is only worth having if it fails on the thing it forbids."""

    _UPLOAD = {"uses": "actions/upload-artifact@v4", "name": "Upload"}

    def _steps(self, with_block):
        return [("fake.yml", "job", {**self._UPLOAD, "with": with_block})]

    def test_missing_retention_is_caught(self, monkeypatch):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_upload_steps",
            lambda: self._steps({"path": "out/"}))
        with pytest.raises(AssertionError, match="no retention-days"):
            TestRetentionIsDeclaredAndBounded().test_every_upload_sets_retention_days()

    def test_over_ceiling_retention_is_caught(self, monkeypatch):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_upload_steps",
            lambda: self._steps({"path": "out/", "retention-days": 90}))
        with pytest.raises(AssertionError, match="ceiling"):
            TestRetentionIsDeclaredAndBounded().test_no_upload_exceeds_the_ceiling()

    def test_uploading_the_reports_dir_is_caught(self, monkeypatch):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_upload_steps",
            lambda: self._steps({
                "path": "career-ops/reports/\ncareer-ops/data/applications.md\n",
                "retention-days": 7,
            }))
        with pytest.raises(AssertionError, match="grows every run"):
            TestNoUploadCarriesAccumulatedState() \
                .test_no_upload_path_names_a_cache_restored_directory()
