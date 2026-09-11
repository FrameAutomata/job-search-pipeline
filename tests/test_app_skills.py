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

from pipeline import agent_cli  # noqa: E402


def _which_for(*names):
    """shutil.which stand-in: a path for `names` (and the RESOLVED agent CLI's
    binary when asked for by `"cli"`), None for everything else."""
    def which(name, path=None):
        # `path=` because resolve_chromium passes it; ignored on purpose, since
        # the stub's whole job is to answer independently of the real PATH.
        wanted = {agent_cli.resolve_cli().binary if n == "cli" else n for n in names}
        return f"/usr/bin/{name}" if name in wanted else None
    return which


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

    from pipeline.app import server
    importlib.reload(server)
    # Clean provider/CLI env so detection is deterministic per test. MUST come
    # after the reload: server.py calls load_dotenv() at module level, which
    # reload re-runs — re-adding the developer's real .env keys if we cleared
    # before. (load_dotenv(override=False) repopulates vars we just deleted.)
    # Key vars derive from the provider table so a new provider can't slip past
    # a hand-copied list (the DeepSeek addition did exactly that — review bug).
    from pipeline.batch_evaluate import _PROVIDER_KEYS
    # The digest's delivery secrets too: the reload above re-ran load_dotenv,
    # so a developer's real DIGEST_DISCORD_WEBHOOK is back in the env here
    # even though conftest cleared it — and a server route that reads it
    # would post to a real channel from a test.
    from pipeline import daily_digest
    for var in ("BATCH_PROVIDER", "BATCH_MODEL", "BATCH_CLI", agent_cli.AGENT_MODEL_ENV,
                "SKILL_PATH_DEFAULT", *_PROVIDER_KEYS.values(),
                *daily_digest.SECRET_VARS, *daily_digest.SETTING_VARS):
        monkeypatch.delenv(var, raising=False)
    # status-overrides is isolated by the autouse _isolate_status_overrides
    # conftest fixture; isolate the pushed-overrides + cache paths here so no
    # push-touching test can write the developer's real (gitignored) .ui-cache.
    server.PUSHED_OVERRIDES_FILE = tmp_path / ".ui-cache" / "pushed-overrides.json"
    server.UI_CACHE = tmp_path / ".ui-cache" / "latest"
    return TestClient(server.app)


# ── Capabilities ─────────────────────────────────────────────────────────────

def test_capabilities_none(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value=None)
    caps = client.get("/api/capabilities").json()
    assert caps["cli"]["available"] is False
    assert caps["api"]["available"] is False
    assert caps["default_path"] == "ask"


def test_capabilities_detects_cli_and_api(client, mocker, monkeypatch):
    mocker.patch("pipeline.app.skills.shutil.which", side_effect=_which_for("cli"))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    caps = client.get("/api/capabilities").json()
    # Subset, not equality: the registry adds label/tier/tier_note/install_hint
    # and the `known` list the UI surface renders.
    assert {"available": True, "name": agent_cli.DEFAULT_CLI}.items() <= caps["cli"].items()
    assert caps["cli"]["tier"] == agent_cli.AGENT_CLIS[agent_cli.DEFAULT_CLI].tier
    assert caps["cli"]["label"] and caps["cli"]["tier_note"] and caps["cli"]["install_hint"]
    assert [k["id"] for k in caps["cli"]["known"]] == list(agent_cli.AGENT_CLIS)
    installed = {k["id"]: k["installed"] for k in caps["cli"]["known"]}
    assert installed[agent_cli.DEFAULT_CLI] is True
    assert all(v is False for cid, v in installed.items() if cid != agent_cli.DEFAULT_CLI)
    assert caps["api"] == {"available": True, "provider": "gemini"}


def test_capabilities_follow_batch_cli(client, mocker, monkeypatch):
    monkeypatch.setenv("BATCH_CLI", "claude")
    mocker.patch("pipeline.app.skills.shutil.which", side_effect=_which_for("cli"))
    caps = client.get("/api/capabilities").json()
    assert caps["cli"]["name"] == "claude" and caps["cli"]["available"] is True
    assert caps["cli"]["tier"] == "paid"


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
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/x")
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "1", "path": "cli"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "cli"
    assert agent_cli.DEFAULT_CLI in body["command"]
    assert "Acme" in body["command"] and "Eng" in body["command"]
    # cd stays relative (UI runs from the repo root)...
    assert body["command"].startswith("cd career-ops && ")
    assert body["cwd"] == "career-ops"
    # ...but the report is referenced by ABSOLUTE path, not a career-ops-relative
    # one (it may live in a Refresh artifact cache, not career-ops/reports/).
    # The closing quote is the shell's — single on POSIX, double on Windows.
    report_ref = body["command"].split("evaluation report: ", 1)[1].rstrip(") \"'")
    assert os.path.isabs(report_ref.replace("/", os.sep)) or report_ref[1:3] == ":/"
    assert report_ref.endswith("/reports/001-acme.md")


@pytest.mark.parametrize("cid", list(agent_cli.AGENT_CLIS))
def test_cli_command_is_the_registry_rendering(client, mocker, monkeypatch, cid):
    # The command is the CLI's own INTERACTIVE argv rendered for this shell —
    # `-i` on gemini/qwen/agy, `--prompt` on opencode, positional on claude —
    # never a bare `<binary> "<prompt>"`. Gemini carries its default model
    # (`-m`), and nothing strips the Google key: an individual runs Gemini
    # CLI on the API key now.
    monkeypatch.setenv("BATCH_CLI", cid)
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/x")
    r = client.post("/api/skills/run", json={"skill": "apply", "num": "1", "path": "cli"})
    assert r.status_code == 200, r.text
    cmd = r.json()["command"]
    cli = agent_cli.AGENT_CLIS[cid]
    assert cmd.startswith("cd career-ops && ")
    if cli.prompt_flag:
        assert f" {cli.prompt_flag} " in cmd
    if cli.default_model:
        assert f" {cli.model_flag} {cli.default_model} " in cmd
    else:
        assert f" {cli.model_flag} " not in cmd
    assert "GEMINI_API_KEY" not in cmd and "env -u" not in cmd


@pytest.mark.parametrize("cid", list(agent_cli.AGENT_CLIS))
def test_cli_command_renders_agent_model(client, mocker, monkeypatch, cid):
    # AGENT_MODEL reaches every CLI's model flag in the rendered command.
    monkeypatch.setenv("BATCH_CLI", cid)
    monkeypatch.setenv(agent_cli.AGENT_MODEL_ENV, "prov/model-x")
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/x")
    r = client.post("/api/skills/run", json={"skill": "apply", "num": "1", "path": "cli"})
    assert r.status_code == 200, r.text
    cli = agent_cli.AGENT_CLIS[cid]
    assert f"{cli.binary} {cli.model_flag} prov/model-x " in r.json()["command"]


@pytest.mark.parametrize("cid", list(agent_cli.AGENT_CLIS))
def test_cli_returns_prereqs_for_browser_skill(client, mocker, monkeypatch, cid):
    # `apply` needs the Playwright MCP server registered with THE CLI IN USE;
    # the response must surface that one-time setup, spelled for that CLI, so
    # a Gemini user is not told to run `claude mcp add`.
    monkeypatch.setenv("BATCH_CLI", cid)
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/x")
    r = client.post("/api/skills/run", json={"skill": "apply", "num": "1", "path": "cli"})
    assert r.status_code == 200
    prereqs = r.json().get("prereqs", [])
    assert prereqs, "apply must list its setup prerequisites"
    joined = " ".join(prereqs)
    assert "Playwright MCP" in joined
    cli = agent_cli.AGENT_CLIS[cid]
    reg = cli.mcp_registration()
    if reg.is_argv:
        assert f"{cli.binary} mcp add" in joined
    else:
        # The config-merge CLIs name their own file: OpenCode's under the XDG
        # config dir, Antigravity's under ~/.gemini/antigravity/.
        assert cli.mcp_config_rel in joined
        assert "mcp add" not in joined
    if cid != "claude":
        assert "claude mcp add" not in joined
    # The sentinel never leaks; the capabilities reader expands it the same way.
    assert skills_module().PLAYWRIGHT_MCP_PREREQ not in prereqs
    caps = client.get("/api/capabilities").json()
    by_id = {s["id"]: s for s in caps["skills"]}
    assert by_id["apply"]["prereqs"] == prereqs


def skills_module():
    from pipeline.app import skills
    return skills


def test_cli_no_prereqs_for_plain_resume_skill(client, mocker):
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/claude")
    r = client.post("/api/skills/run", json={"skill": "tailor-resume", "num": "1", "path": "cli"})
    assert r.json().get("prereqs") == []


def test_capabilities_includes_per_skill_prereqs(client):
    caps = client.get("/api/capabilities").json()
    by_id = {s["id"]: s for s in caps["skills"]}
    assert by_id["apply"]["prereqs"]
    assert by_id["tailor-resume"]["prereqs"] == []


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


# ── Run in terminal ──────────────────────────────────────────────────────────

def test_capabilities_includes_terminal(client):
    caps = client.get("/api/capabilities").json()
    assert "terminal" in caps and "available" in caps["terminal"]


def test_launch_writes_cmd_script_and_spawns(client, mocker, monkeypatch):
    # Force the Windows code path regardless of test runner OS so the launcher
    # is exercised end-to-end on Linux CI too.
    monkeypatch.setattr("pipeline.app.skills.os.name", "nt")
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/x")
    popen = mocker.patch("pipeline.app.skills.subprocess.Popen")
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["launched"] is True
    # Server rebuilt the same hand-off command (clients can't smuggle commands).
    assert agent_cli.DEFAULT_CLI in body["command"]
    assert "Acme" in body["command"] and "Eng" in body["command"]
    # Spawn used a new console (CREATE_NEW_CONSOLE = 0x10) with the .cmd script.
    args, kwargs = popen.call_args
    script_path = args[0][0]
    assert script_path.endswith(".cmd")
    assert kwargs["creationflags"] == 0x10
    # The script wraps the command with chcp 65001 (UTF-8) and a final pause
    # so the window stays open after the agent exits.
    script = open(script_path, encoding="utf-8").read()
    assert "chcp 65001" in script
    assert "pause" in script
    assert body["command"] in script
    # The command is the cmd.exe rendering, not the POSIX one: shell_command
    # reads os.name at call time, so this patch reaches it. The default CLI's
    # own seed flag inside a double-quoted prompt, and no key stripping in
    # either shell's spelling.
    cli = agent_cli.AGENT_CLIS[agent_cli.DEFAULT_CLI]
    assert f'{cli.binary} {cli.prompt_flag} "' in body["command"]
    assert "set GEMINI_API_KEY" not in body["command"]
    assert "env -u" not in body["command"]


def test_launch_refused_when_no_cli(client, mocker, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "nt")
    mocker.patch("pipeline.app.skills.shutil.which", return_value=None)
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    assert r.status_code == 400
    assert "No agent CLI" in r.json()["detail"]


def test_launch_macos_opens_terminal_app(client, mocker, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "darwin")
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/local/bin/x")
    popen = mocker.patch("pipeline.app.skills.subprocess.Popen")
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["launcher"] == "Terminal.app"
    args, _ = popen.call_args
    # `open -a Terminal <script.sh>` opens the script in Terminal.app.
    cmd = args[0]
    assert cmd[:3] == ["open", "-a", "Terminal"]
    assert cmd[3].endswith(".sh")
    # Script is executable and contains the cd, command, and key-wait.
    assert os.access(cmd[3], os.X_OK)
    script = open(cmd[3], encoding="utf-8").read()
    assert "#!/usr/bin/env bash" in script
    assert "cd " in script and "|| exit 1" in script   # cd guard present
    assert agent_cli.DEFAULT_CLI in script
    assert "read -n 1" in script


def test_write_unix_script_quotes_cwd_with_spaces():
    # The real safety property, asserted deterministically (controls the cwd, so
    # it holds on every OS — unlike asserting quoting on an env-dependent runtime
    # cwd, which only contains shell-special chars on Windows).
    from pipeline.app import skills
    path = skills._write_unix_script("claude run", "/home/me/My Projects/x")
    script = open(path, encoding="utf-8").read()
    assert "cd '/home/me/My Projects/x' || exit 1" in script   # space → quoted


def test_write_unix_script_leaves_clean_path_unquoted():
    # A path with no shell-special chars is valid unquoted; shlex.quote leaves it
    # alone, which is exactly why the old "cd '" assertion failed on POSIX CI.
    from pipeline.app import skills
    path = skills._write_unix_script("claude run", "/home/runner/work/x")
    script = open(path, encoding="utf-8").read()
    assert "cd /home/runner/work/x || exit 1" in script


def test_launch_linux_uses_first_resolved_terminal(client, mocker, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "linux")
    monkeypatch.delenv("TERMINAL", raising=False)
    # Only xterm and the default agent CLI on PATH; the resolver should fall
    # through to xterm.
    available = {agent_cli.DEFAULT_CLI, "xterm"}
    mocker.patch("pipeline.app.skills.shutil.which",
                 side_effect=lambda name: f"/usr/bin/{name}" if name in available else None)
    popen = mocker.patch("pipeline.app.skills.subprocess.Popen")
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["launcher"] == "xterm"
    args, _ = popen.call_args
    cmd = args[0]
    assert cmd[0] == "xterm" and cmd[1] == "-e" and cmd[2].endswith(".sh")


def test_launch_keeps_the_google_key_for_gemini(client, mocker, monkeypatch):
    # Gemini CLI stopped serving personal logins on 2026-06-18; the API key
    # in the UI's env (loaded from .env for cloud evaluation) is now the ONLY
    # way it runs, so the console must inherit our environment untouched —
    # no `env=` override — and the script carries the registry's default
    # model rather than any `env -u` prefix.
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "linux")
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setenv("BATCH_CLI", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "from-dotenv")
    mocker.patch("pipeline.app.skills.shutil.which", side_effect=_which_for("gemini", "xterm"))
    popen = mocker.patch("pipeline.app.skills.subprocess.Popen")
    r = client.post("/api/skills/launch", json={"skill": "apply", "num": "1"})
    assert r.status_code == 200, r.text
    assert popen.call_args.kwargs.get("env") is None
    script = open(popen.call_args.args[0][2], encoding="utf-8").read()
    model = agent_cli.AGENT_CLIS["gemini"].default_model
    assert f"gemini -m {model} -i " in script
    assert "env -u" not in script and "GOOGLE_API_KEY" not in script


def test_launch_keeps_the_env_for_claude(client, mocker, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "linux")
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setenv("BATCH_CLI", "claude")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    mocker.patch("pipeline.app.skills.shutil.which", side_effect=_which_for("claude", "xterm"))
    popen = mocker.patch("pipeline.app.skills.subprocess.Popen")
    r = client.post("/api/skills/launch", json={"skill": "apply", "num": "1"})
    assert r.status_code == 200, r.text
    assert popen.call_args.kwargs.get("env") is None


def test_launch_linux_honors_TERMINAL_env(client, mocker, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "linux")
    monkeypatch.setenv("TERMINAL", "alacritty")
    mocker.patch("pipeline.app.skills.shutil.which", return_value="/usr/bin/x")
    popen = mocker.patch("pipeline.app.skills.subprocess.Popen")
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["launcher"] == "alacritty"
    args, _ = popen.call_args
    assert args[0][0] == "alacritty"


def test_launch_linux_no_terminal_emulator(client, mocker, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "linux")
    monkeypatch.delenv("TERMINAL", raising=False)
    # Only the agent CLI is on PATH — no terminal emulators.
    mocker.patch("pipeline.app.skills.shutil.which", side_effect=_which_for("cli"))
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    # capabilities reports terminal.available == False → endpoint 501s.
    assert r.status_code == 501
    assert "terminal" in r.json()["detail"].lower()


def test_launch_refused_on_unsupported_os(client, monkeypatch):
    monkeypatch.setattr("pipeline.app.skills.os.name", "posix")
    monkeypatch.setattr("pipeline.app.skills.sys.platform", "freebsd13")
    r = client.post("/api/skills/launch", json={"skill": "tailor-resume", "num": "1"})
    assert r.status_code == 501


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


# The same three questions again with UI_LAN on. The guard is one middleware
# with two modes, and the mode is read per request, so the loopback-only rules
# above are only half the contract — the other half is that turning LAN mode on
# does not quietly widen them. `testserver` is the Host TestClient sends; the
# default peer address is not an IP at all, which the peer rule reads as remote,
# so these use /api/status — one of the two routes the LAN view may change.

_LAN_PW = "a-long-passphrase"


@pytest.fixture
def lan_env(monkeypatch):
    from pipeline.app import server
    monkeypatch.setenv(server.UI_LAN_ENV, "1")
    monkeypatch.setenv(server.UI_PASSWORD_ENV, _LAN_PW)
    monkeypatch.setenv(server.UI_ALLOWED_HOSTS_ENV, "testserver")
    import base64
    return {"Authorization": "Basic " + base64.b64encode(
        f"me:{_LAN_PW}".encode()).decode()}


def test_lan_still_refuses_a_foreign_origin(client, lan_env):
    r = client.post("/api/status", json={"num": "1", "status": "Applied"},
                    headers={**lan_env, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_lan_accepts_the_loopback_origin(client, lan_env):
    """Loopback never stops working: the same machine's browser is how the UI
    is used even while it is also served to the network."""
    r = client.post("/api/status", json={"num": "1", "status": "Applied"},
                    headers={**lan_env, "Origin": "http://localhost:8000",
                             "Host": "localhost:8000"})
    assert r.status_code == 200, r.text


def test_lan_makes_reads_need_the_password(client, lan_env):
    """Outside LAN mode a GET is unguarded — only this machine can send one.
    Under LAN mode the tracker is on the network, and it is read with a GET."""
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers=lan_env).status_code == 200
