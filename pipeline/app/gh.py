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
    repo = os.environ.get("JOB_SEARCH_REPO", "").strip()
    return ["-R", repo] if repo else []


def _run(args: list[str], timeout: int = 120) -> str:
    """Run `gh <args>`, returning stdout. Raises GhError on any failure."""
    try:
        r = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout
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
    out = _run(["repo", "view", *_repo_args(), "--json", "nameWithOwner",
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


def trigger_workflow(workflow: str, fields: dict | None = None) -> None:
    """Dispatch a workflow_dispatch run of `workflow`. `fields` become -f k=v."""
    args = ["workflow", "run", workflow, *_repo_args()]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]
    _run(args)
