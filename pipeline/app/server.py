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
import time
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
    env_float,
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
from pipeline import recheck

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
# use freshly-downloaded data without restarting the server. Guarded by a lock
# because push_status mutates it across a multi-second gh download while
# /api/use-local (auto-fired when a local run finishes) can null it — an
# unsynchronized interleave could make push write cloud data over the LOCAL
# tracker.
_active_data_dir: Path | None = None
_data_dir_lock = threading.Lock()

# Background add-job tasks: job_id → {status, result?, error?}
_add_job_tasks: dict[str, dict] = {}
_add_job_lock = threading.Lock()

# Apply-review sessions: job_id → task dict. The worker holds ONE live browser
# open between fill and submit (Playwright is thread-affine), so it blocks on the
# task's decision Event; the submit/cancel endpoints signal it. Single-flight:
# only one session may be non-terminal at a time (one visible browser).
_apply_tasks: dict[str, dict] = {}
_apply_lock = threading.Lock()
# Non-terminal statuses (block a new session AND are never evicted). "submitting"
# / "cancelling" are transient states the worker sets, under the lock, between
# the hold and the Playwright action — so a Cancel that races an in-flight submit
# sees a non-"ready" status and is rejected.
_APPLY_ACTIVE = frozenset({"pending", "ready", "submitting", "cancelling"})
# Cap on retained TERMINAL tasks so _apply_tasks doesn't grow unbounded over a
# long-lived server; apply_async prunes the oldest on each new session.
_APPLY_TASK_CAP = 50

# Cloud workflow filenames (must match .github/workflows/*.yml).
DAILY_WORKFLOW = "daily-pipeline.yml"
EASY_APPLY_WORKFLOW = "easy-apply-pipeline.yml"
EDIT_WORKFLOW = "edit-tracker.yml"

# Both pipelines upload the same `pipeline-output-*` artifact. Refresh/push pull
# whichever ran (successfully) most recently — the easy-apply pipeline fires
# several times a day, so it's frequently newer than the daily one.
PIPELINE_WORKFLOWS = [DAILY_WORKFLOW, EASY_APPLY_WORKFLOW]

# Pending status changes the user hasn't pushed yet. Written by kanban drags
# here AND by the apply stage when it auto-submits — one channel, owned by
# pipeline.app.data (atomic writes + an in-process lock). The server delegates
# rather than re-implementing read/modify/write, and resolves the path at call
# time so tests that redirect data.STATUS_OVERRIDES_FILE take effect.
# Overrides that have been dispatched to the cloud but aren't yet reflected in
# a pipeline artifact. Applied on every job load so statuses survive Refresh
# and restarts; self-cleans entry-by-entry once the artifact catches up.
PUSHED_OVERRIDES_FILE = ROOT / ".ui-cache" / "pushed-overrides.json"


def _load_overrides() -> dict:
    return data.load_status_overrides()


def _save_overrides(d: dict) -> None:
    data.save_status_overrides(d)


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


def _refuse_during_local_run() -> None:
    """409 if a local pipeline run is in progress. Add-job mints the next
    report/tracker number in THIS process while a local run's eval stage mints
    them in its subprocess — running both at once collides the numbering
    (overwritten reports, duplicate tracker rows). They must not overlap."""
    if local_run.is_running():
        raise HTTPException(
            status_code=409,
            detail="A local pipeline run is in progress. Wait for it to finish — "
                   "adding a job now would collide on report/tracker numbering.",
        )


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

    # Apply pending-overrides: local kanban drags + apply auto-submits not yet
    # pushed. A plain (num-keyed) override lands on the row with that num; an
    # identity-anchored one (from apply, whose num may be from a different
    # tracker) lands on the row matching its company/role — so it marks the row
    # actually applied to, not whichever row coincidentally shares the num.
    #
    # Prebuild O(1) lookups so the overlay is O(rows + overrides), not
    # O(rows x overrides): identity anchors are keyed by (norm_company, norm_role),
    # with a company-only anchor under (norm_company, "").
    overrides = _load_overrides()
    if overrides:
        by_num: dict[str, str] = {}
        by_identity: dict[tuple[str, str], str] = {}
        for key, value in overrides.items():
            ident = data.override_identity(value)
            if ident:
                company, role = ident
                by_identity[(data.normalize_company(company),
                             data.normalize_company(role))] = data.override_status(value)
            else:
                by_num[key] = data.override_status(value)
        for row in rows:
            status = by_num.get(str(row.get("num")))
            if status is None:
                nc = data.normalize_company(row.get("company", ""))
                status = (by_identity.get((nc, data.normalize_company(row.get("role", ""))))
                          or by_identity.get((nc, "")))
            if status is not None:
                row["status"] = status
                row["status_canonical"] = data.canonical_status(status)
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
    # Through the locked read/modify/write accessor so a concurrent push or
    # apply auto-submit can't lose this drag (or be lost by it).
    data.record_status_override(str(change.num), change.status)
    return JSONResponse({"ok": True, "pending": len(_load_overrides())})


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
    target_apps: Path | None = None   # the exact file base_text came from

    # Try to refresh a fresh base first (the clobber guard).
    try:
        run = gh.latest_successful_run(PIPELINE_WORKFLOWS)
        if run is not None:
            if UI_CACHE.exists():
                shutil.rmtree(UI_CACHE)
            data_dir = gh.download_artifact(run["databaseId"], UI_CACHE)
            with _data_dir_lock:
                _active_data_dir = data_dir
            apps = data_dir / "data" / "applications.md"
            if apps.exists():
                base_text = apps.read_text(encoding="utf-8")
                base_source = "refreshed"
                target_apps = apps
    except gh.GhError:
        pass  # fall through to local base

    if base_text is None:
        # Write back to the LOCAL install — never via _career_ops(), whose
        # _active_data_dir can flip to a cloud artifact mid-request (the
        # use-local auto-fire) and turn this persist into cloud-over-local
        # data loss. Capturing target_apps == the file we read pins the write.
        apps = _career_ops_local() / "data" / "applications.md"
        if not apps.exists():
            raise HTTPException(
                status_code=409,
                detail="No applications.md to update (run the pipeline so the tracker exists).",
            )
        base_text = apps.read_text(encoding="utf-8")
        target_apps = apps

    # Apply the overrides onto the base, resolving each identity-anchored
    # override to the correct num IN THIS base. An identity override that
    # doesn't resolve here (its company isn't in this tracker yet) is returned
    # in `unresolved` — NOT applied and NOT dispatched, so we never mark a
    # different company that merely shares the num. The cloud payload is always
    # {num: status} (what edit-tracker.yml consumes).
    # Persist the merged tracker ONLY to the durable local tracker. A downloaded
    # artifact copy is transient (re-downloaded on every Refresh), and editing it
    # makes the pushed-override bridge self-clean against a copy push itself
    # changed — so the status would appear to vanish on the next Refresh. For the
    # refreshed case the pushed-override overlay shows the change in the UI and
    # persists it until a genuinely fresh pipeline run incorporates it (which is
    # when the self-clean SHOULD fire). The cloud write goes via edit-tracker below.
    persist_local = base_source == "local"
    base_text, cloud_payload, unresolved = data.resolve_overrides_for_push(
        base_text, overrides, build_text=persist_local)
    if persist_local:
        target_apps.parent.mkdir(parents=True, exist_ok=True)
        target_apps.write_text(base_text, encoding="utf-8")

    # Dispatch edit-tracker with only the resolved overrides — avoids GitHub's
    # workflow_dispatch input size limit that the full base64 tracker can exceed.
    # Nothing resolved → nothing to push (don't fire an empty workflow run).
    if cloud_payload:
        try:
            gh.trigger_workflow(EDIT_WORKFLOW, {"status_overrides_json": json.dumps(cloud_payload)})
        except gh.GhError as e:
            raise HTTPException(status_code=502, detail=str(e))
        # Clear ONLY the keys we pushed (re-reading under the lock) so an apply
        # auto-submit recorded during the gh round-trip — and any identity
        # override that DIDN'T resolve — isn't silently dropped.
        pushed_keys = [k for k in overrides if k not in unresolved]
        data.clear_status_overrides(pushed_keys)
        # Persist dispatched overrides so they survive Refresh and restarts until
        # the pipeline produces a new artifact that already has the correct statuses.
        pushed = _load_pushed_overrides()
        pushed.update(cloud_payload)
        _save_pushed_overrides(pushed)
    return JSONResponse({"ok": True, "pushed": len(cloud_payload),
                         "unresolved": len(unresolved), "base": base_source})


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
        with _data_dir_lock:
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
    if req.passes not in local_run.VALID_PASSES:
        raise HTTPException(status_code=400, detail=f"unknown passes value: {req.passes}")
    # A pipeline run rewrites the tracker wholesale; a recheck sweep edits it
    # concurrently. Refuse to overlap (recheck refuses during a run too).
    with _recheck_lock:
        if _recheck_state.get("running"):
            raise HTTPException(status_code=409,
                                detail="A liveness re-check is in progress. Wait for it to "
                                       "finish — both write the tracker.")
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
    with _data_dir_lock:
        _active_data_dir = None
    return JSONResponse({"ok": True, "career_ops": str(_career_ops_local())})


# ── tracker liveness re-check ───────────────────────────────────────────────
# A background sweep that re-checks every Evaluated tracker role's liveness and
# marks closed ones Discarded — the UI counterpart of orchestrate's
# --recheck-liveness. Single-flight (one sweep at a time) and refused while a
# local pipeline run holds the tracker, since both mutate applications.md.
_recheck_lock = threading.Lock()
_recheck_state: dict = {}


def _recheck_idle() -> dict:
    return {"running": False, "started_at": None, "checked": 0, "total": None,
            "discarded": 0, "unconfirmed": 0, "throttled": 0, "deferred": 0,
            "dead": [], "done": False, "ok": None, "error": None}


def _run_recheck() -> None:
    """Worker: run the core sweep against the active tracker, folding live
    progress and the final summary into _recheck_state for the status poll."""
    def _progress(checked, total, discarded):
        with _recheck_lock:
            _recheck_state.update(checked=checked, total=total, discarded=discarded)
    try:
        summary = recheck.run(_career_ops(), progress=_progress)
        with _recheck_lock:
            _recheck_state.update(
                running=False, done=True, ok=True,
                checked=summary["checked"], discarded=summary["discarded"],
                unconfirmed=summary.get("unconfirmed", 0),
                throttled=summary.get("throttled", 0),
                deferred=summary.get("deferred", 0), dead=summary["dead"])
    except Exception as e:  # surface failure to the UI rather than hang on running
        with _recheck_lock:
            _recheck_state.update(running=False, done=True, ok=False, error=str(e))


@app.post("/api/recheck-liveness")
def recheck_liveness() -> JSONResponse:
    """Start a background liveness sweep of the Evaluated tracker roles. 409 if
    one is already running or a local pipeline run is in progress. Poll
    /api/recheck-liveness/status for progress + the dead list."""
    _refuse_during_local_run()
    with _recheck_lock:
        if _recheck_state.get("running"):
            raise HTTPException(status_code=409, detail="A liveness re-check is already running.")
        _recheck_state.clear()
        _recheck_state.update(_recheck_idle())
        _recheck_state.update(running=True, started_at=time.time())
        snapshot = dict(_recheck_state)
    threading.Thread(target=_run_recheck, daemon=True).start()
    return JSONResponse(snapshot)


@app.get("/api/recheck-liveness/status")
def recheck_liveness_status() -> JSONResponse:
    with _recheck_lock:
        return JSONResponse(dict(_recheck_state) if _recheck_state else _recheck_idle())


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
    _refuse_during_local_run()

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
    _refuse_during_local_run()

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


# ── apply review-and-submit ─────────────────────────────────────────────────

def _apply_hold_timeout() -> float:
    """Seconds to hold the browser open waiting for the user's Submit/Cancel
    (APPLY_HOLD_TIMEOUT env, default 300). After this the session auto-closes so
    a forgotten review never leaks a live browser."""
    return env_float("APPLY_HOLD_TIMEOUT", 300.0)


def _apply_job_for_num(num: str):
    """Build an ApplyJob from the tracker row with this num (the active data
    dir), pulling the posting URL from the notes cell. None if no such row."""
    from pipeline.apply.queue import ApplyJob
    for row in data.load_jobs(_career_ops())["rows"]:
        if str(row.get("num")) == str(num):
            return ApplyJob(
                num=str(row.get("num")),
                company=row.get("company", ""),
                role=row.get("role", ""),
                url=data.extract_url(row.get("notes", "")),
                score=row.get("score_value"),
                report_path=row.get("report_path", ""),
            )
    return None


def _set_apply(job_id: str, **fields) -> None:
    with _apply_lock:
        task = _apply_tasks.get(job_id)
        if task is not None:
            task.update(fields)


def _apply_decision(job_id: str) -> str | None:
    """The recorded decision for a session (read under the lock) — backs the
    worker's should_cancel hook."""
    with _apply_lock:
        task = _apply_tasks.get(job_id)
        return task.get("decision") if task else None


def _evict_apply_tasks() -> None:
    """Drop the oldest TERMINAL tasks beyond _APPLY_TASK_CAP. Called under
    _apply_lock; active sessions (pending/ready/submitting/cancelling) are kept."""
    terminal = [jid for jid, t in _apply_tasks.items()
                if t.get("status") not in _APPLY_ACTIVE]
    for jid in terminal[:max(0, len(terminal) - _APPLY_TASK_CAP)]:
        _apply_tasks.pop(jid, None)


# Only these task fields are returned to the client (the held ApplyJob / Event
# aren't JSON-serializable and stay server-side).
_APPLY_PUBLIC = ("status", "num", "company", "role", "answers", "needs_review", "code", "reason")


def _apply_public(task: dict) -> dict:
    return {k: task.get(k) for k in _APPLY_PUBLIC}


def _run_apply_review(job_id: str) -> None:
    """Worker: launch a visible browser, fill the Easy Apply form (bailing early
    if the user cancels mid-fill), stop at the Submit step and surface the
    drafted answers, then block until the user submits/cancels (or the hold times
    out). Submit clicks Submit and marks the row Applied; a closed posting marks
    it Discarded. The tracker path + local career-ops are captured at session
    start, so a mid-hold use-local/refresh flip can't misdirect the write."""
    task = _apply_tasks[job_id]
    job = task["job"]
    applications_md = task["applications_md"]
    co_local = task["co_local"]
    try:
        from pipeline.apply import browser, linkedin
        from pipeline.apply.result import CANCELLED, EXPIRED
        from pipeline import apply as apply_pkg
        report_root = applications_md.parent.parent
        with browser.launch(headless=False) as page:
            if not browser.ensure_logged_in(page, headless=False):
                _set_apply(job_id, status="failed", code="login_issue",
                           reason="not signed in to LinkedIn (run the UI's apply review "
                                  "and sign in to the window when prompted)")
                return
            engine = apply_pkg.build_engine(co_local, provider=None, model=None)
            apply_pkg.configure_engine_for_job(
                engine, job, career_ops=co_local, report_root=report_root,
                provider=None, model=None,
                tailor_min_score=env_float("APPLY_TAILOR_MIN_SCORE", 4.0),
            )
            resume = apply_pkg._resolve_resume(co_local, job)
            result = linkedin.apply_to(
                page, job, engine, mode="review", resume_path=resume,
                should_cancel=lambda: _apply_decision(job_id) == "cancel",
            )

            if result.code == CANCELLED:
                _set_apply(job_id, status="cancelled")
                return
            if result.code == EXPIRED:
                apply_pkg._mark_status(applications_md, job, "Discarded")
                _set_apply(job_id, status="expired", code=result.code, reason=result.reason)
                return
            if not result.applied:
                _set_apply(job_id, status="failed", code=result.code, reason=result.reason)
                return

            _set_apply(job_id, status="ready",
                       answers=[list(a) for a in result.answers],
                       needs_review=list(getattr(engine, "unanswered", [])))

            task["event"].wait(timeout=_apply_hold_timeout())
            # Resolve the outcome atomically: an endpoint may have raced the
            # timeout, so the RECORDED decision (not wait()'s return) is truth.
            # Moving to a transient status under the lock means a later
            # submit/cancel sees a non-"ready" status and is rejected.
            with _apply_lock:
                decision = task.get("decision")
                task["status"] = {"submit": "submitting",
                                  "cancel": "cancelling"}.get(decision, "timeout")
                final = task["status"]

            if final == "submitting":
                sub = linkedin.submit_application(page)
                if sub.submitted:
                    apply_pkg._mark_status(applications_md, job, "Applied")
                    _set_apply(job_id, status="submitted")
                else:
                    _set_apply(job_id, status="failed", code=sub.code, reason=sub.reason)
            elif final == "cancelling":
                _set_apply(job_id, status="cancelled")
            # else: timeout — just close the browser (the `with` exits).
    except ImportError as e:
        _set_apply(job_id, status="failed", code="playwright_missing", reason=str(e))
    except Exception as e:  # never let a browser hiccup wedge the session in "pending"
        msg = (str(e).splitlines() or [""])[0] or type(e).__name__
        _set_apply(job_id, status="failed", code="exception", reason=msg[:200])


class ApplyAsyncRequest(BaseModel):
    num: str


@app.post("/api/jobs/apply-async")
def apply_async(req: ApplyAsyncRequest) -> JSONResponse:
    """Start a visible apply-review session for tracker row `num`. 404 if the
    row is unknown, 400 if it isn't a LinkedIn Easy Apply posting, 409 if a
    session is already in progress (one visible browser at a time)."""
    job = _apply_job_for_num(req.num)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No triaged role #{req.num}.")
    from pipeline.apply.queue import is_linkedin_job
    if not is_linkedin_job(job.url):
        raise HTTPException(
            status_code=400,
            detail="This role has no LinkedIn job URL — the review-and-submit flow "
                   "opens LinkedIn postings (Easy Apply is confirmed once the page loads).",
        )
    with _apply_lock:
        if any(t["status"] in _APPLY_ACTIVE for t in _apply_tasks.values()):
            raise HTTPException(status_code=409,
                                detail="An apply review session is already in progress. "
                                       "Finish or cancel it first.")
        _evict_apply_tasks()
        job_id = str(uuid.uuid4())
        _apply_tasks[job_id] = {
            "status": "pending", "num": job.num, "company": job.company,
            "role": job.role, "answers": [], "needs_review": [], "code": None,
            "reason": None, "job": job, "event": threading.Event(), "decision": None,
            # Pin the tracker + local career-ops at session start so a mid-hold
            # use-local/refresh flip can't redirect the Applied/Discarded write.
            "applications_md": _career_ops() / "data" / "applications.md",
            "co_local": _career_ops_local(),
        }
    threading.Thread(target=_run_apply_review, args=(job_id,), daemon=True).start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/apply-status/{job_id}")
def apply_status(job_id: str) -> JSONResponse:
    with _apply_lock:
        task = _apply_tasks.get(job_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown apply session.")
        return JSONResponse(_apply_public(task))


@app.post("/api/jobs/apply-submit/{job_id}")
def apply_submit(job_id: str) -> JSONResponse:
    """Confirm: signal the held worker to click Submit. Valid only from
    'ready' — the worker is blocked there waiting for this."""
    with _apply_lock:
        task = _apply_tasks.get(job_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown apply session.")
        if task["status"] != "ready":
            raise HTTPException(status_code=409,
                                detail=f"Session isn't awaiting submit (status={task['status']}).")
        task["decision"] = "submit"
        task["event"].set()
    return JSONResponse({"ok": True})


@app.post("/api/jobs/apply-cancel/{job_id}")
def apply_cancel(job_id: str) -> JSONResponse:
    """Signal the worker to stop — interrupts the fill (pending) or the review
    hold (ready). Rejected once the worker has committed to a submit/cancel or
    finished (an in-flight submit can't be un-clicked)."""
    with _apply_lock:
        task = _apply_tasks.get(job_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown apply session.")
        if task["status"] not in ("pending", "ready"):
            raise HTTPException(status_code=409,
                                detail=f"Session can't be cancelled now (status={task['status']}).")
        task["decision"] = "cancel"
        task["event"].set()
    return JSONResponse({"ok": True})


# Mount the SPA last so /api/* routes take precedence. html=True serves
# index.html at "/" and for unknown paths (client-side routing friendly).
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
