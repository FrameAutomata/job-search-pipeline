"""Wiring tests for orchestrate.py's optional stages.

These don't exercise the stages themselves (each has its own test module) — they
pin the argparse-to-call wiring so the opt-in liveness re-check actually runs in
the pipeline (and therefore in the cloud workflow that invokes orchestrate),
and stays OFF by default so existing runs are unchanged.

Plus the stdio setup main() does before any of that, for the same reason: the
UI and the daily workflow both read the stage log while it is being written.
"""

import io
import sys

import pytest

import orchestrate
from pipeline import recheck


@pytest.fixture
def quiet_pipeline(monkeypatch, tmp_path):
    """Neutralise the real stages so main() reaches the optional blocks cheaply:
    a tmp config that exists, a tmp career-ops, no toasts, an empty bridge."""
    cfg = tmp_path / "search.yml"
    cfg.write_text("searches: []\n", encoding="utf-8")
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    monkeypatch.setenv("CAREER_OPS_PATH", str(co))
    monkeypatch.setattr(orchestrate.notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(orchestrate.bridge, "run", lambda *a, **k: [])
    return cfg, co


def _argv(cfg, *extra):
    # Skip every always-on stage; we only care about the optional tail.
    return ["orchestrate.py", "--config", str(cfg), "--skip-scrape", "--skip-filter",
            "--skip-screen", "--skip-bridge", "--skip-batch-prep", *extra]


class TestRecheckWiring:
    def test_flag_runs_recheck(self, quiet_pipeline, monkeypatch):
        cfg, co = quiet_pipeline
        calls = []
        monkeypatch.setattr(recheck, "run", lambda career_ops, **kw: calls.append((career_ops, kw)))
        monkeypatch.setattr(sys, "argv", _argv(cfg, "--recheck-liveness"))
        assert orchestrate.main() == 0
        assert len(calls) == 1
        career_ops, kw = calls[0]
        assert career_ops == co.resolve()
        assert kw.get("timeout") == 8        # default --recheck-timeout

    def test_recheck_timeout_forwarded(self, quiet_pipeline, monkeypatch):
        cfg, _ = quiet_pipeline
        calls = []
        monkeypatch.setattr(recheck, "run", lambda career_ops, **kw: calls.append(kw))
        monkeypatch.setattr(sys, "argv", _argv(cfg, "--recheck-liveness", "--recheck-timeout", "20"))
        orchestrate.main()
        assert calls and calls[0].get("timeout") == 20

    def test_off_by_default(self, quiet_pipeline, monkeypatch):
        cfg, _ = quiet_pipeline
        called = []
        monkeypatch.setattr(recheck, "run", lambda *a, **k: called.append(True))
        monkeypatch.setattr(sys, "argv", _argv(cfg))   # no --recheck-liveness
        assert orchestrate.main() == 0
        assert called == []

    def test_recheck_drain_routes_to_drain(self, quiet_pipeline, monkeypatch):
        """--recheck-drain loops the budgeted sweep until the backlog is covered;
        without it the re-check is a single sweep."""
        cfg, co = quiet_pipeline
        ran, drained = [], []
        monkeypatch.setattr(recheck, "run", lambda career_ops, **kw: ran.append(kw))
        monkeypatch.setattr(recheck, "drain", lambda career_ops, **kw: drained.append(kw))
        monkeypatch.setattr(sys, "argv", _argv(cfg, "--recheck-liveness", "--recheck-drain"))
        assert orchestrate.main() == 0
        assert len(drained) == 1 and not ran          # drained, not single-swept
        assert drained[0].get("timeout") == 8

    def test_plain_recheck_does_not_drain(self, quiet_pipeline, monkeypatch):
        cfg, _ = quiet_pipeline
        ran, drained = [], []
        monkeypatch.setattr(recheck, "run", lambda career_ops, **kw: ran.append(kw))
        monkeypatch.setattr(recheck, "drain", lambda career_ops, **kw: drained.append(kw))
        monkeypatch.setattr(sys, "argv", _argv(cfg, "--recheck-liveness"))   # no --recheck-drain
        orchestrate.main()
        assert len(ran) == 1 and not drained


class TestLineBufferStdio:
    """main() makes the stage log line-buffered before running anything.

    Redirected to a file or a pipe, stdout block-buffers at 8KB and a stage's
    progress lines sit unseen while it works. Both callers that redirect us set
    PYTHONUNBUFFERED today, so this is about the guarantee living in the program
    instead of in every caller remembering to compensate for it."""

    def test_a_redirected_stdout_becomes_line_buffered(self, tmp_path, monkeypatch):
        log = tmp_path / "local-run.log"
        # buffering=8192 is the shape a redirect to a file hands us: the text
        # layer is not line-buffered, so a print goes into the buffer and stays.
        with open(log, "w", buffering=8192, encoding="utf-8") as f:
            assert f.line_buffering is False
            monkeypatch.setattr(sys, "stdout", f)

            orchestrate._line_buffer_stdio()

            print("[scrape] 120 rows -> 118 after dedup")
            # Read through a separate handle — nothing here has flushed f, so
            # the line is only on disk if the newline did it.
            assert "after dedup" in log.read_text(encoding="utf-8")

    def test_a_stdout_that_cannot_reconfigure_is_not_fatal(self, monkeypatch):
        # pytest's own capture, and some embedding hosts, replace sys.stdout
        # with an object that has no reconfigure. Buffering is a nicety;
        # failing the run over it is not.
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())

        orchestrate._line_buffer_stdio()
