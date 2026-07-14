"""OpenClaw browser client for the apply ladder (Phase 4b).

Implements the Phase 3 `Browser` protocol (snapshot/act) over the OpenClaw
browser CLI's chrome extension relay — the user's own logged-in browser. The
widget recipes are the ones proven live on Greenhouse (2026-07-13/14):

  TEXT       type <ref> <value>
  SELECT     click <ref> → type <ref> <option> → press Enter   (react-select)
  TYPEAHEAD  click <ref> → type <ref> <value> → wait for async options →
             click the option whose label MATCHES the value (never blind-first)
  UPLOAD     stage file into ~/.openclaw/media/inbound → upload media://inbound/
             <name> → click <attach ref>

All CLI calls go through an injected `runner(args) -> str` (subprocess in
production, scripted in tests) and carry the browser profile. A failed or
unresolvable action raises — the state machine converts that into escalation,
never a crash.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from pipeline.ats_fill import SELECT, TEXT, TYPEAHEAD, UPLOAD, FillAction, _option_matches
from pipeline.openclaw_browser import SnapshotIndex, parse_snapshot

_SUBMIT = re.compile(r"submit application", re.I)

OPENCLAW = str(Path.home() / "AppData" / "Roaming" / "npm" / "openclaw.cmd")
INBOUND = Path.home() / ".openclaw" / "media" / "inbound"


def default_runner(args: list[str], *, timeout: int = 90) -> str:
    """Run `openclaw browser <args>`; return stdout, raise on non-zero exit."""
    proc = subprocess.run([OPENCLAW, "browser", *args], capture_output=True, timeout=timeout)
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"openclaw browser {args[0]} failed ({proc.returncode}): {err.strip()[:200]}")
    return out


class OpenClawBrowser:
    """The Phase 3 Browser protocol over the OpenClaw chrome relay."""

    def __init__(
        self,
        profile: str = "chrome",
        runner: Callable[..., str] = default_runner,
        *,
        inbound_dir: Path = INBOUND,
        sleep: Callable[[float], None] = time.sleep,
        typeahead_wait: float = 2.0,
        typeahead_tries: int = 3,
        settle_wait: float = 1.0,
        settle_tries: int = 8,
    ):
        self.profile = profile
        self.runner = runner
        self.inbound_dir = inbound_dir
        self.sleep = sleep
        self.typeahead_wait = typeahead_wait
        self.typeahead_tries = typeahead_tries
        self.settle_wait = settle_wait
        self.settle_tries = settle_tries

    def _run(self, *args: str) -> str:
        return self.runner([*args, "--browser-profile", self.profile])

    def open(self, url: str) -> None:
        self._run("open", url)
        self._settle()

    def _settle(self) -> None:
        """Greenhouse re-mints refs while the form loads, so acting too early
        hits dead refs. Wait until the submit button's ref is stable across two
        reads (the form has stopped re-rendering) before anyone plans against
        it. Falls through after settle_tries so a formless page can't hang."""
        prev = object()
        for _ in range(self.settle_tries):
            self.sleep(self.settle_wait)
            submit = self.snapshot().find("button", _SUBMIT)
            ref = submit.ref if submit else None
            if ref is not None and ref == prev:
                return
            prev = ref

    def snapshot(self) -> SnapshotIndex:
        return parse_snapshot(self._run("snapshot", "--timeout", "45000"))

    def act(self, action: FillAction) -> None:
        if action.widget == TEXT:
            self._run("type", action.ref, action.value)
        elif action.widget == SELECT:
            # react-select commit: open, filter to the option, Enter selects it
            self._run("click", action.ref)
            self._run("type", action.ref, action.value)
            self._run("press", "Enter")
        elif action.widget == TYPEAHEAD:
            self._typeahead(action)
        elif action.widget == UPLOAD:
            self._upload(action)
        else:
            raise RuntimeError(f"unknown widget recipe: {action.widget!r}")

    def _typeahead(self, action: FillAction) -> None:
        """Type, wait out the async option fetch, click the option that MATCHES
        the answer — never blind-first (Dallas, Oregon is listed before Dallas,
        Texas on the real form)."""
        self._run("click", action.ref)
        self._run("type", action.ref, action.value)
        for _ in range(self.typeahead_tries):
            self.sleep(self.typeahead_wait)
            index = self.snapshot()
            options = [el for el in index.elements if el.role == "option" and el.ref]
            if options:
                for opt in options:
                    if opt.label and _option_matches(opt.label, action.value):
                        self._run("click", opt.ref)
                        return
                break  # options rendered but none match → not our value
        raise RuntimeError(
            f"typeahead: no option matching {action.value!r} for {action.label!r}")

    def _upload(self, action: FillAction) -> None:
        """Stage the file where the gateway allows uploads from, arm the file
        chooser, then click the form's Attach control (action.ref)."""
        src = Path(action.value)
        if not src.is_file():
            raise RuntimeError(f"upload: file not found: {src}")
        self.inbound_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, self.inbound_dir / src.name)
        self._run("upload", f"media://inbound/{src.name}")
        self._run("click", action.ref)
