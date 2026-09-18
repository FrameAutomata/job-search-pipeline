"""Contract test against the installed career-ops `batch/batch-runner.sh` parser.

`run.sh --batch` ends by calling the real runner with flags of its own choosing:
`--cli` and `--model` from the agent-CLI registry, `--skip-pdf`/`--min-score`,
and the batch options (`--parallel` …) it forwards verbatim.
`tests/test_run_sh_flags.py` pins that routing against a STUB runner that
records whatever it is handed, so it stays green whatever the real runner
accepts. The real runner's answer to a flag it does not know is
`Unknown option: --cli`, exit 1, which `set -e` turns into run.sh's own exit
after a full scrape has already been spent. Preventing exactly that was one of
the two reasons the maintainer's fork existed while `--cli` lived only there.
Upstream merged it (career-ops#738), the pipeline now clones upstream `main`, and
this test is what notices if a `git pull` there drops or renames a flag the
wrapper sends.

The probe is the runner's own parser, with `--help` as a sentinel:
`bash batch-runner.sh <flag> [value] --help`. Parsing is sequential, so an
unknown flag reaches `*) … exit 1` before `--help` is read, while a known one is
consumed and `--help` exits 0. Nothing but assignments and function definitions
comes before the parse loop, so the probe has no side effects in a bare checkout.
The alternatives each test something other than the parser:

- grepping the `case` block is a text-shape assumption. It cannot tell a label
  the loop matches from one in another `case` or in a clause that now exits,
  and a harmless relabel breaks it.
- `--help | grep` reads the usage heredoc, a second copy of the flag list that
  can drift from the parser in either direction.
- `--dry-run` runs past parsing into `check_prerequisites` and `init_state`.
  Those need a queued `batch-input.tsv` and the CLI binary on PATH, and they
  write `batch-state.tsv` and directories into the checkout.

It is a parser contract and nothing more. Whether `--model` then reaches the CLI
a run launches is upstream's behaviour, and nothing here pins it.

The flag set is not restated by hand. It is DERIVED from run.sh's text and must
equal `EXPECTED`, so adding a flag to the wrapper is a deliberate edit here too,
and `EXPECTED` is the set the probe drives. The probe tests are local-only on
`test_merge_tracker_contract.py`'s terms (career-ops is cloned by setup, which
CI does not run). So the skip sits on the probe class, not the module: the
derivation is pure text and runs everywhere, which makes a flag added to run.sh
fail CI straight away rather than on someone's next local run.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.tracker_layout import career_ops_dir

ROOT = Path(__file__).resolve().parent.parent
RUNNER = career_ops_dir() / "batch" / "batch-runner.sh"

# {flag: takes_value} for every flag run.sh can hand batch-runner.sh. It is both
# the literal the derivation below must reproduce AND the set the probe drives,
# so a flag reaches the real parser only by being written down here.
EXPECTED = {
    "--cli": True, "--model": True, "--min-score": True, "--parallel": True,
    "--limit": True, "--start-from": True, "--max-retries": True,
    "--rate-limit-sleep": True,
    "--skip-pdf": False, "--retry-failed": False, "--resume-paused": False,
    "--status": False, "--watch": False,
}

# The wrappers consume these themselves and forward nothing for them.
_WRAPPER_ONLY = {"--batch"}

_CASE_BLOCK = re.compile(
    r'while \[\[ \$# -gt 0 \]\]; do\s*\n\s*case "\$1" in\n(.*?)\n\s*esac', re.S)
# One clause: a label, then its body through the next `;;` (it may span lines).
_CLAUSE = re.compile(r"^\s*([^\s#)][^)\n]*)\)(.*?);;", re.M | re.S)
# Where else run.sh can put a flag into the runner's argv: either array it
# builds, and the call line itself. The case block alone misses a flag set from
# the environment (`batch_passthrough+=(--parallel "$N")`) or written onto the
# call, and both reach the real parser.
_SH_ARRAY = re.compile(r"\b(?:batch_args|batch_passthrough)\+?=\(([^)]*)\)")
_SH_CALL = re.compile(r'^\s*(?:exec\s+)?bash\s+"[^"]*/batch-runner\.sh"(.*)$',
                      re.M)
_SH_LITERAL = re.compile(r'(--[\w-]+)(\s+"\$\{?\w+\}?")?')
# run.ps1: a one-line switch arm, and a quoted flag literal anywhere else.
_PS1_ARM = re.compile(r'^\s*"(--[\w-]+)"\s*\{(.*)\}\s*$')
_PS1_LITERAL = re.compile(r'"(--[\w-]+)"(\s*,\s*\$\w+)?')


def _code(name: str) -> str:
    """A wrapper's text without its whole-line comments. Both wrappers describe
    their flags in comments, and a flag that is only mentioned is not sent."""
    src = (ROOT / name).read_text(encoding="utf-8")
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))


def _add(found: dict[str, bool], wrapper: str, flag: str, takes_value: bool):
    assert found.setdefault(flag, takes_value) == takes_value, (
        f"{wrapper} disagrees with itself about whether {flag} takes a value")


def _run_sh_flags() -> dict[str, bool]:
    """{flag: takes_value} as run.sh's own text states it: the clauses of its
    flag loop (a clause takes a value when its body reads `"$2"`), plus every
    flag literal it puts into the runner's argv outside the loop."""
    src = _code("run.sh")
    found: dict[str, bool] = {}
    m = _CASE_BLOCK.search(src)
    assert m, "run.sh's flag loop not found; update _CASE_BLOCK"
    for label, body in _CLAUSE.findall(m.group(1)):
        for flag in (part.strip() for part in label.split("|")):
            if flag != "*" and flag not in _WRAPPER_ONLY:
                _add(found, "run.sh", flag, '"$2"' in body)
    calls = _SH_CALL.findall(src)
    # Exactly one, so a call rewritten into a shape _SH_CALL cannot read fails
    # here instead of silently contributing nothing.
    assert len(calls) == 1, (
        f"expected one `bash …/batch-runner.sh` call in run.sh, found "
        f"{len(calls)}; update _SH_CALL")
    for chunk in (*_SH_ARRAY.findall(src), *calls):
        for flag, value in _SH_LITERAL.findall(chunk):
            _add(found, "run.sh", flag, bool(value))
    return found


def _run_ps1_flags() -> dict[str, bool]:
    """{flag: takes_value} as run.ps1's text states it: its switch arms (an arm
    takes a value when it advances `$i`), plus every other quoted flag literal,
    which in run.ps1 is only ever an argument built for the runner. Any quoted
    literal, not just lines naming `$batchArgs`: a passthrough ported in run.sh's
    shape collects into a second array, and a filter on that name misses it."""
    found: dict[str, bool] = {}
    for line in _code("run.ps1").splitlines():
        arm = _PS1_ARM.match(line)
        if arm:
            if arm.group(1) not in _WRAPPER_ONLY:
                _add(found, "run.ps1", arm.group(1), "$i++" in arm.group(2))
            continue
        for flag, value in _PS1_LITERAL.findall(line):
            _add(found, "run.ps1", flag, bool(value))
    return found


class TestWrapperFlagSet:
    def test_run_sh_flags_are_the_expected_set(self):
        assert _run_sh_flags() == EXPECTED, (
            "run.sh's flags changed. One it now sends to batch-runner.sh belongs "
            "in EXPECTED, which the probe below runs through the real runner; "
            "one it consumes itself belongs in _WRAPPER_ONLY.")

    def test_run_ps1_forwards_a_subset(self):
        # run.ps1 has no batch-option passthrough (its default arm hands those
        # flags to orchestrate.py), so it forwards only these four. Pinned as an
        # equality so that porting the passthrough widens this deliberately.
        assert _run_ps1_flags() == {
            f: EXPECTED[f] for f in ("--cli", "--model", "--skip-pdf", "--min-score")}


def _probe_unavailable():
    if sys.platform == "win32":
        return "batch-runner.sh is a bash script; probed on POSIX only"
    if shutil.which("bash") is None:
        return "no bash on PATH"
    if not RUNNER.is_file():
        return f"no career-ops checkout with {RUNNER} (local install; CI has none)"
    return None


_SKIP_REASON = _probe_unavailable()

# Read only if the sentinel is SWALLOWED, i.e. a flag the wrapper sends as a
# boolean has started taking a value upstream. Without it, `--status --help`
# would fall out of the parse loop into main(), and in a local checkout with a
# queued batch-input.tsv that takes the lock and starts a real batch. With it,
# the parser meets an unknown flag and exits 1 instead.
_TRIPWIRE = "--batch-runner-contract-tripwire"


def _probe(tmp_path, *argv):
    """Run the real runner's parser over `argv` followed by the sentinel.
    Returns (completed_process, stdout + stderr)."""
    r = subprocess.run(
        ["bash", str(RUNNER), *argv, "--help", _TRIPWIRE],
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        # The parent environment, never a hand-built dict (see the note in
        # test_merge_tracker_contract.py's _merge).
        env={**os.environ},
    )
    return r, r.stdout + r.stderr


@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
class TestRealRunnerParser:
    def test_help_is_reachable(self, tmp_path):
        r, _ = _probe(tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Usage:" in r.stdout

    @pytest.mark.parametrize("flag", list(EXPECTED))
    def test_every_wrapper_flag_is_accepted(self, tmp_path, flag):
        # "1" satisfies every value: nothing inside the parse loop validates
        # one, and the validation after it is never reached past --help.
        r, out = _probe(tmp_path, flag, *(["1"] if EXPECTED[flag] else []))
        detail = f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        assert f"Unknown option: {flag}" not in out, detail
        assert r.returncode == 0, detail
        # Usage printed means the sentinel was read as a flag and not consumed
        # as this flag's value.
        assert "Usage:" in r.stdout, detail

    def test_probe_can_fail(self, tmp_path):
        r, out = _probe(tmp_path, "--no-such-flag")
        assert r.returncode == 1, out
        assert "Unknown option: --no-such-flag" in out

    def test_a_swallowed_sentinel_trips_the_tripwire(self, tmp_path):
        # `--cli` takes the sentinel as its value, which is what a boolean flag
        # turned value-taking would do, so the parser must stop at the tripwire.
        r, out = _probe(tmp_path, "--cli")
        assert r.returncode == 1, out
        assert f"Unknown option: {_TRIPWIRE}" in out
