"""Tests for pipeline/run_artifact.py — the daily artifact holds one run's output.

This is the one place the behaviour can be checked. The bug it fixes is
invisible in this repository: public repos don't consume the account's Actions
storage quota, and `daily-pipeline.yml` skips in the template by design, so the
unbounded artifact only ever materialised in the private copies made from it
(issue #129). "Run the daily and look" is not available; these are.

The cases that matter are the ones where a cheaper implementation would look
right and be wrong — a path-set diff missing a rewritten file, a missing
manifest degrading quietly to the full upload it replaced.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import run_artifact

ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def career_ops(tmp_path):
    """A career-ops tree shaped like the one the workflow stages from: reports
    restored from the state cache, plus the small whole-file state."""
    root = tmp_path / "career-ops"
    _write(root / "reports" / "001-acme-2026-06-01.md", "old report\n")
    _write(root / "reports" / "002-globex-2026-06-02.md", "older report\n")
    _write(root / "data" / "applications.md", "| # | Company |\n")
    return root


class TestScan:
    def test_absent_directory_contributes_nothing(self, tmp_path):
        """The first run in a fresh copy has no reports dir. That is the case
        where everything is new, not an error."""
        assert run_artifact.scan(tmp_path, ["reports"]) == {}

    def test_keys_are_relative_to_root_across_directories(self, career_ops):
        manifest = run_artifact.scan(career_ops, ["reports", "data"])
        assert set(manifest) == {
            "reports/001-acme-2026-06-01.md",
            "reports/002-globex-2026-06-02.md",
            "data/applications.md",
        }

    def test_recurses_into_subdirectories(self, career_ops):
        _write(career_ops / "reports" / "archive" / "000-old.md", "x")
        assert "reports/archive/000-old.md" in run_artifact.scan(career_ops, ["reports"])

    def test_records_size_and_mtime(self, career_ops):
        f = career_ops / "reports" / "001-acme-2026-06-01.md"
        size, mtime_ns = run_artifact.scan(career_ops, ["reports"])[
            "reports/001-acme-2026-06-01.md"]
        assert size == f.stat().st_size
        assert mtime_ns == f.stat().st_mtime_ns


class TestStage:
    def test_only_this_runs_reports_are_staged(self, career_ops, tmp_path):
        """The whole point: the two restored reports stay out of the artifact."""
        manifest = run_artifact.scan(career_ops, ["reports"])
        _write(career_ops / "reports" / "003-initech-2026-06-03.md", "today's report\n")

        into = tmp_path / "artifact"
        delta, _ = run_artifact.stage(career_ops, into, manifest, ["reports"], [])

        assert delta == ["reports/003-initech-2026-06-03.md"]
        assert (into / "reports" / "003-initech-2026-06-03.md").exists()
        assert not (into / "reports" / "001-acme-2026-06-01.md").exists()

    def test_a_rewritten_file_is_staged(self, career_ops, tmp_path):
        """A job that failed in an earlier run and succeeded in this one reuses
        its filename. A path-set diff would call that old and drop it, so the
        comparison is on (size, mtime), not on the key alone."""
        f = _write(career_ops / "reports" / "004-hooli-2026-06-04.md", "aaaaa\n")
        manifest = run_artifact.scan(career_ops, ["reports"])
        # Same length on purpose — size alone must not be what catches this.
        # utime rather than a real clock so the case is about the comparison,
        # not about how fine-grained this filesystem's timestamps happen to be.
        f.write_text("bbbbb\n", encoding="utf-8")
        os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000_000))

        delta, _ = run_artifact.stage(career_ops, tmp_path / "artifact", manifest,
                                      ["reports"], [])
        assert delta == ["reports/004-hooli-2026-06-04.md"]

    def test_untouched_file_is_not_restaged(self, career_ops, tmp_path):
        manifest = run_artifact.scan(career_ops, ["reports"])
        delta, _ = run_artifact.stage(career_ops, tmp_path / "artifact", manifest,
                                      ["reports"], [])
        assert delta == []

    def test_empty_manifest_stages_everything(self, career_ops, tmp_path):
        """A copy whose very first run this is: nothing was restored, so every
        report is genuinely this run's."""
        delta, _ = run_artifact.stage(career_ops, tmp_path / "artifact", {},
                                      ["reports"], [])
        assert len(delta) == 2

    def test_whole_paths_are_copied_entire(self, career_ops, tmp_path):
        """applications.md is the tracker the UI's Refresh merges — its whole
        current value is the point, so it is never deltaed."""
        _write(career_ops / "batch" / "tracker-additions" / "job-1.tsv", "a\tb\n")
        into = tmp_path / "artifact"

        _, whole = run_artifact.stage(
            career_ops, into, {}, [],
            ["data/applications.md", "batch/tracker-additions"])

        assert sorted(whole) == ["batch/tracker-additions", "data/applications.md"]
        assert (into / "data" / "applications.md").read_text(encoding="utf-8") \
            == "| # | Company |\n"
        assert (into / "batch" / "tracker-additions" / "job-1.tsv").exists()

    def test_missing_whole_paths_are_skipped_not_fatal(self, career_ops, tmp_path):
        """A run that evaluated nothing writes no tracker-additions, and
        easy-apply-urls.txt only exists when a pass produced them. The upload
        step's if-no-files-found used to absorb that; now this does."""
        _, whole = run_artifact.stage(
            career_ops, tmp_path / "artifact", {}, [],
            ["data/applications.md", "data/easy-apply-urls.txt",
             "batch/tracker-additions"])
        assert whole == ["data/applications.md"]

    def test_staged_layout_mirrors_the_source(self, career_ops, tmp_path):
        """`gh.download_artifact` looks for reports/ or data/ at the artifact
        root, and `data.sync_pulled_tracker` reads data/applications.md from
        there. Staging must not reshape the tree those two agreed on."""
        into = tmp_path / "artifact"
        run_artifact.stage(career_ops, into, {}, ["reports"], ["data/applications.md"])
        assert (into / "reports").is_dir()
        assert (into / "data" / "applications.md").is_file()


class TestManifestIsRequired:
    def test_missing_manifest_refuses_rather_than_uploading_everything(self, tmp_path):
        """Degrading to "no manifest means nothing was there" would restore the
        unbounded upload silently, on the one path nobody watches."""
        with pytest.raises(SystemExit) as exc:
            run_artifact._read_manifest(tmp_path / "absent.json", tmp_path, ["reports"])
        assert "snapshot step" in str(exc.value)

    def test_a_manifest_of_the_wrong_thing_refuses_too(self, tmp_path):
        """Same failure, same reason: keys that don't line up make every restored
        file read as new, which is the unbounded artifact again."""
        m = tmp_path / "manifest.json"
        m.write_text(json.dumps(
            {"scope": run_artifact._scope(tmp_path, ["reports"]), "files": {}}),
            encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            run_artifact._read_manifest(m, tmp_path, ["batch/jds"])
        assert "same" in str(exc.value) and "--delta" in str(exc.value)

    def test_a_matching_scope_is_accepted(self, tmp_path):
        m = tmp_path / "manifest.json"
        m.write_text(json.dumps(
            {"scope": run_artifact._scope(tmp_path, ["reports"]), "files": {"a": [1, 2]}}),
            encoding="utf-8")
        assert run_artifact._read_manifest(m, tmp_path, ["reports"]) == {"a": [1, 2]}


class TestCli:
    """The workflow calls this as `python -m pipeline.run_artifact`, so the
    argument surface is part of the contract, not an implementation detail."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "pipeline.run_artifact", *map(str, args)],
            cwd=ROOT, capture_output=True, text=True)

    def test_snapshot_then_stage_yields_only_the_new_report(self, career_ops, tmp_path):
        manifest = tmp_path / "manifest.json"
        snap = self._run("snapshot", "--root", career_ops, "--manifest", manifest,
                         "--delta", "reports")
        assert snap.returncode == 0, snap.stderr
        assert len(json.loads(manifest.read_text(encoding="utf-8"))["files"]) == 2

        _write(career_ops / "reports" / "003-initech-2026-06-03.md", "today\n")

        into = tmp_path / "artifact"
        staged = self._run("stage", "--root", career_ops, "--manifest", manifest,
                           "--delta", "reports", "--whole", "data/applications.md",
                           "--into", into)
        assert staged.returncode == 0, staged.stderr
        assert [p.name for p in (into / "reports").iterdir()] == \
            ["003-initech-2026-06-03.md"]
        assert (into / "data" / "applications.md").exists()

    def test_snapshot_of_an_absent_tree_still_writes_a_manifest(self, tmp_path):
        """So the stage step's hard requirement for one can't fail a first run."""
        manifest = tmp_path / "manifest.json"
        out = self._run("snapshot", "--root", tmp_path / "nothing-here",
                        "--manifest", manifest, "--delta", "reports")
        assert out.returncode == 0, out.stderr
        assert json.loads(manifest.read_text(encoding="utf-8"))["files"] == {}

    def test_stage_rebuilds_the_target_tree(self, career_ops, tmp_path):
        """A leftover tree from a re-run of the step would upload files this run
        neither produced nor knows about."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(
            {"scope": run_artifact._scope(career_ops, ["reports"]), "files": {}}),
            encoding="utf-8")
        into = tmp_path / "artifact"
        _write(into / "reports" / "stale-from-a-previous-attempt.md", "x")

        out = self._run("stage", "--root", career_ops, "--manifest", manifest,
                        "--delta", "reports", "--into", into)
        assert out.returncode == 0, out.stderr
        assert not (into / "reports" / "stale-from-a-previous-attempt.md").exists()

    def test_a_refused_stage_leaves_the_target_tree_alone(self, career_ops, tmp_path):
        """Both refusals above are fatal, so wiping --into on the way to one
        would destroy the only copy of what a re-run was meant to inspect."""
        into = tmp_path / "artifact"
        _write(into / "reports" / "staged-earlier.md", "keep me")

        out = self._run("stage", "--root", career_ops,
                        "--manifest", tmp_path / "absent.json",
                        "--delta", "reports", "--into", into)
        assert out.returncode != 0
        assert (into / "reports" / "staged-earlier.md").read_text(encoding="utf-8") \
            == "keep me"

    def test_stage_without_a_manifest_exits_nonzero(self, career_ops, tmp_path):
        out = self._run("stage", "--root", career_ops,
                        "--manifest", tmp_path / "absent.json",
                        "--delta", "reports", "--into", tmp_path / "artifact")
        assert out.returncode != 0
        assert "snapshot step" in (out.stderr + out.stdout)
