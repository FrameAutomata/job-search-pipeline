"""Tests for pipeline/agent_cli.py — the agent-CLI registry.

Four places used to answer "which CLI" on their own, all saying claude. The
registry is now the one answer, and every file that has to restate it because
it can't import it gets a guard here that parses the mirror: `.env.example`,
`setup-profile.mjs`, the run/setup wrappers. (`onboard.html`'s select is
guarded beside the sites guard in tests/test_app_onboard.py.)

The argv shapes and MCP registrations were read off the installed CLIs' own
`--help`; the tests pin what was read, so a change here is a deliberate one.
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
    AGENT_CLIS, DEFAULT_CLI, GOOGLE_KEY_VARS, PLAYWRIGHT_MCP_COMMAND,
    REGISTER_MCP_CMD, AgentCli, cli_available, installed, playwright_mcp_note,
    register_playwright_mcp, resolve_cli, shell_command,
)

ROOT = Path(__file__).resolve().parent.parent

COMPANY = "Acme \"Bob\" & Sons 'Ltd' $tore"
PROMPT = f"use apply mode to help me fill out the application for {COMPANY} / Rep"


def _which(available):
    """A shutil.which stand-in: a path for the names in `available`, else None."""
    return lambda name: f"/usr/bin/{name}" if name in available else None


# ── Registry integrity ───────────────────────────────────────────────────────

class TestRegistry:
    def test_ids_in_display_order_free_first(self):
        assert list(AGENT_CLIS) == ["gemini", "opencode", "claude", "qwen"]
        tiers = [c.tier for c in AGENT_CLIS.values()]
        assert tiers == ["free", "free", "paid", "paid"]

    def test_default_is_the_free_one(self):
        assert DEFAULT_CLI == "gemini"
        assert AGENT_CLIS[DEFAULT_CLI].tier == "free"

    def test_every_entry_is_complete_and_keyed_by_its_id(self):
        for cid, c in AGENT_CLIS.items():
            assert c.id == cid
            assert c.label and c.binary and c.tier_note and c.install_hint
            assert c.docs_url.startswith("https://")
            assert c.tier in ("free", "paid")
        binaries = [c.binary for c in AGENT_CLIS.values()]
        assert len(set(binaries)) == len(binaries)

    def test_free_tier_notes_state_the_daily_limit_or_rotation(self):
        # "Free" without a number is how a person discovers the ceiling
        # mid-application; the note must translate it or say it moves.
        for c in AGENT_CLIS.values():
            if c.tier != "free":
                continue
            note = c.tier_note.lower()
            assert re.search(r"\d[\d,]*\s*(requests|req)?\s*(/|per)\s*day", note) \
                or "rotating" in note, c.id

    def test_gemini_note_says_the_login_is_the_free_tier_and_the_key_is_stripped(self):
        note = AGENT_CLIS["gemini"].tier_note
        assert "login" in note.lower()
        assert "strips" in note
        assert "GEMINI_API_KEY" in note
        # Data use is disclosed, with the notice cited.
        assert "opt out" in note
        assert "privacy-notice-gemini-code-assist-individuals" in note

    def test_qwen_is_paid_since_its_free_login_ended(self):
        assert AGENT_CLIS["qwen"].tier == "paid"
        assert "2026-04-15" in AGENT_CLIS["qwen"].tier_note

    def test_google_keys_are_unset_for_gemini_and_qwen_only(self):
        for c in AGENT_CLIS.values():
            if c.id in ("gemini", "qwen"):
                assert c.env_unset == GOOGLE_KEY_VARS
            else:
                assert c.env_unset == ()

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


# ── resolve_cli ──────────────────────────────────────────────────────────────

class TestResolveCli:
    def test_default_when_unset_or_blank(self):
        assert resolve_cli({}).id == DEFAULT_CLI
        assert resolve_cli({"BATCH_CLI": "   "}).id == DEFAULT_CLI

    def test_known_value_case_insensitive(self):
        assert resolve_cli({"BATCH_CLI": "claude"}).id == "claude"
        assert resolve_cli({"BATCH_CLI": " OpenCode "}).id == "opencode"

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


# ── interactive_argv ─────────────────────────────────────────────────────────

class TestInteractiveArgv:
    def test_gemini_and_qwen_use_the_prompt_interactive_flag(self):
        assert AGENT_CLIS["gemini"].interactive_argv("hi") == ["gemini", "-i", "hi"]
        assert AGENT_CLIS["qwen"].interactive_argv("hi") == ["qwen", "-i", "hi"]

    def test_opencode_prefills_the_tui_with_prompt(self):
        # Its root positional is a PROJECT DIRECTORY and `opencode run` is
        # non-interactive — neither is the apply assistant.
        assert AGENT_CLIS["opencode"].interactive_argv("hi") == ["opencode", "--prompt", "hi"]

    def test_claude_takes_the_positional(self):
        assert AGENT_CLIS["claude"].interactive_argv("hi") == ["claude", "hi"]

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

    def test_posix_exact_string_for_gemini_carries_the_unset_prefix(self):
        got = shell_command(AGENT_CLIS["gemini"], PROMPT, os_name="posix")
        assert got == (
            "cd career-ops && env -u GEMINI_API_KEY -u GOOGLE_API_KEY gemini -i "
            "'use apply mode to help me fill out the application for Acme \"Bob\" "
            "& Sons '\"'\"'Ltd'\"'\"' $tore / Rep'"
        )

    def test_nt_exact_string_for_gemini_carries_the_unset_prefix(self):
        got = shell_command(AGENT_CLIS["gemini"], PROMPT, os_name="nt")
        assert got == (
            "cd career-ops && set GEMINI_API_KEY=&& set GOOGLE_API_KEY=&& gemini -i "
            '"use apply mode to help me fill out the application for '
            "Acme 'Bob' & Sons 'Ltd' $tore / Rep\""
        )

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
        # region except the `&&` separators the prefix and `cd` emit.
        quoted, bare = False, []
        for ch in got:
            if ch == '"':
                quoted = not quoted
            elif ch in "&|<>" and not quoted:
                bare.append(ch)
        assert not quoted
        assert bare == ["&", "&"] * (1 + len(AGENT_CLIS[cid].env_unset))

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
        skip = 3 + (1 + 2 * len(cli.env_unset) if cli.env_unset else 0)
        assert tokens[skip:] == cli.interactive_argv(PROMPT)

    def test_per_cli_shape(self):
        assert " -i " in shell_command(AGENT_CLIS["gemini"], PROMPT, os_name="posix")
        assert " -i " in shell_command(AGENT_CLIS["qwen"], PROMPT, os_name="posix")
        assert " --prompt " in shell_command(AGENT_CLIS["opencode"], PROMPT, os_name="posix")
        claude = shell_command(AGENT_CLIS["claude"], PROMPT, os_name="posix")
        assert claude.startswith("cd career-ops && claude '")

    def test_only_gemini_and_qwen_carry_the_prefix(self):
        for cid in ("claude", "opencode"):
            for os_name in ("posix", "nt"):
                got = shell_command(AGENT_CLIS[cid], PROMPT, os_name=os_name)
                assert "GEMINI_API_KEY" not in got and "env -u" not in got and "set " not in got
        for os_name in ("posix", "nt"):
            assert "GOOGLE_API_KEY" in shell_command(AGENT_CLIS["qwen"], PROMPT, os_name=os_name)

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
        assert reg.merge == {"mcp": {"playwright": {
            "type": "local", "command": list(PLAYWRIGHT_MCP_COMMAND), "enabled": True,
        }}}

    def test_opencode_honours_xdg_config_home(self, tmp_path):
        reg = AGENT_CLIS["opencode"].mcp_registration(
            home=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
        assert reg.config_path == tmp_path / "xdg" / "opencode" / "opencode.json"


class TestRegisterPlaywrightMcp:
    def test_missing_binary_returns_the_install_hint_and_never_raises(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        calls = []
        msg = register_playwright_mcp(AGENT_CLIS["gemini"], run=lambda *a, **k: calls.append(a))
        assert AGENT_CLIS["gemini"].install_hint in msg
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


# ── the command line ─────────────────────────────────────────────────────────

class TestMain:
    def test_list_prints_every_id_with_tier_and_marks_the_default(self, capsys, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"gemini"}))
        assert agent_cli.main(["--list"]) == 0
        out = capsys.readouterr().out
        for c in AGENT_CLIS.values():
            assert re.search(rf"^{c.id}\s.*\b{c.tier}\b", out, re.M), c.id
        assert re.search(r"^gemini\s.*\byes\b.*\(default\)", out, re.M)
        assert re.search(r"^claude\s.*\bno\b", out, re.M)

    def test_check_exits_1_with_the_hint_when_not_installed(self, capsys, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        assert agent_cli.main(["--check"]) == 1
        assert AGENT_CLIS[DEFAULT_CLI].install_hint in capsys.readouterr().out

    def test_check_exits_0_when_installed(self, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"gemini"}))
        assert agent_cli.main(["--check"]) == 0

    def test_register_all_installed_always_exits_0_and_prints_hints_when_none(self, capsys, mocker):
        # setup.sh runs it under `set -euo pipefail`.
        mocker.patch("pipeline.agent_cli.shutil.which", return_value=None)
        assert agent_cli.main(["--register-mcp-all-installed"]) == 0
        out = capsys.readouterr().out
        assert list(AGENT_CLIS)[0] in out.split("\n", 2)[1]   # free first
        for c in AGENT_CLIS.values():
            assert c.install_hint in out

    def test_register_all_installed_registers_each_installed_cli(self, capsys, mocker):
        mocker.patch("pipeline.agent_cli.shutil.which", side_effect=_which({"gemini", "claude"}))
        fake = mocker.patch("pipeline.agent_cli.subprocess.run",
                            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert agent_cli.main(["--register-mcp-all-installed"]) == 0
        ran = [c.args[0][0] for c in fake.call_args_list]
        assert ran == ["/usr/bin/gemini", "/usr/bin/claude"]   # resolved, in order
        out = capsys.readouterr().out
        assert "Gemini CLI" in out and "Claude Code" in out

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
        monkeypatch.setenv("BATCH_CLI", "opencode")
        assert agent_cli.main(["--resolved"]) == 0
        assert capsys.readouterr().out.strip() == "opencode"

    def test_resolved_prints_the_default_when_unset(self, capsys, monkeypatch):
        # conftest clears BATCH_CLI; a .env in the checkout could re-add it, so
        # point the loader at nothing.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        assert agent_cli.main(["--resolved"]) == 0
        assert capsys.readouterr().out.strip() == DEFAULT_CLI

    def test_module_runs_as_a_script(self):
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.agent_cli", "--list"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "gemini" in proc.stdout


# ── Mirrors ──────────────────────────────────────────────────────────────────

class TestEnvExampleMirror:
    """`.env.example` restates the registry in a fixed, machine-readable shape:
    one `#   <id>    <tier>   …` line per CLI above `BATCH_CLI=<default>`."""

    @staticmethod
    def _block():
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
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


class TestAgentCliDataclass:
    def test_is_frozen(self):
        with pytest.raises(Exception):
            AGENT_CLIS["gemini"].tier = "paid"  # type: ignore[misc]

    def test_custom_entry_shapes(self):
        c = AgentCli(id="x", label="X", binary="x", tier="paid", tier_note="n",
                     install_hint="i", docs_url="https://x", prompt_flag="--go")
        assert c.interactive_argv("p") == ["x", "--go", "p"]
        assert not c.mcp_registration(home=Path("/h"), env={}).is_argv
