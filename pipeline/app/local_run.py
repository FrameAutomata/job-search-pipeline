"""Run the pipeline locally from the UI.

Spawns `orchestrate.py` as a subprocess (same venv, repo root cwd) with output
captured to a log file, and exposes start/status/cancel for the UI to drive.
One run at a time — the pipeline mutates shared state (scan history, tracker,
batch files), so concurrent runs would corrupt it.

This is the local counterpart of the cloud "Run now" (workflow dispatch): the
cloud run needs a private fork + secrets, while this needs nothing but the
local setup — and it's the prerequisite for the apply review flow, which is
local-only (it drives a real logged-in browser).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = ROOT / ".ui-cache" / "local-run.log"

# Stage markers orchestrate's stages print, in pipeline order — used to report
# coarse progress without any orchestrate-side changes.
STAGES = ["scrape", "filter", "screen", "bridge", "batch-prep", "batch-eval"]
_STAGE_RE = re.compile(r"^\[(%s)\]" % "|".join(re.escape(s) for s in STAGES), re.MULTILINE)

_lock = threading.Lock()
_state: dict = {}   # proc, log_file (open handle), started_at, options, cmd


def _build_cmd(options: dict) -> list[str]:
    """Map UI options onto orchestrate flags. Deliberately small: pass
    selection + whether to evaluate. Everything else stays at orchestrate's
    defaults (the same chain the cloud workflows run)."""
    cmd = [sys.executable, str(ROOT / "orchestrate.py")]
    passes = (options.get("passes") or "all").strip().lower()
    if passes == "easy-only":
        cmd.append("--easy-apply-only")
    elif passes == "no-easy":
        cmd.append("--no-easy-apply")
    if options.get("evaluate", True):
        cmd.append("--evaluate-batch")
    return cmd


def is_running() -> bool:
    proc = _state.get("proc")
    return proc is not None and proc.poll() is None


def start(options: dict | None = None) -> dict:
    """Start a local pipeline run. Raises RuntimeError when one is already
    running (single-flight). Returns the status dict."""
    options = options or {}
    with _lock:
        if is_running():
            raise RuntimeError("a local pipeline run is already in progress")
        cmd = _build_cmd(options)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(LOG_PATH, "w", encoding="utf-8", errors="replace")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT, env=env,
        )
        _state.clear()
        _state.update(proc=proc, log_file=log_file, started_at=time.time(),
                      options=options, cmd=cmd)
    return status()


def cancel() -> dict:
    """Terminate the running pipeline (everything runs in this one process —
    stages are in-process and evaluation uses threads — so terminate() is
    enough). No-op when nothing is running."""
    with _lock:
        proc = _state.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        _close_log()
    return status()


def _close_log() -> None:
    f = _state.get("log_file")
    if f is not None and not f.closed:
        try:
            f.close()
        except OSError:
            pass


def _log_text() -> str:
    try:
        return LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def status(tail_lines: int = 30) -> dict:
    """Current run state: stage progress parsed from the log's [stage] markers,
    a log tail for display, and the exit code once finished."""
    proc = _state.get("proc")
    running = proc is not None and proc.poll() is None
    if not running:
        _close_log()
    log = _log_text()
    seen = _STAGE_RE.findall(log)
    stage = seen[-1] if seen else None
    tail = "\n".join(log.splitlines()[-tail_lines:])
    exit_code = None if proc is None or running else proc.poll()
    return {
        "running": running,
        "started_at": _state.get("started_at"),
        "cmd": _state.get("cmd"),
        "stage": stage,
        "stages": STAGES,
        "stages_seen": sorted(set(seen), key=STAGES.index),
        "exit_code": exit_code,
        "ok": (exit_code == 0) if exit_code is not None else None,
        "log_tail": tail,
    }
