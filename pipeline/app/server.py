"""FastAPI server for the local triage UI.

Serves a single-page frontend on localhost and a small JSON API over the
evaluation results. Read operations (list jobs, view report), status write-back
(kanban), cloud operations via the gh CLI (refresh, trigger a run, push status),
and guided onboarding (generate profile artifacts + write GitHub secrets).

Run:
    uvicorn pipeline.app.server:app --port 8000
or use the run-ui.sh / run-ui.ps1 launchers.
"""

import datetime
import json
import os
import shutil
import threading
import urllib.parse
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline.app import data, gh, local_run, onboard, skills
from pipeline._batch_common import (
    build_system_prompt,
    build_user_message,
    max_report_num,
    max_tracker_num,
    write_job_result,
    run_merge_tracker,
)
from pipeline.batch_evaluate import (
    _build_caller,
    _call_with_retry,
    _detect_provider,
    PROVIDER_DEFAULTS,
)
from pipeline.screen import extract_description, fetch_and_classify, linkedin_guest_jd_url

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Load .env so JOB_SEARCH_REPO, API keys, BATCH_PROVIDER, etc. are visible to
# the server. Pipeline scripts call load_dotenv themselves; the server didn't,
# which meant .env values were silently ignored unless set in the shell.
# override=False so a shell-level export always wins over .env.
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=False)

# Where `gh run download` drops artifacts. Gitignored. When a Refresh has
# populated this, the data layer reads from here; otherwise it falls back to
# CAREER_OPS_PATH (a local run, or a manually-passed --data dir).
UI_CACHE = ROOT / ".ui-cache" / "latest"

# Set by /api/refresh to the artifact subdir gh extracted, so subsequent reads
# use freshly-downloaded data without restarting the server.
_active_data_dir: Path | None = None

# Background add-job tasks: job_id → {status, result?, error?}
_add_job_tasks: dict[str, dict] = {}
_add_job_lock = threading.Lock()

# Cloud workflow filenames (must match .github/workflows/*.yml).
DAILY_WORKFLOW = "daily-pipeline.yml"
EASY_APPLY_WORKFLOW = "easy-apply-pipeline.yml"
EDIT_WORKFLOW = "edit-tracker.yml"

# Both pipelines upload the same `pipeline-output-*` artifact. Refresh/push pull
# whichever ran (successfully) most recently — the easy-apply pipeline fires
# several times a day, so it's frequently newer than the daily one.
PIPELINE_WORKFLOWS = [DAILY_WORKFLOW, EASY_APPLY_WORKFLOW]

# Pending status changes the user hasn't pushed yet, keyed by tracker number →
# canonical status. Written by kanban drags here AND by the apply stage when it
# auto-submits (data.record_status_override) — one channel, one path constant.
# Persisted so they survive a server restart mid-triage; cleared on push.
OVERRIDES_FILE = data.STATUS_OVERRIDES_FILE
# Overrides that have been dispatched to the cloud but aren't yet reflected in
# a pipeline artifact. Applied on every job load so statuses survive Refresh
# and restarts; self-cleans entry-by-entry once the artifact catches up.
PUSHED_OVERRIDES_FILE = ROOT / ".ui-cache" / "pushed-overrides.json"


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


def _load_pushed_overrides() -> dict:
    if PUSHED_OVERRIDES_FILE.exists():
        try:
            return json.loads(PUSHED_OVERRIDES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_pushed_overrides(d: dict) -> None:
    PUSHED_OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUSHED_OVERRIDES_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


class StatusChange(BaseModel):
    num: str
    status: str


def _career_ops_local() -> Path:
    """The local career-ops install (CAREER_OPS_PATH, resolved like
    orchestrate.py does). This is where cv.md / config / modes / output live —
    a downloaded artifact only carries reports/ + data/, so skills that need the
    CV must read from here, not from a Refresh artifact."""
    raw = os.environ.get("CAREER_OPS_PATH") or "career-ops"
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _career_ops() -> Path:
    """Resolve the data directory for reads (jobs, reports). A successful
    Refresh wins; otherwise the local install."""
    return _active_data_dir if _active_data_dir is not None else _career_ops_local()


app = FastAPI(title="job-search-pipeline UI")

# ── Cross-origin guard ───────────────────────────────────────────────────────
# This server binds to localhost, but "localhost" is reachable from any web page
# the user has open: a malicious site could fire a cross-origin `fetch` at
# http://localhost:8000 and trigger secret writes, cloud runs, or skill
# subprocesses (CSRF). Browsers attach an `Origin` header to such cross-origin
# requests, so we refuse any state-changing request whose Origin is present and
# not loopback. Same-origin SPA calls send a loopback Origin (allowed); non-
# browser clients (curl, tests) send no Origin (allowed).
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_origin(origin: str) -> bool:
    try:
        return urllib.parse.urlsplit(origin).hostname in _LOOPBACK_HOSTS
    except ValueError:
        return False


@app.middleware("http")
async def _same_origin_guard(request, call_next):
    if request.method in _MUTATING_METHODS:
        origin = request.headers.get("origin")
        if origin and not _is_loopback_origin(origin):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin request refused (this UI is localhost-only)."},
            )
    return await call_next(request)


@app.get("/api/jobs")
def list_jobs() -> JSONResponse:
    """Return tracker rows as JSON. Prefers the merged applications.md, falls
    back to raw tracker-additions when the merge step hasn't run. Overlays any
    un-pushed status overrides so the board reflects pending edits across page
    reloads. Response: {"rows": [...], "source": ..., "pending": N}."""
    payload = data.load_jobs(_career_ops())
    rows = payload["rows"]

    # Apply pushed-overrides: changes dispatched to the cloud that aren't yet in
    # a pipeline artifact. Self-clean any entry where the artifact has caught up.
    pushed = _load_pushed_overrides()
    if pushed:
        caught_up: set[str] = set()
        for row in rows:
            num = str(row.get("num"))
            if num in pushed:
                if row.get("status_canonical") == pushed[num]:
                    caught_up.add(num)
                else:
                    row["status"] = pushed[num]
                    row["status_canonical"] = pushed[num]
        if caught_up:
            for num in caught_up:
                del pushed[num]
            _save_pushed_overrides(pushed)

    # Apply pending-overrides: local kanban drags not yet pushed.
    overrides = _load_overrides()
    if overrides:
        for row in rows:
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

    # Dispatch edit-tracker with only the pending overrides — avoids GitHub's
    # workflow_dispatch input size limit that the full base64 tracker can exceed.
    try:
        gh.trigger_workflow(EDIT_WORKFLOW, {"status_overrides_json": json.dumps(overrides)})
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))

    count = len(overrides)
    _save_overrides({})  # clear pending on success
    # Persist dispatched overrides so they survive Refresh and restarts until
    # the pipeline produces a new artifact that already has the correct statuses.
    pushed = _load_pushed_overrides()
    pushed.update(overrides)
    _save_pushed_overrides(pushed)
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


# ── local pipeline run ──────────────────────────────────────────────────────

class LocalRunRequest(BaseModel):
    passes: str = "all"        # "all" | "easy-only" | "no-easy"
    evaluate: bool = True


@app.post("/api/run-local")
def run_local(req: LocalRunRequest) -> JSONResponse:
    """Start a local pipeline run (orchestrate.py subprocess). Single-flight:
    409 when one is already running. Poll /api/run-local/status for progress;
    on success the UI switches to the local tracker via /api/use-local."""
    if req.passes not in ("all", "easy-only", "no-easy"):
        raise HTTPException(status_code=400, detail=f"unknown passes value: {req.passes}")
    try:
        state = local_run.start({"passes": req.passes, "evaluate": req.evaluate})
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"could not start the pipeline: {e}")
    # NB: status()'s own "ok" field means "run succeeded" (None while running) —
    # the request-level acknowledgement gets its own key.
    return JSONResponse({"started": True, **state})


@app.get("/api/run-local/status")
def run_local_status() -> JSONResponse:
    return JSONResponse(local_run.status())


@app.post("/api/run-local/cancel")
def run_local_cancel() -> JSONResponse:
    return JSONResponse(local_run.cancel())


@app.post("/api/use-local")
def use_local() -> JSONResponse:
    """Point the data layer back at the LOCAL career-ops (the default source).
    Used after a local pipeline run so its fresh results are what the UI
    shows, instead of a previously downloaded cloud artifact."""
    global _active_data_dir
    _active_data_dir = None
    return JSONResponse({"ok": True, "career_ops": str(_career_ops_local())})


# ── career-ops skills ──────────────────────────────────────────────────────

class SkillRequest(BaseModel):
    skill: str
    num: str
    path: str  # "api" | "cli"


def _find_role(num: str) -> dict | None:
    for row in data.load_jobs(_career_ops())["rows"]:
        if str(row.get("num")) == str(num):
            return row
    return None


@app.get("/api/capabilities")
def capabilities() -> JSONResponse:
    """Report which skill execution paths are available (agent CLI / API key),
    the user's preferred default, and the skill catalog, so the UI can render
    actions and route or prompt per skill."""
    return JSONResponse(skills.capabilities())


@app.post("/api/skills/run")
def run_skill(req: SkillRequest) -> JSONResponse:
    """Run a career-ops skill for a triaged role.

    `path="cli"` returns a ready-to-run command for the user's agent (we don't
    spawn it) — works for every skill. `path="api"` runs a bounded synchronous
    provider call and is only valid for skills that declare `api=True`
    (currently résumé-markdown); CLI-only skills reject it with guidance."""
    spec = skills.SKILLS.get(req.skill)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill {req.skill!r}.")
    role = _find_role(req.num)
    if role is None:
        raise HTTPException(status_code=404, detail=f"No triaged role #{req.num}.")
    company = role.get("company") or "company"
    title = role.get("role") or "role"
    # Resolve the report to an absolute path from the *active* data dir — it may
    # live in a Refresh artifact cache, not under career-ops/reports/.
    report_file = data.find_report_file(_career_ops() / "reports", role.get("report_num", ""))

    if req.path == "cli":
        if not skills.cli_available():
            raise HTTPException(
                status_code=400,
                detail=f"No agent CLI found (looked for '{skills.cli_name()}'). "
                       "Install one or set BATCH_CLI"
                       + ("." if not spec["api"] else ", or use the API path."),
            )
        return JSONResponse({
            "ok": True, "path": "cli",
            "command": skills.skill_command(req.skill, report_file, company, title),
            "cwd": "career-ops",
            "prereqs": spec.get("prereqs", []),
        })

    if req.path == "api":
        if not spec["api"]:
            raise HTTPException(
                status_code=400,
                detail=f"'{spec['label']}' is CLI-only (it needs agent tools the "
                       "API path can't provide). Use the CLI path.",
            )
        provider = skills.detect_provider()
        if provider is None:
            raise HTTPException(
                status_code=400,
                detail="No LLM API key configured. Set one (e.g. GEMINI_API_KEY) "
                       "or use the CLI path.",
            )
        # The report (resolved above) is the JD signal.
        role_context = report_file.read_text(encoding="utf-8") if report_file else \
            f"# {company} — {title}\n(No evaluation report on disk; tailor from the CV only.)"
        try:
            out = skills.tailor_resume_markdown(
                _career_ops_local(), role_context, company,
                provider, os.environ.get("BATCH_MODEL") or None,
            )
        except skills.SkillError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:  # provider/SDK failure
            raise HTTPException(status_code=502, detail=f"Provider call failed: {e}")
        return JSONResponse({
            "ok": True, "path": "api", "provider": provider,
            "output_file": out.name,
            "download_url": f"/api/skills/output/{out.name}",
        })

    raise HTTPException(status_code=400, detail="path must be 'api' or 'cli'.")


class SkillLaunchRequest(BaseModel):
    skill: str
    num: str


@app.post("/api/skills/launch")
def launch_skill(req: SkillLaunchRequest) -> JSONResponse:
    """Open a new terminal window and run the skill's CLI hand-off command —
    one-click alternative to copy/paste. The command is rebuilt server-side
    (same logic as the CLI path of /api/skills/run), so the client can't smuggle
    arbitrary commands; the new window is visible and the user can kill it."""
    if not skills.terminal_available():
        raise HTTPException(
            status_code=501,
            detail="Run-in-terminal isn't wired up on this OS yet. Use Copy command.",
        )
    if req.skill not in skills.SKILLS:
        raise HTTPException(status_code=404, detail=f"Unknown skill {req.skill!r}.")
    if not skills.cli_available():
        raise HTTPException(
            status_code=400,
            detail=f"No agent CLI found (looked for '{skills.cli_name()}'). "
                   "Install one or set BATCH_CLI.",
        )
    role = _find_role(req.num)
    if role is None:
        raise HTTPException(status_code=404, detail=f"No triaged role #{req.num}.")
    company = role.get("company") or "company"
    title = role.get("role") or "role"
    report_file = data.find_report_file(_career_ops() / "reports", role.get("report_num", ""))
    command = skills.skill_command(req.skill, report_file, company, title)
    # Launch from the repo root so the `cd career-ops` at the start of the
    # command resolves consistently regardless of where the UI was launched.
    try:
        info = skills.launch_in_terminal(command, str(ROOT))
    except skills.SkillError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except OSError as e:  # rare: temp-file or Popen problem
        raise HTTPException(status_code=502, detail=f"Couldn't open a terminal: {e}")
    return JSONResponse({
        "ok": True, "launched": True, "command": command, **info,
    })


@app.get("/api/skills/output/{filename}")
def skill_output(filename: str) -> FileResponse:
    """Download a generated skill artifact from the local career-ops output/."""
    safe = Path(filename).name  # strip any path components (traversal guard)
    f = _career_ops_local() / "output" / safe
    if not f.exists():
        raise HTTPException(status_code=404, detail="Output file not found.")
    return FileResponse(str(f), filename=safe, media_type="text/markdown")


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


@app.get("/api/onboard/load-config")
def onboard_load_config() -> JSONResponse:
    """Return the last-submitted onboarding form (minus api_key) so the wizard
    can prefill every field on a revisit. Returns {"form": null, "has_resume":
    false} on a first-time setup so the UI knows it's not in edit mode."""
    resume_present = (ROOT / "resumes" / "resume.pdf").exists()
    return JSONResponse({
        "form": onboard.load_sidecar(ROOT),
        "has_resume": resume_present,
    })


@app.post("/api/onboard/parse-resume")
async def onboard_parse_resume(resume: UploadFile = File(...)) -> JSONResponse:
    """Extract contact details from an uploaded resume PDF to autofill the
    onboarding 'About' step. Pure local parse — no gh calls, nothing written."""
    pdf_bytes = await resume.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="resume file is empty")
    try:
        text = onboard.extract_pdf_text(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not read PDF: {e}")
    return JSONResponse(onboard.parse_resume_info(text))


_KNOWN_CLIS = ["claude", "gemini", "opencode", "qwen"]
_KNOWN_PROVIDERS = set(onboard.PROVIDER_SECRETS) | {"ollama"}


@app.get("/api/onboard/providers")
def get_providers() -> JSONResponse:
    """Return detected local LLM providers (API keys present in env) and
    installed CLI tools so the setup wizard can show what's available for
    local Add Job evaluations."""
    api_providers = []
    for name, key_var in onboard.PROVIDER_SECRETS.items():
        api_providers.append({
            "name": name,
            "configured": bool(os.environ.get(key_var, "").strip()),
            "key_var": key_var,
            "default_model": PROVIDER_DEFAULTS.get(name, ""),
        })
    api_providers.append({
        "name": "ollama",
        "configured": bool(os.environ.get("OLLAMA_BASE_URL", "").strip()),
        "key_var": "OLLAMA_BASE_URL",
        "default_model": PROVIDER_DEFAULTS.get("ollama", "qwen2.5:32b"),
    })
    cli_tools = [
        {"name": name, "available": shutil.which(name) is not None}
        for name in _KNOWN_CLIS
    ]
    return JSONResponse({
        "api_providers": api_providers,
        "cli_tools": cli_tools,
        "current": {
            "batch_provider": os.environ.get("BATCH_PROVIDER", ""),
            "batch_model": os.environ.get("BATCH_MODEL", ""),
            "batch_cli": os.environ.get("BATCH_CLI", "claude"),
        },
        "provider_defaults": dict(PROVIDER_DEFAULTS),
    })


class LocalConfigRequest(BaseModel):
    batch_provider: str = ""
    batch_model: str = ""
    batch_cli: str = ""
    api_key: str = ""   # optional — write the provider's API key to .env too


@app.post("/api/onboard/local-config")
def save_local_config(req: LocalConfigRequest) -> JSONResponse:
    """Write BATCH_PROVIDER, BATCH_MODEL, BATCH_CLI (and optionally the
    provider API key) to the local .env and update os.environ immediately."""
    from dotenv import set_key, unset_key

    provider = req.batch_provider.strip().lower()
    if provider and provider not in _KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {provider!r}. Valid: {', '.join(sorted(_KNOWN_PROVIDERS))}",
        )
    cli = req.batch_cli.strip()
    if cli and cli not in _KNOWN_CLIS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown CLI {cli!r}. Valid: {', '.join(_KNOWN_CLIS)}",
        )

    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text("", encoding="utf-8")

    updated: list[str] = []

    def _set(key: str, value: str) -> None:
        if value:
            set_key(str(env_path), key, value, quote_mode="never")
            os.environ[key] = value
        else:
            unset_key(str(env_path), key)
            os.environ.pop(key, None)
        updated.append(key)

    _set("BATCH_PROVIDER", provider)
    _set("BATCH_MODEL", req.batch_model.strip())
    if cli:
        _set("BATCH_CLI", cli)
    # Write the provider's API key to .env so the server detects it immediately.
    api_key = req.api_key.strip()
    if api_key and provider and provider in onboard.PROVIDER_SECRETS:
        _set(onboard.PROVIDER_SECRETS[provider], api_key)

    return JSONResponse({"ok": True, "updated": updated})


@app.post("/api/onboard")
async def onboard_submit(
    form: str = Form(...),
    resume: UploadFile = File(default=None),
) -> JSONResponse:
    """Generate the profile artifacts from form answers (and optionally a new
    resume) and write them as GitHub secrets. Refuses to write to a public repo.

    Resume + API key are optional on re-submit. If no new resume is uploaded
    and resumes/resume.pdf already exists locally, we reuse it (the user is
    just tweaking config). Same for the API key: a blank value keeps whatever
    is already in GitHub Secrets, avoiding a forced re-paste every edit."""
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

    resumes_dir = ROOT / "resumes"
    pdf_path = resumes_dir / "resume.pdf"
    txt_path = resumes_dir / "resume.txt"

    # Resume: prefer a freshly-uploaded PDF; fall back to the persisted one
    # from a prior submit. UploadFile is always non-None when the multipart
    # field exists, so check filename/size rather than identity.
    pdf_bytes = await resume.read() if resume is not None else b""
    if pdf_bytes:
        try:
            resume_text = onboard.extract_pdf_text(pdf_bytes)
        except Exception as e:  # pdfplumber raises various errors on bad PDFs
            raise HTTPException(status_code=400, detail=f"could not read PDF: {e}")
        if not resume_text.strip():
            raise HTTPException(
                status_code=400,
                detail="No text found in the PDF (is it a scanned image?).",
            )
        resumes_dir.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(pdf_bytes)
        txt_path.write_text(resume_text, encoding="utf-8")
    elif txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")
    else:
        raise HTTPException(
            status_code=400,
            detail=("No resume on file — upload a PDF on the first onboarding "
                    "step before submitting."),
        )

    # Generate artifacts via the shared node generator, then collect base64.
    try:
        onboard.run_generation(ROOT, onboard.build_onboarding_json(payload, resume_text))
        blobs = onboard.collect_secret_blobs(ROOT)
    except onboard.OnboardError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Write the artifact secrets, then the provider key (if supplied), then
    # optional vars. Blank api_key means "keep whatever's already in Secrets" —
    # supports the common edit-mode flow where the user only changed search
    # settings, not their provider.
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

    # Sidecar the (non-sensitive parts of the) payload so the next wizard
    # visit can prefill instead of forcing a full re-fill.
    onboard.save_sidecar(ROOT, payload)

    return JSONResponse({"ok": True, "repo": gh.current_repo(), "secrets_written": written})


def _fetch_jd(url: str, timeout: int = 20) -> str:
    """Fetch and extract the job description text from a URL.

    Uses the LinkedIn guest endpoint for LinkedIn jobs (bypasses the login
    wall). Returns empty string on any fetch failure so the LLM can still
    evaluate with the URL and metadata alone."""
    fetch_url = linkedin_guest_jd_url(url) or url
    try:
        _, _, html_body = fetch_and_classify(fetch_url, timeout=timeout)
        return extract_description(html_body)
    except Exception:
        return ""


def _load_eval_system_prompt() -> str:
    """Load the evaluation system prompt from the local career-ops install."""
    co = _career_ops_local()

    def _opt(p: Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""

    cv = _opt(co / "cv.md")
    profile_yml = _opt(co / "config" / "profile.yml")
    if not cv and not profile_yml:
        raise HTTPException(
            status_code=409,
            detail=(
                "cv.md and profile.yml not found in career-ops. "
                "Complete the Setup wizard first."
            ),
        )
    return build_system_prompt(
        cv=cv,
        profile_yml=profile_yml,
        profile_md=_opt(co / "modes" / "_profile.md"),
        article_digest=_opt(co / "article-digest.md"),
    )


class AddJobRequest(BaseModel):
    url: str
    company: str = ""
    role: str = ""


@app.post("/api/jobs/add")
def add_job(req: AddJobRequest) -> JSONResponse:
    """Fetch, evaluate, and add a manually-entered job to the tracker.

    Reuses the same LLM evaluation prompt and result-writing pipeline as the
    batch evaluator so the output is indistinguishable from a pipeline-sourced
    role. The request is synchronous — evaluation takes 20-60 s depending on
    the provider."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="url is required")

    provider = _detect_provider()
    if not provider:
        raise HTTPException(
            status_code=503,
            detail="No LLM provider configured. Use ⚙ Setup → Local evaluation to pick one, or add an API key to .env.",
        )

    # Fetch job description.
    jd_text = _fetch_jd(req.url.strip())

    # Load evaluation system prompt from local career-ops profile.
    system = _load_eval_system_prompt()

    # Assign the next available tracker and report numbers.
    co = _career_ops_local()
    apps_md = co / "data" / "applications.md"
    reports_dir = co / "reports"
    tracker_dir = co / "batch" / "tracker-additions"

    tracker_num = max_tracker_num(apps_md, {}) + 1
    report_num  = max_report_num(reports_dir, {}) + 1
    report_num_str = str(report_num).zfill(3)
    today = datetime.date.today().isoformat()
    job_id = f"manual-{tracker_num}"

    job_meta = {
        "id":          job_id,
        "url":         req.url.strip(),
        "company":     req.company.strip(),
        "role":        req.role.strip(),
        "report_num":  report_num_str,
        "tracker_num": tracker_num,
        "jd_text":     jd_text,
    }

    # Evaluate via LLM.
    model = os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider]
    caller = _build_caller(provider, model)
    user = build_user_message(job_meta, today)
    try:
        response_text = _call_with_retry(caller, system, user)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM evaluation failed: {exc}")

    # Write report .md and tracker-additions .tsv.
    reports_dir.mkdir(parents=True, exist_ok=True)
    tracker_dir.mkdir(parents=True, exist_ok=True)
    result = write_job_result(response_text, job_meta, reports_dir, tracker_dir, today)

    # Merge into applications.md. Failure is non-fatal — the tracker-additions
    # file is written and the next pipeline run will pick it up.
    merged = run_merge_tracker(co)

    return JSONResponse({
        "ok":          True,
        "report_num":  result["summary"].get("report_num", report_num_str),
        "tracker_num": tracker_num,
        "company":     result["summary"].get("company", req.company),
        "role":        result["summary"].get("role", req.role),
        "score":       result["summary"].get("score"),
        "provider":    provider,
        "merged":      merged,
    })


def _run_add_job(job_id: str, url: str, company: str, role: str) -> None:
    try:
        jd_text = _fetch_jd(url)
        system = _load_eval_system_prompt()

        co = _career_ops_local()
        apps_md = co / "data" / "applications.md"
        reports_dir = co / "reports"
        tracker_dir = co / "batch" / "tracker-additions"

        tracker_num = max_tracker_num(apps_md, {}) + 1
        report_num  = max_report_num(reports_dir, {}) + 1
        report_num_str = str(report_num).zfill(3)
        today = datetime.date.today().isoformat()

        job_meta = {
            "id":          f"manual-{tracker_num}",
            "url":         url,
            "company":     company,
            "role":        role,
            "report_num":  report_num_str,
            "tracker_num": tracker_num,
            "jd_text":     jd_text,
        }

        provider = _detect_provider()
        model = os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider]
        caller = _build_caller(provider, model)
        user = build_user_message(job_meta, today)
        response_text = _call_with_retry(caller, system, user)

        reports_dir.mkdir(parents=True, exist_ok=True)
        tracker_dir.mkdir(parents=True, exist_ok=True)
        result = write_job_result(response_text, job_meta, reports_dir, tracker_dir, today)
        run_merge_tracker(co)

        # If the UI is currently showing a cloud-artifact directory (_active_data_dir),
        # list_jobs reads from there instead of the local career-ops path. Copy the
        # updated applications.md and the new report into that directory so the
        # next loadJobs() call sees the newly added entry.
        if _active_data_dir is not None:
            local_apps = co / "data" / "applications.md"
            cache_apps = _active_data_dir / "data" / "applications.md"
            if local_apps.exists():
                cache_apps.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_apps, cache_apps)
            if result.get("report_file"):
                cache_reports = _active_data_dir / "reports"
                cache_reports.mkdir(parents=True, exist_ok=True)
                local_report = reports_dir / result["report_file"]
                if local_report.exists():
                    shutil.copy2(local_report, cache_reports / result["report_file"])

        with _add_job_lock:
            _add_job_tasks[job_id] = {
                "status":      "done",
                "result": {
                    "report_num":  result["summary"].get("report_num", report_num_str),
                    "tracker_num": tracker_num,
                    "company":     result["summary"].get("company", company),
                    "role":        result["summary"].get("role", role),
                    "score":       result["summary"].get("score"),
                },
            }
    except Exception as exc:
        with _add_job_lock:
            _add_job_tasks[job_id] = {"status": "error", "error": str(exc)}


@app.post("/api/jobs/add-async")
def add_job_async(req: AddJobRequest) -> JSONResponse:
    """Start a background add-job evaluation and return immediately."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="url is required")

    provider = _detect_provider()
    if not provider:
        raise HTTPException(
            status_code=503,
            detail="No LLM provider configured. Use ⚙ Setup → Local evaluation to pick one, or add an API key to .env.",
        )

    job_id = str(uuid.uuid4())
    with _add_job_lock:
        _add_job_tasks[job_id] = {"status": "pending"}

    t = threading.Thread(
        target=_run_add_job,
        args=(job_id, req.url.strip(), req.company.strip(), req.role.strip()),
        daemon=True,
    )
    t.start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/add-status/{job_id}")
def add_job_status(job_id: str) -> JSONResponse:
    with _add_job_lock:
        task = _add_job_tasks.get(job_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return JSONResponse(task)


# Mount the SPA last so /api/* routes take precedence. html=True serves
# index.html at "/" and for unknown paths (client-side routing friendly).
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
