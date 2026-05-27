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
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("[]"))
        gh.latest_run("daily-pipeline.yml")
        args = run.call_args.args[0]
        assert "-R" not in args

    def test_override_adds_repo_flag(self, mocker, monkeypatch):
        # Subcommands that accept --repo (run/secret/variable) use -R.
        monkeypatch.setenv("JOB_SEARCH_REPO", "me/job-search-private")
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("[]"))
        gh.latest_run("daily-pipeline.yml")
        args = run.call_args.args[0]
        assert "-R" in args and "me/job-search-private" in args

    def test_repo_view_uses_positional_not_dash_R(self, mocker, monkeypatch):
        # `gh repo view` does NOT accept -R — the repo is positional.
        monkeypatch.setenv("JOB_SEARCH_REPO", "me/job-search-private")
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("me/job-search-private\n"))
        gh.current_repo()
        args = run.call_args.args[0]
        assert "-R" not in args
        assert "me/job-search-private" in args  # positional


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


class TestLatestSuccessfulRun:
    def _per_workflow(self, mapping):
        """Build a subprocess.run side_effect that returns each workflow's runs
        based on the --workflow value in argv."""
        def fake(args, **kwargs):
            wf = args[args.index("--workflow") + 1]
            return _completed(json.dumps(mapping.get(wf, [])))
        return fake

    def test_picks_newest_across_workflows(self, mocker):
        mapping = {
            "daily-pipeline.yml": [
                {"databaseId": 1, "status": "completed", "conclusion": "success",
                 "createdAt": "2026-05-27T00:00:00Z", "displayTitle": "Daily"}],
            "easy-apply-pipeline.yml": [
                {"databaseId": 2, "status": "completed", "conclusion": "success",
                 "createdAt": "2026-05-27T06:00:00Z", "displayTitle": "Easy Apply"}],
        }
        mocker.patch("pipeline.app.gh.subprocess.run",
                     side_effect=self._per_workflow(mapping))
        r = gh.latest_successful_run(["daily-pipeline.yml", "easy-apply-pipeline.yml"])
        assert r["databaseId"] == 2  # easy-apply ran later

    def test_skips_failed_or_in_progress(self, mocker):
        mapping = {
            "daily-pipeline.yml": [
                {"databaseId": 1, "status": "completed", "conclusion": "success",
                 "createdAt": "2026-05-27T00:00:00Z"}],
            "easy-apply-pipeline.yml": [
                # newest is in-progress (no artifact); next is failed; then success.
                {"databaseId": 4, "status": "in_progress", "conclusion": None,
                 "createdAt": "2026-05-27T08:00:00Z"},
                {"databaseId": 3, "status": "completed", "conclusion": "failure",
                 "createdAt": "2026-05-27T04:00:00Z"},
                {"databaseId": 2, "status": "completed", "conclusion": "success",
                 "createdAt": "2026-05-26T20:00:00Z"}],
        }
        mocker.patch("pipeline.app.gh.subprocess.run",
                     side_effect=self._per_workflow(mapping))
        r = gh.latest_successful_run(["daily-pipeline.yml", "easy-apply-pipeline.yml"])
        # easy-apply's only success (2) predates daily's success (1) -> daily wins.
        assert r["databaseId"] == 1

    def test_returns_none_when_none_successful(self, mocker):
        mapping = {"daily-pipeline.yml": [
            {"databaseId": 1, "status": "completed", "conclusion": "failure",
             "createdAt": "2026-05-27T00:00:00Z"}]}
        mocker.patch("pipeline.app.gh.subprocess.run",
                     side_effect=self._per_workflow(mapping))
        assert gh.latest_successful_run(["daily-pipeline.yml"]) is None


class TestRepoVisibility:
    def test_returns_uppercased(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("private\n"))
        assert gh.repo_visibility() == "PRIVATE"


class TestSecrets:
    def test_list_secret_names_splits_lines(self, mocker):
        mocker.patch("pipeline.app.gh.subprocess.run",
                     return_value=_completed("CV_MD_B64\nGEMINI_API_KEY\n"))
        assert gh.list_secret_names() == ["CV_MD_B64", "GEMINI_API_KEY"]

    def test_set_secret_pipes_value_via_stdin_not_argv(self, mocker):
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(""))
        gh.set_secret("GEMINI_API_KEY", "super-secret-value")
        args = run.call_args.args[0]
        # Command shape: gh secret set NAME [--R repo] --body -
        assert args[:3] == ["gh", "secret", "set"]
        assert "GEMINI_API_KEY" in args
        assert "-" in args  # --body - sentinel
        # The value must NOT appear in argv (process listing / argv limits).
        assert "super-secret-value" not in args
        # It must be piped via stdin.
        assert run.call_args.kwargs.get("input") == "super-secret-value"

    def test_set_variable_uses_body(self, mocker):
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(""))
        gh.set_variable("BATCH_PROVIDER", "gemini")
        args = run.call_args.args[0]
        assert args[:3] == ["gh", "variable", "set"]
        assert "BATCH_PROVIDER" in args and "gemini" in args


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
