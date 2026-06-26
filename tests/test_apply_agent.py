"""Contract tests for the agentic apply runner (pipeline/apply/agent.py).

The subprocess spawn + MCP wiring are integration (verified manually, like
browser.py); here we pin the pure, parseable surface: the MCP config points at
the CDP endpoint, the stream-json stdout is collapsed to the agent's text, and
the single RESULT:* line maps to the right ApplyResult — including FAILED:reason
promotion and the no-result fallback. run_agent is exercised with a fake Popen."""

import json

import pytest

from pipeline.apply import agent
from pipeline.apply.result import APPLIED, EXPIRED, CAPTCHA, LOGIN_ISSUE


class TestMcpConfig:
    def test_points_playwright_at_cdp_endpoint(self):
        cfg = agent.make_mcp_config("http://localhost:9222")
        pw = cfg["mcpServers"]["playwright"]
        assert any("--cdp-endpoint=http://localhost:9222" == a for a in pw["args"])

    def test_no_imap_env_is_playwright_only(self):
        cfg = agent.make_mcp_config("http://localhost:9222")
        assert set(cfg["mcpServers"]) == {"playwright"}

    def test_imap_env_adds_verification_server(self):
        cfg = agent.make_mcp_config("http://localhost:9222",
                                    imap_env={"APPLY_IMAP_HOST": "imap.gmail.com"})
        assert "imap" in cfg["mcpServers"]
        imap = cfg["mcpServers"]["imap"]
        assert "pipeline.apply.imap_mcp" in " ".join(imap["args"])   # runs our module
        assert imap["env"]["APPLY_IMAP_HOST"] == "imap.gmail.com"     # creds via env


class TestParseResult:
    def test_applied_submitted_flag_follows_caller(self):
        r = agent.parse_result("done.\nRESULT:APPLIED", submitted=True)
        assert r.code == APPLIED and r.submitted is True
        r2 = agent.parse_result("RESULT:APPLIED (dry run)", submitted=False)
        assert r2.code == APPLIED and r2.submitted is False

    @pytest.mark.parametrize("token,code", [
        ("RESULT:EXPIRED", EXPIRED),
        ("RESULT:CAPTCHA", CAPTCHA),
        ("RESULT:LOGIN_ISSUE", LOGIN_ISSUE),
    ])
    def test_terminal_codes(self, token, code):
        assert agent.parse_result(f"x\n{token}\ny", submitted=False).code == code

    def test_ready_is_a_held_not_submitted_outcome(self):
        # The review-mode "filled but parked at submit" signal. Must NOT be a
        # submission even when the run was live — it's the hold point.
        from pipeline.apply.result import READY
        r = agent.parse_result("filled all pages; at review step\nRESULT:READY",
                               submitted=True)
        assert r.code == READY and r.submitted is False

    def test_failed_with_reason(self):
        r = agent.parse_result("RESULT:FAILED:cloudflare_blocked", submitted=False)
        assert r.code == "failed" and r.reason == "cloudflare_blocked"

    def test_failed_reason_promoted_to_terminal_code(self):
        # A FAILED:captcha/expired/login_issue is really that terminal outcome.
        assert agent.parse_result("RESULT:FAILED:captcha", submitted=False).code == CAPTCHA
        assert agent.parse_result("RESULT:FAILED:expired", submitted=False).code == EXPIRED

    def test_trailing_markdown_stripped_from_reason(self):
        r = agent.parse_result("RESULT:FAILED:stuck**", submitted=False)
        assert r.reason == "stuck"

    def test_no_result_line_is_failure(self):
        r = agent.parse_result("the agent rambled but never concluded", submitted=False)
        assert r.code == "failed" and "no_result" in r.reason

    def test_uses_last_result_line_when_multiple(self):
        # The agent may name a code mid-reasoning then conclude differently; the
        # FINAL RESULT line is the verdict, not the first one mentioned.
        out = "I'll output RESULT:APPLIED if the form submits.\n...\nRESULT:FAILED:stuck"
        r = agent.parse_result(out, submitted=True)
        assert r.code == "failed" and r.reason == "stuck"


class TestCollectText:
    def test_collapses_stream_json_to_text(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "navigating"},
                {"type": "tool_use", "name": "mcp__playwright__browser_navigate",
                 "input": {"url": "https://x"}},
            ]}}),
            json.dumps({"type": "result", "result": "RESULT:APPLIED", "num_turns": 7}),
            "not json — keep as-is",
        ]
        text = agent._collect_agent_text(lines)
        assert "navigating" in text
        assert "RESULT:APPLIED" in text       # the result-message text is included
        assert "not json — keep as-is" in text  # non-JSON lines pass through


class TestRunAgent:
    def test_spawns_claude_feeds_prompt_and_parses(self, monkeypatch):
        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["stdin_written"] = []
                self.returncode = 0
                self.stdout = iter([
                    json.dumps({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": "filled the form"}]}}),
                    json.dumps({"type": "result", "result": "RESULT:APPLIED"}),
                ])

                class _Stdin:
                    def write(self_, s): captured["stdin_written"].append(s)
                    def close(self_): pass
                self.stdin = _Stdin()

            def wait(self, timeout=None): return 0

        monkeypatch.setattr(agent.subprocess, "Popen", FakePopen)
        r = agent.run_agent("PROMPT-BODY", cdp_endpoint="http://localhost:9222",
                            model="opus", dry_run=False)
        assert r.code == APPLIED and r.submitted is True
        assert captured["stdin_written"] == ["PROMPT-BODY"]
        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "--output-format" in cmd and "stream-json" in cmd
        assert "--permission-mode" in cmd and "bypassPermissions" in cmd
        assert "--mcp-config" in cmd

    def test_unexpected_subprocess_error_becomes_failed(self, monkeypatch):
        # If claude dies mid-stdin-write (BrokenPipe) or anything else goes wrong,
        # the engine must return an ApplyResult, not let the exception escape.
        class BoomPopen:
            def __init__(self, cmd, **kw):
                self.returncode = 0
                self.stdout = iter([])

                class _Stdin:
                    def write(self_, s): raise BrokenPipeError("claude died")
                    def close(self_): pass
                self.stdin = _Stdin()

            def wait(self, timeout=None): return 0

            def kill(self): pass

        monkeypatch.setattr(agent.subprocess, "Popen", BoomPopen)
        r = agent.run_agent("P", cdp_endpoint="http://localhost:9222")
        assert r.code == "failed" and "agent_error" in r.reason

    def test_dry_run_applied_is_not_submitted(self, monkeypatch):
        class FakePopen:
            def __init__(self, cmd, **kw):
                self.returncode = 0
                self.stdout = iter([json.dumps({"type": "result", "result": "RESULT:APPLIED dry run"})])
                class _S:
                    def write(self_, s): pass
                    def close(self_): pass
                self.stdin = _S()
            def wait(self, timeout=None): return 0
        monkeypatch.setattr(agent.subprocess, "Popen", FakePopen)
        r = agent.run_agent("P", cdp_endpoint="http://localhost:9222", dry_run=True)
        assert r.code == APPLIED and r.submitted is False
