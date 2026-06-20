"""Template self-update: pull the maintainer's latest changes into a user's copy.

Copies are made via GitHub's "Use this template", so they have NO fork/git link
to the source. update_available() therefore checks whether the copy's history
already contains the template's latest `main` commit (via the gh API) rather than
a cross-repo compare. apply_update() does a local `git fetch template main →
merge → push origin`, which updates BOTH the local clone running the UI and the
cloud copy in one synchronous step, aborting cleanly on conflict.

A dispatch-only workflow (update-from-template.yml) covers users who update from
the Actions tab without a local clone.
"""

import subprocess
from pathlib import Path

from pipeline.app import gh

TEMPLATE_REPO = "FrameAutomata/job-search-pipeline"
TEMPLATE_URL = f"https://github.com/{TEMPLATE_REPO}.git"


def _is_not_found(err: gh.GhError) -> bool:
    msg = str(err).lower()
    return "404" in msg or "not found" in msg


def update_available(copy_repo: str | None = None, template_repo: str = TEMPLATE_REPO) -> dict:
    """Whether the template has commits the user's copy doesn't have yet.

    Returns {"available": bool, "template_sha": str} (or {"available": False,
    "reason": ...} when there's nothing to check). A copy is "behind" when it
    doesn't contain the template's latest main commit."""
    copy = copy_repo or gh._target_repo()
    if not copy:
        return {"available": False, "reason": "no repo configured"}
    if copy.lower() == template_repo.lower():
        return {"available": False, "reason": "this is the template repo"}

    template_sha = gh._run(["api", f"repos/{template_repo}/commits/main", "--jq", ".sha"]).strip()
    try:
        # 200 (any output) = the copy already contains that commit.
        gh._run(["api", f"repos/{copy}/commits/{template_sha}", "--silent"])
        return {"available": False, "template_sha": template_sha}
    except gh.GhError as e:
        if _is_not_found(e):
            return {"available": True, "template_sha": template_sha}
        raise


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args],
                          capture_output=True, text=True)


def apply_update(repo_root: Path, template_url: str = TEMPLATE_URL) -> dict:
    """Merge the template's latest main into the local clone and push to origin.

    One synchronous operation updates the local UI clone AND the cloud copy.
    Returns {"ok": bool, ...}: {"ok": True, "updated": bool} on success,
    {"ok": False, "conflict": True, ...} on a merge conflict (left aborted so the
    working tree is clean), or {"ok": False, "error": ...} otherwise."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return _run_git(repo_root, *args)

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return {"ok": False, "error": "not a git repository"}
    if git("remote", "get-url", "origin").returncode != 0:
        return {"ok": False, "error": "no 'origin' remote to push to"}

    # Only update the main branch: merging template/main into a feature branch
    # would pollute it and leave the cloud copy's main un-updated.
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        return {"ok": False, "error": f"switch to the 'main' branch to update (currently on '{branch}')"}

    # A dirty tree makes `git merge` refuse pre-merge — that's not a conflict, so
    # report it distinctly rather than telling the user to resolve a merge.
    if git("status", "--porcelain").stdout.strip():
        return {"ok": False, "error": "commit or stash your local changes before updating"}

    fetched = git("fetch", template_url, "main")
    if fetched.returncode != 0:
        return {"ok": False, "error": f"fetch from template failed: {fetched.stderr.strip()}"}

    merged = git("merge", "--no-edit", "FETCH_HEAD")
    if merged.returncode != 0:
        git("merge", "--abort")
        return {"ok": False, "conflict": True,
                "error": "merge conflict — resolve it manually, then push"}
    if "Already up to date" in (merged.stdout + merged.stderr):
        return {"ok": True, "updated": False}

    pushed = git("push", "origin", "HEAD")
    if pushed.returncode != 0:
        return {"ok": False, "error": f"push to origin failed: {pushed.stderr.strip()}"}
    return {"ok": True, "updated": True}
