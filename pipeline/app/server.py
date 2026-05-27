"""FastAPI server for the local triage UI.

Serves a single-page frontend on localhost and a small JSON API over the
evaluation results. Read operations (list jobs, view report), status write-back
(kanban), cloud operations via the gh CLI (refresh, trigger a run, push status),
and guided onboarding (generate profile artifacts + write GitHub secrets).

Run:
    uvicorn pipeline.app.server:app --port 8000
or use the run-ui.sh / run-ui.ps1 launchers.
"""

import base64
import json
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline.app import data, gh, onboard

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
EASY_APPLY_WORKFLOW = "easy-apply-pipeline.yml"
EDIT_WORKFLOW = "edit-tracker.yml"

# Both pipelines upload the same `pipeline-output-*` artifact. Refresh/push pull
# whichever ran (successfully) most recently — the easy-apply pipeline fires
# several times a day, so it's frequently newer than the daily one.
PIPELINE_WORKFLOWS = [DAILY_WORKFLOW, EASY_APPLY_WORKFLOW]

# Pending status changes (kanban drags) the user hasn't pushed yet, keyed by
# tracker number → canonical status. Persisted so they survive a server
# restart mid-triage; cleared on a successful push.
OVERRIDES_FILE = ROOT / ".ui-cache" / "status-overrides.json"


def _load_overrides() -> dict:
    if OVERRIDES_FILE.exists():
        try:
            return json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_overrides(d: dict) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


class StatusChange(BaseModel):
    num: str
    status: str


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
    back to raw tracker-additions when the merge step hasn't run. Overlays any
    un-pushed status overrides so the board reflects pending edits across page
    reloads. Response: {"rows": [...], "source": ..., "pending": N}."""
    payload = data.load_jobs(_career_ops())
    overrides = _load_overrides()
    if overrides:
        for row in payload["rows"]:
            ov = overrides.get(str(row.get("num")))
            if ov:
                row["status"] = ov
                row["status_canonical"] = data.canonical_status(ov)
                row["pending"] = True
    payload["pending"] = len(overrides)
    return JSONResponse(payload)


@app.post("/api/status")
def set_status(change: StatusChange) -> JSONResponse:
    """Record a pending status change (from a kanban drag). Doesn't touch any
    file or the cloud yet — that happens on push. Validates against the
    canonical states so a typo can't poison the tracker."""
    if change.status not in data.CANONICAL_STATES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown status {change.status!r}. Valid: {', '.join(data.CANONICAL_STATES)}",
        )
    overrides = _load_overrides()
    overrides[str(change.num)] = change.status
    _save_overrides(overrides)
    return JSONResponse({"ok": True, "pending": len(overrides)})


@app.post("/api/push-status")
def push_status() -> JSONResponse:
    """Push pending status changes to the cloud tracker via the edit-tracker
    workflow.

    Guard against clobbering cloud-added rows: we first pull a FRESH base
    (download the latest pipeline artifact's applications.md), apply the
    pending status overrides onto *that*, and push the merged result. So rows
    the pipeline added since the last refresh are preserved, and the user's
    status edits win on the rows they touched. Falls back to the local
    applications.md only if the refresh can't be done (no runs / gh error)."""
    overrides = _load_overrides()
    if not overrides:
        raise HTTPException(status_code=400, detail="No pending status changes to push.")

    global _active_data_dir
    base_text = None
    base_source = "local"

    # Try to refresh a fresh base first (the clobber guard).
    try:
        run = gh.latest_successful_run(PIPELINE_WORKFLOWS)
        if run is not None:
            if UI_CACHE.exists():
                shutil.rmtree(UI_CACHE)
            data_dir = gh.download_artifact(run["databaseId"], UI_CACHE)
            _active_data_dir = data_dir
            apps = data_dir / "data" / "applications.md"
            if apps.exists():
                base_text = apps.read_text(encoding="utf-8")
                base_source = "refreshed"
    except gh.GhError:
        pass  # fall through to local base

    if base_text is None:
        apps = _career_ops() / "data" / "applications.md"
        if not apps.exists():
            raise HTTPException(
                status_code=409,
                detail="No applications.md to update (run the pipeline so the tracker exists).",
            )
        base_text = apps.read_text(encoding="utf-8")

    # Apply the pending overrides onto the base (line-level, minimal diff).
    for num, status in overrides.items():
        base_text = data.set_status_in_text(base_text, num, status)

    # Persist the merged tracker locally so the UI is consistent post-push.
    apps = _career_ops() / "data" / "applications.md"
    apps.parent.mkdir(parents=True, exist_ok=True)
    apps.write_text(base_text, encoding="utf-8")

    # Dispatch edit-tracker with the merged tracker.
    b64 = base64.b64encode(base_text.encode("utf-8")).decode("ascii")
    try:
        gh.trigger_workflow(EDIT_WORKFLOW, {"applications_md_b64": b64})
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))

    count = len(overrides)
    _save_overrides({})  # clear pending on success
    return JSONResponse({"ok": True, "pushed": count, "base": base_source})


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
    """Download the most recent successful pipeline artifact (daily or
    easy-apply, whichever ran later) via gh and point the data layer at it, so
    the user can pull fresh cloud results without leaving the UI. Returns the
    run that was downloaded."""
    global _active_data_dir
    try:
        run = gh.latest_successful_run(PIPELINE_WORKFLOWS)
        if run is None:
            raise HTTPException(status_code=404, detail="No successful pipeline runs found yet.")
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


# ── Onboarding (Phase 3) ───────────────────────────────────────────────────

@app.get("/onboard", response_class=HTMLResponse)
def onboard_page() -> FileResponse:
    """Serve the onboarding wizard SPA."""
    page = STATIC_DIR / "onboard.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="onboard.html not found")
    return FileResponse(str(page))


@app.get("/api/onboard/status")
def onboard_status() -> JSONResponse:
    """Report the target repo, its visibility, and which required secrets are
    already set — so the UI can show 'needs setup' vs 'already configured'."""
    try:
        repo = gh.current_repo()
        visibility = gh.repo_visibility()
        present = set(gh.list_secret_names())
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))
    required_present = [s for s in onboard.REQUIRED_SECRETS if s in present]
    has_provider = any(v in present for v in onboard.PROVIDER_SECRETS.values())
    ready = len(required_present) == len(onboard.REQUIRED_SECRETS) and has_provider
    return JSONResponse({
        "repo": repo,
        "visibility": visibility,
        "secrets_present": sorted(present),
        "ready": ready,
    })


@app.post("/api/onboard")
async def onboard_submit(
    resume: UploadFile = File(...),
    form: str = Form(...),
) -> JSONResponse:
    """Generate the profile artifacts from the uploaded resume + form answers and
    write them as GitHub secrets. Refuses to write to a public repo."""
    try:
        payload = json.loads(form)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="form field is not valid JSON")

    provider = (payload.get("provider") or "").strip().lower()
    api_key = (payload.get("api_key") or "").strip()
    if provider and provider not in onboard.PROVIDER_SECRETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {provider!r}. Valid: {', '.join(onboard.PROVIDER_SECRETS)}",
        )

    # Privacy guard: never write secrets to a public repo.
    try:
        visibility = gh.repo_visibility()
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if visibility == "PUBLIC":
        raise HTTPException(
            status_code=409,
            detail=("Target repo is PUBLIC. Make your fork private before onboarding "
                    "so your job search stays off the public Actions tab."),
        )

    # Extract resume text + persist the PDF (local keyword scoring) and .txt.
    pdf_bytes = await resume.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="resume file is empty")
    try:
        resume_text = onboard.extract_pdf_text(pdf_bytes)
    except Exception as e:  # pdfplumber raises various errors on bad PDFs
        raise HTTPException(status_code=400, detail=f"could not read PDF: {e}")
    if not resume_text.strip():
        raise HTTPException(
            status_code=400,
            detail="No text found in the PDF (is it a scanned image?).",
        )

    resumes_dir = ROOT / "resumes"
    resumes_dir.mkdir(parents=True, exist_ok=True)
    (resumes_dir / "resume.pdf").write_bytes(pdf_bytes)
    (resumes_dir / "resume.txt").write_text(resume_text, encoding="utf-8")

    # Generate artifacts via the shared node generator, then collect base64.
    try:
        onboard.run_generation(ROOT, onboard.build_onboarding_json(payload, resume_text))
        blobs = onboard.collect_secret_blobs(ROOT)
    except onboard.OnboardError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Write the artifact secrets, then the provider key, then optional vars.
    written: list[str] = []
    try:
        for name, b64 in blobs.items():
            gh.set_secret(name, b64)
            written.append(name)
        if provider and api_key:
            secret = onboard.PROVIDER_SECRETS[provider]
            gh.set_secret(secret, api_key)
            written.append(secret)
            gh.set_variable("BATCH_PROVIDER", provider)
            model = (payload.get("batch_model") or "").strip()
            if model:
                gh.set_variable("BATCH_MODEL", model)
    except gh.GhError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Wrote {written} but then gh failed: {e}",
        )

    return JSONResponse({"ok": True, "repo": gh.current_repo(), "secrets_written": written})


# Mount the SPA last so /api/* routes take precedence. html=True serves
# index.html at "/" and for unknown paths (client-side routing friendly).
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
