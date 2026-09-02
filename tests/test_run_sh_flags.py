"""run.sh flag routing: batch-runner options must reach batch-runner.

run.sh sends anything it doesn't recognize to orchestrate.py, which calls
parse_args and exits on an unknown flag. That made every batch-runner.sh option
unreachable: `./run.sh --batch --parallel 4` handed --parallel to the
orchestrator and died before scraping. --parallel is what decides whether a
few-hundred-job queue finishes at all, so the routing is worth pinning.

The test runs the real run.sh against a stub root — a recording `python`, a
recording batch-runner.sh, and a profile.yml so the first-run wizard gate stays
shut — and asserts which program each flag landed in.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="run.sh is a bash script; no usable bash here",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Appends, because run.sh --batch now invokes python TWICE — orchestrate.py, then
# `-m pipeline.merge_additions` after the runner — and every assertion below is
# a membership test over the whole recording.
_RECORDER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "{out}"
"""


@pytest.fixture
def stub_root(tmp_path):
    """A fake repo root: real run.sh, recording python + batch-runner."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "career-ops" / "batch").mkdir(parents=True)
    (tmp_path / "career-ops" / "config").mkdir(parents=True)
    # Present so run.sh's "Profile not found" gate doesn't shell out to node.
    (tmp_path / "career-ops" / "config" / "profile.yml").write_text("x: 1\n")

    py = tmp_path / ".venv" / "bin" / "python"
    py.write_text(_RECORDER.format(out=tmp_path / "orch_args.txt"))
    py.chmod(0o755)

    runner = tmp_path / "career-ops" / "batch" / "batch-runner.sh"
    runner.write_text(_RECORDER.format(out=tmp_path / "batch_args.txt"))
    runner.chmod(0o755)

    (tmp_path / "orchestrate.py").write_text("")
    shutil.copy(REPO_ROOT / "run.sh", tmp_path / "run.sh")
    (tmp_path / "run.sh").chmod(0o755)
    return tmp_path


def _run(root, *args):
    return subprocess.run(
        ["bash", str(root / "run.sh"), *args],
        capture_output=True, text=True, cwd=root,
    )


def _recorded(root, name):
    f = root / name
    return f.read_text().split("\n") if f.exists() else []


class TestBatchFlagForwarding:
    def test_value_flags_reach_batch_runner_not_orchestrate(self, stub_root):
        proc = _run(stub_root, "--batch", "--parallel", "4", "--limit", "10")
        assert proc.returncode == 0, proc.stderr

        batch = _recorded(stub_root, "batch_args.txt")
        assert "--parallel" in batch and "4" in batch
        assert "--limit" in batch and "10" in batch

        # The bug: these used to land here and abort the run.
        orch = _recorded(stub_root, "orch_args.txt")
        assert "--parallel" not in orch
        assert "--limit" not in orch

    def test_boolean_flags_reach_batch_runner(self, stub_root):
        proc = _run(stub_root, "--batch", "--retry-failed", "--resume-paused")
        assert proc.returncode == 0, proc.stderr
        batch = _recorded(stub_root, "batch_args.txt")
        assert "--retry-failed" in batch
        assert "--resume-paused" in batch

    def test_orchestrate_flags_still_reach_orchestrate(self, stub_root):
        """Forwarding must not swallow the orchestrator's own flags."""
        proc = _run(stub_root, "--batch", "--skip-scrape", "--parallel", "2")
        assert proc.returncode == 0, proc.stderr
        assert "--skip-scrape" in _recorded(stub_root, "orch_args.txt")
        assert "--parallel" in _recorded(stub_root, "batch_args.txt")

    def test_batch_flag_without_batch_is_an_error(self, stub_root):
        """Without --batch these reach nothing, so refuse rather than run a
        full scrape and drop them silently."""
        proc = _run(stub_root, "--parallel", "4")
        assert proc.returncode == 2
        assert "--parallel" in proc.stderr
        assert not (stub_root / "batch_args.txt").exists()

    def test_existing_flags_unchanged(self, stub_root):
        proc = _run(stub_root, "--batch", "--skip-pdf", "--min-score", "3")
        assert proc.returncode == 0, proc.stderr
        batch = _recorded(stub_root, "batch_args.txt")
        assert "--skip-pdf" in batch
        assert "--min-score" in batch and "3" in batch
