"""UI handoff endpoints: build the browser-agent work-order from the triage UI
(batch — the old "Batch apply" slot) and produce a paste-ready, agent-agnostic
prompt for handing off ONE specific role from the report pane.

The UI never launches a browser agent (none of them expose a programmatic
session API) — it produces the artifacts + prompts the user pastes into
whichever agent they use. Skips if FastAPI isn't installed."""

import importlib
import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pipeline import handoff  # noqa: E402


TRACKER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-07-01 | Acme | AI Engineer | 4.8/5 | Evaluated | X | [001](reports/001-acme.md) | https://www.linkedin.com/jobs/view/101 — strong fit |\n"
    "| 2 | 2026-07-01 | Globex | Backend Engineer | 4.2/5 | Evaluated | X | [002](reports/002-globex.md) | no url recorded here |\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    career_ops = tmp_path / "career-ops"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "reports").mkdir()
    (career_ops / "config").mkdir()
    (career_ops / "data" / "applications.md").write_text(TRACKER, encoding="utf-8")
    (career_ops / "reports" / "001-acme.md").write_text("# Acme — AI Engineer\nStrong fit.", encoding="utf-8")
    (career_ops / "config" / "profile.yml").write_text('candidate:\n  full_name: "Jane Dev"\n', encoding="utf-8")
    monkeypatch.setenv("CAREER_OPS_PATH", str(career_ops))
    # The UI writes the work-order where the user's agent can reach it.
    monkeypatch.setenv("HANDOFF_OUT_DIR", str(tmp_path / "agent-home"))

    from pipeline.app import server
    importlib.reload(server)
    server.PUSHED_OVERRIDES_FILE = tmp_path / ".ui-cache" / "pushed-overrides.json"
    server.UI_CACHE = tmp_path / ".ui-cache" / "latest"
    return TestClient(server.app)


def _fake_run(out_dir, rows=0, board="linkedin"):
    """A handoff.run stand-in that writes ONE site's work-order with `rows`
    entries (the real run writes next-roles-<site>.{jsonl,md} per site)."""
    def run(**kw):
        run.captured = kw
        out_dir.mkdir(parents=True, exist_ok=True)
        jsonl, md = handoff.work_order_paths(out_dir, board)
        lines = [json.dumps({"rank": i + 1, "company": "Acme", "role": "AI Engineer",
                             "board": board, "url": "u", "status": ""}) for i in range(rows)]
        jsonl.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        md.write_text("# Work order\n", encoding="utf-8")
        return 0
    return run


def _wait_done(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/handoff/build-status/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("build task never finished")


# ── Batch: POST /api/handoff/build ─────────────────────────────────────────────
class TestBuildEndpoint:
    def test_build_passes_options_through_and_reports_counts(self, client, tmp_path, monkeypatch):
        fake_run = _fake_run(tmp_path / "agent-home", rows=1)
        monkeypatch.setattr("pipeline.handoff.run", fake_run)
        r = client.post("/api/handoff/build",
                        json={"board": "linkedin", "limit": 25, "tailor": True})
        assert r.status_code == 200
        body = _wait_done(client, r.json()["job_id"])
        assert body["status"] == "done"
        # Options reached handoff.run unchanged.
        assert fake_run.captured["board"] == "linkedin"
        assert fake_run.captured["limit"] == 25
        assert fake_run.captured["tailor"] is True
        # L2: the UI passes its ROOT-anchored career-ops so a relative
        # CAREER_OPS_PATH resolves the same as everywhere else in the server.
        assert fake_run.captured["career_ops"] is not None
        assert str(fake_run.captured["career_ops"]).endswith("career-ops")
        # Result reports one session per site — here a single LinkedIn session.
        result = body["result"]
        assert result["total_fresh"] == 1
        assert len(result["sessions"]) == 1
        s = result["sessions"][0]
        assert s["board"] == "linkedin"
        assert s["fresh"] == 1
        assert s["work_order"].endswith("next-roles-linkedin.jsonl")
        assert "agent-home" in s["work_order"]   # HANDOFF_OUT_DIR honored

    def test_result_includes_agent_agnostic_kickoff_prompt(self, client, tmp_path, monkeypatch):
        fake_run = _fake_run(tmp_path / "agent-home", rows=1)
        monkeypatch.setattr("pipeline.handoff.run", fake_run)
        r = client.post("/api/handoff/build", json={})
        body = _wait_done(client, r.json()["job_id"])
        sessions = body["result"]["sessions"]
        assert len(sessions) == 1
        kickoff = sessions[0]["kickoff"]
        # Paste-ready per site: names that site's work-order file and the
        # writeback statuses, and never hard-codes a specific browser agent.
        assert "next-roles-linkedin.jsonl" in kickoff
        assert "applied" in kickoff and "skip:" in kickoff
        assert "cowork" not in kickoff.lower()

    def test_empty_build_reports_no_sessions(self, client, tmp_path, monkeypatch):
        # A build that finds nothing fresh (0 rows written) reports zero sessions
        # rather than a phantom empty one.
        monkeypatch.setattr("pipeline.handoff.run", _fake_run(tmp_path / "agent-home", rows=0))
        r = client.post("/api/handoff/build", json={})
        body = _wait_done(client, r.json()["job_id"])
        assert body["status"] == "done"
        assert body["result"]["sessions"] == []
        assert body["result"]["total_fresh"] == 0

    def test_build_single_flight_409(self, client, monkeypatch):
        release = threading.Event()

        def slow_run(**kw):
            release.wait(timeout=5)
            return 0

        monkeypatch.setattr("pipeline.handoff.run", slow_run)
        first = client.post("/api/handoff/build", json={})
        assert first.status_code == 200
        try:
            second = client.post("/api/handoff/build", json={})
            assert second.status_code == 409
        finally:
            release.set()
        _wait_done(client, first.json()["job_id"])

    def test_build_rejects_unknown_board(self, client, monkeypatch):
        # `board` is unconstrained on the wire; an unknown one is rejected before
        # a build spins up (else run() writes a stray next-roles-<garbage>.jsonl).
        called = []
        monkeypatch.setattr("pipeline.handoff.run", lambda **kw: called.append(kw) or 0)
        r = client.post("/api/handoff/build", json={"board": "linkdin"})
        assert r.status_code == 400
        assert not called            # never reached handoff.run

    def test_build_refused_during_local_pipeline_run(self, client, monkeypatch):
        from pipeline.app import server
        monkeypatch.setattr(server.local_run, "is_running", lambda: True)
        r = client.post("/api/handoff/build", json={})
        assert r.status_code == 409

    def test_build_failure_surfaces_as_failed_task(self, client, monkeypatch):
        monkeypatch.setattr("pipeline.handoff.run", lambda **kw: 1)
        r = client.post("/api/handoff/build", json={})
        body = _wait_done(client, r.json()["job_id"])
        assert body["status"] == "failed"

    def test_unknown_task_404(self, client):
        assert client.get("/api/handoff/build-status/nope").status_code == 404


# ── Single role: GET /api/handoff/role-prompt/{num} ────────────────────────────
class TestRolePrompt:
    def test_prompt_contains_role_facts_and_writeback(self, client):
        r = client.get("/api/handoff/role-prompt/1")
        assert r.status_code == 200
        body = r.json()
        assert body["company"] == "Acme"
        prompt = body["prompt"]
        assert "Acme" in prompt and "AI Engineer" in prompt
        assert "https://www.linkedin.com/jobs/view/101" in prompt
        assert "001-acme.md" in prompt          # evaluation report path
        assert "PROFILE.md" in prompt           # the living master (not the raw profile.yml)
        assert "profile.yml" not in prompt
        assert "next-roles-linkedin.jsonl" in prompt   # writeback target (the role's site file)
        assert "applied" in prompt and "skip:" in prompt
        assert "cowork" not in prompt.lower()   # agent-agnostic (public template)

    def test_prompt_includes_cached_tailored_resume(self, client, tmp_path):
        out = tmp_path / "career-ops" / "output"
        out.mkdir()
        (out / "Acme - resume.pdf").write_bytes(b"%PDF-1.4 tailored")
        prompt = client.get("/api/handoff/role-prompt/1").json()["prompt"]
        assert "Acme - resume.pdf" in prompt

    def test_prompt_without_cache_points_at_default_resume(self, client):
        prompt = client.get("/api/handoff/role-prompt/1").json()["prompt"]
        assert "resume" in prompt.lower()
        assert "Acme - resume.pdf" not in prompt

    def test_unknown_num_404(self, client):
        assert client.get("/api/handoff/role-prompt/999").status_code == 404

    def test_row_without_url_400(self, client):
        r = client.get("/api/handoff/role-prompt/2")
        assert r.status_code == 400
        assert "url" in r.json()["detail"].lower()
