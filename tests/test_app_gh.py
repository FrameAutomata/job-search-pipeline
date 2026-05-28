"""Tests for pipeline/app/gh.py — gh CLI wrappers with mocked subprocess.

These verify the *command construction* and *output handling*. They don't hit
the real gh CLI or GitHub (that needs the user's authed environment + a repo
with real runs), so live behavior is verified manually.
"""

import json
import subprocess

import pytest

from pipeline.app import gh

# Captured before the autouse fixture patches it, so one test can exercise the
# real origin-resolution logic with a mocked subprocess.
_REAL_ORIGIN_REPO = gh._origin_repo


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _isolate_repo_target(monkeypatch):
    """Default: no env override and no origin remote, so _repo_args/_positional
    add no repo flag and the subprocess.run mocks only ever see `gh` calls.
    Tests that care about repo targeting override _origin_repo / set the env."""
    monkeypatch.delenv("JOB_SEARCH_REPO", raising=False)
    monkeypatch.setattr(gh, "_origin_repo", lambda: None)


class TestResolveGh:
    def test_gh_bin_env_wins(self, mocker, monkeypatch, tmp_path):
        # GH_BIN should override shutil.which even when PATH has gh, so users
        # can point us at a working executable explicitly.
        fake = tmp_path / "my-gh.exe"
        fake.write_text("")
        monkeypatch.setenv("GH_BIN", str(fake))
        # shutil.which would normally win, but GH_BIN wins ahead of it.
        which = mocker.patch("pipeline.app.gh.shutil.which", return_value="/some/other/gh")
        assert gh._resolve_gh() == str(fake)
        which.assert_not_called()

    def test_falls_back_to_which(self, monkeypatch, mocker):
        monkeypatch.delenv("GH_BIN", raising=False)
        mocker.patch("pipeline.app.gh.shutil.which", return_value="/usr/bin/gh")
        assert gh._resolve_gh() == "/usr/bin/gh"

    def test_returns_none_when_nothing_resolves(self, monkeypatch, mocker):
        # No GH_BIN, nothing on PATH; on Windows the standard install paths are
        # missing too. Resolver returns None so _run can fail fast with guidance.
        monkeypatch.delenv("GH_BIN", raising=False)
        mocker.patch("pipeline.app.gh.shutil.which", return_value=None)
        mocker.patch("pipeline.app.gh.os.path.isfile", return_value=False)
        assert gh._resolve_gh() is None


class TestRunErrorHandling:
    def test_missing_gh_raises_install_hint(self, mocker):
        # Resolver finds nothing → fail fast with the install/PATH guidance,
        # without ever calling subprocess.
        mocker.patch("pipeline.app.gh._resolve_gh", return_value=None)
        with pytest.raises(gh.GhError, match="gh CLI not found"):
            gh.current_repo()

    def test_resolver_path_but_exec_fails(self, mocker):
        # Edge: resolver claimed a path existed, but launching it raises
        # FileNotFoundError (broken alias / removed between checks). Surface
        # the path so the user can see what we tried.
        mocker.patch("pipeline.app.gh._resolve_gh", return_value="/fake/gh")
        mocker.patch("pipeline.app.gh.subprocess.run", side_effect=FileNotFoundError())
        with pytest.raises(gh.GhError, match="Couldn't launch gh at '/fake/gh'"):
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


class TestParseOwnerName:
    @pytest.mark.parametrize("url,expected", [
        ("https://github.com/Owner/my-repo.git", "Owner/my-repo"),
        ("https://github.com/Owner/my-repo", "Owner/my-repo"),
        ("https://github.com/Owner/my-repo/", "Owner/my-repo"),
        ("git@github.com:Owner/my-repo.git", "Owner/my-repo"),
        ("ssh://git@github.com/Owner/my-repo.git", "Owner/my-repo"),
    ])
    def test_parse(self, url, expected):
        assert gh._parse_owner_name(url) == expected

    def test_origin_repo_reads_git_remote(self, mocker):
        # Exercise the real _origin_repo against a mocked `git remote get-url`.
        mocker.patch("pipeline.app.gh.subprocess.run",
                     return_value=_completed("https://github.com/Me/whatever-they-named-it.git\n"))
        assert _REAL_ORIGIN_REPO() == "Me/whatever-they-named-it"


class TestRepoArgs:
    def test_no_env_no_origin_adds_no_flag(self, mocker):
        # Single-remote clone (origin resolves fine on its own) -> let gh decide.
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("[]"))
        gh.latest_run("daily-pipeline.yml")
        assert "-R" not in run.call_args.args[0]

    def test_falls_back_to_origin_repo(self, mocker):
        # No env override, but origin resolves -> pin to it (multi-remote case).
        mocker.patch.object(gh, "_origin_repo", lambda: "whoever/their-repo")
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("[]"))
        gh.latest_run("daily-pipeline.yml")
        args = run.call_args.args[0]
        assert "-R" in args and "whoever/their-repo" in args

    def test_env_override_wins_over_origin(self, mocker, monkeypatch):
        monkeypatch.setenv("JOB_SEARCH_REPO", "me/private")
        mocker.patch.object(gh, "_origin_repo", lambda: "other/repo")
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("[]"))
        gh.latest_run("daily-pipeline.yml")
        args = run.call_args.args[0]
        assert "me/private" in args and "other/repo" not in args

    def test_repo_view_uses_positional_not_dash_R(self, mocker, monkeypatch):
        # `gh repo view` does NOT accept -R — the repo is positional.
        monkeypatch.setenv("JOB_SEARCH_REPO", "me/private")
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed("me/private\n"))
        gh.current_repo()
        args = run.call_args.args[0]
        assert "-R" not in args
        assert "me/private" in args  # positional


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
        # args[0] is the resolved gh path, not bare "gh", so check the verb pair.
        assert args[1:3] == ["workflow", "run"]
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
        # Command shape: gh secret set NAME [-R repo]. Crucially NO --body: gh
        # reads the value from stdin only when --body is omitted (`--body -`
        # would store the literal "-", which is the bug this guards against).
        assert args[1:3] == ["secret", "set"]
        assert "GEMINI_API_KEY" in args
        assert "--body" not in args
        # The value must NOT appear in argv (process listing / argv limits)...
        assert "super-secret-value" not in args
        # ...it must be piped via stdin.
        assert run.call_args.kwargs.get("input") == "super-secret-value"

    def test_set_variable_uses_body(self, mocker):
        run = mocker.patch("pipeline.app.gh.subprocess.run", return_value=_completed(""))
        gh.set_variable("BATCH_PROVIDER", "gemini")
        args = run.call_args.args[0]
        assert args[1:3] == ["variable", "set"]
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
