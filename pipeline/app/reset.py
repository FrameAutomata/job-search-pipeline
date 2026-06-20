"""Start-over reset: wipe the accumulated job-search state while keeping setup.

For someone returning after a long break: clears the tracker, scan-history,
reports, eval queue, batch state, generated outputs, and UI overlays — but keeps
who you are (profile, CV, modes/_profile, story bank, portals, search config,
resumes) and all career-ops system code. Everything wiped is snapshotted first
so a misclick is recoverable.

clear_cloud_caches() removes the cloud GitHub Actions state cache so the cloud
pipeline restarts dedup from scratch (otherwise the offline-first Refresh would
merge the old cloud history straight back into the freshly-reset local tracker).
"""

import json
import shutil
from pathlib import Path

from pipeline.app import gh

CLOUD_CACHE_PREFIX = "pipeline-state-v1"

# career-ops search-state FILES to remove (relative to the career-ops root).
_CO_FILES = [
    "data/applications.md", "data/scan-history.tsv", "data/pipeline.md",
    "data/recheck-state.tsv", "data/easy-apply-urls.txt", "data/follow-ups.md",
    "batch/batch-input.tsv", "batch/batch-state.tsv", "batch/batch-api-state.json",
]
# career-ops DIRS whose contents to clear (keep the dir + its .gitkeep so the
# pipeline can write fresh). These hold reports, generated PDFs, cached JDs, etc.
_CO_DIRS = [
    "data/parser-output", "reports", "output", "jds",
    "batch/jds", "batch/tracker-additions", "batch/logs",
]
# Main-repo output files + the downloaded-artifact dirs (glob).
_REPO_FILES = ["output/jobs.csv", "output/filtered_jobs.csv", "output/_keywords.json"]
_REPO_GLOBS = [("output", "pipeline-output-*")]
# .ui-cache overlays (files) + cache dirs (removed wholesale — no .gitkeep).
_UI_FILES = ["status-overrides.json", "pushed-overrides.json"]
_UI_DIRS = ["latest", "apply"]


def reset_job_search(career_ops: Path, repo_root: Path, ui_cache: Path,
                     backup_dir: Path) -> dict:
    """Snapshot then wipe the job-search state. Returns a summary of what was
    removed. Missing paths are skipped (safe to run on an already-clean tree)."""
    removed: list[str] = []

    def snapshot(src: Path, rel: str) -> None:
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)

    def remove_file(src: Path, rel: str) -> None:
        if src.is_file():
            snapshot(src, rel)
            src.unlink()
            removed.append(rel)

    def remove_path(src: Path, rel: str) -> None:
        if src.is_dir():
            snapshot(src, rel)
            shutil.rmtree(src)
            removed.append(rel)
        else:
            remove_file(src, rel)

    def clear_dir(d: Path, rel: str) -> None:
        if not d.is_dir():
            return
        for child in sorted(d.iterdir()):
            if child.name == ".gitkeep":
                continue
            remove_path(child, f"{rel}/{child.name}")

    for f in _CO_FILES:
        remove_file(career_ops / f, f"career-ops/{f}")
    for d in _CO_DIRS:
        clear_dir(career_ops / d, f"career-ops/{d}")
    for f in _REPO_FILES:
        remove_file(repo_root / f, f"repo/{f}")
    for parent, pattern in _REPO_GLOBS:
        for match in sorted((repo_root / parent).glob(pattern)):
            remove_path(match, f"repo/{parent}/{match.name}")
    for f in _UI_FILES:
        remove_file(ui_cache / f, f".ui-cache/{f}")
    for d in _UI_DIRS:
        remove_path(ui_cache / d, f".ui-cache/{d}")

    return {"removed": removed, "count": len(removed), "backup_dir": str(backup_dir)}


def clear_cloud_caches(prefix: str = CLOUD_CACHE_PREFIX) -> dict:
    """Delete the cloud Actions state caches (key prefix `pipeline-state-v1`) in
    the configured repo, so the next pipeline run dedups from scratch. Best-effort
    — raises gh.GhError if gh isn't available/authed (the caller treats it so)."""
    out = gh._run(["cache", "list", *gh._repo_args(), "--limit", "100", "--json", "id,key"])
    caches = json.loads(out or "[]")
    deleted: list[str] = []
    for c in caches:
        if str(c.get("key", "")).startswith(prefix):
            gh._run(["cache", "delete", str(c["id"]), *gh._repo_args()])
            deleted.append(c["key"])
    return {"deleted": deleted}
