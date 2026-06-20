"""Tests for pipeline/app/self_update.py — the template self-update logic.

update_available(): does the user's copy already contain the template's latest
main commit? (template-copies aren't forks, so we check commit membership via
the gh API rather than a cross-repo compare.)

apply_update(): local `git fetch template main → merge → push origin`, aborting
cleanly on conflict — updates the local UI clone AND the cloud copy in one shot.
"""

from types import SimpleNamespace

import pytest

from pipeline.app import self_update
from pipeline.app import gh


class TestUpdateAvailable:
    def test_up_to_date_when_copy_contains_template_head(self, mocker):
        def fake_run(args, **kw):
            if "commits/main" in args[1]:
                return "tpl-sha-123\n"
            return ""  # repos/<copy>/commits/<sha> → exit 0 = commit present
        mocker.patch.object(gh, "_run", side_effect=fake_run)
        r = self_update.update_available(copy_repo="user/copy")
        assert r["available"] is False
        assert r["template_sha"] == "tpl-sha-123"

    def test_update_available_when_commit_missing(self, mocker):
        def fake_run(args, **kw):
            if "commits/main" in args[1]:
                return "tpl-sha-123\n"
            raise gh.GhError("gh: Not Found (HTTP 404)")
        mocker.patch.object(gh, "_run", side_effect=fake_run)
        r = self_update.update_available(copy_repo="user/copy")
        assert r["available"] is True
        assert r["template_sha"] == "tpl-sha-123"

    def test_no_update_when_copy_is_the_template(self, mocker):
        run = mocker.patch.object(gh, "_run")
        r = self_update.update_available(copy_repo=self_update.TEMPLATE_REPO)
        assert r["available"] is False
        run.assert_not_called()   # short-circuits — never hits the API

    def test_non_404_gh_error_propagates(self, mocker):
        def fake_run(args, **kw):
            if "commits/main" in args[1]:
                return "tpl-sha-123\n"
            raise gh.GhError("HTTP 500: server error")
        mocker.patch.object(gh, "_run", side_effect=fake_run)
        with pytest.raises(gh.GhError):
            self_update.update_available(copy_repo="user/copy")


def _cp(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestApplyUpdate:
    def _fake_git(self, mocker, overrides=None):
        """Patch _run_git with all-success defaults, overriding by subcommand
        signature (a space-joined prefix of the git args)."""
        overrides = overrides or {}
        calls = []

        def run_git(repo_root, *args):
            calls.append(args)
            sig = " ".join(args)
            for s, cp in overrides.items():
                if sig.startswith(s):
                    return cp
            # Happy-path defaults for the guards: on main, clean tree.
            if sig.startswith("rev-parse --abbrev-ref"):
                return _cp(0, "main\n")
            if sig.startswith("status --porcelain"):
                return _cp(0, "")
            return _cp(0, "", "")
        mocker.patch.object(self_update, "_run_git", side_effect=run_git)
        return calls

    def test_clean_update_merges_and_pushes(self, tmp_path, mocker):
        calls = self._fake_git(mocker, {"merge --no-edit": _cp(0, "Updating a..b\n")})
        r = self_update.apply_update(tmp_path)
        assert r == {"ok": True, "updated": True}
        sigs = [" ".join(a) for a in calls]
        assert any(s.startswith("fetch ") for s in sigs)
        assert any(s.startswith("merge --no-edit") for s in sigs)
        assert any(s.startswith("push origin") for s in sigs)

    def test_already_up_to_date_skips_push(self, tmp_path, mocker):
        calls = self._fake_git(mocker, {"merge --no-edit": _cp(0, "Already up to date.\n")})
        r = self_update.apply_update(tmp_path)
        assert r == {"ok": True, "updated": False}
        assert not any(" ".join(a).startswith("push") for a in calls)

    def test_merge_conflict_aborts_and_does_not_push(self, tmp_path, mocker):
        calls = self._fake_git(mocker, {
            "merge --no-edit": _cp(1, "", "CONFLICT (content): merge conflict in x"),
        })
        r = self_update.apply_update(tmp_path)
        assert r["ok"] is False and r["conflict"] is True
        sigs = [" ".join(a) for a in calls]
        assert any(s.startswith("merge --abort") for s in sigs)
        assert not any(s.startswith("push") for s in sigs)

    def test_push_failure_reported(self, tmp_path, mocker):
        self._fake_git(mocker, {
            "merge --no-edit": _cp(0, "Updating a..b\n"),
            "push origin": _cp(1, "", "remote rejected"),
        })
        r = self_update.apply_update(tmp_path)
        assert r["ok"] is False
        assert "push" in r["error"].lower()

    def test_not_a_git_repo(self, tmp_path, mocker):
        self._fake_git(mocker, {"rev-parse": _cp(128, "", "not a git repository")})
        r = self_update.apply_update(tmp_path)
        assert r["ok"] is False
        assert "git" in r["error"].lower()

    def test_no_origin_remote(self, tmp_path, mocker):
        self._fake_git(mocker, {"remote get-url origin": _cp(2, "", "no such remote")})
        r = self_update.apply_update(tmp_path)
        assert r["ok"] is False
        assert "origin" in r["error"].lower()

    def test_refuses_when_not_on_main(self, tmp_path, mocker):
        # On a feature branch, merging+pushing would pollute it and miss main.
        calls = self._fake_git(mocker, {"rev-parse --abbrev-ref": _cp(0, "feature-x\n")})
        r = self_update.apply_update(tmp_path)
        assert r["ok"] is False
        assert "main" in r["error"].lower()
        sigs = [" ".join(a) for a in calls]
        assert not any(s.startswith(("fetch", "merge", "push")) for s in sigs)

    def test_refuses_dirty_working_tree(self, tmp_path, mocker):
        # Uncommitted changes make `git merge` refuse pre-merge — that's NOT a
        # conflict; report it distinctly so the user commits/stashes.
        calls = self._fake_git(mocker, {"status --porcelain": _cp(0, " M orchestrate.py\n")})
        r = self_update.apply_update(tmp_path)
        assert r["ok"] is False
        assert r.get("conflict") is not True
        assert ("commit" in r["error"].lower()) or ("stash" in r["error"].lower())
        sigs = [" ".join(a) for a in calls]
        assert not any(s.startswith(("merge", "push")) for s in sigs)
