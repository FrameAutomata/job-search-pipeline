"""Tests for pipeline/stdio.py — the one stage-log buffering rule.

"The log reaches the reader while the run is in flight" used to be spread across
three overlapping mechanisms, and a contributor adding a print had to know which
applied to the paths their code ran on. Now every entry point calls
line_buffer_stdout() first and nothing uses flush=True.

The two drift guards at the bottom are what make that a rule rather than a
convention, so they are written to be hard to slip past: both walk the AST
rather than matching substrings. The substring version of the flush guard was
blind to the multi-line

    print(
        ...,
        flush=True,
    )

form — which is exactly what two of the seventeen deletions looked like, so it
would have permitted the shape that actually occurred.
"""

import ast
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipeline.stdio import line_buffer_stdout

ROOT = Path(__file__).resolve().parent.parent


class TestLineBufferStdout:
    def test_a_redirected_stdout_becomes_line_buffered(self, tmp_path, monkeypatch):
        log = tmp_path / "local-run.log"
        # buffering=8192 is the shape a redirect to a file hands us: the text
        # layer is not line-buffered, so a print goes into the buffer and stays.
        #
        # Closed via try/finally rather than `with`, and only after
        # monkeypatch.undo(): monkeypatch restores sys.stdout at fixture
        # teardown, which runs AFTER the test body, so a `with` block would
        # leave sys.stdout pointing at a closed file in between. Anything that
        # printed in that window (a teardown hook, a warning, `pytest -s`)
        # would raise "I/O operation on closed file" against the wrong test.
        f = open(log, "w", buffering=8192, encoding="utf-8")
        try:
            assert f.line_buffering is False
            monkeypatch.setattr(sys, "stdout", f)

            line_buffer_stdout()

            print("[scrape] 120 rows -> 118 after dedup")
            # Read through a separate handle — nothing here has flushed f, so
            # the line is only on disk if the newline did it.
            assert "after dedup" in log.read_text(encoding="utf-8")
        finally:
            monkeypatch.undo()
            f.close()

    def test_a_stdout_with_no_reconfigure_is_not_fatal(self, monkeypatch):
        # pytest's own capture, and some embedding hosts, replace sys.stdout
        # with an object that has no reconfigure at all.
        monkeypatch.setattr(sys, "stdout", io.StringIO())

        line_buffer_stdout()

        # Asserting the call had no effect on a stream it cannot configure —
        # without this the test passes just as well on an empty function body.
        assert sys.stdout.getvalue() == ""

    def test_a_broken_stdout_is_not_fatal(self, tmp_path, monkeypatch):
        # The case a real redirect can produce: reconfigure() flushes before it
        # reconfigures, so a stream whose underlying buffer is gone raises from
        # inside the call. An entry point has not parsed argv yet, so a
        # traceback here would report buffering instead of the actual work.
        f = open(tmp_path / "gone.log", "w", buffering=8192, encoding="utf-8")
        f.close()
        monkeypatch.setattr(sys, "stderr", f)
        monkeypatch.setattr(sys, "stdout", f)

        line_buffer_stdout()

    def test_stderr_is_left_alone(self, tmp_path, monkeypatch):
        # CPython line-buffers stderr by default since 3.9 even off a tty, so
        # reconfiguring it would be a no-op — and a no-op is still a way to
        # break a stream we never had to touch. Asserted by observing the
        # stream's buffering, not its identity: a `sys.stderr.reconfigure(...)`
        # preserves identity, so `is` would pass against the thing it forbids.
        f = open(tmp_path / "err.log", "w", buffering=8192, encoding="utf-8")
        try:
            monkeypatch.setattr(sys, "stderr", f)
            line_buffer_stdout()
            assert f.line_buffering is False
        finally:
            monkeypatch.undo()
            f.close()


def _main_guard_body(tree: ast.Module) -> list | None:
    """The statements under a module-level `if __name__ == "__main__":`, or None.

    Matched structurally rather than by text so quote style, spacing and a
    reversed comparison can't hide an entry point from the guard below.
    """
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        names = {
            n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)
        } | {
            c.value for c in ast.walk(node.test)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
        }
        if "__name__" in names and "__main__" in names:
            return node.body
    return None


def _entry_points() -> list[Path]:
    """Every module under pipeline/ runnable with `python -m`.

    rglob, not glob: pipeline/app/ has no entry point today, and a guard that
    silently stops looking is how the previous mess accumulated.
    """
    found = []
    for path in sorted((ROOT / "pipeline").rglob("*.py")):
        if _main_guard_body(ast.parse(path.read_text(encoding="utf-8"))) is not None:
            found.append(path)
    return found


class TestEveryEntryPointBuffers:
    """The rule is only one rule if nothing opts out of it."""

    def test_every_main_block_calls_it_first(self):
        entries = _entry_points()
        # A collection bug that returned [] would make this class pass forever.
        assert len(entries) >= 10, f"only found {len(entries)} entry points"

        offenders = []
        for path in entries:
            body = _main_guard_body(ast.parse(path.read_text(encoding="utf-8")))
            first = body[0]
            called_first = (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == "line_buffer_stdout"
            )
            if not called_first:
                offenders.append(str(path.relative_to(ROOT)))
        # First, not merely present: a stage that calls it after thirty seconds
        # of setup passes a "was it mentioned" check while failing the rule.
        assert not offenders, (
            f"{offenders}: line_buffer_stdout() must be the first statement of the "
            "__main__ block, or `python -m pipeline.<stage> > log` is block-buffered."
        )

    def test_orchestrate_calls_it_before_parsing_argv(self):
        tree = ast.parse((ROOT / "orchestrate.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        first = main.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
        assert first.value.func.id == "line_buffer_stdout"


class TestNoResidualFlushCargo:
    """The 17 `flush=True` calls existed only to compensate for the absence of
    the reconfigure, on paths they did not actually cover. They are gone; this
    stops them being copied forward into the next print."""

    def test_no_call_passes_flush(self):
        offenders = []
        for path in sorted(list((ROOT / "pipeline").rglob("*.py")) + [ROOT / "orchestrate.py"]):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and any(
                    kw.arg == "flush" for kw in node.keywords
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        # AST, not substrings: it sees every spelling (`flush = True`, the
        # multi-line keyword form) and never sees prose, so stdio.py's docstring
        # naming the retired idiom needs no exclusion.
        assert not offenders, (
            f"{offenders} reintroduce a flush= argument — pipeline.stdio is the rule now."
        )


class TestEndToEnd:
    def test_a_redirected_child_streams_before_it_exits(self, tmp_path):
        """The primitive under a real redirect, in a real process.

        Not an entry point: this proves line_buffer_stdout() itself works when
        stdout is a file and PYTHONUNBUFFERED is absent. That the 11 entry
        points call it is what TestEveryEntryPointBuffers asserts.

        The child blocks on stdin rather than sleeping a fixed interval, so the
        test costs what it needs and no more.
        """
        script = tmp_path / "probe.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pipeline.stdio import line_buffer_stdout\n"
            "line_buffer_stdout()\n"
            "print('[scrape] searching')\n"
            "sys.stdin.readline()\n",
            encoding="utf-8",
        )
        log = tmp_path / "out.log"
        # Inherit the environment and remove only the thing under test — the
        # rule exists precisely because the environment should not have to
        # compensate, and a hand-built env bakes in a platform assumption.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}

        with open(log, "w", encoding="utf-8") as f:
            proc = subprocess.Popen(
                [sys.executable, str(script)], stdout=f, stdin=subprocess.PIPE, env=env
            )
            try:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if "searching" in log.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.01)
                # Read while the child is still blocked: without line buffering
                # the line sits in an 8KB buffer until it exits.
                assert "searching" in log.read_text(encoding="utf-8")
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)
