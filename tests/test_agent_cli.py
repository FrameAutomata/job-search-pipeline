"""Tests for pipeline/agent_cli.py — the agent-CLI registry.

Four places used to answer "which CLI" on their own, all saying claude. The
registry is now the one answer, and every file that has to restate it because
it can't import it gets a guard here that parses the mirror: `.env.example`,
`setup-profile.mjs`, the run/setup wrappers. (`onboard.html`'s select is
guarded beside the sites guard in tests/test_app_onboard.py.)

The argv shapes and MCP registrations were read off the installed CLIs' own
`--help` (gemini 0.59.0, qwen 0.23.2, opencode 1.18.30); Antigravity's come
from documentation only and the registry says so. The tests pin what was
read, so a change here is a deliberate one.
"""

import ast
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import agent_cli
from pipeline.agent_cli import (
    AGENT_CLIS, AGENT_MODEL_ENV, DEFAULT_CLI, PLAYWRIGHT_MCP_COMMAND,
    REGISTER_MCP_CMD, AgentCli, cli_available, installed, playwright_mcp_note,
    register_playwright_mcp, resolve_cli, resolve_model, shell_command,
)

ROOT = Path(__file__).resolve().parent.parent

COMPANY = "Acme \"Bob\" & Sons 'Ltd' $tore"
PROMPT = f"use apply mode to help me fill out the application for {COMPANY} / Rep"

GEMINI_MODEL = AGENT_CLIS["gemini"].default_model


def _which(available):
    """A shutil.which stand-in: a path for the names in `available`, else None."""
    return lambda name: f"/usr/bin/{name}" if name in available else None


# ── Registry integrity ───────────────────────────────────────────────────────

class TestRegistry:
    def test_ids_in_display_order_free_first(self):
        assert list(AGENT_CLIS) == ["opencode", "agy", "gemini", "claude", "qwen"]
        tiers = [c.tier for c in AGENT_CLIS.values()]
        assert tiers == ["free", "free", "free", "paid", "paid"]

    def test_default_is_the_free_provider_agnostic_one(self):
        # OpenCode is free with no key at all (Zen's rotating models) AND the
        # paid path (any provider key) — one CLI for both halves of the goal.
        assert DEFAULT_CLI == "opencode"
        assert AGENT_CLIS[DEFAULT_CLI].tier == "free"

    def test_every_entry_is_complete_and_keyed_by_its_id(self):
        for cid, c in AGENT_CLIS.items():
            assert c.id == cid
            assert c.label and c.binary and c.tier_note and c.install_hint
            assert c.docs_url.startswith("https://")
            assert c.tier in ("free", "paid")
        binaries = [c.binary for c in AGENT_CLIS.values()]
        assert len(set(binaries)) == len(binaries)

    def test_free_tier_notes_state_a_daily_or_weekly_limit_or_rotation(self):
        # "Free" without a number is how a person discovers the ceiling
        # mid-application; the note must translate it or say it moves.
        for c in AGENT_CLIS.values():
            if c.tier != "free":
                continue
            note = c.tier_note.lower()
            assert re.search(r"\d[\d,]*\s*(requests|req)?\s*(/|per)\s*(day|week)", note) \
                or "weekly" in note or "rotating" in note, c.id

    def test_gemini_note_states_the_login_ended_and_the_key_and_model_it_runs_on(self):
        c = AGENT_CLIS["gemini"]
        note = c.tier_note
        assert "2026-06-18" in note
        assert "GEMINI_API_KEY" in note
        # The model the launcher passes is the registry's, not a second copy.
        assert f"-m {c.default_model}" in note
        assert "requests/day" in note
        # Data use is disclosed.
        assert "improve Google's products" in note
        # Nothing says the key is stripped any more.
        assert "strip" not in note.lower()

    def test_agy_note_states_the_weekly_quota_and_data_use(self):
        note = AGENT_CLIS["agy"].tier_note
        assert "WEEKLY" in note and "week" in note
        assert "opt out" in note
        assert AGENT_CLIS["agy"].label == "Antigravity CLI"

    def test_opencode_note_names_both_free_routes_and_the_paid_one(self):
        note = AGENT_CLIS["opencode"].tier_note
        assert "rotating" in note
        assert "Gemini API key" in note and "requests/day" in note
        assert "Paid keys" in note
        assert "--prompt" in note

    def test_qwen_is_paid_since_its_free_login_ended(self):
        assert AGENT_CLIS["qwen"].tier == "paid"
        assert "2026-04-15" in AGENT_CLIS["qwen"].tier_note

    def test_no_entry_strips_environment_variables(self):
        # Gemini CLI now runs on the API key, so nothing may unset it — and the
        # field that did is gone rather than empty, so no launcher can revive it.
        for c in AGENT_CLIS.values():
            assert not hasattr(c, "env_unset"), c.id
        assert not hasattr(agent_cli, "GOOGLE_KEY_VARS")

    def test_only_gemini_has_a_default_model(self):
        assert GEMINI_MODEL == "gemini-3.1-flash-lite"
        for c in AGENT_CLIS.values():
            if c.id != "gemini":
                assert c.default_model == "", c.id

    def test_every_entry_takes_a_model_flag(self):
        assert {c.id: c.model_flag for c in AGENT_CLIS.values()} == {
            "opencode": "--model", "agy": "--model", "gemini": "-m",
            "claude": "--model", "qwen": "-m",
        }

    def test_verified_shapes_name_their_help_and_agy_admits_it_is_not(self):
        for c in AGENT_CLIS.values():
            if c.id == "agy":
                assert c.verified_against == ""
            else:
                assert c.verified_against, c.id

    def test_module_is_a_stdlib_leaf(self):
        # The jobspy-free UI venv imports it; dotenv is deferred into main().
        # Full dotted names, so a `pipeline.batch_evaluate` (provider clients)
        # or `pipeline.app.server` import can't pass as "pipeline".
        tree = ast.parse((ROOT / "pipeline" / "agent_cli.py").read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = set()
        for n in top:
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            else:
                names.add(n.module or "")
        ours = {n for n in names if n.startswith("pipeline")}
        assert ours == {"pipeline.stdio"}   # itself stdlib-only
        assert {n.split(".")[0] for n in names - ours} <= set(sys.stdlib_module_names)


# ── resolve_cli / resolve_model ──────────────────────────────────────────────

class TestResolveCli:
    def test_default_when_unset_or_blank(self):
        assert resolve_cli({}).id == DEFAULT_CLI
        assert resolve_cli({"BATCH_CLI": "   "}).id == DEFAULT_CLI

    def test_known_value_case_insensitive(self):
        assert resolve_cli({"BATCH_CLI": "claude"}).id == "claude"
        assert resolve_cli({"BATCH_CLI": " Gemini "}).id == "gemini"
        assert resolve_cli({"BATCH_CLI": "agy"}).id == "agy"

    def test_unknown_value_warns_once_and_uses_the_default(self, capsys, monkeypatch):
        monkeypatch.setattr(agent_cli, "_warned", set())
        assert resolve_cli({"BATCH_CLI": "copilot"}).id == DEFAULT_CLI
        assert resolve_cli({"BATCH_CLI": "copilot"}).id == DEFAULT_CLI
        err = capsys.readouterr().err
        assert err.count("copilot") == 1
        assert DEFAULT_CLI in err

    def test_reads_the_process_env_by_default(self, monkeypatch):
        monkeypatch.setenv("BATCH_CLI", "qwen")
        assert resolve_cli().id == "qwen"

    def test_cli_available_and_installed_go_through_shutil_which(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"gemini", "claude"}))
        assert cli_available(AGENT_CLIS["gemini"]) is True
        assert cli_available(AGENT_CLIS["qwen"]) is False
        assert [c.id for c in installed()] == ["gemini", "claude"]


class TestResolveModel:
    def test_env_wins_for_every_cli(self):
        for c in AGENT_CLIS.values():
            assert resolve_model(c, {AGENT_MODEL_ENV: " my/model "}) == "my/model"

    def test_unset_falls_back_to_the_registry_default(self):
        assert resolve_model(AGENT_CLIS["gemini"], {}) == GEMINI_MODEL
        assert resolve_model(AGENT_CLIS["gemini"], {AGENT_MODEL_ENV: "  "}) == GEMINI_MODEL
        for cid in ("opencode", "agy", "claude", "qwen"):
            assert resolve_model(AGENT_CLIS[cid], {}) == ""

    def test_reads_the_process_env_by_default(self, monkeypatch):
        monkeypatch.setenv(AGENT_MODEL_ENV, "x/y")
        assert resolve_model(AGENT_CLIS["claude"]) == "x/y"
        monkeypatch.delenv(AGENT_MODEL_ENV)
        assert resolve_model(AGENT_CLIS["claude"]) == ""


# ── interactive_argv ─────────────────────────────────────────────────────────

class TestInteractiveArgv:
    def test_gemini_carries_its_default_model_before_the_seed_flag(self):
        # Its own default Flash model has ~20 requests/day on a free key.
        assert AGENT_CLIS["gemini"].interactive_argv("hi") == \
            ["gemini", "-m", GEMINI_MODEL, "-i", "hi"]

    def test_qwen_and_agy_use_the_prompt_interactive_flag_with_no_model(self):
        assert AGENT_CLIS["qwen"].interactive_argv("hi") == ["qwen", "-i", "hi"]
        assert AGENT_CLIS["agy"].interactive_argv("hi") == ["agy", "-i", "hi"]

    def test_opencode_prefills_the_tui_with_prompt(self):
        # Its root positional is a PROJECT DIRECTORY and `opencode run` is
        # non-interactive — neither is the apply assistant.
        assert AGENT_CLIS["opencode"].interactive_argv("hi") == ["opencode", "--prompt", "hi"]

    def test_claude_takes_the_positional(self):
        assert AGENT_CLIS["claude"].interactive_argv("hi") == ["claude", "hi"]

    @pytest.mark.parametrize("cid,expected", [
        ("opencode", ["opencode", "--model", "google/m", "--prompt", "hi"]),
        ("agy", ["agy", "--model", "google/m", "-i", "hi"]),
        ("gemini", ["gemini", "-m", "google/m", "-i", "hi"]),
        ("claude", ["claude", "--model", "google/m", "hi"]),
        ("qwen", ["qwen", "-m", "google/m", "-i", "hi"]),
    ])
    def test_agent_model_renders_the_model_flag_per_cli(self, cid, expected, monkeypatch):
        monkeypatch.setenv(AGENT_MODEL_ENV, "google/m")
        assert AGENT_CLIS[cid].interactive_argv("hi") == expected
        # The explicit kwarg is the same rendering without the env.
        monkeypatch.delenv(AGENT_MODEL_ENV)
        assert AGENT_CLIS[cid].interactive_argv("hi", model="google/m") == expected

    def test_explicit_empty_model_renders_none_even_for_gemini(self):
        assert AGENT_CLIS["gemini"].interactive_argv("hi", model="") == ["gemini", "-i", "hi"]

    def test_never_a_bare_run_subcommand(self):
        for c in AGENT_CLIS.values():
            assert "run" not in c.interactive_argv("hi")


# ── shell_command ────────────────────────────────────────────────────────────

class TestShellCommand:
    def test_posix_exact_string_for_claude(self):
        got = shell_command(AGENT_CLIS["claude"], PROMPT, os_name="posix")
        assert got == (
            "cd career-ops && claude 'use apply mode to help me fill out the "
            "application for Acme \"Bob\" & Sons '\"'\"'Ltd'\"'\"' $tore / Rep'"
        )

    def test_nt_exact_string_for_claude(self):
        # Embedded `"` become `'` (cmd would read list2cmdline's `\"` as the
        # end of the quoted region — see the `&` case below).
        got = shell_command(AGENT_CLIS["claude"], PROMPT, os_name="nt")
        assert got == (
            'cd career-ops && claude "use apply mode to help me fill out the '
            "application for Acme 'Bob' & Sons 'Ltd' $tore / Rep\""
        )

    def test_posix_exact_string_for_gemini_carries_the_default_model_and_no_unset(self):
        got = shell_command(AGENT_CLIS["gemini"], PROMPT, os_name="posix")
        assert got == (
            f"cd career-ops && gemini -m {GEMINI_MODEL} -i "
            "'use apply mode to help me fill out the application for Acme \"Bob\" "
            "& Sons '\"'\"'Ltd'\"'\"' $tore / Rep'"
        )

    def test_nt_exact_string_for_gemini_carries_the_default_model_and_no_unset(self):
        got = shell_command(AGENT_CLIS["gemini"], PROMPT, os_name="nt")
        assert got == (
            f"cd career-ops && gemini -m {GEMINI_MODEL} -i "
            '"use apply mode to help me fill out the application for '
            "Acme 'Bob' & Sons 'Ltd' $tore / Rep\""
        )

    def test_posix_exact_string_for_the_default_cli(self):
        got = shell_command(AGENT_CLIS[DEFAULT_CLI], PROMPT, os_name="posix")
        assert got == (
            "cd career-ops && opencode --prompt 'use apply mode to help me fill out the "
            "application for Acme \"Bob\" & Sons '\"'\"'Ltd'\"'\"' $tore / Rep'"
        )

    @pytest.mark.parametrize("cid", list(AGENT_CLIS))
    def test_no_command_unsets_or_names_the_google_keys(self, cid):
        # The key is how an individual runs Gemini CLI now; stripping it was
        # the M1 rule this correction retires.
        for os_name in ("posix", "nt"):
            got = shell_command(AGENT_CLIS[cid], PROMPT, os_name=os_name)
            assert "env -u" not in got
            assert "set GEMINI_API_KEY" not in got and "GOOGLE_API_KEY" not in got

    @pytest.mark.parametrize("cid", list(AGENT_CLIS))
    def test_agent_model_reaches_both_shells(self, cid, monkeypatch):
        cli = AGENT_CLIS[cid]
        monkeypatch.setenv(AGENT_MODEL_ENV, "prov/model-x")
        posix = shell_command(cli, "hi", os_name="posix")
        nt = shell_command(cli, "hi", os_name="nt")
        assert shlex.split(posix)[3:] == [cli.binary, cli.model_flag, "prov/model-x",
                                          *([cli.prompt_flag] if cli.prompt_flag else []), "hi"]
        assert nt.startswith(f"cd career-ops && {cli.binary} {cli.model_flag} prov/model-x ")
        assert nt.endswith(f"{cli.prompt_flag} hi" if cli.prompt_flag else " hi")

    @pytest.mark.parametrize("cid", list(AGENT_CLIS))
    def test_without_agent_model_only_gemini_renders_a_model(self, cid):
        cli = AGENT_CLIS[cid]
        for os_name in ("posix", "nt"):
            got = shell_command(cli, "hi", os_name=os_name)
            if cid == "gemini":
                assert f" -m {GEMINI_MODEL} -i " in got
            else:
                assert cli.model_flag not in got.split("&&", 1)[1]

    def test_model_kwarg_is_passed_through(self):
        got = shell_command(AGENT_CLIS["claude"], "hi", os_name="posix", model="m1")
        assert got == "cd career-ops && claude --model m1 hi"

    @pytest.mark.parametrize("cid", list(AGENT_CLIS))
    def test_nt_never_emits_an_escaped_quote_so_cmd_keeps_the_prompt_whole(self, cid):
        # cmd.exe toggles its quote state on EVERY `"` and reads `\` as a
        # literal, so list2cmdline's `\"` closes the quoted region for it and
        # an `&` between two embedded quotes split the command — the CLI got a
        # truncated prompt and cmd ran `Sons…` as a second command.
        got = shell_command(AGENT_CLIS[cid], 'use apply mode for Acme "Bob & Sons" / Rep', os_name="nt")
        assert '\\"' not in got
        assert "Acme 'Bob & Sons' / Rep" in got
        # Walk cmd's quote state: no `&`, `|`, `<`, `>` outside a quoted
        # region except the one `&&` separator after `cd`.
        quoted, bare = False, []
        for ch in got:
            if ch == '"':
                quoted = not quoted
            elif ch in "&|<>" and not quoted:
                bare.append(ch)
        assert not quoted
        assert bare == ["&", "&"]

    def test_os_name_is_read_at_call_time(self):
        # A default bound at import (`os_name=os.name`) can't see a test's
        # `monkeypatch.setattr("os.name", "nt")`, so the Windows launcher test
        # on CI would render the POSIX branch into a .cmd and pass anyway.
        assert shell_command.__kwdefaults__["os_name"] is None
        assert shell_command(AGENT_CLIS["claude"], "x") == \
            shell_command(AGENT_CLIS["claude"], "x", os_name=agent_cli.os.name)

    @pytest.mark.parametrize("cid", list(AGENT_CLIS))
    def test_posix_round_trips_to_interactive_argv(self, cid):
        cli = AGENT_CLIS[cid]
        posix = shell_command(cli, PROMPT, os_name="posix")
        tokens = shlex.split(posix)
        assert tokens[:3] == ["cd", "career-ops", "&&"]
        assert tokens[3:] == cli.interactive_argv(PROMPT)

    def test_per_cli_shape(self):
        assert " -i " in shell_command(AGENT_CLIS["gemini"], PROMPT, os_name="posix")
        assert " -i " in shell_command(AGENT_CLIS["qwen"], PROMPT, os_name="posix")
        assert " -i " in shell_command(AGENT_CLIS["agy"], PROMPT, os_name="posix")
        assert " --prompt " in shell_command(AGENT_CLIS["opencode"], PROMPT, os_name="posix")
        claude = shell_command(AGENT_CLIS["claude"], PROMPT, os_name="posix")
        assert claude.startswith("cd career-ops && claude '")

    def test_newlines_collapse_to_spaces(self):
        got = shell_command(AGENT_CLIS["claude"], "line one\nline two\r\nthree", os_name="posix")
        assert "\n" not in got and "\r" not in got
        assert shlex.split(got)[3:] == ["claude", "line one line two three"]

    def test_cwd_hint(self):
        got = shell_command(AGENT_CLIS["claude"], "x", cwd_hint="elsewhere", os_name="posix")
        assert got.startswith("cd elsewhere && ")


# ── MCP registration ─────────────────────────────────────────────────────────

class TestMcpRegistration:
    def test_claude_argv_forces_user_scope(self):
        # Its scope DEFAULT is `local` (per cwd): a server added from the repo
        # root is invisible to `cd career-ops && claude …`, a separate checkout.
        reg = AGENT_CLIS["claude"].mcp_registration()
        assert reg.is_argv
        assert list(reg.argv) == ["claude", "mcp", "add", "-s", "user", "playwright", "--", *PLAYWRIGHT_MCP_COMMAND]

    def test_every_argv_registration_is_user_scoped(self):
        for c in AGENT_CLIS.values():
            reg = c.mcp_registration(home=Path("/h"), env={})
            if reg.is_argv:
                assert reg.argv[1:5] == ("mcp", "add", "-s", "user"), c.id

    def test_gemini_argv_forces_user_scope(self):
        # Its scope DEFAULT is `project` — without -s user the registration
        # lands in the cwd's .gemini/settings.json.
        reg = AGENT_CLIS["gemini"].mcp_registration()
        assert list(reg.argv) == ["gemini", "mcp", "add", "-s", "user", "playwright", *PLAYWRIGHT_MCP_COMMAND]

    def test_qwen_argv(self):
        reg = AGENT_CLIS["qwen"].mcp_registration()
        assert list(reg.argv) == ["qwen", "mcp", "add", "-s", "user", "playwright", *PLAYWRIGHT_MCP_COMMAND]

    def test_opencode_is_a_config_merge(self, tmp_path):
        reg = AGENT_CLIS["opencode"].mcp_registration(home=tmp_path, env={})
        assert not reg.is_argv
        assert reg.config_path == tmp_path / ".config" / "opencode" / "opencode.json"
        assert reg.servers_key == "mcp"
        assert reg.merge == {"mcp": {"playwright": {
            "type": "local", "command": list(PLAYWRIGHT_MCP_COMMAND), "enabled": True,
        }}}

    def test_opencode_honours_xdg_config_home(self, tmp_path):
        reg = AGENT_CLIS["opencode"].mcp_registration(
            home=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
        assert reg.config_path == tmp_path / "xdg" / "opencode" / "opencode.json"

    def test_agy_is_a_config_merge_under_home_in_the_mcpservers_shape(self, tmp_path):
        # No `agy mcp add`; the file is home-relative (not XDG), so
        # XDG_CONFIG_HOME must not move it — and on Windows `Path.home()` is
        # %USERPROFILE%, which is where Google's docs put it too.
        reg = AGENT_CLIS["agy"].mcp_registration(
            home=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
        assert not reg.is_argv
        assert reg.config_path == tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
        assert reg.servers_key == "mcpServers"
        assert reg.merge == {"mcpServers": {"playwright": {
            "command": "npx", "args": ["-y", "@playwright/mcp@latest"],
        }}}


class TestRegisterPlaywrightMcp:
    def test_missing_binary_returns_the_install_hint_and_never_raises(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        calls = []
        for cid in ("gemini", "agy"):
            msg = register_playwright_mcp(AGENT_CLIS[cid], run=lambda *a, **k: calls.append(a))
            assert AGENT_CLIS[cid].install_hint in msg
        assert calls == []

    def test_argv_success(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"gemini"}))
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        msg = register_playwright_mcp(AGENT_CLIS["gemini"], run=run)
        # argv[0] is the RESOLVED path: on Windows the npm shim is `gemini.cmd`,
        # which `which` finds through PATHEXT and a bare-name CreateProcess
        # (only `.exe` appended) does not — WinError 2 for every npm CLI.
        argv = list(AGENT_CLIS["gemini"].mcp_registration().argv)
        assert seen["argv"] == ["/usr/bin/gemini", *argv[1:]]
        assert seen["kw"].get("capture_output") is True
        assert "Registered" in msg and "Gemini CLI" in msg

    def test_failure_message_shows_the_bare_command_not_the_resolved_path(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"claude"}))
        run = lambda argv, **kw: SimpleNamespace(returncode=2, stdout="", stderr="boom\n")  # noqa: E731
        msg = register_playwright_mcp(AGENT_CLIS["claude"], run=run)
        assert "`claude mcp add" in msg and "/usr/bin/" not in msg

    def test_claude_already_registered_is_success(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"claude"}))
        run = lambda argv, **kw: SimpleNamespace(  # noqa: E731
            returncode=1, stdout="", stderr="MCP server playwright already exists in local config\n")
        msg = register_playwright_mcp(AGENT_CLIS["claude"], run=run)
        assert "already registered" in msg
        assert "failed" not in msg

    def test_other_failure_reports_the_output(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"claude"}))
        run = lambda argv, **kw: SimpleNamespace(returncode=2, stdout="", stderr="boom: no such option\n")  # noqa: E731
        msg = register_playwright_mcp(AGENT_CLIS["claude"], run=run)
        assert "failed" in msg and "boom" in msg

    def test_run_raising_oserror_is_a_message_not_a_traceback(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"claude"}))

        def run(argv, **kw):
            raise OSError("exec format error")

        msg = register_playwright_mcp(AGENT_CLIS["claude"], run=run)
        assert "exec format error" in msg

    def test_opencode_merge_creates_parents_and_keeps_other_keys(self, tmp_path, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"opencode"}))
        cfg = tmp_path / ".config" / "opencode" / "opencode.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"theme": "dark", "mcp": {"other": {"type": "remote", "url": "http://x"}}}))

        msg = register_playwright_mcp(AGENT_CLIS["opencode"], home=tmp_path, env={})
        assert "registered in" in msg
        got = json.loads(cfg.read_text())
        assert got["theme"] == "dark"
        assert got["mcp"]["other"] == {"type": "remote", "url": "http://x"}
        assert got["mcp"]["playwright"] == {
            "type": "local", "command": list(PLAYWRIGHT_MCP_COMMAND), "enabled": True}
        # No temp file left beside it.
        assert sorted(p.name for p in cfg.parent.iterdir()) == ["opencode.json"]

    def test_opencode_merge_creates_the_file_from_nothing(self, tmp_path, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"opencode"}))
        register_playwright_mcp(AGENT_CLIS["opencode"], home=tmp_path, env={})
        cfg = tmp_path / ".config" / "opencode" / "opencode.json"
        assert json.loads(cfg.read_text())["mcp"]["playwright"]["enabled"] is True

    def test_opencode_merge_is_idempotent(self, tmp_path, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"opencode"}))
        first = register_playwright_mcp(AGENT_CLIS["opencode"], home=tmp_path, env={})
        cfg = tmp_path / ".config" / "opencode" / "opencode.json"
        before = cfg.read_text()
        second = register_playwright_mcp(AGENT_CLIS["opencode"], home=tmp_path, env={})
        assert cfg.read_text() == before
        assert "already" in second and "already" not in first

    def test_opencode_corrupt_config_is_left_alone(self, tmp_path, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"opencode"}))
        cfg = tmp_path / ".config" / "opencode" / "opencode.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{not json")
        msg = register_playwright_mcp(AGENT_CLIS["opencode"], home=tmp_path, env={})
        assert cfg.read_text() == "{not json"
        assert "left it alone" in msg and "playwright" in msg

    def test_agy_merge_keeps_other_servers_under_mcpservers_and_is_idempotent(self, tmp_path, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"agy"}))
        cfg = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "extra": 1}))
        first = register_playwright_mcp(AGENT_CLIS["agy"], home=tmp_path, env={})
        assert "registered in" in first and "already" not in first
        got = json.loads(cfg.read_text())
        assert got["extra"] == 1
        assert got["mcpServers"]["other"] == {"command": "x"}
        assert got["mcpServers"]["playwright"] == {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
        assert "mcp" not in got   # not OpenCode's shape
        before = cfg.read_text()
        second = register_playwright_mcp(AGENT_CLIS["agy"], home=tmp_path, env={})
        assert "already" in second and cfg.read_text() == before

    def test_agy_merge_creates_the_file_from_nothing(self, tmp_path, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"agy"}))
        register_playwright_mcp(AGENT_CLIS["agy"], home=tmp_path, env={})
        cfg = tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
        assert json.loads(cfg.read_text()) == {"mcpServers": {"playwright": {
            "command": "npx", "args": ["-y", "@playwright/mcp@latest"]}}}


class TestPlaywrightMcpNote:
    @pytest.mark.parametrize("cid", ["gemini", "claude", "qwen"])
    def test_argv_form_names_the_command(self, cid):
        cli = AGENT_CLIS[cid]
        note = playwright_mcp_note(cli)
        assert "Playwright MCP" in note
        assert cli.label in note
        assert shlex.join(cli.mcp_registration().argv) in note
        assert note.endswith(f"`{REGISTER_MCP_CMD}` does this for you")

    def test_config_form_names_the_file(self):
        note = playwright_mcp_note(AGENT_CLIS["opencode"])
        assert "merge the `playwright` server into" in note
        assert "opencode/opencode.json" in note
        assert "claude mcp add" not in note
        assert note.endswith(f"`{REGISTER_MCP_CMD}` does this for you")

    def test_agy_config_form_names_its_file(self):
        note = playwright_mcp_note(AGENT_CLIS["agy"])
        assert "Antigravity CLI" in note
        assert "merge the `playwright` server into" in note
        assert ".gemini/antigravity/mcp_config.json" in note
        assert "mcp add" not in note
        assert note.endswith(f"`{REGISTER_MCP_CMD}` does this for you")


# ── the command line ─────────────────────────────────────────────────────────

class TestMain:
    def test_list_prints_every_id_with_tier_and_marks_the_default(self, capsys, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({DEFAULT_CLI}))
        assert agent_cli.main(["--list"]) == 0
        out = capsys.readouterr().out
        for c in AGENT_CLIS.values():
            assert re.search(rf"^{c.id}\s.*\b{c.tier}\b", out, re.M), c.id
        assert re.search(rf"^{DEFAULT_CLI}\s.*\byes\b.*\(default\)", out, re.M)
        assert re.search(r"^agy\s.*\bfree\b.*\bno\b", out, re.M)
        assert re.search(r"^claude\s.*\bno\b", out, re.M)

    def test_check_exits_1_with_the_hint_when_not_installed(self, capsys, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        assert agent_cli.main(["--check"]) == 1
        assert AGENT_CLIS[DEFAULT_CLI].install_hint in capsys.readouterr().out

    def test_check_exits_0_and_prints_the_launch_line_when_installed(self, capsys, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({DEFAULT_CLI}))
        assert agent_cli.main(["--check"]) == 0
        out = capsys.readouterr().out
        assert "Launches as: cd career-ops && opencode --prompt" in out
        assert "read from `opencode 1.18.30` --help" in out

    def test_check_on_agy_says_the_shape_is_unverified(self, capsys, mocker, monkeypatch):
        # Its installer is unreachable from the sandbox that read the other
        # CLIs' --help, so the user's first run is the verification.
        monkeypatch.setenv("BATCH_CLI", "agy")
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"agy"}))
        assert agent_cli.main(["--check"]) == 0
        out = capsys.readouterr().out
        assert "Launches as: cd career-ops && agy -i" in out
        assert "documentation only" in out

    def test_check_on_agy_not_installed_prints_the_curl_hint(self, capsys, mocker, monkeypatch):
        monkeypatch.setenv("BATCH_CLI", "agy")
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        assert agent_cli.main(["--check"]) == 1
        assert "antigravity.google/cli/install.sh" in capsys.readouterr().out

    def test_register_all_installed_always_exits_0_and_prints_hints_when_none(self, capsys, mocker):
        # setup.sh runs it under `set -euo pipefail`.
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        assert agent_cli.main(["--register-mcp-all-installed"]) == 0
        out = capsys.readouterr().out
        assert list(AGENT_CLIS)[0] in out.split("\n", 2)[1]   # free first
        for c in AGENT_CLIS.values():
            assert c.install_hint in out

    def test_register_all_installed_registers_each_installed_cli(self, capsys, mocker, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_cli.Path, "home", classmethod(lambda cls: tmp_path))
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"gemini", "claude", "agy"}))
        fake = mocker.patch("pipeline.agent_cli.subprocess.run",
                            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert agent_cli.main(["--register-mcp-all-installed"]) == 0
        ran = [c.args[0][0] for c in fake.call_args_list]
        assert ran == ["/usr/bin/gemini", "/usr/bin/claude"]   # resolved, registry order
        out = capsys.readouterr().out
        assert "Gemini CLI" in out and "Claude Code" in out and "Antigravity CLI" in out
        # agy went through the config merge, not a subprocess.
        assert (tmp_path / ".gemini" / "antigravity" / "mcp_config.json").exists()

    def test_register_mcp_defaults_to_the_resolved_cli(self, mocker, monkeypatch):
        monkeypatch.setenv("BATCH_CLI", "qwen")
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"qwen"}))
        fake = mocker.patch("pipeline.agent_cli.subprocess.run",
                            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert agent_cli.main(["--register-mcp"]) == 0
        assert fake.call_args.args[0][0] == "/usr/bin/qwen"

    def test_register_mcp_unknown_id_is_a_usage_error(self, capsys):
        assert agent_cli.main(["--register-mcp", "copilot"]) == 2
        assert "copilot" in capsys.readouterr().err

    def test_resolved_prints_the_id(self, capsys, monkeypatch):
        monkeypatch.setenv("BATCH_CLI", "gemini")
        assert agent_cli.main(["--resolved"]) == 0
        assert capsys.readouterr().out.strip() == "gemini"

    def test_resolved_prints_the_default_when_unset(self, capsys, monkeypatch):
        # conftest clears BATCH_CLI; a .env in the checkout could re-add it, so
        # point the loader at nothing.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        assert agent_cli.main(["--resolved"]) == 0
        assert capsys.readouterr().out.strip() == DEFAULT_CLI

    def test_resolved_model_prints_agent_model_when_set(self, capsys, monkeypatch):
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        monkeypatch.setenv("BATCH_CLI", "opencode")
        monkeypatch.setenv(AGENT_MODEL_ENV, "google/model-x")
        assert agent_cli.main(["--resolved-model"]) == 0
        assert capsys.readouterr().out.strip() == "google/model-x"

    def test_resolved_model_falls_back_to_the_registry_default_for_gemini(self, capsys, monkeypatch):
        # This is the line run.sh/run.ps1 forward as batch-runner's --model, so
        # gemini's --batch runs on the ~500 req/day model, not the CLI's ~20.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        monkeypatch.setenv("BATCH_CLI", "gemini")
        assert agent_cli.main(["--resolved-model"]) == 0
        assert capsys.readouterr().out.strip() == GEMINI_MODEL

    def test_resolved_model_prints_nothing_when_the_cli_has_no_default(self, capsys, monkeypatch):
        # An empty line, not an error: the wrappers pass --model only when
        # there is one, and "" means the CLI's own default.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        assert agent_cli.main(["--resolved-model"]) == 0
        assert capsys.readouterr().out == "\n"

    def test_module_runs_as_a_script(self):
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.agent_cli", "--list"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        for cid in AGENT_CLIS:
            assert cid in proc.stdout


# ── Mirrors ──────────────────────────────────────────────────────────────────

class TestEnvExampleMirror:
    """`.env.example` restates the registry in a fixed, machine-readable shape:
    one `#   <id>    <tier>   …` line per CLI above `BATCH_CLI=<default>`, and
    an `AGENT_MODEL` block right after it."""

    text = (ROOT / ".env.example").read_text(encoding="utf-8")

    @classmethod
    def _block(cls):
        lines = cls.text.splitlines()
        idx = next(i for i, l in enumerate(lines) if l.startswith("BATCH_CLI="))
        start = idx
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        return lines[start:idx], lines[idx]

    def test_ids_and_tiers_equal_the_registry(self):
        comments, _ = self._block()
        found = {}
        for line in comments:
            m = re.match(r"^#\s{3}(\w+)\s+(free|paid)\b", line)
            if m:
                found[m.group(1)] = m.group(2)
        assert found == {c.id: c.tier for c in AGENT_CLIS.values()}
        # Free first, as the registry orders them.
        assert list(found) == list(AGENT_CLIS)

    def test_assigned_value_is_the_default(self):
        _, assignment = self._block()
        assert assignment == f"BATCH_CLI={DEFAULT_CLI}"

    def test_gemini_line_no_longer_claims_a_personal_login(self):
        comments, _ = self._block()
        line = next(l for l in comments if re.match(r"^#\s{3}gemini\b", l))
        assert "personal Google login" not in line or "ended" in line
        assert "GEMINI_API_KEY" in line
        # The launch model is restated here too; keep it the registry's.
        assert f"-m {GEMINI_MODEL}" in line

    def test_agent_model_is_documented_with_the_registry_default(self):
        m = re.search(r"^#?\s*" + AGENT_MODEL_ENV + r"=", self.text, re.M)
        assert m, f"no {AGENT_MODEL_ENV} line in .env.example"
        block = self.text[:m.start()].rsplit("\n\n", 1)[-1]
        assert GEMINI_MODEL in block
        for c in AGENT_CLIS.values():
            # Whole token: `-m` is a substring of `--model`, so a plain `in`
            # would be satisfied for gemini/qwen by any `--model` mention.
            assert re.search(r"(?<![\w-])" + re.escape(c.model_flag) + r"(?![\w-])", block), c.id

    def test_agent_model_block_says_it_reaches_batch_and_what_outranks_it(self):
        # AGENT_MODEL sits under "the one that applies for you and runs
        # --batch", so it must actually reach --batch — and say that
        # OLLAMA_MODEL, the older --batch-only name, still wins there.
        m = re.search(r"^#?\s*" + AGENT_MODEL_ENV + r"=", self.text, re.M)
        block = self.text[:m.start()].rsplit("\n\n", 1)[-1]
        assert "--batch" in block
        assert "OLLAMA_MODEL" in block
        m2 = re.search(r"^#?\s*OLLAMA_MODEL=", self.text, re.M)
        assert m2, "no OLLAMA_MODEL line in .env.example"
        ollama_block = self.text[:m2.start()].rsplit("\n\n", 1)[-1]
        assert AGENT_MODEL_ENV in ollama_block and "--batch" in ollama_block


class TestSetupProfileMirror:
    """setup-profile.mjs carries its own CLI table (Node can't import the
    registry) and a default of its own."""

    src = (ROOT / "setup-profile.mjs").read_text(encoding="utf-8")

    def test_every_registry_id_is_a_cli_commands_key(self):
        m = re.search(r"const CLI_COMMANDS = \{(.*?)\n\};", self.src, re.S)
        assert m, "no CLI_COMMANDS literal in setup-profile.mjs"
        keys = set(re.findall(r"^\s*(\w+):\s*\(prompt\)", m.group(1), re.M))
        assert set(AGENT_CLIS) <= keys, set(AGENT_CLIS) - keys

    def test_mjs_default_equals_the_registry_default(self):
        m = re.search(r"^\s*cli:\s*'(\w+)',", self.src, re.M)
        assert m, "no `cli: '…'` default in setup-profile.mjs"
        assert m.group(1) == DEFAULT_CLI


class TestWrapperMirror:
    """run.sh / run.ps1 ask the registry (`--resolved`) rather than carrying a
    default of their own — a CLI chosen in the wizard is written to .env, and
    the wrappers cannot read .env themselves."""

    @pytest.mark.parametrize("name", ["run.sh", "run.ps1"])
    def test_calls_resolved(self, name):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"-m pipeline\.agent_cli --resolved\b", src), name

    @pytest.mark.parametrize("name", ["run.sh", "run.ps1"])
    def test_calls_resolved_model(self, name):
        # AGENT_MODEL reaches --batch through this, not just the hand-off
        # command; OLLAMA_MODEL is consulted first (the older --batch name).
        src = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"-m pipeline\.agent_cli --resolved-model\b", src), name
        assert "OLLAMA_MODEL" in src

    def test_no_literal_default_remains(self):
        sh = (ROOT / "run.sh").read_text(encoding="utf-8")
        ps = (ROOT / "run.ps1").read_text(encoding="utf-8")
        assert not re.search(r"\$\{BATCH_CLI:-(\w+)\}", sh)
        assert not re.search(r'\$env:BATCH_CLI\s*\}\s*else\s*\{\s*"(\w+)"', ps)

    @pytest.mark.parametrize("name", ["setup.sh", "setup.ps1"])
    def test_setup_registers_through_the_registry(self, name):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "-m pipeline.agent_cli --register-mcp-all-installed" in src
        assert "claude mcp add" not in src


class TestNoKeyStrippingAnywhere:
    """The M1 rule that stripped GEMINI_API_KEY/GOOGLE_API_KEY from the CLI's
    environment is retired: the key is the only way an individual runs Gemini
    CLI since 2026-06-18. Nothing in the launcher path may re-grow it."""

    def test_skills_launcher_passes_no_env_override(self):
        src = (ROOT / "pipeline" / "app" / "skills.py").read_text(encoding="utf-8")
        assert "_launch_env" not in src
        assert "env_unset" not in src
        assert "GOOGLE_API_KEY" not in src
        # The prose form as well: the identifiers left before the docstring
        # describing the rendered command did.
        assert "env prefix" not in src
        assert not re.search(r"key out of", src)

    def test_registry_source_has_no_unset_rendering(self):
        src = (ROOT / "pipeline" / "agent_cli.py").read_text(encoding="utf-8")
        assert "env -u" not in src
        assert "env_unset" not in src


class TestAgentCliDataclass:
    def test_is_frozen(self):
        with pytest.raises(Exception):
            AGENT_CLIS["gemini"].tier = "paid"  # type: ignore[misc]

    def test_is_hashable_despite_the_dict_field(self):
        # frozen=True advertises hashability; the dict field opts out of the
        # generated __hash__ (hash=False) rather than making it raise.
        assert len({c for c in AGENT_CLIS.values()}) == len(AGENT_CLIS)
        assert hash(AGENT_CLIS[DEFAULT_CLI].mcp_registration(home=Path("/h"), env={})) is not None

    def test_custom_entry_shapes(self):
        c = AgentCli(id="x", label="X", binary="x", tier="paid", tier_note="n",
                     install_hint="i", docs_url="https://x", prompt_flag="--go")
        assert c.interactive_argv("p", model="") == ["x", "--go", "p"]
        # No model flag: a model is ignored rather than rendered as a positional.
        assert c.interactive_argv("p", model="m") == ["x", "--go", "p"]
        assert not c.mcp_registration(home=Path("/h"), env={}).is_argv
