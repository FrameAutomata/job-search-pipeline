"""run-ui.sh's LAN wiring, and the PowerShell mirror of it.

`--lan` is the one flag in this repo that changes who can reach the process, so
what it does is worth pinning rather than reading: it must bind 0.0.0.0, set
UI_LAN for the server, and REFUSE outright when UI_PASSWORD is not in the
exported environment. That last one is the whole safety story on the shell
side — the server refuses too, but only the launcher can fail before uvicorn
binds, and only the server can see a password that lives in .env. Both halves
exist because neither can see what the other sees.

The test runs the real run-ui.sh against a stub root whose `python` records its
argv and the LAN env, the same shape as tests/test_run_sh_flags.py. run-ui.ps1
cannot be executed here, so it is checked as a mirror: the same four decisions
must be spelled in it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="run-ui.sh is a bash script; no usable bash here",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# run-ui.sh invokes python twice — the dependency probe (`-c "import uvicorn…"`)
# and the uvicorn exec — so every assertion below is a membership test over the
# whole recording. UI_LAN is recorded per invocation because "the flag was
# passed" and "the server was told" are two different claims.
_PY_RECORDER = """#!/usr/bin/env bash
printf 'ARGS %s\\n' "$*" >> "{out}"
printf 'UI_LAN=%s\\n' "${{UI_LAN:-}}" >> "{out}"
"""


@pytest.fixture
def stub_root(tmp_path):
    """A fake repo root: the real run-ui.sh over a recording python."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    py = tmp_path / ".venv" / "bin" / "python"
    py.write_text(_PY_RECORDER.format(out=tmp_path / "py_args.txt"))
    py.chmod(0o755)
    shutil.copy(REPO_ROOT / "run-ui.sh", tmp_path / "run-ui.sh")
    (tmp_path / "run-ui.sh").chmod(0o755)
    return tmp_path


def _run(root, *args, password=None):
    env = {k: v for k, v in os.environ.items() if k not in ("UI_LAN", "UI_PASSWORD")}
    if password is not None:
        env["UI_PASSWORD"] = password
    return subprocess.run(
        ["bash", str(root / "run-ui.sh"), *args],
        capture_output=True, text=True, cwd=root, env=env,
    )


def _recording(root):
    path = root / "py_args.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_default_run_stays_on_loopback(stub_root):
    """No flag, no change: uvicorn is started without --host, so it keeps its
    127.0.0.1 default and the server never sees UI_LAN."""
    r = _run(stub_root)
    assert r.returncode == 0, r.stderr
    rec = _recording(stub_root)
    assert "-m uvicorn pipeline.app.server:app --port 8000" in rec
    assert "--host" not in rec
    assert "UI_LAN=1" not in rec


def test_lan_binds_every_interface_and_tells_the_server(stub_root):
    r = _run(stub_root, "--lan", password="a-long-passphrase")
    assert r.returncode == 0, r.stderr
    rec = _recording(stub_root)
    assert "--host 0.0.0.0" in rec
    # The env the uvicorn process inherits is what turns the guard on; a flag
    # the server can't see would bind the world with the loopback-only rules.
    assert "UI_LAN=1" in rec


def test_lan_refuses_without_a_password(stub_root):
    """The refusal is the feature. Exit non-zero, say which variable, and never
    reach uvicorn — a bound port with no password cannot be un-bound by a
    warning the user scrolled past."""
    r = _run(stub_root, "--lan")
    assert r.returncode != 0
    assert "UI_PASSWORD" in r.stderr
    assert "--host" not in _recording(stub_root)


def test_lan_composes_with_port(stub_root):
    r = _run(stub_root, "--lan", "--port", "8123", password="x")
    assert r.returncode == 0, r.stderr
    rec = _recording(stub_root)
    assert "--port 8123" in rec and "--host 0.0.0.0" in rec


def test_unknown_flag_still_refused(stub_root):
    r = _run(stub_root, "--nope")
    assert r.returncode != 0
    assert "unknown arg" in r.stdout + r.stderr


class TestPowerShellMirror:
    """run-ui.ps1 can't be executed here, so its four LAN decisions are checked
    as text. They are the same four the bash tests above drive, and a Windows
    user gets no protection from a rule only the bash half spells."""

    @pytest.fixture
    def ps1(self):
        return (REPO_ROOT / "run-ui.ps1").read_text(encoding="utf-8")

    def test_takes_a_lan_switch(self, ps1):
        assert "[switch]$Lan" in ps1

    def test_binds_every_interface(self, ps1):
        assert "--host" in ps1 and "0.0.0.0" in ps1

    def test_sets_ui_lan_for_the_server(self, ps1):
        assert "$env:UI_LAN" in ps1

    def test_refuses_without_a_password(self, ps1):
        assert "$env:UI_PASSWORD" in ps1
        assert "exit 1" in ps1
