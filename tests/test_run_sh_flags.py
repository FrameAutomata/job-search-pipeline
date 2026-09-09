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

import os
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

# Appends, because run.sh --batch now invokes python FOUR times — orchestrate.py,
# `-m pipeline.agent_cli --resolved` to learn the agent CLI, `--resolved-model`
# for the model it starts with (when OLLAMA_MODEL is unset), then
# `-m pipeline.merge_additions` after the runner — and every assertion below is
# a membership test over the whole recording.
_RECORDER = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "{out}"
"""

# The python stub also answers `--resolved` and `--resolved-model` the way the
# registry would, since run.sh captures that stdout as the `--cli` / `--model`
# it forwards to batch-runner.sh.
STUB_CLI = "stubcli"
STUB_MODEL = "stub/model"
_PY_RECORDER = _RECORDER + """case " $* " in
  *" --resolved "*) echo "{cli}" ;;
  *" --resolved-model "*) echo "{model}" ;;
esac
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
    py.write_text(_PY_RECORDER.format(out=tmp_path / "orch_args.txt", cli=STUB_CLI, model=STUB_MODEL))
    py.chmod(0o755)

    runner = tmp_path / "career-ops" / "batch" / "batch-runner.sh"
    runner.write_text(_RECORDER.format(out=tmp_path / "batch_args.txt"))
    runner.chmod(0o755)

    (tmp_path / "orchestrate.py").write_text("")
    shutil.copy(REPO_ROOT / "run.sh", tmp_path / "run.sh")
    (tmp_path / "run.sh").chmod(0o755)
    return tmp_path


def _run(root, *args):
    # Without OLLAMA_MODEL: run.sh reads it, and a developer's own value would
    # short-circuit the model-resolution assertions below.
    env = {k: v for k, v in os.environ.items() if k != "OLLAMA_MODEL"}
    return subprocess.run(
        ["bash", str(root / "run.sh"), *args],
        capture_output=True, text=True, cwd=root, env=env,
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


class TestAgentCliResolution:
    """The `--cli` run.sh forwards comes from `python -m pipeline.agent_cli
    --resolved`, which reads .env — so a CLI chosen in the Setup wizard is
    honoured — and owns the default. run.sh carries no default of its own
    (tests/test_agent_cli.py::TestWrapperMirror pins the text)."""

    def test_cli_comes_from_the_registry_resolver(self, stub_root):
        proc = _run(stub_root, "--batch")
        assert proc.returncode == 0, proc.stderr
        orch = _recorded(stub_root, "orch_args.txt")
        assert "pipeline.agent_cli" in orch and "--resolved" in orch
        batch = _recorded(stub_root, "batch_args.txt")
        assert batch[batch.index("--cli") + 1] == STUB_CLI
        # The banner names the CLI (and the model, which the next test pins).
        assert f"({STUB_CLI} / " in proc.stdout

    def test_batch_cli_env_is_not_read_by_the_wrapper(self, stub_root):
        # The wrapper must not short-circuit the resolver with the raw env
        # value: the registry validates and lower-cases it, and warns on a typo.
        proc = subprocess.run(
            ["bash", str(stub_root / "run.sh"), "--batch"],
            capture_output=True, text=True, cwd=stub_root,
            env={**os.environ, "BATCH_CLI": "Something-Else"},
        )
        assert proc.returncode == 0, proc.stderr
        batch = _recorded(stub_root, "batch_args.txt")
        assert batch[batch.index("--cli") + 1] == STUB_CLI

    def test_model_comes_from_the_registry_resolver_after_the_cli(self, stub_root):
        # AGENT_MODEL / the registry default reach --batch as batch-runner's
        # --model; before this, only OLLAMA_MODEL did, so a gemini --batch ran
        # on the CLI's own ~20 req/day default model.
        proc = _run(stub_root, "--batch")
        assert proc.returncode == 0, proc.stderr
        assert "--resolved-model" in _recorded(stub_root, "orch_args.txt")
        batch = _recorded(stub_root, "batch_args.txt")
        assert batch[batch.index("--cli") + 1] == STUB_CLI
        assert batch.index("--model") > batch.index("--cli")
        assert batch[batch.index("--model") + 1] == STUB_MODEL
        assert f"({STUB_CLI} / {STUB_MODEL})" in proc.stdout

    def test_ollama_model_outranks_the_resolver(self, stub_root):
        # The older --batch-only name still wins when set, and the resolver is
        # not even asked.
        proc = subprocess.run(
            ["bash", str(stub_root / "run.sh"), "--batch"],
            capture_output=True, text=True, cwd=stub_root,
            env={**os.environ, "OLLAMA_MODEL": "local:7b"},
        )
        assert proc.returncode == 0, proc.stderr
        assert "--resolved-model" not in _recorded(stub_root, "orch_args.txt")
        batch = _recorded(stub_root, "batch_args.txt")
        assert batch[batch.index("--model") + 1] == "local:7b"

    def test_an_empty_model_resolution_passes_no_model_flag(self, stub_root):
        # "" means the CLI's own default — batch-runner must not see
        # `--model ""`.
        py = stub_root / ".venv" / "bin" / "python"
        py.write_text(_PY_RECORDER.format(out=stub_root / "orch_args.txt", cli=STUB_CLI, model=""))
        proc = _run(stub_root, "--batch")
        assert proc.returncode == 0, proc.stderr
        batch = _recorded(stub_root, "batch_args.txt")
        assert "--model" not in batch
        assert f"({STUB_CLI})" in proc.stdout

    def test_an_empty_resolution_is_an_error_not_a_blank_cli(self, stub_root):
        py = stub_root / ".venv" / "bin" / "python"
        py.write_text(_RECORDER.format(out=stub_root / "orch_args.txt"))
        proc = _run(stub_root, "--batch")
        assert proc.returncode == 1
        assert "could not resolve the agent CLI" in proc.stderr
        assert not (stub_root / "batch_args.txt").exists()
