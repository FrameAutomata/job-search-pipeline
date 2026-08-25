"""Stage-log buffering, in one place.

Redirected to a file or a pipe, Python block-buffers stdout at 8KB — so a
stage's progress lines sit unseen while it works, and the run looks hung on
exactly the slow steps a reader is watching.

That used to be spread across three overlapping mechanisms: PYTHONUNBUFFERED at
the two callers that redirect us, a reconfigure in orchestrate.main(), and
`flush=True` on 17 of ~83 prints across pipeline/. The last one was the problem
— distributed unevenly (scrape 9/9, screen 3/7, filter and bridge 0), so it did
not cover the paths it existed for, and someone adding a print had to work out
which of the three applied to them.

The rule now is: **every entry point calls line_buffer_stdout() first** —
orchestrate.main() and every `__main__` block under pipeline/ — and no print
anywhere needs `flush=True`. tests/test_stdio.py guards both halves, since a
convention with no enforcement is how the previous mess accumulated.

That is one rule for the output *we* write, not one mechanism overall.
PYTHONUNBUFFERED stays at pipeline/app/local_run.py and in daily-pipeline.yml,
and it is not redundant: it is the only thing covering stdout we don't route
through our own `print` — jobspy and other libraries logging directly, and any
grandchild process the run spawns (batch-runner.sh, the resume tooling) that
inherits the environment. A reconfigure of our own stream structurally cannot
reach either. Deleting it would lose that quietly. (The redundant `python -u` in
the workflow, which duplicated the env var exactly and covered nothing more, is
gone.)

Two deliberate exclusions:

- **Not pipeline/__init__.py**, which looks like the tidier home and would be
  genuinely better on the merits — zero call sites, no drift guard needed, and
  automatic coverage for `python -m pipeline.<anything>` including modules not
  yet written. The trade is that it fires on the UI server's `import
  pipeline.sites` and would reconfigure uvicorn's stdout as a side effect of an
  import. The concrete harm there is small; the reason it still loses is that an
  import mutating interpreter-global state is a class of bug you cannot debug
  from the call site. So: 12 conventional call sites plus two guards, bought at
  the price of not doing that. (If the stage modules and the UI ever stop
  sharing one package, this becomes free and should be revisited.)
The UI process is covered explicitly rather than by the rule above: uvicorn
imports pipeline.app.server instead of running a `__main__` block, and unlike
local_run.py's child it inherits no PYTHONUNBUFFERED — while pipeline code does
print from it (recheck.drain on a background thread, batch_evaluate's retry
notices from Add-Job). So server.py calls this at import. That is an entry point
calling it, not a package doing it as a side effect, which is the distinction
the __init__.py note below turns on.

One deliberate exception, and one deliberate exclusion:

- **pipeline/batch_evaluate.py's interrupt summary** still pushes explicitly,
  because the line after it is `os._exit(130)` — which skips stdio flushing and
  atexit entirely. line_buffer_stdout() swallows its own failure by design, so
  if the reconfigure didn't take, a buffered summary would simply be discarded.
  It uses `sys.stdout.flush()` rather than `flush=True`, so it reads as the
  bypass it is rather than as the cargo this change removed.
- **Not pipeline/app/server.py's handoff log**, which opens its sink with
  `buffering=1` under a `redirect_stdout`. That is the same guarantee reached
  the only way it can be there — reconfiguring `sys.stdout` cannot help when the
  target is an in-process redirect — so it is an exception on purpose, not one
  this module missed.

A dependency-free leaf (stdlib only) on the same terms as pipeline.sites.
"""

import sys


def line_buffer_stdout() -> None:
    """Flush the stage log on every newline, however the program was launched.

    Call it as the first statement of an entry point — orchestrate.main() and
    every `if __name__ == "__main__":` block under pipeline/. Making it true
    here rather than at each print means the guarantee belongs to the program
    instead of to every caller remembering, and a print added to a stage
    tomorrow is correct without anyone thinking about buffering.

    stdout only: CPython has line-buffered stderr by default since 3.9, even
    when it isn't a tty, so reconfiguring it would be a no-op.

    Never fatal. Buffering is a nicety, and an entry point calling this has
    typically not parsed argv yet, so a traceback here would report buffering
    instead of the actual work. sys.stdout is not always a TextIOWrapper
    (pytest's capture and some embedding hosts replace it), a proxy's
    reconfigure may reject the keyword, and reconfigure() flushes before it
    reconfigures — so a stream whose underlying buffer is gone raises from
    inside the call.
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
