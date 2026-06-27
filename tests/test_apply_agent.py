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

    def test_needs_human_parsed(self):
        from pipeline.apply.result import NEEDS_HUMAN
        r = agent.parse_result("can't clear the captcha\nRESULT:NEEDS_HUMAN", submitted=False)
        assert r.code == NEEDS_HUMAN

    def test_defer_carries_target_engine(self):
        from pipeline.apply.result import DEFER
        r = agent.parse_result("this is Indeed SmartApply\nRESULT:DEFER:indeed", submitted=False)
        assert r.code == DEFER and r.deferred_to == "indeed"
        r2 = agent.parse_result("RESULT:DEFER:linkedin", submitted=True)
        assert r2.code == DEFER and r2.deferred_to == "linkedin"

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
                self.returncode = 0

            def communicate(self, input=None, timeout=None):
                captured["input"] = input
                captured["timeout"] = timeout
                return ("\n".join([
                    json.dumps({"type": "assistant", "message": {"content": [
                        {"type": "text", "text": "filled the form"}]}}),
                    json.dumps({"type": "result", "result": "RESULT:APPLIED"}),
                ]), "")

        monkeypatch.setattr(agent.subprocess, "Popen", FakePopen)
        r = agent.run_agent("PROMPT-BODY", cdp_endpoint="http://localhost:9222",
                            model="opus", dry_run=False)
        assert r.code == APPLIED and r.submitted is True
        assert captured["input"] == "PROMPT-BODY"        # prompt fed via communicate
        assert captured["timeout"] is not None           # the read is deadline-bounded
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

            def communicate(self, input=None, timeout=None):
                raise BrokenPipeError("claude died")

            def kill(self): pass

        monkeypatch.setattr(agent.subprocess, "Popen", BoomPopen)
        r = agent.run_agent("P", cdp_endpoint="http://localhost:9222")
        assert r.code == "failed" and "agent_error" in r.reason

    def test_dry_run_applied_is_not_submitted(self, monkeypatch):
        class FakePopen:
            def __init__(self, cmd, **kw):
                self.returncode = 0
            def communicate(self, input=None, timeout=None):
                return (json.dumps({"type": "result", "result": "RESULT:APPLIED dry run"}), "")
            def kill(self): pass
        monkeypatch.setattr(agent.subprocess, "Popen", FakePopen)
        r = agent.run_agent("P", cdp_endpoint="http://localhost:9222", dry_run=True)
        assert r.code == APPLIED and r.submitted is False

    def test_transcript_logged_only_when_env_set(self, tmp_path, monkeypatch):
        # The opt-in APPLY_AGENT_LOG dump is what made the iCIMS hCaptcha diagnosis
        # possible — verify it writes the prompt + transcript when set, and is a
        # silent no-op (no file, no raise) when unset.
        log = tmp_path / "agent.log"
        monkeypatch.delenv("APPLY_AGENT_LOG", raising=False)
        agent._maybe_log_transcript("P", "T")            # no env -> no-op
        assert not log.exists()
        monkeypatch.setenv("APPLY_AGENT_LOG", str(log))
        agent._maybe_log_transcript("THE-PROMPT", "THE-TRANSCRIPT")
        text = log.read_text(encoding="utf-8")
        assert "THE-PROMPT" in text and "THE-TRANSCRIPT" in text

    def test_runaway_agent_is_killed_at_timeout(self, monkeypatch):
        # A wedged agent (e.g. looping browser_click on the disabled button at an
        # iCIMS account wall) must NOT hang the run forever: the timeout has to
        # govern the blocking read itself, so run_agent kills the process and
        # returns failed("timeout"). Regression for the live hang on the iCIMS role.
        killed = {"n": 0}

        class HangPopen:
            def __init__(self, cmd, **kw):
                self.returncode = None
                self.captured_timeout = None

            def communicate(self, input=None, timeout=None):
                self.captured_timeout = timeout
                if timeout is not None:               # the real read: never returns
                    raise agent.subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
                return ("", "")                        # the post-kill reap

            def kill(self):
                killed["n"] += 1

        monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: HangPopen(cmd, **kw))
        r = agent.run_agent("P", cdp_endpoint="http://localhost:9222", timeout=2)
        assert r.code == "failed" and r.reason == "timeout"
        assert killed["n"] >= 1                         # the wedged subprocess was actually killed
