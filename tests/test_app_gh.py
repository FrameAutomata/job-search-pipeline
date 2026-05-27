"""Tests for pipeline/app/gh.py — gh CLI wrappers with mocked subprocess.

These verify the *command construction* and *output handling*. They don't hit
the real gh CLI or GitHub (that needs the user's authed environment + a repo
with real runs), so live behavior is verified manually.
"""

import json
import subprocess

import pytest

from pipeline.app import gh


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestRunErrorHandling:
    def test_missing_gh_raises_install_hint(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run", side_effect=FileNotFoundError())
        with pytest.raises(gh.GhError, match="gh CLI not found"):
            gh.current_repo()

    def test_auth_failure_message(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run",
                     return_value=_completed(returncode=1, stderr="error: not logged into any hosts"))
        with pytest.raises(gh.GhError, match="not authenticated"):
            gh.current_repo()

    def test_generic_failure_surfaces_stderr(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run",
                     return_value=_completed(returncode=1, stderr="boom"))
        with pytest.raises(gh.GhError, match="boom"):
            gh.current_repo()

    def test_timeout_raises(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run",
                     side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=5))
        with pytest.raises(gh.GhError, match="timed out"):
            gh.current_repo()


class TestRepoArgs:
    def test_no_override_no_repo_flag(self, mocker, monkeypatch):
        monkeypatch.delenv("JOB_SEARCH_REPO", raising=False)
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("owner/repo\n"))
        gh.current_repo()
        args = run.call_args.args[0]
        assert "-R" not in args

    def test_override_adds_repo_flag(self, mocker, monkeypatch):
        monkeypatch.setenv("JOB_SEARCH_REPO", "me/job-search-private")
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("me/job-search-private\n"))
        gh.current_repo()
        args = run.call_args.args[0]
        assert "-R" in args and "me/job-search-private" in args


class TestLatestRun:
    def test_returns_first_run(self, mocker):
        runs = [{"databaseId": 123, "status": "completed", "conclusion": "success",
                 "createdAt": "2026-05-27T00:00:00Z", "displayTitle": "Daily"}]
        mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(json.dumps(runs)))
        r = gh.latest_run("daily-pipeline.yml")
        assert r["databaseId"] == 123

    def test_returns_none_when_no_runs(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("[]"))
        assert gh.latest_run("daily-pipeline.yml") is None


class TestTriggerWorkflow:
    def test_builds_workflow_run_command(self, mocker):
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(""))
        gh.trigger_workflow("daily-pipeline.yml")
        args = run.call_args.args[0]
        assert args[:3] == ["gh", "workflow", "run"]
        assert "daily-pipeline.yml" in args

    def test_passes_fields(self, mocker):
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(""))
        gh.trigger_workflow("edit-tracker.yml", fields={"applications_md_b64": "QUJD"})
        args = run.call_args.args[0]
        assert "-f" in args
        assert "applications_md_b64=QUJD" in args


class TestDownloadArtifact:
    def test_returns_subdir_with_data(self, tmp_path, mocker):
        # Simulate gh extracting into dest/pipeline-output-NNN/{data,reports}
        def fake_run(args, **kwargs):
            # find the --dir value
            dest = args[args.index("--dir") + 1]
            from pathlib import Path
            sub = Path(dest) / "pipeline-output-999"
            (sub / "data").mkdir(parents=True)
            (sub / "reports").mkdir(parents=True)
            return _completed("")
        mocker.patch("pipeline.app.gh.subprocess.run", side_effect=fake_run)
        result = gh.download_artifact(999, tmp_path / "cache")
        assert result.name == "pipeline-output-999"
        assert (result / "data").exists()

    def test_returns_dest_when_extracted_flat(self, tmp_path, mocker):
        # Some gh versions extract a single artifact's contents directly.
        def fake_run(args, **kwargs):
            dest = args[args.index("--dir") + 1]
            from pathlib import Path
            (Path(dest) / "reports").mkdir(parents=True)
            return _completed("")
        mocker.patch("pipeline.app.gh.subprocess.run", side_effect=fake_run)
        result = gh.download_artifact(999, tmp_path / "cache")
        assert (result / "reports").exists()

    def test_raises_when_no_output_found(self, tmp_path, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(""))
        with pytest.raises(gh.GhError, match="no reports/ or data/"):
            gh.download_artifact(999, tmp_path / "cache")
