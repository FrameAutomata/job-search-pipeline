"""Thin wrappers around the `gh` CLI for the UI's cloud operations.

Isolated here so every GitHub interaction is one mockable surface and the
server stays declarative. All calls raise GhError with a user-facing message
on failure (gh missing, not authenticated, command failed).

Repo targeting: by default gh uses the current directory's git remote. Set
the JOB_SEARCH_REPO env var (owner/name) to override — useful if the UI is
launched from outside your private copy's clone.
"""

import json
import os
import subprocess
from pathlib import Path


class GhError(RuntimeError):
    """A gh CLI call failed in a way worth surfacing to the user."""


def _repo_args() -> list[str]:
    """`-R <repo>` for subcommands that take the --repo flag (run, secret, …)."""
    repo = os.environ.get("JOB_SEARCH_REPO", "").strip()
    return ["-R", repo] if repo else []


def _repo_positional() -> list[str]:
    """`gh repo view` takes the repository as a positional arg, not -R."""
    repo = os.environ.get("JOB_SEARCH_REPO", "").strip()
    return [repo] if repo else []


def _run(args: list[str], timeout: int = 120, stdin: str | None = None) -> str:
    """Run `gh <args>`, returning stdout. Raises GhError on any failure.

    `stdin`, when given, is piped to the process — used for secret values so key
    material and large base64 blobs never appear in argv (visible in process
    listings) or hit the OS argument-length limit."""
    try:
        r = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout,
            input=stdin,
        )
    except FileNotFoundError:
        raise GhError(
            "gh CLI not found. Install it from https://cli.github.com and run "
            "`gh auth login`."
        )
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
    material / base64 blobs stay out of process listings and argv limits."""
    _run(["secret", "set", name, *_repo_args(), "--body", "-"], stdin=value)


def set_variable(name: str, value: str) -> None:
    """Set a repository variable (non-secret config, e.g. BATCH_PROVIDER)."""
    _run(["variable", "set", name, *_repo_args(), "--body", value])


def trigger_workflow(workflow: str, fields: dict | None = None) -> None:
    """Dispatch a workflow_dispatch run of `workflow`. `fields` become -f k=v."""
    args = ["workflow", "run", workflow, *_repo_args()]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]
    _run(args)
