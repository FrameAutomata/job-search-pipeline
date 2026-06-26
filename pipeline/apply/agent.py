"""Agentic apply runner — the universal fallback engine.

Drives a `claude` + Playwright-MCP agent over the warm, Cloudflare-cleared CDP
session that `browser.launch_session` opens, then maps the agent's single
RESULT:* line to an ApplyResult. Used for jobs the deterministic engines can't
handle (Indeed, off-site employer ATS, arbitrary forms). The prompt comes from
prompt.build_prompt; this module just runs the agent and reads its verdict.

The subprocess spawn + MCP wiring are verified manually (like browser.py); the
MCP config, stream-json collapsing, and RESULT parsing are unit-tested. Ported
from ApplyPilot's launcher.py (run_job / _make_mcp_config / result handling),
trimmed to the Playwright MCP (no gmail) and this repo's ApplyResult vocabulary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Iterable

from pipeline.apply.result import (APPLIED, CAPTCHA, DEFER, EXPIRED, LOGIN_ISSUE,
                                   READY, ApplyResult, failed)

_VIEWPORT = "1280,900"
_DEFAULT_TIMEOUT = 300  # seconds; an agentic application is many LLM turns

# RESULT:FAILED:<reason> values that are really a terminal outcome of their own —
# promote them to that code so .permanent / reporting are right.
_PROMOTE = {"captcha": CAPTCHA, "expired": EXPIRED, "login_issue": LOGIN_ISSUE}
_REASON_JUNK = re.compile(r'[*`"\s]+$')  # trailing markdown the agent sometimes adds


def make_mcp_config(cdp_endpoint: str, *, viewport: str = _VIEWPORT,
                    imap_env: dict | None = None) -> dict:
    """The `--mcp-config` payload: a Playwright MCP attached to the SAME Chrome the
    deterministic engine drives, via its CDP endpoint — so the agent inherits the
    warm, logged-in, Cloudflare-cleared session rather than a fresh one. When
    `imap_env` is given (IMAP configured), also exposes the read_verification_code
    tool so the agent can clear email confirmations during account creation."""
    servers = {"playwright": {"command": "npx", "args": [
        "@playwright/mcp@latest",
        f"--cdp-endpoint={cdp_endpoint}",
        f"--viewport-size={viewport}",
    ]}}
    if imap_env:
        # Run our IMAP reader as a second MCP server on this same Python; creds go
        # to the subprocess via env (the agent only ever sees the returned token).
        servers["imap"] = {
            "command": sys.executable,
            "args": ["-m", "pipeline.apply.imap_mcp"],
            "env": dict(imap_env),
        }
    return {"mcpServers": servers}


def _collect_agent_text(lines: Iterable[str]) -> str:
    """Collapse claude's stream-json stdout to the agent's text output: assistant
    text blocks plus the final result message; non-JSON lines pass through (so a
    stray plain `RESULT:` line is still seen)."""
    parts: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue
        mtype = msg.get("type")
        if mtype == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
        elif mtype == "result":
            parts.append(msg.get("result", ""))
    return "\n".join(parts)


def parse_result(output: str, *, submitted: bool) -> ApplyResult:
    """Map the agent's text output to an ApplyResult via its RESULT:* line.
    `submitted` (only meaningful for APPLIED) records whether this was a live run
    that actually clicked Submit vs. a dry-run rehearsal.

    Reads the LAST RESULT line, not the first: the agent may name a code while
    reasoning ("I'll output RESULT:APPLIED if it submits") and then conclude
    differently, so only its final verdict counts."""
    verdict = next((ln for ln in reversed(output.splitlines()) if "RESULT:" in ln), None)
    if verdict is None:
        return failed("no_result_line")
    # READY is the review-mode hold point: filled but parked before submit, so
    # it's never a submission (the `code == APPLIED` guard keeps submitted False).
    for token, code in (("APPLIED", APPLIED), ("READY", READY), ("EXPIRED", EXPIRED),
                        ("CAPTCHA", CAPTCHA), ("LOGIN_ISSUE", LOGIN_ISSUE)):
        if f"RESULT:{token}" in verdict:
            return ApplyResult(code=code, submitted=submitted and code == APPLIED)
    if "RESULT:DEFER" in verdict:
        rest = verdict.split("RESULT:DEFER", 1)[1].lstrip(":").strip()
        tokens = rest.split()
        target = _REASON_JUNK.sub("", tokens[0] if tokens else "").lower()
        return ApplyResult(code=DEFER, deferred_to=target)
    if "RESULT:FAILED" in verdict:
        reason = verdict.split("RESULT:FAILED", 1)[1].lstrip(":").strip()
        reason = _REASON_JUNK.sub("", reason) or "unknown"
        if reason in _PROMOTE:
            return ApplyResult(code=_PROMOTE[reason])
        return failed(reason)
    return failed("no_result_line")


def run_agent(prompt: str, *, cdp_endpoint: str, model: str | None = None,
              dry_run: bool = False, timeout: int = _DEFAULT_TIMEOUT,
              claude_bin: str = "claude", imap_env: dict | None = None) -> ApplyResult:
    """Spawn `claude` + Playwright MCP on `cdp_endpoint`, feed `prompt` on stdin,
    parse the stream-json stdout, and return an ApplyResult. APPLIED counts as
    submitted only on a live (non-dry-run) run. `imap_env` (when set) adds the
    read_verification_code MCP server so the agent can clear email confirmations."""
    # mkstemp creates the file 0600 (owner-only). With imap_env this config holds
    # the plaintext IMAP password for the agent run's duration (~secs–mins) — an
    # accepted local-only window; anyone able to read it has already broken in.
    fd, mcp_path = tempfile.mkstemp(suffix=".json", prefix="apply-mcp-")
    os.close(fd)
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(make_mcp_config(cdp_endpoint, imap_env=imap_env), f)

    cmd = [claude_bin]
    if model:
        cmd += ["--model", model]
    cmd += ["-p", "--mcp-config", mcp_path, "--permission-mode", "bypassPermissions",
            "--no-session-persistence", "--output-format", "stream-json", "--verbose", "-"]

    # claude refuses to nest inside another Claude Code session; drop the markers
    # so the runner works even when launched from within Claude Code.
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", env=env)
        proc.stdin.write(prompt)
        proc.stdin.close()
        output = _collect_agent_text(proc.stdout)
        proc.wait(timeout=timeout)
    except FileNotFoundError:
        return failed("claude_not_found")
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        return failed("timeout")
    except Exception as e:
        # Any other subprocess failure (e.g. claude dies mid-stdin-write ->
        # BrokenPipeError) must become an ApplyResult, not escape into the apply
        # loop — the engine always returns a verdict.
        if proc:
            proc.kill()
        return failed(f"agent_error:{type(e).__name__}")
    finally:
        try:
            os.unlink(mcp_path)
        except OSError:
            pass

    return parse_result(output, submitted=not dry_run)
