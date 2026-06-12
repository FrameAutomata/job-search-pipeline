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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from pipeline._batch_common import (
    env_float,
    pid_alive as _pid_alive,
    process_lock_active,
    read_process_lock,
    write_process_lock,
)

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = ROOT / ".ui-cache" / "local-run.log"
# Records the orchestrate child's pid (via the shared _batch_common lock format:
# "pid timestamp") so single-flight survives a server restart: the orphaned run
# keeps going (its stdout is a file), and a fresh server — whose in-memory
# _state is empty — would otherwise report not-running and let the user launch a
# SECOND concurrent full pipeline that corrupts shared state. The lock lets
# is_running()/start() detect and refuse (or, on cancel, kill) the orphan, and
# the timestamp makes it self-heal: a recycled-pid lock that has gone un-touched
# past LOCAL_RUN_LOCK_MAX_AGE is reclaimed instead of wedging forever.
PID_PATH = ROOT / ".ui-cache" / "local-run.pid"


def _lock_max_age() -> float:
    """Generous (LOCAL_RUN_LOCK_MAX_AGE env, default 6h). After a server restart
    the orphan child can't heartbeat, so its timestamp freezes; the cap must
    exceed the longest expected pipeline run so a genuine orphan isn't reclaimed
    out from under itself — while still bounding a recycled-pid lockout to 6h."""
    return env_float("LOCAL_RUN_LOCK_MAX_AGE", 6 * 3600)

# Stage markers orchestrate's stages print, in pipeline order — used to report
# coarse progress without any orchestrate-side changes.
STAGES = ["scrape", "filter", "screen", "bridge", "batch-prep", "batch-eval"]
_STAGE_RE = re.compile(r"^\[(%s)\]" % "|".join(re.escape(s) for s in STAGES), re.MULTILINE)

# The pass-selection vocabulary, defined once so the request model, the
# endpoint validation, and _build_cmd can't drift apart.
VALID_PASSES = ("all", "easy-only", "no-easy")

_lock = threading.Lock()
_state: dict = {}   # proc, started_at, options, cmd, stages_seen, log_offset, log_carry


def _read_env_snapshot(env_path: Path | None = None) -> dict:
    """The .env values present when the server started. _child_env diffs against
    this to decide what to strip (see there)."""
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(env_path or (ROOT / ".env")).items()
                if v is not None}
    except Exception:
        return {}


# Captured once at import (i.e. server startup). The server's own
# load_dotenv(override=False) merged these into os.environ already.
_ENV_SNAPSHOT = _read_env_snapshot()


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


def _child_env(snapshot: dict | None = None) -> dict:
    """The subprocess environment.

    orchestrate.py runs its own load_dotenv(override=False), so to make a
    UI-triggered run pick up CURRENT .env edits we hand it an environment where
    the server's STARTUP .env values have been STRIPPED — letting the child
    reload them fresh. Stripping (rather than overlaying a fresh read) is what
    makes the two earlier bugs go away:

      * keys the user REMOVED from .env vanish in the child too (an overlay
        could only ever ADD a fresh value, never unset a stale inherited one);
      * a shell export still beats .env, matching every other entry point —
        we strip a key only when its inherited value still equals what .env set
        at startup; a shell-overridden value differs, so it's left intact and
        the child's own load_dotenv(override=False) keeps it.
    """
    snap = _ENV_SNAPSHOT if snapshot is None else snapshot
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    for key, started_value in (snap or {}).items():
        if env.get(key) == started_value:
            env.pop(key, None)
    return env


def _clear_pid_file() -> None:
    # The holder recorded in the file is the orchestrate CHILD, not us, so we
    # unlink directly rather than via release_process_lock (which guards on our
    # own pid).
    try:
        PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def is_running() -> bool:
    """True while a run is in flight — whether we launched it (live Popen) or
    it's an orphan from a previous server lifetime (a live, non-stale child pid
    recorded in the lock file)."""
    proc = _state.get("proc")
    if proc is not None:
        return proc.poll() is None
    return process_lock_active(PID_PATH, max_age=_lock_max_age())


def start(options: dict | None = None) -> dict:
    """Start a local pipeline run. Raises RuntimeError when one is already
    running (single-flight, orphan-aware). Returns the status dict."""
    options = options or {}
    with _lock:
        if is_running():
            raise RuntimeError("a local pipeline run is already in progress")
        cmd = _build_cmd(options)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(LOG_PATH, "w", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT,
                env=_child_env(),
            )
        except Exception:
            # Don't leak the just-opened 'w' handle — on Windows it would hold a
            # sharing lock that breaks the next start()'s open(..., 'w').
            log_file.close()
            raise
        # The child got its own inherited dup of the log fd; close the parent's
        # copy so we don't hold a second writer and status() never has to manage
        # (or race on) an open handle.
        log_file.close()
        _state.clear()
        _state.update(proc=proc, started_at=time.time(), options=options, cmd=cmd,
                      stages_seen=[], log_offset=0, log_carry="")
        write_process_lock(PID_PATH, proc.pid)   # record the child as the holder
    return status()


def cancel() -> dict:
    """Terminate the running pipeline. Handles both a run we own (live Popen)
    and an orphan from a prior server lifetime (kill by pid). No-op when nothing
    is running."""
    with _lock:
        proc = _state.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            orphan, _ = read_process_lock(PID_PATH)
            if orphan and _pid_alive(orphan):
                try:
                    os.kill(orphan, signal.SIGTERM)
                except OSError:
                    pass
        _clear_pid_file()
    return status()


def _scan_log_for_stages() -> list:
    """Incrementally fold any new [stage] markers into _state['stages_seen'],
    reading only the bytes appended since the last poll (a full re-read every
    2.5s poll is wasteful on a multi-MB scrape log). A trailing partial line is
    carried to the next poll so a marker split across reads still matches."""
    seen = _state.setdefault("stages_seen", [])
    offset = _state.get("log_offset", 0)
    carry = _state.get("log_carry", "")
    try:
        size = LOG_PATH.stat().st_size
        if size < offset:        # log truncated (a new run reopened it 'w')
            offset, carry, seen = 0, "", []
            _state["stages_seen"] = seen
        with open(LOG_PATH, "rb") as f:
            f.seek(offset)
            raw = f.read()
            _state["log_offset"] = f.tell()
    except OSError:
        return seen
    text = carry + raw.decode("utf-8", errors="replace")
    nl = text.rfind("\n")
    if nl == -1:                 # no complete line yet — keep buffering
        _state["log_carry"] = text
        return seen
    _state["log_carry"] = text[nl + 1:]
    for marker in _STAGE_RE.findall(text[:nl + 1]):
        if marker not in seen:
            seen.append(marker)
    return seen


def _read_tail(tail_lines: int, max_bytes: int = 16384) -> str:
    """The last `tail_lines` lines, reading at most `max_bytes` from the end of
    the log rather than the whole file."""
    try:
        size = LOG_PATH.stat().st_size
        with open(LOG_PATH, "rb") as f:
            f.seek(max(0, size - max_bytes))
            raw = f.read()
    except OSError:
        return ""
    return "\n".join(raw.decode("utf-8", errors="replace").splitlines()[-tail_lines:])


def _idle_status() -> dict:
    """Nothing is running and nothing ran this session — don't parse a leftover
    log into a phantom finished run."""
    return {
        "running": False,
        "started_at": _state.get("started_at"),
        "cmd": _state.get("cmd"),
        "stage": None,
        "stages": STAGES,
        "stages_seen": [],
        "exit_code": None,
        "ok": None,
        "log_tail": "",
    }


def status(tail_lines: int = 30) -> dict:
    """Current run state: stage progress parsed from the log's [stage] markers,
    a log tail for display, and the exit code once finished. Takes _lock so a
    poll can't interleave with start()'s _state reset."""
    with _lock:
        proc = _state.get("proc")
        if proc is not None:
            code = proc.poll()
            running = code is None
            exit_code = None if running else code
            if running:
                write_process_lock(PID_PATH, proc.pid)   # heartbeat while we own it
            else:
                _clear_pid_file()
        else:
            # No handle this lifetime: an orphan (a live, non-stale child pid)
            # reads as running but its exit code is unknowable. Otherwise nothing
            # is running — and we must NOT scan the leftover log, or a brand-new
            # server would report a previous lifetime's run as a phantom finished
            # one (it never launched it).
            running = process_lock_active(PID_PATH, max_age=_lock_max_age())
            exit_code = None
            if not running:
                _clear_pid_file()
                return _idle_status()
        seen = _scan_log_for_stages()
        return {
            "running": running,
            "started_at": _state.get("started_at"),
            "cmd": _state.get("cmd"),
            "stage": seen[-1] if seen else None,
            "stages": STAGES,
            "stages_seen": sorted(set(seen), key=STAGES.index),
            "exit_code": exit_code,
            "ok": (exit_code == 0) if exit_code is not None else None,
            "log_tail": _read_tail(tail_lines),
        }
