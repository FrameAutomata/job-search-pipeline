"""Tests for pipeline/app/reset.py — the "start over" reset.

reset_job_search() snapshots then wipes the accumulated job-search state (tracker,
history, reports, queue, batch, outputs, UI overlays) while keeping the user's
setup (profile, CV, modes/_profile, search config, resumes) and all system code.
clear_cloud_caches() deletes the cloud GitHub Actions state cache so a Refresh /
next run can't restore the old history.
"""

import json

import pytest

from pipeline.app import reset
from pipeline.app import gh


def _build(tmp):
    co, repo = tmp / "career-ops", tmp / "repo"
    uic = repo / ".ui-cache"

    # --- career-ops SETUP (keep) ---
    (co / "config").mkdir(parents=True)
    (co / "config" / "profile.yml").write_text("profile", encoding="utf-8")
    (co / "cv.md").write_text("cv", encoding="utf-8")
    (co / "modes").mkdir()
    (co / "modes" / "_profile.md").write_text("prof", encoding="utf-8")
    (co / "modes" / "_shared.md").write_text("shared", encoding="utf-8")   # system code
    (co / "scan.mjs").write_text("code", encoding="utf-8")                  # system code

    # --- career-ops SEARCH STATE (wipe) ---
    (co / "data").mkdir()
    for f in ("applications.md", "scan-history.tsv", "pipeline.md",
              "recheck-state.tsv", "easy-apply-urls.txt", "follow-ups.md"):
        (co / "data" / f).write_text("x", encoding="utf-8")
    (co / "reports").mkdir()
    (co / "reports" / ".gitkeep").write_text("", encoding="utf-8")
    (co / "reports" / "001-x.md").write_text("r", encoding="utf-8")
    (co / "output").mkdir()
    (co / "output" / ".gitkeep").write_text("", encoding="utf-8")
    (co / "output" / "Acme - resume.pdf").write_text("pdf", encoding="utf-8")
    (co / "jds").mkdir()
    (co / "jds" / "a.txt").write_text("jd", encoding="utf-8")
    (co / "batch").mkdir()
    for f in ("batch-input.tsv", "batch-state.tsv", "batch-api-state.json"):
        (co / "batch" / f).write_text("b", encoding="utf-8")
    (co / "batch" / "jds").mkdir()
    (co / "batch" / "jds" / "1.txt").write_text("j", encoding="utf-8")
    (co / "batch" / "tracker-additions").mkdir()
    (co / "batch" / "tracker-additions" / "1.tsv").write_text("t", encoding="utf-8")

    # --- repo output (wipe) + setup (keep) ---
    (repo / "output").mkdir(parents=True)
    for f in ("jobs.csv", "filtered_jobs.csv", "_keywords.json"):
        (repo / "output" / f).write_text("o", encoding="utf-8")
    art = repo / "output" / "pipeline-output-123"
    art.mkdir()
    (art / "x").write_text("art", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config" / "search.yml").write_text("search", encoding="utf-8")   # keep
    (repo / "resumes").mkdir()
    (repo / "resumes" / "resume.pdf").write_text("res", encoding="utf-8")      # keep

    # --- .ui-cache (wipe overlays/cache, keep onboarding.json) ---
    uic.mkdir(parents=True)
    (uic / "status-overrides.json").write_text("{}", encoding="utf-8")
    (uic / "pushed-overrides.json").write_text("{}", encoding="utf-8")
    (uic / "onboarding.json").write_text("{}", encoding="utf-8")              # keep
    (uic / "latest" / "data").mkdir(parents=True)
    (uic / "latest" / "data" / "applications.md").write_text("cloud", encoding="utf-8")
    (uic / "apply").mkdir()
    (uic / "apply" / "x").write_text("a", encoding="utf-8")
    return co, repo, uic


class TestResetJobSearch:
    def test_wipes_search_state(self, tmp_path):
        co, repo, uic = _build(tmp_path)
        reset.reset_job_search(co, repo, uic, tmp_path / "backup")
        for p in [
            co / "data" / "applications.md", co / "data" / "scan-history.tsv",
            co / "data" / "pipeline.md", co / "data" / "recheck-state.tsv",
            co / "data" / "easy-apply-urls.txt", co / "data" / "follow-ups.md",
            co / "reports" / "001-x.md", co / "output" / "Acme - resume.pdf",
            co / "jds" / "a.txt", co / "batch" / "batch-input.tsv",
            co / "batch" / "batch-state.tsv", co / "batch" / "batch-api-state.json",
            co / "batch" / "jds" / "1.txt", co / "batch" / "tracker-additions" / "1.tsv",
            repo / "output" / "jobs.csv", repo / "output" / "filtered_jobs.csv",
            repo / "output" / "_keywords.json", repo / "output" / "pipeline-output-123",
            uic / "status-overrides.json", uic / "pushed-overrides.json",
            uic / "latest", uic / "apply",
        ]:
            assert not p.exists(), f"should be wiped: {p}"

    def test_keeps_setup_and_system_code(self, tmp_path):
        co, repo, uic = _build(tmp_path)
        reset.reset_job_search(co, repo, uic, tmp_path / "backup")
        for p in [
            co / "config" / "profile.yml", co / "cv.md", co / "modes" / "_profile.md",
            co / "modes" / "_shared.md", co / "scan.mjs",
            repo / "config" / "search.yml", repo / "resumes" / "resume.pdf",
            uic / "onboarding.json",
        ]:
            assert p.exists(), f"should be kept: {p}"

    def test_preserves_dir_scaffolding(self, tmp_path):
        co, repo, uic = _build(tmp_path)
        reset.reset_job_search(co, repo, uic, tmp_path / "backup")
        assert (co / "reports").is_dir()
        assert (co / "reports" / ".gitkeep").exists()
        assert (co / "output" / ".gitkeep").exists()

    def test_snapshots_before_wipe(self, tmp_path):
        co, repo, uic = _build(tmp_path)
        backup = tmp_path / "backup"
        reset.reset_job_search(co, repo, uic, backup)
        assert (backup / "career-ops" / "data" / "applications.md").read_text(encoding="utf-8") == "x"
        assert (backup / "career-ops" / "reports" / "001-x.md").read_text(encoding="utf-8") == "r"
        assert (backup / "repo" / "output" / "jobs.csv").read_text(encoding="utf-8") == "o"
        assert (backup / ".ui-cache" / "status-overrides.json").exists()

    def test_tolerant_of_missing_paths(self, tmp_path):
        # Already-clean / nonexistent dirs must not raise.
        reset.reset_job_search(tmp_path / "co", tmp_path / "repo",
                               tmp_path / "uic", tmp_path / "backup")


class TestClearCloudCaches:
    def test_deletes_only_matching_prefix(self, mocker):
        listing = json.dumps([
            {"id": 1, "key": "pipeline-state-v1-100"},
            {"id": 2, "key": "some-other-cache"},
            {"id": 3, "key": "pipeline-state-v1-200"},
        ])
        deleted = []

        def fake_run(args, **kw):
            if args[:2] == ["cache", "list"]:
                return listing
            if args[:2] == ["cache", "delete"]:
                deleted.append(args[2])
                return ""
            return ""
        mocker.patch.object(gh, "_run", side_effect=fake_run)
        r = reset.clear_cloud_caches()
        assert set(r["deleted"]) == {"pipeline-state-v1-100", "pipeline-state-v1-200"}
        assert "2" not in deleted   # the non-matching cache was left alone

    def test_no_match_deletes_nothing(self, mocker):
        mocker.patch.object(gh, "_run",
                            return_value=json.dumps([{"id": 1, "key": "unrelated"}]))
        assert reset.clear_cloud_caches()["deleted"] == []

    def test_gh_error_propagates(self, mocker):
        mocker.patch.object(gh, "_run", side_effect=gh.GhError("not authenticated"))
        with pytest.raises(gh.GhError):
            reset.clear_cloud_caches()
