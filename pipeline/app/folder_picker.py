"""Native OS folder picker for the local onboarding UI.

A browser can't hand a web page a folder's absolute path (security) — but this UI
runs on the user's own machine, so the server can pop a native folder dialog and
return the chosen path (the wizard's "Browse…" button). The dialog runs in a
SUBPROCESS so the Tk GUI owns its own main thread: Tk must run on the main thread
(hard rule on macOS), and a uvicorn request handler runs on a worker thread — a
subprocess sidesteps that entirely and can't wedge the server.
"""
import os
import subprocess
import sys

# The child prints the chosen path (and NOTHING else) to stdout; a cancelled
# dialog prints "". Kept as an inline -c script so there's no packaged helper file
# to ship. `title` is passed as a real argv element (no interpolation → no quoting
# surface); tests compile this string so a typo is caught before a Browse-click.
_PICKER = """\
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw()
try:
    root.attributes('-topmost', True)  # surface it above the browser
except tk.TclError:
    pass
title = sys.argv[1] if len(sys.argv) > 1 else 'Select a folder'
sys.stdout.write(filedialog.askdirectory(title=title) or '')
root.destroy()
"""


def pick_directory(title: str = "Select a folder", *, timeout: float = 300) -> str | None:
    """Open a native folder dialog on the machine running the server and return
    the chosen absolute path, ``""`` if the user cancelled, or ``None`` if no
    picker is available (headless box, tkinter missing, or the dialog timed out)."""
    try:
        # UTF-8 both ends — parent decode + the child's stdout via PYTHONIOENCODING —
        # so a non-cp1252 folder path (a non-Latin username, an emoji-named folder)
        # round-trips instead of crashing the child on Windows (cp1252 default). Same
        # pattern as local_run.py's subprocesses.
        proc = subprocess.run(
            [sys.executable, "-c", _PICKER, title],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:     # tkinter import failed / no $DISPLAY
        return None
    return proc.stdout.strip()
