"""Route smoke tests for pipeline/app/server.py.

Skips entirely if FastAPI isn't installed (it's an optional UI dependency in
requirements-ui.txt, not part of the core pipeline deps)."""

import importlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Point the server at a tmp career-ops dir with one tracker row + report,
    then return a TestClient bound to a freshly-imported app."""
    career_ops = tmp_path / "career-ops"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "reports").mkdir(parents=True)
    (career_ops / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-05-27 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme.md) | APPLY |\n",
        encoding="utf-8",
    )
    (career_ops / "reports" / "001-acme.md").write_text(
        "# Acme — Eng\n\n**Score:** 4.2/5\n\nGreat match.", encoding="utf-8"
    )
    monkeypatch.setenv("CAREER_OPS_PATH", str(career_ops))

    # Re-import the server so its module-level paths pick up the env var. The
    # _career_ops() helper reads the env at request time, so a plain import is
    # fine, but reload keeps the test hermetic across runs.
    from pipeline.app import server
    importlib.reload(server)
    return TestClient(server.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["applications_md_exists"] is True


def test_list_jobs(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    payload = r.json()
    assert payload["source"] == "applications"
    jobs = payload["rows"]
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["score_value"] == 4.2
    assert jobs[0]["report_num"] == "001"


def test_get_report_renders(client):
    r = client.get("/api/reports/001")
    assert r.status_code == 200
    assert "Acme" in r.text
    assert "Great match" in r.text


def test_get_report_404_for_unknown(client):
    r = client.get("/api/reports/999")
    assert r.status_code == 404


def test_index_served(client):
    # The SPA shell should be served at root.
    r = client.get("/")
    assert r.status_code == 200
    assert "Triage" in r.text


def test_run_triggers_workflow(client, mocker):
    from pipeline.app import server
    trigger = mocker.patch.object(server.gh, "trigger_workflow")
    r = client.post("/api/run")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    trigger.assert_called_once_with(server.DAILY_WORKFLOW)


def test_run_surfaces_gh_error(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.gh, "trigger_workflow",
                        side_effect=server.gh.GhError("gh not authenticated"))
    r = client.post("/api/run")
    assert r.status_code == 502
    assert "not authenticated" in r.json()["detail"]


def test_refresh_404_when_no_runs(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.gh, "latest_run", return_value=None)
    r = client.post("/api/refresh")
    assert r.status_code == 404


def test_refresh_downloads_and_repoints(client, tmp_path, mocker):
    from pipeline.app import server
    art = tmp_path / "dl" / "pipeline-output-7"
    (art / "data").mkdir(parents=True)
    (art / "reports").mkdir(parents=True)
    (art / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 9 | 2026-05-27 | Refreshed Co | Eng | 4.9/5 | Evaluated | ❌ | [009](reports/009-x.md) | APPLY |\n",
        encoding="utf-8",
    )
    mocker.patch.object(server.gh, "latest_run",
                        return_value={"databaseId": 7, "createdAt": "t", "displayTitle": "Daily"})
    mocker.patch.object(server.gh, "download_artifact", return_value=art)
    try:
        r = client.post("/api/refresh")
        assert r.status_code == 200
        assert r.json()["run_id"] == 7
        # After refresh, /api/jobs reads the downloaded dir, not the env one.
        jobs = client.get("/api/jobs").json()
        assert jobs["rows"][0]["company"] == "Refreshed Co"
    finally:
        server._active_data_dir = None  # reset module state for other tests
