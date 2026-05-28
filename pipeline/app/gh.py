"""Thin wrappers around the `gh` CLI for the UI's cloud operations.

Isolated here so every GitHub interaction is one mockable surface and the
server stays declarative. All calls raise GhError with a user-facing message
on failure (gh missing, not authenticated, command failed).

Repo targeting: every call is pinned to a specific repo so a template-copied
clone with multiple remotes (origin + upstream) doesn't trip gh's "multiple
remotes detected" error. Resolution order: JOB_SEARCH_REPO env var (owner/name)
if set, else the `origin` remote of the current directory. So it works against
whatever the user named their repo after copying the template, no env var
needed; the override is only for launching from outside that clone.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


class GhError(RuntimeError):
    """A gh CLI call failed in a way worth surfacing to the user."""


def _parse_owner_name(remote_url: str) -> str | None:
    """Extract owner/name from a git remote URL (https or ssh form).
    https://github.com/owner/name(.git) | git@github.com:owner/name(.git)"""
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$", remote_url.strip())
    return m.group(1) if m else None


def _origin_repo() -> str | None:
    """owner/name of the `origin` git remote in the current directory, or None.

    Used as the default target so the app works against whatever the user named
    their repo after copying the template — without an env var. It also resolves
    the 'multiple remotes detected' ambiguity gh hits when a template-copied
    clone keeps both `origin` and an `upstream`/template remote: we pin to
    `origin` explicitly rather than letting gh guess."""
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return _parse_owner_name(r.stdout)


def _target_repo() -> str | None:
    """The repo every gh call should target: JOB_SEARCH_REPO if set, else the
    `origin` remote. None means 'let gh resolve it' (single-remote clones)."""
    repo = os.environ.get("JOB_SEARCH_REPO", "").strip()
    return repo or _origin_repo()


def _repo_args() -> list[str]:
    """`-R <repo>` for subcommands that take the --repo flag (run, secret, …)."""
    repo = _target_repo()
    return ["-R", repo] if repo else []


def _repo_positional() -> list[str]:
    """`gh repo view` takes the repository as a positional arg, not -R."""
    repo = _target_repo()
    return [repo] if repo else []


# Standard Windows install locations for gh, in priority order. Used as a
# last-resort fallback when neither GH_BIN nor PATH resolves it — which happens
# when gh ships as a Microsoft Store "App Execution Alias" (a reparse point
# under WindowsApps that CreateProcess can't follow), or PATH was set in the
# user's shell init but not in the environment that launched the UI.
_WIN_GH_FALLBACKS = (
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe"),
)


def _resolve_gh() -> str | None:
    """Return an absolute path to the gh executable, or None if we can't find
    it. ``GH_BIN`` env var wins; otherwise ``shutil.which`` (which honors
    PATHEXT so it picks up gh.exe / gh.cmd correctly); on Windows we also try
    the standard install locations. Re-resolved each call so a user can install
    gh without restarting the UI."""
    override = os.environ.get("GH_BIN", "").strip()
    if override and os.path.isfile(override):
        return override
    found = shutil.which("gh")
    if found:
        return found
    if os.name == "nt":
        for path in _WIN_GH_FALLBACKS:
            if os.path.isfile(path):
                return path
    return None


# Common Windows error codes we hit when a launch is blocked, with a one-line
# explanation of what each typically means in this context. Helps the user
# pinpoint the real cause (AV/EDR vs missing file vs policy) instead of guessing.
_WINERROR_HINTS = {
    2:    "ERROR_FILE_NOT_FOUND — the OS reports the executable can't be located.",
    3:    "ERROR_PATH_NOT_FOUND — a directory along the path is missing.",
    5:    "ERROR_ACCESS_DENIED — usually an AV/EDR or AppLocker rule blocking python.exe from launching this binary. Check Defender / your security tool's logs.",
    740:  "ERROR_ELEVATION_REQUIRED — launching this needs admin elevation.",
    1260: "ERROR_ACCESS_DISABLED_BY_POLICY — Group Policy / AppLocker / SRP is blocking the launch.",
}


def _fmt_oserror(exc: OSError) -> str:
    win = getattr(exc, "winerror", None)
    return f"winerror={win}, errno={exc.errno}, strerror={exc.strerror!r}"


def _explain_launch_failure(gh_bin: str, direct_err: OSError,
                            shell_err: OSError) -> str:
    """Build a diagnostic error message when both the direct and shell-mediated
    gh launches failed. Surfaces the Windows error codes so the user (and we)
    can tell AV/EDR blocks (winerror 5) from policy blocks (1260) from genuine
    file-not-found cases — instead of all of them collapsing to one message."""
    win = (getattr(shell_err, "winerror", None) or
           getattr(direct_err, "winerror", None))
    hint = _WINERROR_HINTS.get(win, "")
    return (
        f"Couldn't launch gh at {gh_bin!r}. "
        f"Direct: {_fmt_oserror(direct_err)}. "
        f"Shell:  {_fmt_oserror(shell_err)}. "
        + (hint + " " if hint else "")
        + "Set GH_BIN to a working gh executable, or reinstall from "
          "https://cli.github.com."
    )


def _run(args: list[str], timeout: int = 120, stdin: str | None = None) -> str:
    """Run `gh <args>`, returning stdout. Raises GhError on any failure.

    `stdin`, when given, is piped to the process — used for secret values so key
    material and large base64 blobs never appear in argv (visible in process
    listings) or hit the OS argument-length limit."""
    gh_bin = _resolve_gh()
    if gh_bin is None:
        raise GhError(
            "gh CLI not found. If it's installed, the UI process can't see it "
            "(common on Windows when gh is a Microsoft Store alias, or the "
            "terminal that launched the UI doesn't have it on PATH). Fixes: "
            "(1) install via the MSI from https://cli.github.com, (2) restart "
            "the terminal/VS Code that launched the UI, or (3) set "
            "GH_BIN=<full path to gh.exe> in your .env. Then run `gh auth login`."
        )
    try:
        r = subprocess.run(
            [gh_bin, *args], capture_output=True, text=True, timeout=timeout,
            input=stdin,
        )
    except OSError as direct_err:
        # Catch OSError (not just FileNotFoundError) so PermissionError
        # (winerror 5 = ACCESS_DENIED, common when an EDR/AV blocks direct exec)
        # and policy-block errors (winerror 1260) don't escape as uncaught 500s.
        # Retry once via shell=True so cmd.exe does the launch — some envs allow
        # cmd-mediated launches where direct CreateProcess is blocked.
        try:
            r = subprocess.run(
                subprocess.list2cmdline([gh_bin, *args]),
                capture_output=True, text=True, timeout=timeout, input=stdin,
                shell=True,
            )
        except OSError as shell_err:
            raise GhError(_explain_launch_failure(gh_bin, direct_err, shell_err))
    except subprocess.TimeoutExpired:
        raise GhError(f"gh {' '.join(args)} timed out after {timeout}s")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        # Surface the most common actionable cause clearly.
        if "auth" in msg.lower() or "not logged" in msg.lower():
            raise GhError("gh is not authenticated. Run `gh auth login`.")
        raise GhError(msg or f"gh {' '.join(args)} failed (exit {r.returncode})")
    return r.stdout


def current_repo() -> str:
    """nameWithOwner of the repo gh is targeting (cwd remote, or override)."""
    out = _run(["repo", "view", *_repo_positional(), "--json", "nameWithOwner",
                "-q", ".nameWithOwner"])
    return out.strip()


def latest_run(workflow: str) -> dict | None:
    """Return the most recent run of `workflow` (filename), or None if there
    are no runs yet. Dict has databaseId, status, conclusion, createdAt."""
    out = _run([
        "run", "list", *_repo_args(), "--workflow", workflow, "--limit", "1",
        "--json", "databaseId,status,conclusion,createdAt,displayTitle",
    ])
    runs = json.loads(out or "[]")
    return runs[0] if runs else None


def latest_successful_run(workflows: list[str], per_workflow: int = 10) -> dict | None:
    """Most recently-created *successful* run across several workflow files.

    The UI should load whichever pipeline produced fresh output last — the
    easy-apply pipeline runs several times a day, so it's often newer than the
    daily one. We require conclusion=success (and look back a few runs per
    workflow) so a failed or in-progress tick never gets chosen — that run has
    no downloadable artifact. Returns the run dict, or None if none succeeded."""
    best = None
    for wf in workflows:
        out = _run([
            "run", "list", *_repo_args(), "--workflow", wf, "--limit", str(per_workflow),
            "--json", "databaseId,status,conclusion,createdAt,displayTitle",
        ])
        runs = json.loads(out or "[]")
        ok = next((r for r in runs
                   if r.get("status") == "completed" and r.get("conclusion") == "success"),
                  None)
        # createdAt is ISO-8601 UTC (…Z), so lexical comparison is chronological.
        if ok and (best is None or ok["createdAt"] > best["createdAt"]):
            best = ok
    return best


def download_artifact(run_id: int, dest: Path, name_pattern: str = "pipeline-output-*") -> Path:
    """Download a run's artifact(s) into `dest` and return the directory that
    contains the pipeline output (reports/ + data/).

    `gh run download` lays each artifact into dest/<artifact-name>/. We return
    the matching artifact subdir so callers can point the data layer at it."""
    dest.mkdir(parents=True, exist_ok=True)
    _run([
        "run", "download", str(run_id), *_repo_args(),
        "--pattern", name_pattern, "--dir", str(dest),
    ], timeout=300)
    # gh nests each artifact under its own name. Find the one holding results.
    candidates = [
        d for d in dest.iterdir()
        if d.is_dir() and (d / "data").exists() or (d / "reports").exists()
    ]
    if candidates:
        return candidates[0]
    # Some gh versions extract a single artifact's contents directly into dest.
    if (dest / "reports").exists() or (dest / "data").exists():
        return dest
    raise GhError(
        "Downloaded the artifact but found no reports/ or data/ inside it — "
        "the run may not have produced output yet."
    )


def repo_visibility() -> str:
    """Return the target repo's visibility: 'PUBLIC', 'PRIVATE', or 'INTERNAL'.
    Used to refuse writing secrets to a public repo (the privacy guard)."""
    out = _run(["repo", "view", *_repo_positional(), "--json", "visibility",
                "-q", ".visibility"])
    return out.strip().upper()


def list_secret_names() -> list[str]:
    """Names of repository secrets already set on the target repo."""
    out = _run(["secret", "list", *_repo_args(), "--json", "name",
                "-q", ".[].name"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def set_secret(name: str, value: str) -> None:
    """Set a repository secret. The value is piped via stdin (never argv) so key
    material / base64 blobs stay out of process listings and argv limits.

    `gh secret set` reads the value from stdin **only when --body is omitted** —
    passing `--body -` would store the literal string "-", not stdin (it has no
    `-`-means-stdin convention). So we deliberately leave --body off here."""
    _run(["secret", "set", name, *_repo_args()], stdin=value)


def set_variable(name: str, value: str) -> None:
    """Set a repository variable (non-secret config, e.g. BATCH_PROVIDER)."""
    _run(["variable", "set", name, *_repo_args(), "--body", value])


def trigger_workflow(workflow: str, fields: dict | None = None) -> None:
    """Dispatch a workflow_dispatch run of `workflow`. `fields` become -f k=v."""
    args = ["workflow", "run", workflow, *_repo_args()]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]
    _run(args)
