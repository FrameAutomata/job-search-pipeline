"""UI apply-run endpoint: the report pane's per-role ⚡ Apply button actually
DRIVES the apply ladder in the user's browser (via OpenClaw), unlike the
paste-prompt hand-off. Runs in the background and is polled; on a wall the
ladder fires a desktop toast. Skips if FastAPI isn't installed."""

import importlib
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pipeline.apply_driver import ApplyReport  # noqa: E402


TRACKER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-07-01 | Acme | AI Engineer | 4.8/5 | Evaluated | X | [001](reports/001-acme.md) | https://job-boards.greenhouse.io/acme/jobs/1 — strong fit |\n"
    "| 2 | 2026-07-01 | Globex | Backend Engineer | 4.2/5 | Evaluated | X | [002](reports/002-globex.md) | no url recorded here |\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    career_ops = tmp_path / "career-ops"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "reports").mkdir()
    (career_ops / "data" / "applications.md").write_text(TRACKER, encoding="utf-8")
    (career_ops / "reports" / "001-acme.md").write_text("# Acme\nFit.", encoding="utf-8")
    monkeypatch.setenv("CAREER_OPS_PATH", str(career_ops))
    monkeypatch.setenv("HANDOFF_OUT_DIR", str(tmp_path / "agent-home"))

    from pipeline.app import server
    importlib.reload(server)
    server.PUSHED_OVERRIDES_FILE = tmp_path / ".ui-cache" / "pushed-overrides.json"
    server.UI_CACHE = tmp_path / ".ui-cache" / "latest"

    # A profile always resolves (truthy) and the browser never really launches.
    monkeypatch.setattr("pipeline.handoff.resolve_profile_md", lambda: "PROFILE")
    monkeypatch.setattr("pipeline.openclaw_client.OpenClawBrowser", lambda *a, **k: object())
    return TestClient(server.app)


def _fake_ladder(**report_kw):
    """A run_apply_ladder stand-in that records its call and returns a report."""
    def ladder(url, profile_md, *, browser, resume_path=None, notifier=None):
        ladder.captured = dict(url=url, profile_md=profile_md, browser=browser,
                               resume_path=resume_path, notifier=notifier)
        return ApplyReport(url, report_kw.get("status", "ready-to-submit"),
                           filled=report_kw.get("filled", ["First Name"]),
                           needs_you=report_kw.get("needs_you", []),
                           optional=report_kw.get("optional", []),
                           blocker=report_kw.get("blocker"),
                           message=report_kw.get("message", "Filled 1 field(s)."))
    return ladder


def _patch_ladder(monkeypatch, ladder):
    monkeypatch.setattr("pipeline.apply_driver.run_apply_ladder", ladder)


def _wait_done(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/apply/run-status/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("apply task never finished")


class TestApplyRun:
    def test_drives_ladder_and_reports(self, client, monkeypatch):
        ladder = _fake_ladder(filled=["First Name", "Last Name"], optional=["Pronouns"])
        _patch_ladder(monkeypatch, ladder)
        r = client.post("/api/apply/run", json={"num": "1"})
        assert r.status_code == 200
        body = _wait_done(client, r.json()["job_id"])
        assert body["status"] == "done"
        rep = body["report"]
        assert rep["status"] == "ready-to-submit"
        assert rep["filled"] == ["First Name", "Last Name"]
        assert rep["optional"] == ["Pronouns"]
        assert rep["needs_you"] == []

    def test_resolves_url_from_tracker_and_passes_profile(self, client, monkeypatch):
        ladder = _fake_ladder()
        _patch_ladder(monkeypatch, ladder)
        _wait_done(client, client.post("/api/apply/run", json={"num": "1"}).json()["job_id"])
        assert ladder.captured["url"] == "https://job-boards.greenhouse.io/acme/jobs/1"
        assert ladder.captured["profile_md"] == "PROFILE"
        assert ladder.captured["notifier"] is not None  # wall toasts wired

    def test_role_without_url_fails_cleanly(self, client, monkeypatch):
        _patch_ladder(monkeypatch, _fake_ladder())
        body = _wait_done(client, client.post("/api/apply/run", json={"num": "2"}).json()["job_id"])
        assert body["status"] == "failed"
        assert "url" in body["error"].lower()

    def test_unknown_role_fails(self, client, monkeypatch):
        _patch_ladder(monkeypatch, _fake_ladder())
        body = _wait_done(client, client.post("/api/apply/run", json={"num": "999"}).json()["job_id"])
        assert body["status"] == "failed"

    def test_single_flight_409(self, client, monkeypatch):
        release = threading.Event()

        def slow(url, profile_md, *, browser, resume_path=None, notifier=None):
            release.wait(timeout=5)
            return ApplyReport(url, "ready-to-submit")

        _patch_ladder(monkeypatch, slow)
        first = client.post("/api/apply/run", json={"num": "1"})
        assert first.status_code == 200
        try:
            assert client.post("/api/apply/run", json={"num": "1"}).status_code == 409
        finally:
            release.set()
        _wait_done(client, first.json()["job_id"])

    def test_refused_during_local_pipeline_run(self, client, monkeypatch):
        from pipeline.app import server
        monkeypatch.setattr(server.local_run, "is_running", lambda: True)
        assert client.post("/api/apply/run", json={"num": "1"}).status_code == 409

    def test_refused_during_handoff_build(self, client, monkeypatch):
        from pipeline.app import server
        monkeypatch.setattr(server, "_handoff_running", lambda: True)
        assert client.post("/api/apply/run", json={"num": "1"}).status_code == 409

    def test_ladder_exception_surfaces_as_failed(self, client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("relay down")

        _patch_ladder(monkeypatch, boom)
        body = _wait_done(client, client.post("/api/apply/run", json={"num": "1"}).json()["job_id"])
        assert body["status"] == "failed"
        assert "relay down" in body["error"]

    def test_missing_profile_fails(self, client, monkeypatch):
        monkeypatch.setattr("pipeline.handoff.resolve_profile_md", lambda: "")
        _patch_ladder(monkeypatch, _fake_ladder())
        body = _wait_done(client, client.post("/api/apply/run", json={"num": "1"}).json()["job_id"])
        assert body["status"] == "failed"
        assert "profile" in body["error"].lower()

    def test_unknown_task_404(self, client):
        assert client.get("/api/apply/run-status/nope").status_code == 404
