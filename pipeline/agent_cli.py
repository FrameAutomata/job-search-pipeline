"""Single source of truth for the agent CLI that applies for you and runs --batch.

The same shape as `pipeline/sites.py`: a stdlib-only leaf module the jobspy-free
UI venv can import, one registry, and a guard for every file that has to
restate it because it can't import it (`.env.example`, `setup-profile.mjs`,
`onboard.html` — tests/test_agent_cli.py and tests/test_app_onboard.py).

Four things used to answer "which CLI" independently — `skills.cli_name()`,
`run.sh`/`run.ps1`'s `${BATCH_CLI:-claude}`, `server._KNOWN_CLIS`, and the
wizard's default — and every one of them said claude, which has no free tier.
The product goal is a loop that runs free by default, so the default is now the
free CLI (`DEFAULT_CLI`) and the wrappers ask this module (`--resolved`) rather
than carrying a default of their own.

Three facts here are load-bearing and were read off the installed CLIs' own
`--help` (gemini 0.59.0, qwen 0.23.2, opencode 1.18.30) — re-run them before
changing a shape:

- `interactive_argv` opens the CLI INTERACTIVELY with a seed prompt, because the
  apply assistant must stay interactive so the person can intervene. That is
  `-i` on Gemini CLI and Qwen Code (the positional prompt is one-shot on qwen
  and was one-shot on older gemini), `--prompt` on OpenCode (its root
  positional is a PROJECT DIRECTORY, and `opencode run` is non-interactive),
  and the positional on Claude Code. `shell_command` therefore never renders a
  bare `<binary> "<prompt>"`.
- Gemini CLI's free tier is the personal Google LOGIN, not an API key. Both the
  UI process and `orchestrate` load `.env`, so a `GEMINI_API_KEY` set for cloud
  evaluation sits in the environment the CLI would inherit — and the CLI can
  switch to the key's tier silently. `env_unset` names the variables the
  launcher strips (`env -u` on POSIX, `set NAME=` in cmd, and an env copy in
  `skills.launch_in_terminal`).
- Playwright MCP registration has two forms: an argv (`<cli> mcp add …`) and a
  JSON config merge for OpenCode, whose `mcp add` has no non-interactive form
  for a local server. Gemini's `mcp add` scope DEFAULTS to `project`, and
  Claude Code's to `local` (per cwd), so `-s user` is required on both or the
  registration lands in the directory setup ran from and `cd career-ops &&
  <cli> …` — a separate checkout — never sees it.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.stdio import line_buffer_stdout

BATCH_CLI_ENV = "BATCH_CLI"
DEFAULT_CLI = "gemini"

# The one MCP server the apply skill needs. Registered the same way with every
# CLI; only the wrapper around it differs.
PLAYWRIGHT_MCP_SERVER = "playwright"
PLAYWRIGHT_MCP_COMMAND = ("npx", "-y", "@playwright/mcp@latest")

# The Google key names Gemini CLI (and Qwen Code, which shares its lineage)
# read from the environment. See the module docstring for why they are unset.
GOOGLE_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

REGISTER_MCP_CMD = "python -m pipeline.agent_cli --register-mcp"

# Where OpenCode's config lives when set (default `~/.config`). Read from the
# process env by `mcp_registration` — and so cleared by tests/conftest.py's
# provider-env fixture, like `BATCH_CLI`.
XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"

_PRIVACY_NOTICE_URL = (
    "https://developers.google.com/gemini-code-assist/resources/"
    "privacy-notice-gemini-code-assist-individuals"
)


@dataclass(frozen=True)
class McpRegistration:
    """How one CLI learns about the Playwright MCP server.

    Exactly one of the two forms is populated: `argv` (run it), or
    `config_path` + `merge` (read the JSON there, merge the `playwright` server
    entry in, write back atomically, creating parents). A config merge is
    idempotent by construction; an argv registration is made idempotent by the
    caller (`register_playwright_mcp` reads "already" as success)."""
    argv: tuple = ()
    config_path: Path | None = None
    merge: dict = field(default_factory=dict)

    @property
    def is_argv(self) -> bool:
        return bool(self.argv)


@dataclass(frozen=True)
class AgentCli:
    id: str
    label: str
    binary: str
    tier: str          # "free" | "paid"
    tier_note: str     # one line: what free means / what paid costs
    install_hint: str  # one line: how to install
    docs_url: str
    # The option that seeds an INTERACTIVE session with a prompt; "" means the
    # prompt is the positional argument.
    prompt_flag: str = ""
    # Environment variables the launcher strips before the CLI starts.
    env_unset: tuple = ()
    # `<binary> mcp add …` argv (after the binary), or () when the CLI has no
    # non-interactive form and the registration is a config merge.
    mcp_add_args: tuple = ()
    # For the config-merge form: the config file, relative to the XDG config
    # dir (`$XDG_CONFIG_HOME`, default `~/.config`).
    mcp_config_rel: str = ""

    def interactive_argv(self, prompt: str) -> list:
        """The argv that opens the CLI interactively, seeded with `prompt`."""
        if self.prompt_flag:
            return [self.binary, self.prompt_flag, prompt]
        return [self.binary, prompt]

    def mcp_registration(self, *, home: Path | None = None,
                         env: dict | None = None) -> McpRegistration:
        """How to register the Playwright MCP server with this CLI.

        `home`/`env` exist for the config-merge form (they decide where the
        config file lives) and for tests; they default to the real ones."""
        if self.mcp_add_args:
            return McpRegistration(argv=(self.binary, *self.mcp_add_args))
        home = Path.home() if home is None else Path(home)
        env = os.environ if env is None else env
        xdg = (env.get(XDG_CONFIG_HOME_ENV) or "").strip()
        config_dir = Path(xdg) if xdg else home / ".config"
        return McpRegistration(
            config_path=config_dir / self.mcp_config_rel,
            merge={"mcp": {PLAYWRIGHT_MCP_SERVER: {
                "type": "local",
                "command": list(PLAYWRIGHT_MCP_COMMAND),
                "enabled": True,
            }}},
        )


# Display order: free first, the default at the top. Every tier note for a free
# entry must translate the limit into applications or say "rotating" — a guard
# in tests/test_agent_cli.py holds that, because "free" without a number is how
# a person discovers the ceiling mid-application.
AGENT_CLIS: dict = {c.id: c for c in (
    AgentCli(
        id="gemini",
        label="Gemini CLI",
        binary="gemini",
        tier="free",
        tier_note=(
            "Free with a personal Google login — about 1,000 requests/day "
            "≈ 10–20 prepared applications. The free tier is the LOGIN, not an "
            "API key: a GEMINI_API_KEY in the environment switches the CLI to "
            "that key's tier, so the launcher strips GEMINI_API_KEY/GOOGLE_API_KEY "
            "before starting it. Under the individual free tier Google may use "
            "prompts (your profile) to improve its products unless you opt out "
            "(`privacy.usageStatisticsEnabled: false` in ~/.gemini/settings.json; "
            "`usageStatisticsEnabled` at the top level in older releases — see "
            f"{_PRIVACY_NOTICE_URL})."
        ),
        install_hint="npm install -g @google/gemini-cli   (then run `gemini` once and sign in with a personal Google account)",
        docs_url="https://geminicli.com/docs/",
        prompt_flag="-i",
        env_unset=GOOGLE_KEY_VARS,
        mcp_add_args=("mcp", "add", "-s", "user", PLAYWRIGHT_MCP_SERVER, *PLAYWRIGHT_MCP_COMMAND),
    ),
    AgentCli(
        id="opencode",
        label="OpenCode",
        binary="opencode",
        tier="free",
        tier_note=(
            "Free CLI with a rotating set of free hosted models via its Zen "
            "gateway (availability changes), or bring your own API key. "
            "`--prompt` pre-fills the TUI input: press Enter to start, which "
            "suits assisted applying."
        ),
        install_hint="curl -fsSL https://opencode.ai/install | bash   (or: npm install -g opencode-ai)",
        docs_url="https://opencode.ai/docs/",
        prompt_flag="--prompt",
        mcp_config_rel="opencode/opencode.json",
    ),
    AgentCli(
        id="claude",
        label="Claude Code",
        binary="claude",
        tier="paid",
        tier_note=(
            "Paid — a Claude Pro subscription ($20/mo) or API credits; there is "
            "no free tier. The strongest at agentic browser work."
        ),
        install_hint="npm install -g @anthropic-ai/claude-code",
        docs_url="https://docs.claude.com/en/docs/claude-code",
        # Its scope DEFAULT is `local` — keyed on the cwd — so a server added
        # from the repo root is invisible to `cd career-ops && claude …`, a
        # separate checkout. `-s user` makes it seen from everywhere.
        mcp_add_args=("mcp", "add", "-s", "user", PLAYWRIGHT_MCP_SERVER, "--", *PLAYWRIGHT_MCP_COMMAND),
    ),
    AgentCli(
        id="qwen",
        label="Qwen Code",
        binary="qwen",
        tier="paid",
        tier_note=(
            "Paid — the free login ended 2026-04-15, so it needs a paid API key "
            "now. Same `-i` seed flag as Gemini CLI; the launcher strips the "
            "Google key names for it too."
        ),
        install_hint="npm install -g @qwen-code/qwen-code",
        docs_url="https://qwenlm.github.io/qwen-code-docs/",
        prompt_flag="-i",
        env_unset=GOOGLE_KEY_VARS,
        mcp_add_args=("mcp", "add", "-s", "user", PLAYWRIGHT_MCP_SERVER, *PLAYWRIGHT_MCP_COMMAND),
    ),
)}

assert DEFAULT_CLI in AGENT_CLIS

_warned: set = set()


def resolve_cli(env=None) -> AgentCli:
    """The CLI `BATCH_CLI` names, else the default.

    An unknown value warns once per process and falls back rather than raising:
    the wrappers and the UI both call this on every request, and a typo in
    `.env` should cost one line, not every skill."""
    env = os.environ if env is None else env
    raw = (env.get(BATCH_CLI_ENV) or "").strip()
    key = raw.lower()
    if not key:
        return AGENT_CLIS[DEFAULT_CLI]
    if key in AGENT_CLIS:
        return AGENT_CLIS[key]
    if raw not in _warned:
        _warned.add(raw)
        print(f"[agent-cli] {BATCH_CLI_ENV}={raw!r} is not a known agent CLI "
              f"(known: {', '.join(AGENT_CLIS)}); using {DEFAULT_CLI}.",
              file=sys.stderr)
    return AGENT_CLIS[DEFAULT_CLI]


def cli_available(cli: AgentCli) -> bool:
    # Through the module attribute, never `from shutil import which`: the UI
    # tests patch `shutil.which` by that path and must keep reaching this.
    return shutil.which(cli.binary) is not None


def installed() -> list:
    return [c for c in AGENT_CLIS.values() if cli_available(c)]


def _collapse(prompt: str) -> str:
    return " ".join(prompt.splitlines())


def shell_command(cli: AgentCli, prompt: str, *, cwd_hint: str = "career-ops",
                  os_name: str | None = None) -> str:
    """The string the UI shows and "Run in terminal" executes.

    `cd <cwd_hint> && ` + the CLI's `interactive_argv` rendered for the server's
    shell — `shlex.join` on POSIX, `subprocess.list2cmdline` on Windows — with
    the `env_unset` prefix in that shell's spelling. Newlines are collapsed
    first: a prompt is one line to every shell here.

    `os_name` is read at CALL time (not bound as a default at import), so a
    test that patches `os.name` to fake Windows reaches this branch.

    The Windows rendering is for cmd.exe — the shell "Run in terminal" opens.
    cmd toggles its quote state on EVERY `"` and reads backslash as a literal,
    so the `\"` list2cmdline would emit for an embedded quote ends the quoted
    region as far as cmd is concerned, and a `&`/`|`/`<`/`>` after it splits
    the command (`Acme "Bob & Sons"` started the CLI with a truncated prompt
    and ran `Sons…` as a second command). Embedded `"` are therefore turned
    into `'` on this branch — the prompt is prose for the model, so nothing is
    lost — and the rendering never contains `\"`. (Pasted into PowerShell, the
    `set NAME=&&` prefix does not unset anything; Run in terminal launches cmd.)"""
    os_name = os.name if os_name is None else os_name
    prompt = _collapse(prompt)
    if os_name == "nt":
        argv = cli.interactive_argv(prompt.replace('"', "'"))
        prefix = "".join(f"set {v}=&& " for v in cli.env_unset)
        rendered = subprocess.list2cmdline(argv)
    else:
        argv = cli.interactive_argv(prompt)
        prefix = ("env " + " ".join(f"-u {v}" for v in cli.env_unset) + " ") if cli.env_unset else ""
        rendered = shlex.join(argv)
    return f"cd {cwd_hint} && {prefix}{rendered}"


def _display_path(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix()


def playwright_mcp_note(cli: AgentCli) -> str:
    """The one-time setup note for the apply skill, rendered for THAT CLI."""
    reg = cli.mcp_registration()
    if reg.is_argv:
        how = f"`{shlex.join(reg.argv)}`"
    else:
        how = f"merge the `{PLAYWRIGHT_MCP_SERVER}` server into {_display_path(reg.config_path)}"
    return (f"Needs the Playwright MCP server registered with {cli.label} (one-time):  "
            f"{how} — `{REGISTER_MCP_CMD}` does this for you")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".opencode-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _merge_config(reg: McpRegistration) -> str:
    path = reg.config_path
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as e:
            # Never clobber a config we can't read back: the rest of the user's
            # OpenCode setup lives in this file.
            return (f"Could not read {_display_path(path)} ({e}); left it alone. "
                    f"Add the `{PLAYWRIGHT_MCP_SERVER}` server by hand: {json.dumps(reg.merge)}")
        if not isinstance(existing, dict):
            return (f"{_display_path(path)} is not a JSON object; left it alone. "
                    f"Add the `{PLAYWRIGHT_MCP_SERVER}` server by hand: {json.dumps(reg.merge)}")
    servers = dict(existing.get("mcp") or {})
    entry = reg.merge["mcp"][PLAYWRIGHT_MCP_SERVER]
    already = servers.get(PLAYWRIGHT_MCP_SERVER) == entry
    servers[PLAYWRIGHT_MCP_SERVER] = entry
    merged = {**existing, "mcp": servers}
    if not already:
        _write_json_atomic(path, merged)
    verb = "already registered in" if already else "registered in"
    return f"Playwright MCP server {verb} {_display_path(path)}."


def register_playwright_mcp(cli: AgentCli, *, run=None,
                            home: Path | None = None, env: dict | None = None) -> str:
    """Register the Playwright MCP server with `cli`; return a one-line message.

    Never raises for a missing binary — returns the install hint — and reads a
    non-zero `mcp add` whose output says "already" as success, because Claude
    Code errors on a re-run and setup runs under `set -e`.

    `run` defaults to `subprocess.run` at CALL time (not in the signature), so
    a test patching `subprocess.run` reaches the command-line entry points."""
    run = subprocess.run if run is None else run
    exe = shutil.which(cli.binary)
    if exe is None:
        return f"{cli.label} is not installed — install it first: {cli.install_hint}"
    reg = cli.mcp_registration(home=home, env=env)
    if not reg.is_argv:
        try:
            return _merge_config(reg)
        except OSError as e:
            return f"Could not write {_display_path(reg.config_path)}: {e}"
    # argv[0] is the RESOLVED path, not the bare name the registration
    # displays: on Windows npm installs these CLIs as `gemini.cmd` shims, which
    # `shutil.which` finds through PATHEXT but CreateProcess (what a shell-less
    # `subprocess.run` uses) does not — it appends only `.exe`, so the bare
    # name raised WinError 2 for every npm-installed CLI and setup.ps1's
    # registration did nothing. A full path to a `.cmd` file does run.
    try:
        proc = run([exe, *reg.argv[1:]], capture_output=True, text=True)
    except OSError as e:
        return f"Could not run `{shlex.join(reg.argv)}`: {e}"
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    if proc.returncode == 0:
        return f"Registered the Playwright MCP server with {cli.label}."
    if "already" in output.lower():
        return f"Playwright MCP server already registered with {cli.label}."
    tail = output.splitlines()[-1] if output else f"exit {proc.returncode}"
    return f"`{shlex.join(reg.argv)}` failed: {tail}"


def _print_install_hints() -> None:
    print("No agent CLI is installed. The free options first:")
    for c in AGENT_CLIS.values():
        print(f"  {c.id:<9} {c.tier:<5} {c.label}: {c.install_hint}")


def _print_list() -> None:
    print(f"{'id':<9} {'label':<12} {'tier':<5} installed")
    for c in AGENT_CLIS.values():
        mark = "yes" if cli_available(c) else "no"
        tag = "  (default)" if c.id == DEFAULT_CLI else ""
        print(f"{c.id:<9} {c.label:<12} {c.tier:<5} {mark}{tag}")


def main(argv=None) -> int:
    line_buffer_stdout()
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.agent_cli",
        description="The agent CLI that applies for you and runs --batch.",
    )
    ap.add_argument("--list", action="store_true", help="table: id, label, tier, installed")
    ap.add_argument("--register-mcp", nargs="?", const="", metavar="ID",
                    help="register the Playwright MCP server (default: the resolved CLI)")
    ap.add_argument("--register-mcp-all-installed", action="store_true",
                    help="register it with every installed CLI (what setup runs; always exits 0)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the resolved CLI is not installed")
    ap.add_argument("--resolved", action="store_true",
                    help="print the resolved CLI id (reads .env)")
    args = ap.parse_args(argv)

    if args.resolved:
        # Only here: the module stays import-clean for the UI venv and tests,
        # but the wrappers need the same `.env` view the wizard writes to.
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        print(resolve_cli().id)
        return 0
    if args.list:
        _print_list()
        return 0
    if args.register_mcp is not None:
        key = args.register_mcp.strip().lower()
        if key and key not in AGENT_CLIS:
            print(f"Unknown agent CLI {key!r}. Known: {', '.join(AGENT_CLIS)}", file=sys.stderr)
            return 2
        cli = AGENT_CLIS[key] if key else resolve_cli()
        print(register_playwright_mcp(cli))
        return 0
    if args.register_mcp_all_installed:
        found = installed()
        if not found:
            _print_install_hints()
            return 0
        for cli in found:
            print(f"  {cli.label}: {register_playwright_mcp(cli)}")
        return 0
    if args.check:
        cli = resolve_cli()
        if cli_available(cli):
            print(f"{cli.label} ({cli.id}) is installed.")
            return 0
        print(f"{cli.label} ({cli.id}) is not installed: {cli.install_hint}")
        return 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    line_buffer_stdout()
    sys.exit(main())
