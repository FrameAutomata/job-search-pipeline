"""Tests for the UI liveness-recheck trigger (server endpoints).

The recheck core (pipeline/recheck.py) is monkeypatched — these cover the server
glue: a single-flight background sweep with progress polling, mirroring the
run-local controls. The real fetch/mark logic is in test_recheck.py.

Design under test:
- POST /api/recheck-liveness: start a background tracker liveness sweep. 409 if
  one is already running or a local pipeline run is in progress. Returns the
  initial status.
- GET  /api/recheck-liveness/status: {running, checked, total, discarded, dead,
  done, ok}. `progress(checked, total, discarded)` from the core updates it live;
  on completion the final summary lands with done=True, ok=True.
"""

import importlib
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pipeline import recheck as recheck_mod  # noqa: E402


_TRACKER = (
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-06-01 | Acme | Engineer | 4.5/5 | Evaluated | ❌ | [001](reports/001.md) | "
    "https://www.linkedin.com/jobs/view/111 — strong fit |\n"
)


@pytest.fixture
def server_mod(tmp_path, monkeypatch):
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    (co / "data" / "applications.md").write_text(_TRACKER, encoding="utf-8")
    monkeypatch.setenv("CAREER_OPS_PATH", str(co))
    from pipeline.app import server
    importlib.reload(server)
    server._active_data_dir = None
    # Default: no local run in progress (each test can override).
    monkeypatch.setattr(server.local_run, "is_running", lambda: False)
    return server


@pytest.fixture
def client(server_mod):
    return TestClient(server_mod.app)


def _wait_done(client, timeout=5.0):
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = client.get("/api/recheck-liveness/status").json()
        if body.get("done"):
            return body
        time.sleep(0.02)
    return body


class TestRecheckTrigger:
    def test_starts_and_completes_with_summary(self, client, monkeypatch):
        def fake_run(career_ops, *, progress=None, **kw):
            if progress:
                progress(1, 1, 1)
            return {"checked": 1, "discarded": 1, "skipped": 0, "unconfirmed": 0,
                    "dead": [{"num": "1", "company": "Acme", "role": "Engineer",
                              "url": "https://www.linkedin.com/jobs/view/111",
                              "reason": "HTTP 404"}]}
        monkeypatch.setattr(recheck_mod, "run", fake_run)

        r = client.post("/api/recheck-liveness")
        assert r.status_code == 200
        assert r.json().get("running") is True

        done = _wait_done(client)
        assert done["done"] is True and done["ok"] is True
        assert done["checked"] == 1 and done["discarded"] == 1
        assert [d["num"] for d in done["dead"]] == ["1"]

    def test_single_flight_409(self, client, monkeypatch):
        release = threading.Event()

        def blocking_run(career_ops, *, progress=None, **kw):
            release.wait(timeout=5)
            return {"checked": 0, "discarded": 0, "skipped": 0, "unconfirmed": 0, "dead": []}
        monkeypatch.setattr(recheck_mod, "run", blocking_run)

        try:
            assert client.post("/api/recheck-liveness").status_code == 200
            # Wait until it's actually marked running, then a 2nd start is rejected.
            deadline = time.time() + 2
            while time.time() < deadline and not client.get(
                    "/api/recheck-liveness/status").json().get("running"):
                time.sleep(0.02)
            assert client.post("/api/recheck-liveness").status_code == 409
        finally:
            release.set()
        _wait_done(client)

    def test_refused_during_local_run(self, client, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod.local_run, "is_running", lambda: True)
        # recheck.run must never be reached while a local run holds the tracker.
        monkeypatch.setattr(recheck_mod, "run",
                            lambda *a, **k: pytest.fail("recheck ran during a local pipeline run"))
        assert client.post("/api/recheck-liveness").status_code == 409

    def test_failure_reports_not_ok(self, client, monkeypatch):
        def boom(career_ops, *, progress=None, **kw):
            raise RuntimeError("network down")
        monkeypatch.setattr(recheck_mod, "run", boom)
        client.post("/api/recheck-liveness")
        done = _wait_done(client)
        assert done["done"] is True and done["ok"] is False

    def test_status_idle_before_any_run(self, client):
        body = client.get("/api/recheck-liveness/status").json()
        assert body.get("running") is False
