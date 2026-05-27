"""FastAPI server for the local triage UI.

Serves a single-page frontend on localhost and a small JSON API over the
evaluation results. Read operations (list jobs, view report) + cloud
operations via the gh CLI (refresh from the latest artifact, trigger a run).
Status write-back + onboarding land in later phases.

Run:
    uvicorn pipeline.app.server:app --port 8000
or use the run-ui.sh / run-ui.ps1 launchers.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline.app import data, gh

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Where `gh run download` drops artifacts. Gitignored. When a Refresh has
# populated this, the data layer reads from here; otherwise it falls back to
# CAREER_OPS_PATH (a local run, or a manually-passed --data dir).
UI_CACHE = ROOT / ".ui-cache" / "latest"

# Set by /api/refresh to the artifact subdir gh extracted, so subsequent reads
# use freshly-downloaded data without restarting the server.
_active_data_dir: Path | None = None

# Cloud workflow filenames (must match .github/workflows/*.yml).
DAILY_WORKFLOW = "daily-pipeline.yml"


def _career_ops() -> Path:
    """Resolve the data directory. A successful Refresh wins; otherwise fall
    back to CAREER_OPS_PATH (resolved like orchestrate.py does)."""
    if _active_data_dir is not None:
        return _active_data_dir
    raw = os.environ.get("CAREER_OPS_PATH") or "career-ops"
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p).resolve()


app = FastAPI(title="job-search-pipeline UI")


@app.get("/api/jobs")
def list_jobs() -> JSONResponse:
    """Return tracker rows as JSON. Prefers the merged applications.md, falls
    back to raw tracker-additions when the merge step hasn't run. The response
    is {"rows": [...], "source": ...} so the UI can flag unmerged output."""
    return JSONResponse(data.load_jobs(_career_ops()))


@app.get("/api/reports/{report_num}", response_class=HTMLResponse)
def get_report(report_num: str) -> HTMLResponse:
    """Return a single report rendered to HTML, looked up by its number."""
    reports_dir = _career_ops() / "reports"
    report_file = data.find_report_file(reports_dir, report_num)
    if report_file is None:
        raise HTTPException(status_code=404, detail=f"No report found for #{report_num}")
    return HTMLResponse(data.render_report_html(report_file))


@app.get("/api/health")
def health() -> dict:
    """Basic readiness probe — confirms where the UI is reading data from and
    whether the tracker exists yet."""
    apps_md = _career_ops() / "data" / "applications.md"
    return {
        "status": "ok",
        "career_ops": str(_career_ops()),
        "applications_md_exists": apps_md.exists(),
        "refreshed": _active_data_dir is not None,
    }


@app.post("/api/refresh")
def refresh() -> JSONResponse:
    """Download the latest daily-pipeline artifact via gh and point the data
    layer at it, so the user can pull fresh cloud results without leaving the
    UI. Returns the run that was downloaded."""
    global _active_data_dir
    try:
        run = gh.latest_run(DAILY_WORKFLOW)
        if run is None:
            raise HTTPException(status_code=404, detail="No pipeline runs found yet.")
        # Clear any prior download so stale files can't linger.
        if UI_CACHE.exists():
            import shutil
            shutil.rmtree(UI_CACHE)
        data_dir = gh.download_artifact(run["databaseId"], UI_CACHE)
        _active_data_dir = data_dir
        return JSONResponse({
            "ok": True,
            "run_id": run["databaseId"],
            "created_at": run.get("createdAt"),
            "title": run.get("displayTitle"),
            "data_dir": str(data_dir),
        })
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/run")
def run_pipeline() -> JSONResponse:
    """Trigger a daily-pipeline run in the cloud via gh. Returns immediately —
    the run executes on GitHub's schedule infra; Refresh later to pull results."""
    try:
        gh.trigger_workflow(DAILY_WORKFLOW)
        return JSONResponse({"ok": True, "workflow": DAILY_WORKFLOW})
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))


# Mount the SPA last so /api/* routes take precedence. html=True serves
# index.html at "/" and for unknown paths (client-side routing friendly).
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
