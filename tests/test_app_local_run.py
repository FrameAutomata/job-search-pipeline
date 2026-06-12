"""Tests for the local pipeline run endpoints (pipeline/app/local_run.py +
server routes). The orchestrate subprocess is faked — these cover option→flag
mapping, single-flight, stage parsing from the log, cancel, and the
use-local data-source switch."""

import importlib

import pytest
from fastapi.testclient import TestClient

from pipeline.app import local_run


class FakeProc:
    # A dead-but-fixed pid: start() writes proc.pid to the pid file, but every
    # test keeps a live Popen in _state, so the orphan (pid-file) path is never
    # the running signal here — the value just has to exist.
    def __init__(self, cmd):
        self.cmd = cmd
        self.pid = 424242
        self._exit = None
        self.terminated = False

    def poll(self):
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = 1

    def wait(self, timeout=None):
        return self._exit

    def kill(self):
        self._exit = 9

    def finish(self, code=0):
        self._exit = code


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREER_OPS_PATH", str(tmp_path / "career-ops"))
    (tmp_path / "career-ops" / "data").mkdir(parents=True)
    (tmp_path / "career-ops" / "data" / "applications.md").write_text(
        "# Applications Tracker\n\n| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n",
        encoding="utf-8",
    )
    from pipeline.app import server
    importlib.reload(server)
    server._active_data_dir = None
    return TestClient(server.app)


@pytest.fixture
def fake_popen(tmp_path, monkeypatch):
    """Replace Popen with a fake and isolate module state + log path."""
    spawned = []

    def popen(cmd, **kwargs):
        proc = FakeProc(cmd)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(local_run.subprocess, "Popen", popen)
    monkeypatch.setattr(local_run, "LOG_PATH", tmp_path / "local-run.log")
    monkeypatch.setattr(local_run, "PID_PATH", tmp_path / "local-run.pid")
    local_run._state.clear()
    yield spawned
    local_run._state.clear()


class TestChildEnv:
    """The child (orchestrate.py) runs its own load_dotenv(override=False), so
    _child_env strips the server's STARTUP .env values and lets the child reload
    the CURRENT .env — picking up edits, honoring removals, and preserving
    shell-export precedence."""

    def test_strips_dotenv_origin_value_so_child_reloads(self, monkeypatch):
        # A value still equal to the startup snapshot came from .env (not a shell
        # export) → stripped, so the child's own load_dotenv re-reads current .env.
        monkeypatch.setenv("BATCH_MODEL", "startup-model")
        env = local_run._child_env(snapshot={"BATCH_MODEL": "startup-model"})
        assert "BATCH_MODEL" not in env
        assert env["PYTHONUNBUFFERED"] == "1"

    def test_preserves_shell_override(self, monkeypatch):
        # Inherited value differs from the snapshot → it's a shell export, which
        # must keep beating .env (matching load_dotenv(override=False)).
        monkeypatch.setenv("BATCH_MODEL", "shell-value")
        env = local_run._child_env(snapshot={"BATCH_MODEL": "dotenv-value"})
        assert env["BATCH_MODEL"] == "shell-value"

    def test_removed_key_is_stripped(self, monkeypatch):
        # Key was .env-origin at startup, still inherited, now removed from .env:
        # stripping unsets it in the child (an overlay never could).
        monkeypatch.setenv("OLD_KEY", "v")
        env = local_run._child_env(snapshot={"OLD_KEY": "v"})
        assert "OLD_KEY" not in env

    def test_missing_snapshot_keeps_environment(self):
        env = local_run._child_env(snapshot={})
        assert env["PYTHONUNBUFFERED"] == "1"


class TestBuildCmd:
    def test_defaults(self):
        cmd = local_run._build_cmd({})
        assert cmd[1].endswith("orchestrate.py")
        assert "--evaluate-batch" in cmd
        assert "--easy-apply-only" not in cmd and "--no-easy-apply" not in cmd

    def test_pass_selection(self):
        assert "--easy-apply-only" in local_run._build_cmd({"passes": "easy-only"})
        assert "--no-easy-apply" in local_run._build_cmd({"passes": "no-easy"})

    def test_evaluate_off(self):
        assert "--evaluate-batch" not in local_run._build_cmd({"evaluate": False})


class TestRunLocalEndpoints:
    def test_start_returns_running_status(self, client, fake_popen):
        r = client.post("/api/run-local", json={"passes": "easy-only", "evaluate": True})
        assert r.status_code == 200
        body = r.json()
        assert body["started"] is True and body["running"] is True
        assert body["ok"] is None   # run outcome unknown while running
        assert "--easy-apply-only" in fake_popen[0].cmd

    def test_single_flight_409(self, client, fake_popen):
        assert client.post("/api/run-local", json={}).status_code == 200
        r = client.post("/api/run-local", json={})
        assert r.status_code == 409

    def test_invalid_passes_400(self, client, fake_popen):
        assert client.post("/api/run-local", json={"passes": "everything"}).status_code == 400

    def test_status_parses_stages_from_log(self, client, fake_popen, tmp_path):
        client.post("/api/run-local", json={})
        (tmp_path / "local-run.log").write_text(
            "[scrape] pass 1: 40 results\n[filter] 12 kept\n[screen] checking...\n",
            encoding="utf-8",
        )
        s = client.get("/api/run-local/status").json()
        assert s["running"] is True
        assert s["stage"] == "screen"
        assert s["stages_seen"] == ["scrape", "filter", "screen"]
        assert "[filter] 12 kept" in s["log_tail"]

    def test_completion_reports_exit_code(self, client, fake_popen, tmp_path):
        client.post("/api/run-local", json={})
        fake_popen[0].finish(0)
        s = client.get("/api/run-local/status").json()
        assert s["running"] is False and s["exit_code"] == 0 and s["ok"] is True
        # a new run can start after completion
        assert client.post("/api/run-local", json={}).status_code == 200

    def test_failure_reports_exit_code(self, client, fake_popen):
        client.post("/api/run-local", json={})
        fake_popen[0].finish(3)
        s = client.get("/api/run-local/status").json()
        assert s["exit_code"] == 3 and s["ok"] is False

    def test_cancel_terminates(self, client, fake_popen):
        client.post("/api/run-local", json={})
        r = client.post("/api/run-local/cancel")
        assert r.status_code == 200
        assert fake_popen[0].terminated is True
        assert r.json()["running"] is False

    def test_status_without_any_run(self, client, fake_popen):
        s = client.get("/api/run-local/status").json()
        assert s["running"] is False and s["exit_code"] is None and s["ok"] is None


class TestUseLocal:
    def test_resets_active_data_dir(self, client, tmp_path):
        from pipeline.app import server
        server._active_data_dir = tmp_path / "artifact"
        r = client.post("/api/use-local")
        assert r.status_code == 200
        assert server._active_data_dir is None
        assert r.json()["ok"] is True


class TestOrphanGuard:
    """Single-flight must survive a server restart: the orchestrate child keeps
    running (its stdout is a file), so a fresh server — empty _state — would
    otherwise let a SECOND concurrent full pipeline corrupt shared state. The
    pid file lets is_running()/start() detect and refuse the orphan."""

    def test_status_reports_orphan_running(self, fake_popen, tmp_path, monkeypatch):
        local_run._state.clear()                     # as if the server just restarted
        (tmp_path / "local-run.pid").write_text("4242", encoding="utf-8")
        monkeypatch.setattr(local_run, "_pid_alive", lambda pid: pid == 4242)
        s = local_run.status()
        assert s["running"] is True and s["exit_code"] is None

    def test_start_refuses_when_orphan_alive(self, fake_popen, tmp_path, monkeypatch):
        local_run._state.clear()
        (tmp_path / "local-run.pid").write_text("4242", encoding="utf-8")
        monkeypatch.setattr(local_run, "_pid_alive", lambda pid: pid == 4242)
        with pytest.raises(RuntimeError):
            local_run.start({})

    def test_dead_orphan_is_not_running(self, fake_popen, tmp_path, monkeypatch):
        local_run._state.clear()
        (tmp_path / "local-run.pid").write_text("4242", encoding="utf-8")
        monkeypatch.setattr(local_run, "_pid_alive", lambda pid: False)
        assert local_run.is_running() is False


class TestAddJobGuard:
    """Add-job mints the next report/tracker number in the server process while
    a local run's eval stage mints them in its subprocess — they must not
    overlap, or the numbering collides."""

    def test_add_job_refused_during_local_run(self, client, fake_popen):
        assert client.post("/api/run-local", json={}).status_code == 200
        r = client.post("/api/jobs/add", json={"url": "https://example.com/job"})
        assert r.status_code == 409
        r2 = client.post("/api/jobs/add-async", json={"url": "https://example.com/job"})
        assert r2.status_code == 409
