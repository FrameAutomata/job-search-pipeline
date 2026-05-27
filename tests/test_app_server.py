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
