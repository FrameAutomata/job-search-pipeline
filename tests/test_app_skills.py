"""Tests for the career-ops skill launchpad (capabilities, resume tailoring,
CLI hand-off) and the cross-origin guard. Reuses the `client` fixture from
test_app_server via a local copy of its setup so cv.md is present too.

Skips if FastAPI isn't installed (optional UI dependency)."""

import importlib
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    career_ops = tmp_path / "career-ops"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "reports").mkdir(parents=True)
    (career_ops / "config").mkdir(parents=True)
    (career_ops / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-05-27 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme.md) | APPLY |\n",
        encoding="utf-8",
    )
    (career_ops / "reports" / "001-acme.md").write_text(
        "# Acme — Eng\n\n**Score:** 4.2/5\n\n### Requirements Map\n- Python, FastAPI\n",
        encoding="utf-8",
    )
    (career_ops / "cv.md").write_text(
        "# Jane Dev\n\n## Skills\n- Python\n\n## Professional Experience\n- Built APIs\n",
        encoding="utf-8",
    )
    (career_ops / "config" / "profile.yml").write_text('name: "Jane Dev"\n', encoding="utf-8")
    monkeypatch.setenv("CAREER_OPS_PATH", str(career_ops))
    # Clean provider/CLI env so detection is deterministic per test.
    for var in ("BATCH_PROVIDER", "BATCH_MODEL", "BATCH_CLI", "SKILL_PATH_DEFAULT",
                "GEMINI_API_KEY", "GROQ_API_KEY", "DEEPINFRA_API_KEY",
                "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from pipeline.app import server
    importlib.reload(server)
    server.OVERRIDES_FILE = tmp_path / ".ui-cache" / "status-overrides.json"
    server.UI_CACHE = tmp_path / ".ui-cache" / "latest"
    server._active_data_dir = None
    return TestClient(server.app)


# ── Capabilities ─────────────────────────────────────────────────────────────

def test_capabilities_none(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value=None)
    caps = client.get("/api/capabilities").json()
    assert caps["cli"]["available"] is False
    assert caps["api"]["available"] is False
    assert caps["default_path"] == "ask"


def test_capabilities_detects_cli_and_api(client, mocker, monkeypatch):
    mocker.patch("pipeline.app.skills.shutil.which",
                 side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    caps = client.get("/api/capabilities").json()
    assert caps["cli"] == {"available": True, "name": "claude"}
    assert caps["api"] == {"available": True, "provider": "gemini"}


def test_capabilities_provider_with_missing_key_not_available(client, monkeypatch):
    # BATCH_PROVIDER names anthropic but its key is unset → not usable.
    monkeypatch.setenv("BATCH_PROVIDER", "anthropic")
    caps = client.get("/api/capabilities").json()
    assert caps["api"]["available"] is False


def test_capabilities_lists_skills(client):
    caps = client.get("/api/capabilities").json()
    by_id = {s["id"]: s for s in caps["skills"]}
    # All four advertised; only résumé-markdown is api-capable.
    assert set(by_id) == {"tailor-resume", "tailor-resume-pdf", "interview-prep", "apply"}
    assert by_id["tailor-resume"]["api"] is True
    assert by_id["tailor-resume-pdf"]["api"] is False
    assert by_id["interview-prep"]["api"] is False
    assert by_id["apply"]["api"] is False


# ── CLI hand-off ─────────────────────────────────────────────────────────────

def test_cli_returns_command(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/claude")
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "1", "path": "cli"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "cli"
    assert "claude" in body["command"]
    assert "Acme" in body["command"] and "Eng" in body["command"]
    assert "reports/001-acme.md" in body["command"]


def test_cli_command_per_skill_uses_mode(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/claude")
    for skill, mode in [("interview-prep", "interview-prep"), ("apply", "apply"),
                        ("tailor-resume-pdf", "pdf")]:
        r = client.post("/api/skills/run", json={"skill": skill, "num": "1", "path": "cli"})
        assert r.status_code == 200, r.text
        assert f"use {mode} mode" in r.json()["command"]


def test_cli_no_agent_400(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value=None)
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "1", "path": "cli"})
    assert r.status_code == 400
    assert "No agent CLI" in r.json()["detail"]


# ── API path ─────────────────────────────────────────────────────────────────

def test_api_writes_file_and_downloads(client, mocker, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    captured = {}

    def fake_caller(system, user):
        captured["system"] = system
        captured["user"] = user
        return "# Jane Dev\n\n## Skills\n- Python, FastAPI\n"

    mocker.patch("pipeline.app.skills.be._build_caller", return_value=fake_caller)
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "1", "path": "api"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "gemini"
    assert body["output_file"].startswith("cv-jane-dev-acme-")
    # CV + report context flowed into the prompt.
    assert "Built APIs" in captured["system"]       # cv.md is the source of truth
    assert "Requirements Map" in captured["user"]    # report is the JD signal
    # The generated file is downloadable.
    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert "FastAPI" in dl.text


def test_api_rejected_for_cli_only_skill(client, monkeypatch):
    # interview-prep is CLI-only; asking for the API path must 400 with guidance,
    # even when a key is configured.
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    r = client.post("/api/skills/run", json={"skill": "interview-prep", "num": "1", "path": "api"})
    assert r.status_code == 400
    assert "CLI-only" in r.json()["detail"]


def test_api_no_key_400(client):
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "1", "path": "api"})
    assert r.status_code == 400
    assert "No LLM API key" in r.json()["detail"]


def test_unknown_skill_404(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/claude")
    r = client.post("/api/skills/run", json={"skill": "nope", "num": "1", "path": "cli"})
    assert r.status_code == 404


def test_unknown_role_404(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/claude")
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "999", "path": "cli"})
    assert r.status_code == 404


def test_skill_output_rejects_traversal(client):
    r = client.get("/api/skills/output/" + "..%2f..%2fcv.md")
    # Path components are stripped, so this resolves to output/cv.md which
    # doesn't exist → 404 (never escapes the output dir).
    assert r.status_code == 404


# ── Cross-origin guard ───────────────────────────────────────────────────────

def test_cross_origin_post_refused(client):
    r = client.post("/api/status", json={"num": "1", "status": "Applied"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "Cross-origin" in r.json()["detail"]


def test_same_origin_post_allowed(client):
    r = client.post("/api/status", json={"num": "1", "status": "Applied"},
                    headers={"Origin": "http://localhost:8000"})
    assert r.status_code == 200


def test_get_unaffected_by_origin(client):
    r = client.get("/api/jobs", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200
