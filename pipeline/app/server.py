"""FastAPI server for the local triage UI.

Serves a single-page frontend on localhost and a small JSON API over the
evaluation results. Read-only in this phase: list jobs from applications.md,
view a rendered report. Onboarding + run-controls + write-back land in later
phases.

Run:
    uvicorn pipeline.app.server:app --port 8000
or use the run-ui.sh / run-ui.ps1 launchers.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline.app import data

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _career_ops() -> Path:
    """Resolve the career-ops directory the same way orchestrate.py does, so
    the UI reads the same data the pipeline writes."""
    raw = os.environ.get("CAREER_OPS_PATH") or "career-ops"
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p).resolve()


app = FastAPI(title="job-search-pipeline UI")


@app.get("/api/jobs")
def list_jobs() -> JSONResponse:
    """Return all tracker rows from applications.md as JSON."""
    apps_md = _career_ops() / "data" / "applications.md"
    return JSONResponse(data.parse_applications(apps_md))


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
    }


# Mount the SPA last so /api/* routes take precedence. html=True serves
# index.html at "/" and for unknown paths (client-side routing friendly).
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
