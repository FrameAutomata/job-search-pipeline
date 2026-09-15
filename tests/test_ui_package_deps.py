"""The flake's UI package and requirements-ui.txt must name the same deps.

Two statements of one dependency set, in two languages, with nothing else
keeping them in sync — the shape CLAUDE.md keeps calling "a copy of a file that
sits on disk is a bug with a delay on it", and which `sites.py`, `.env.example`
and the agent-CLI registry all carry a guard for.

The failure this catches is specific and nasty: a UI route grows a dependency,
someone adds it to `requirements-ui.txt` — the file the install instructions
name and the one `run-ui.sh`'s pip fallback reads — and every developer and
every CI leg stays green. Only the HOSTED instance breaks, at import, and that
is the one with non-technical users on it.

Pure text inspection, so it runs wherever pytest does: no nix on the runner,
which is the same reason `tests/test_run_ui_flags.py` reads the PowerShell
wrapper as text rather than executing it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `uvicorn[standard]` on the pip side is `ps.uvicorn.optional-dependencies.standard`
# on the nix side. Neither spells out the six members, and neither should.
_EXTRA = "optional-dependencies.standard"


def _requirements() -> set[str]:
    out = set()
    for line in (ROOT / "requirements-ui.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(re.split(r"[\[<>=!;]", line, 1)[0].strip().lower())
    return out


def _flake_block() -> str:
    """The `withPackages` list from the UI package."""
    text = (ROOT / "flake.nix").read_text(encoding="utf-8")
    start = text.index("py = pkgs.python3.withPackages")
    return text[start:text.index(");", start)]


def _flake_packages() -> set[str]:
    block = _flake_block()
    listed = re.search(r"\[(.*?)\]", block, re.S)
    names = {n.strip().lower() for n in (listed.group(1) if listed else "").split()}
    if _EXTRA in block:
        names.add("uvicorn")
    return {n for n in names if n}


def test_the_two_dependency_lists_agree():
    assert _flake_packages() == _requirements()


def test_the_uvicorn_extra_is_not_spelled_out():
    """Its six members are data nixpkgs already ships. Listing them by hand
    froze today's answer and made each read as an independent decision."""
    block = _flake_block()
    assert _EXTRA in block, "the flake should take uvicorn's `standard` extra, not its members"
    for member in ("httptools", "uvloop", "websockets", "watchfiles", "python-dotenv"):
        assert member not in block, f"{member} comes with [standard]; do not list it"


def test_the_guard_is_reading_something():
    """A parse that silently returned an empty set would pass the first test
    forever."""
    assert len(_requirements()) >= 4
    assert "fastapi" in _flake_packages() and "uvicorn" in _flake_packages()
