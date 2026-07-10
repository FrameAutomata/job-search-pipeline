"""FastAPI server for the local triage UI.

Serves a single-page frontend on localhost and a small JSON API over the
evaluation results. Read operations (list jobs, view report), status write-back
(kanban), cloud operations via the gh CLI (refresh, trigger a run, push status),
and guided onboarding (generate profile artifacts + write GitHub secrets).

Run:
    uvicorn pipeline.app.server:app --port 8000
or use the run-ui.sh / run-ui.ps1 launchers.
"""

import contextlib
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

from pipeline.app import data, gh, local_run, onboard, reset, self_update, skills
from pipeline import article_digest
from pipeline import gemini_limits
from pipeline._batch_common import (
    build_user_message,
    eval_system_prompt,
    max_report_num,
    max_tracker_num,
    read_text,
    tail_text,
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
from pipeline import handoff, recheck

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Load .env so JOB_SEARCH_REPO, API keys, BATCH_PROVIDER, etc. are visible to
# the server. Pipeline scripts call load_dotenv themselves; the server didn't,
# which meant .env values were silently ignored unless set in the shell.
# override=False so a shell-level export always wins over .env.
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=False)

# Scratch dir where `gh run download` drops artifacts before Refresh merges them
# into the local career-ops. Gitignored. The data layer ALWAYS reads from the
# local install (offline-first); Refresh/Push just keep it synced with the cloud.
UI_CACHE = ROOT / ".ui-cache" / "latest"

# Background add-job tasks: job_id → {status, result?, error?}
_add_job_tasks: dict[str, dict] = {}
_add_job_lock = threading.Lock()

# Cloud workflow filenames (must match .github/workflows/*.yml).
DAILY_WORKFLOW = "daily-pipeline.yml"
EDIT_WORKFLOW = "edit-tracker.yml"

# The daily pipeline uploads the `pipeline-output-*` artifact. Refresh/push pull
# its most recent successful run. (Kept as a list — latest_successful_run takes
# several — in case more artifact-producing workflows are added later.)
PIPELINE_WORKFLOWS = [DAILY_WORKFLOW]

# Pending status changes the user hasn't pushed yet. Written by kanban drags —
# one channel, owned by
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
    """The data directory for reads (jobs, reports) — always the local install.
    Refresh merges the cloud artifact INTO local (offline-first), so there's a
    single durable source of truth that survives restarts and offline periods."""
    return _career_ops_local()


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

    # Apply pending-overrides: local kanban drags + recheck discards not yet
    # pushed. A plain (num-keyed) override lands on the row with that num; an
    # identity-anchored one (from the liveness recheck, whose num may be from a
    # different tracker) lands on the row matching its company/role — so it
    # marks the intended row, not whichever row coincidentally shares the num.
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
    # recheck discard can't lose this drag (or be lost by it).
    data.record_status_override(str(change.num), change.status)
    return JSONResponse({"ok": True, "pending": len(_load_overrides())})


@app.post("/api/push-status")
def push_status() -> JSONResponse:
    """Push pending status changes to the cloud tracker via the edit-tracker
    workflow.

    Clobber guard: we first pull the latest pipeline artifact and MERGE it into
    the local tracker (so rows the pipeline added since the last Refresh are
    present), then apply the pending status overrides onto the local base and
    dispatch the resolved {num: status} to edit-tracker. Offline (no runs / gh
    error) → push against the last-synced local tracker."""
    overrides = _load_overrides()
    if not overrides:
        raise HTTPException(status_code=400, detail="No pending status changes to push.")

    local_apps = _career_ops_local() / "data" / "applications.md"

    # Clobber guard: pull the latest cloud artifact and MERGE it into local
    # first, so rows the pipeline added since the last Refresh are present and
    # overrides resolve against current cloud numbers. Offline (no runs / gh
    # error) → push against the last-synced local tracker, untouched.
    base_source = "local"
    try:
        run = gh.latest_successful_run(PIPELINE_WORKFLOWS)
        if run is not None:
            if UI_CACHE.exists():
                shutil.rmtree(UI_CACHE)
            artifact = gh.download_artifact(run["databaseId"], UI_CACHE)
            data.sync_pulled_tracker(artifact, _career_ops_local())
            base_source = "refreshed"
    except gh.GhError:
        pass  # offline — push against the last-synced local tracker

    if not local_apps.exists():
        raise HTTPException(
            status_code=409,
            detail="No applications.md to update (run the pipeline so the tracker exists).",
        )
    base_text = local_apps.read_text(encoding="utf-8")

    # Resolve each identity-anchored override to the correct num IN THIS base. An
    # override whose company isn't in the tracker is returned in `unresolved` —
    # NOT applied and NOT dispatched, so we never mark a different company that
    # merely shares the num. cloud_payload is {num: status} (edit-tracker.yml's
    # input). We do NOT write the status into applications.md: that file is the
    # last-synced CLOUD mirror, and the pending edit lives in the pushed-override
    # overlay (below) until a real cloud run incorporates it — only then does the
    # self-clean in list_jobs fire. Writing it here would self-clean immediately
    # and then vanish when Refresh's cloud-wins reverts the row.
    _, cloud_payload, unresolved = data.resolve_overrides_for_push(
        base_text, overrides, build_text=False)

    # An unresolved DISCARD override targets a closed role that isn't in the cloud
    # tracker: it's already applied locally, a closed role won't reappear to match
    # on a later push, and the cloud's own daily recheck catches its own closed
    # roles — so drop it instead of keeping it to nag on every future push.
    # Non-Discard unresolved overrides (e.g. an auto-submitted Applied) are KEPT:
    # their company may appear in a later cloud run, and then the next push lands.
    stale_discards = [k for k in unresolved
                      if data.canonical_status(data.override_status(overrides[k])) == "Discarded"]
    kept_unresolved = [k for k in unresolved if k not in stale_discards]

    # Dispatch edit-tracker with only the resolved overrides — avoids GitHub's
    # workflow_dispatch input size limit that the full base64 tracker can exceed.
    # Nothing resolved → nothing to push (don't fire an empty workflow run).
    if cloud_payload:
        try:
            gh.trigger_workflow(EDIT_WORKFLOW, {"status_overrides_json": json.dumps(cloud_payload)})
        except gh.GhError as e:
            raise HTTPException(status_code=502, detail=str(e))
        # Clear the keys we pushed PLUS the dead Discard overrides (re-reading
        # under the lock), leaving only the genuinely-pending unresolved ones so
        # a recheck discard recorded during the gh round-trip isn't dropped.
        pushed_keys = [k for k in overrides if k not in unresolved]
        data.clear_status_overrides(pushed_keys + stale_discards)
        # Persist dispatched overrides so they survive Refresh and restarts until
        # the pipeline produces a new artifact that already has the correct statuses.
        pushed = _load_pushed_overrides()
        pushed.update(cloud_payload)
        _save_pushed_overrides(pushed)
    elif stale_discards:
        # Nothing resolved to dispatch, but still drop the dead Discard overrides.
        data.clear_status_overrides(stale_discards)
    return JSONResponse({"ok": True, "pushed": len(cloud_payload),
                         "unresolved": len(kept_unresolved), "base": base_source})


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
    """Basic readiness probe — confirms where the UI reads data from (always the
    local install now) and whether the tracker exists yet."""
    apps_md = _career_ops() / "data" / "applications.md"
    return {
        "status": "ok",
        "career_ops": str(_career_ops()),
        "applications_md_exists": apps_md.exists(),
    }


@app.post("/api/refresh")
def refresh() -> JSONResponse:
    """Pull the most recent successful pipeline artifact and MERGE it into the
    local career-ops (offline-first): cloud wins for shared roles, local-only
    rows (offline `Run local` results) are preserved. The merged tracker lives
    durably in local, so it survives restarts and offline periods. Only writes
    to local on a successful download — a gh/credits error leaves local intact."""
    try:
        run = gh.latest_successful_run(PIPELINE_WORKFLOWS)
        if run is None:
            raise HTTPException(status_code=404, detail="No successful pipeline runs found yet.")
        # Fresh scratch dir for the download, then merge it into local.
        if UI_CACHE.exists():
            shutil.rmtree(UI_CACHE)
        artifact = gh.download_artifact(run["databaseId"], UI_CACHE)
        summary = data.sync_pulled_tracker(artifact, _career_ops_local())
        return JSONResponse({
            "ok": True,
            "run_id": run["databaseId"],
            "created_at": run.get("createdAt"),
            "title": run.get("displayTitle"),
            "rows": summary["rows"],
            "renamed_reports": len(summary["renames"]),
        })
    except gh.GhError as e:
        # Offline / no credits: keep the last-synced local tracker untouched.
        raise HTTPException(status_code=502, detail=f"{e} — showing last-synced local data.")


@app.post("/api/run")
def run_pipeline() -> JSONResponse:
    """Trigger a daily-pipeline run in the cloud via gh. Returns immediately —
    the run executes on GitHub's schedule infra; Refresh later to pull results."""
    try:
        gh.trigger_workflow(DAILY_WORKFLOW)
        return JSONResponse({"ok": True, "workflow": DAILY_WORKFLOW})
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/template/status")
def template_status() -> JSONResponse:
    """Whether the maintainer's template has updates this copy doesn't have yet.
    Drives the UI's 'update available' badge. Read-only."""
    try:
        return JSONResponse(self_update.update_available())
    except gh.GhError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/template/update")
def template_update() -> JSONResponse:
    """Pull the template's latest main into the local clone and push to origin —
    updating both the local UI and the cloud copy. 409 on a merge conflict so the
    user resolves it manually (the working tree is left clean)."""
    result = self_update.apply_update(ROOT)
    if result.get("ok"):
        return JSONResponse(result)
    raise HTTPException(status_code=409 if result.get("conflict") else 500,
                        detail=result.get("error", "update failed"))


class ResetRequest(BaseModel):
    confirm: str = ""
    clear_cloud: bool = True


@app.post("/api/reset")
def reset_search(req: ResetRequest) -> JSONResponse:
    """Start over: snapshot then wipe the job-search state (tracker, history,
    reports, queue, batch, outputs, UI overlays), keeping setup + system code.
    Requires confirm == "RESET" so it can't fire by accident. With clear_cloud,
    also delete the cloud Actions state cache (best-effort) so a Refresh / next
    run can't restore the old history."""
    if req.confirm != "RESET":
        raise HTTPException(status_code=400, detail='Type "RESET" to confirm — nothing was reset.')
    _refuse_during_local_run()   # a wipe mid-run would race the pipeline's writes

    ui_root = UI_CACHE.parent     # .ui-cache/ (UI_CACHE is .ui-cache/latest)
    backup = ui_root / "backups" / f"reset-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    summary = reset.reset_job_search(_career_ops_local(), ROOT, ui_root, backup)
    if req.clear_cloud:
        try:
            summary["cloud"] = reset.clear_cloud_caches()
        except gh.GhError as e:
            summary["cloud_error"] = str(e)   # local reset still succeeded
    return JSONResponse({"ok": True, **summary})


# ── local pipeline run ──────────────────────────────────────────────────────

class LocalRunRequest(BaseModel):
    passes: str = "all"        # "all" | "easy-only" | "no-easy"
    evaluate: bool = True


@app.post("/api/run-local")
def run_local(req: LocalRunRequest) -> JSONResponse:
    """Start a local pipeline run (orchestrate.py subprocess). Single-flight:
    409 when one is already running. Poll /api/run-local/status for progress;
    on success the fresh local results show on the next loadJobs()."""
    if req.passes not in local_run.VALID_PASSES:
        raise HTTPException(status_code=400, detail=f"unknown passes value: {req.passes}")
    # A pipeline run rewrites the tracker wholesale; a recheck sweep edits it
    # concurrently. Refuse to overlap (recheck refuses during a run too).
    with _recheck_lock:
        if _recheck_state.get("running"):
            raise HTTPException(status_code=409,
                                detail="A liveness re-check is in progress. Wait for it to "
                                       "finish — both write the tracker.")
    # Same for a work-order build: it reads the tracker (and may be tailoring
    # for minutes) — the mirror of handoff_build's _refuse_during_local_run.
    if _handoff_running():
        raise HTTPException(status_code=409,
                            detail="A work-order build is in progress. Wait for it to "
                                   "finish — the pipeline run would rewrite the tracker under it.")
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


# ── local search-config override ────────────────────────────────────────────
# A full standalone search config for LOCAL runs (config/search.local.yml) that
# diverges from the cloud-shared config/search.yml. orchestrate.py auto-prefers
# it when present (resolve_search_config), so both the UI "Run" button and CLI
# runs use it; the cloud is untouched (the daily decodes SEARCH_CONFIG_B64 into
# search.yml and never sees this gitignored file). The UI seeds the editor from
# search.yml so "full replacement" starts from the current cloud config.
# These filenames mirror orchestrate.{LOCAL,SHARED}_SEARCH_CONFIG — not imported
# from there because orchestrate pulls jobspy transitively and this UI process
# shouldn't. Keep the two in sync if either is ever renamed.
def _local_search_path() -> Path:
    return ROOT / "config" / "search.local.yml"


class LocalSearchConfig(BaseModel):
    content: str


def _search_entries(parsed) -> list | None:
    """The per-search mappings pipeline.scrape.load_searches would yield, or None
    if the shape is invalid. Mirrors load_searches (a `searches:` list, or the
    legacy single `search:`) but additionally requires at least one entry and
    every entry to be a mapping — the pipeline reads dict fields off each, so a
    scalar/null entry or an empty list would crash the next run, not no-op."""
    if not isinstance(parsed, dict):
        return None
    if "searches" in parsed:
        entries = parsed["searches"]
    elif "search" in parsed:
        entries = [parsed["search"]]
    else:
        return None
    if not isinstance(entries, list) or not entries:
        return None
    if not all(isinstance(e, dict) for e in entries):
        return None
    return entries


@app.get("/api/local-search")
def local_search_get() -> JSONResponse:
    """Return the local override if present, else seed the editor with the
    cloud-shared search.yml so the user edits a full copy. `active` is whether an
    override file exists (i.e. what local runs will actually use)."""
    local = _local_search_path()
    if local.exists():
        return JSONResponse({"active": True, "content": local.read_text(encoding="utf-8"),
                             "path": "config/search.local.yml"})
    shared = ROOT / "config" / "search.yml"
    content = shared.read_text(encoding="utf-8") if shared.exists() else ""
    return JSONResponse({"active": False, "content": content, "path": "config/search.local.yml"})


@app.post("/api/local-search")
def local_search_set(req: LocalSearchConfig) -> JSONResponse:
    """Validate and write the local override. Rejects anything that wouldn't load
    as a search config so the next run can't choke on a broken file — including a
    non-mapping or empty search entry, which would crash scrape, not no-op."""
    import yaml
    try:
        parsed = yaml.safe_load(req.content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Not valid YAML: {e}")
    if _search_entries(parsed) is None:
        raise HTTPException(status_code=400,
                            detail="Config must have a non-empty `searches:` list of search "
                                   "mappings (or a single legacy `search:` mapping).")
    local = _local_search_path()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(req.content, encoding="utf-8")
    return JSONResponse({"ok": True, "active": True})


@app.delete("/api/local-search")
def local_search_delete() -> JSONResponse:
    """Remove the override so local runs fall back to the cloud-shared config.
    Idempotent — a no-op when no override exists."""
    _local_search_path().unlink(missing_ok=True)
    return JSONResponse({"ok": True, "active": False})


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
            "unverifiable": 0, "dead": [], "done": False, "ok": None, "error": None}


def _run_recheck() -> None:
    """Worker: run the core sweep against the active tracker, folding live
    progress and the final summary into _recheck_state for the status poll."""
    def _progress(checked, total, discarded):
        with _recheck_lock:
            _recheck_state.update(checked=checked, total=total, discarded=discarded)
    try:
        # drain() loops budgeted sweeps until the backlog is covered (it
        # self-limits to a single sweep when there are <= budget roles), so a
        # backlog larger than one budget is fully gone through from the UI
        # instead of stopping at one budget with hundreds deferred.
        summary = recheck.drain(_career_ops(), progress=_progress)
        with _recheck_lock:
            # A drain that hit an error mid-way still returns the completed
            # cycles' partial counts (already written to disk) — surface those
            # AND ok=False, rather than a zeroed failure.
            _recheck_state.update(
                running=False, done=True, ok=not summary.get("error"),
                error=summary.get("error"),
                checked=summary["checked"], discarded=summary["discarded"],
                unconfirmed=summary.get("unconfirmed", 0),
                throttled=summary.get("throttled", 0),
                deferred=summary.get("deferred", 0),
                unverifiable=summary.get("unverifiable", 0), dead=summary["dead"])
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
    return FileResponse(str(page), headers={"Cache-Control": "no-cache"})


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
    from pipeline.resume_text import IMPORT_SUFFIXES
    resume_present = any((ROOT / "resumes" / f"resume{s}").exists() for s in IMPORT_SUFFIXES)
    return JSONResponse({
        "form": onboard.load_sidecar(ROOT),
        "has_resume": resume_present,
    })


@app.post("/api/onboard/parse-resume")
async def onboard_parse_resume(resume: UploadFile = File(...)) -> JSONResponse:
    """Extract contact details from an uploaded resume (PDF / DOCX / ODT) to
    autofill the onboarding 'About' step. Pure local parse — no gh calls,
    nothing written."""
    data = await resume.read()
    if not data:
        raise HTTPException(status_code=400, detail="resume file is empty")
    try:
        text = onboard.extract_resume_text(data, resume.filename or "")
    except ValueError as e:  # unsupported format
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not read resume: {e}")
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
            "gemini_free_tier": gemini_limits.conforming_enabled(),
            # Tailoring model/provider (blank = inherit the eval model/provider).
            "tailor_provider": os.environ.get("TAILOR_PROVIDER", ""),
            "tailor_model": os.environ.get("TAILOR_MODEL", ""),
            "handoff_out_dir": os.environ.get("HANDOFF_OUT_DIR", ""),
        },
        "provider_defaults": dict(PROVIDER_DEFAULTS),
    })


class LocalConfigRequest(BaseModel):
    batch_provider: str = ""
    batch_model: str = ""
    batch_cli: str = ""
    api_key: str = ""   # optional — write the provider's API key to .env too
    gemini_free_tier: bool = False   # conform eval/tailoring to Gemini free-tier limits
    # Resume tailoring can use a different (usually stronger) model — and even a
    # different provider — than bulk evaluation. Blank tailor_* = inherit the eval
    # model/provider. tailor_api_key (optional) is the tailor provider's key.
    tailor_provider: str = ""
    tailor_model: str = ""
    tailor_api_key: str = ""
    # Where the browser-agent work-orders land (blank = output/handoff default).
    # Point it at a folder your agent can reach; setting it creates + seeds the dir.
    handoff_out_dir: str = ""


def _validate_provider(name: str, label: str) -> str:
    """Lowercase + validate a provider name against the known set (400 on a typo);
    returns the normalized name, empty when unset. Shared by the eval and tailoring
    provider fields so the check lives in one place."""
    norm = name.strip().lower()
    if norm and norm not in _KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown {label} {norm!r}. Valid: {', '.join(sorted(_KNOWN_PROVIDERS))}",
        )
    return norm


@app.post("/api/onboard/local-config")
def save_local_config(req: LocalConfigRequest) -> JSONResponse:
    """Write BATCH_PROVIDER, BATCH_MODEL, BATCH_CLI (and optionally the
    provider API key) to the local .env and update os.environ immediately."""
    from dotenv import set_key, unset_key

    provider = _validate_provider(req.batch_provider, "provider")
    cli = req.batch_cli.strip()
    if cli and cli not in _KNOWN_CLIS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown CLI {cli!r}. Valid: {', '.join(_KNOWN_CLIS)}",
        )
    tailor_provider = _validate_provider(req.tailor_provider, "tailoring provider")

    # A key can only be written under its provider's env var — with the select
    # on "auto-detect" (blank) there is nowhere to put it, and silently
    # returning ok while dropping the key strands the user with "no LLM
    # provider configured" later. Reject loudly instead.
    if req.api_key.strip() and not provider:
        raise HTTPException(
            status_code=400,
            detail="Pick a provider for this API key — auto-detect can't tell "
                   "which provider the key belongs to.",
        )
    if req.tailor_api_key.strip() and not tailor_provider:
        raise HTTPException(
            status_code=400,
            detail="Pick a tailoring provider for this API key — leaving it on "
                   "'inherit' can't tell which provider the key belongs to.",
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
    # Opt into Gemini free-tier conforming (RPM pacing + RPD capping).
    _set("GEMINI_FREE_TIER", "true" if req.gemini_free_tier else "")

    # Tailoring model/provider (blank = inherit the eval model/provider). Write the
    # tailor provider's API key too when given, so a cross-provider tailor (e.g.
    # evaluate on Gemini, tailor on Anthropic) can authenticate.
    _set("TAILOR_PROVIDER", tailor_provider)
    _set("TAILOR_MODEL", req.tailor_model.strip())
    tailor_key = req.tailor_api_key.strip()
    if tailor_key and tailor_provider and tailor_provider in onboard.PROVIDER_SECRETS:
        _set(onboard.PROVIDER_SECRETS[tailor_provider], tailor_key)

    # Where the browser-agent work-orders land. Setting it creates + seeds the dir
    # (the agent README); blank clears it so run() falls back to output/handoff.
    handoff_dir = req.handoff_out_dir.strip()
    _set("HANDOFF_OUT_DIR", handoff_dir)
    seed_warning = ""
    if handoff_dir:
        try:
            handoff.bootstrap_handoff_dir(handoff_dir)
        except OSError as e:
            # Best-effort: the .env write (the primary action) succeeded, so don't
            # 500 — just report that the folder couldn't be prepared.
            seed_warning = f"saved, but couldn't create/seed {handoff_dir}: {e}"

    return JSONResponse({"ok": True, "updated": updated, "warning": seed_warning})


@app.post("/api/onboard/pick-folder")
def pick_folder() -> JSONResponse:
    """Open a native OS folder dialog on this machine (the UI is local) and return
    the chosen path — the "Browse…" button beside the handoff-folder field. Returns
    {"path": ""} when the user cancels; 503 when no picker is available (headless /
    no tkinter), so the field stays a plain typed path."""
    from pipeline.app.folder_picker import pick_directory

    path = pick_directory("Select the browser-agent handoff folder")
    if path is None:
        raise HTTPException(
            status_code=503,
            detail="No folder picker available on this machine — type the path instead.",
        )
    return JSONResponse({"path": path})


def _maybe_generate_article_digest(payload: dict, resume_text: str,
                                   provider: str, api_key: str) -> None:
    """Best-effort: draft career-ops/article-digest.md (the proof-points corpus
    career-ops inlines into every evaluation) from the résumé + the candidate's
    GitHub repo READMEs, using the provider + key just entered. Skips silently
    when there's no key or a non-empty digest already exists; never raises — a
    failed digest must not fail onboarding."""
    if not (provider and api_key):
        return
    try:
        written = article_digest.generate_and_write(
            ROOT / "career-ops", resume_text, onboard.portfolio_urls(payload),
            provider=provider, api_key=api_key,
            model=(payload.get("batch_model") or "").strip() or None,
        )
        # One-line signal for troubleshooting "why no digest?": None means we
        # wrote nothing (existing digest kept, no grounding, or the LLM was
        # unavailable). Not an error — onboarding proceeds regardless.
        if written is None:
            print("[onboard] article-digest not generated "
                  "(existing digest kept, no grounding, or LLM unavailable)")
    except Exception as e:
        # best-effort — onboarding succeeds regardless; log so it's not invisible.
        print(f"[onboard] article-digest generation errored (ignored): {e}")


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
    txt_path = resumes_dir / "resume.txt"

    # Resume: prefer a freshly-uploaded file (PDF/DOCX/ODT); fall back to the
    # persisted text from a prior submit. UploadFile is always non-None when the
    # multipart field exists, so check size rather than identity.
    resume_bytes = await resume.read() if resume is not None else b""
    if resume_bytes:
        filename = (resume.filename or "").strip()
        suffix = Path(filename).suffix.lower()
        try:
            resume_text = onboard.extract_resume_text(resume_bytes, filename)
        except ValueError as e:  # unsupported format
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:  # libraries raise various errors on bad files
            raise HTTPException(status_code=400, detail=f"could not read resume: {e}")
        if not resume_text.strip():
            raise HTTPException(
                status_code=400,
                detail="No text found in the resume (is it a scanned image or empty?).",
            )
        resumes_dir.mkdir(parents=True, exist_ok=True)
        # Persist under the real extension (resume.pdf / resume.docx / resume.odt)
        # so a DOCX import doubles as the resume-tailoring source
        # (resume_tailor.source_docx() defaults to resumes/resume.docx).
        # Retire sibling formats first: the filter's probe is pdf-first, so a
        # stale resume.pdf left beside a fresh resume.docx would keep winning
        # keyword scoring forever (split-brain resume — review bug). The latest
        # upload is THE resume.
        from pipeline.resume_text import IMPORT_SUFFIXES
        for other in IMPORT_SUFFIXES:
            if other != suffix:
                (resumes_dir / f"resume{other}").unlink(missing_ok=True)
        (resumes_dir / f"resume{suffix}").write_bytes(resume_bytes)
        txt_path.write_text(resume_text, encoding="utf-8")
    elif txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")
    else:
        raise HTTPException(
            status_code=400,
            detail=("No resume on file — upload a PDF, DOCX, or ODT on the first "
                    "onboarding step before submitting."),
        )

    # Generate artifacts via the shared node generator, then collect base64.
    try:
        onboard.run_generation(ROOT, onboard.build_onboarding_json(payload, resume_text))
        # Best-effort proof-points corpus, written before we snapshot files into
        # secrets so it rides along to the cloud. Never blocks onboarding.
        _maybe_generate_article_digest(payload, resume_text, provider, api_key)
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
    """Load the evaluation system prompt from the local career-ops install.

    Delegates to the shared eval_system_prompt builder so the UI add-job eval
    resolves the candidate profile identically to `--evaluate-batch` — the living
    PROFILE.md when present, else the seed files (see add_job's parity contract).
    The 409 guard keeps the pre-Setup error message."""
    co = _career_ops_local()
    if not read_text(co / "cv.md") and not read_text(co / "config" / "profile.yml"):
        raise HTTPException(
            status_code=409,
            detail=(
                "cv.md and profile.yml not found in career-ops. "
                "Complete the Setup wizard first."
            ),
        )
    return eval_system_prompt(co)


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
        # list_jobs reads the local career-ops directly (offline-first), so the
        # newly written tracker row + report are visible on the next loadJobs().

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


# ── browser-agent handoff ────────────────────────────────────────────────────
# The UI never launches a browser agent (none expose a programmatic session
# API) — it builds the work-order + paste-ready prompts the user hands to
# whichever agent they use. Batch = the old "Batch apply" slot; per-role = a
# prompt from the report pane.

# The one build slot: {"id", "status": running|done|failed, "result"?, "error"?}.
# A singleton (not a task registry like add-job) because builds are
# single-flight by design — a new build simply replaces the finished one.
_handoff_task: dict | None = None
_handoff_lock = threading.Lock()
# The build runs in-process (so tests can monkeypatch handoff.run and the result
# stays a plain function return), so to stream its progress to the UI the way the
# local pipeline run does we redirect its stdout to this file and tail it on each
# status poll. Single-flight (one build at a time), so a fixed path — truncated
# per build — is enough; no per-job filenames needed.
_HANDOFF_LOG = ROOT / ".ui-cache" / "handoff-build.log"


class HandoffBuildRequest(BaseModel):
    board: str = "both"
    limit: int | None = None
    tailor: bool = False


def _handoff_running() -> bool:
    with _handoff_lock:
        return bool(_handoff_task and _handoff_task.get("status") == "running")


def _finish_handoff(job_id: str, **fields) -> None:
    global _handoff_task
    with _handoff_lock:
        if _handoff_task and _handoff_task.get("id") == job_id:
            _handoff_task = {"id": job_id, **fields}


def _run_handoff_build(job_id: str, board: str, limit: int | None, tailor: bool) -> None:
    try:
        # Capture the build's [handoff] progress to a file so the status poll can
        # stream it to the UI. Line-buffered (buffering=1) so each printed line is
        # flushed to disk immediately and the tail updates live; sys.stdout is
        # restored on exit. Caveat: redirect_stdout/stderr are process-global, so
        # a concurrent background thread that prints during a long (--tailor) build
        # — a recheck sweep, onboarding — has its output diverted here too (and
        # away from the console). Accepted for a localhost single-user UI: at worst
        # a stray line lands in this log; the build result itself is unaffected.
        _HANDOFF_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_HANDOFF_LOG, "w", encoding="utf-8", errors="replace", buffering=1) as log, \
                contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            # Pass the server's ROOT-anchored career-ops so a RELATIVE
            # CAREER_OPS_PATH resolves the same tree the UI reads from — handoff's
            # own default only anchors an absolute value (review L2).
            rc = handoff.run(board=board, limit=limit, tailor=tailor,
                             career_ops=_career_ops_local())
        if rc != 0:
            _finish_handoff(job_id, status="failed",
                            error="handoff exited non-zero — no scored roles found "
                                  "(run an evaluation first, or point --queue at a scored export)")
            return
        # One session per site: hand back a paste-ready kickoff prompt for each
        # non-empty per-site work-order run() just wrote (session_summaries owns
        # the filename→session mapping; a site emptied this run is skipped).
        sessions = handoff.session_summaries(handoff.default_out_dir())
        _finish_handoff(job_id, status="done", result={
            "sessions": sessions,
            "total_fresh": sum(s["fresh"] for s in sessions),
        })
    except Exception as exc:  # surface, never wedge the slot in "running"
        _finish_handoff(job_id, status="failed", error=str(exc))


@app.post("/api/handoff/build")
def handoff_build(req: HandoffBuildRequest) -> JSONResponse:
    """Build the work-order in the background (tailoring can take minutes).
    Single-flight: one build at a time, and never while a local pipeline run
    is rewriting the tracker under us (run_local refuses the reverse too)."""
    global _handoff_task
    # `board` is an unconstrained str on the wire; reject an unknown one before it
    # reaches handoff.run() (where a narrowed build would write a stray empty
    # next-roles-<garbage>.jsonl). "both" = a session per site.
    if req.board != "both" and req.board not in handoff.KNOWN_BOARDS:
        raise HTTPException(status_code=400, detail=f"Unknown board '{req.board}'.")
    _refuse_during_local_run()
    with _handoff_lock:
        if _handoff_task and _handoff_task.get("status") == "running":
            raise HTTPException(status_code=409,
                                detail="A work-order build is already in progress.")
        # Clear the previous build's log BEFORE publishing this task (and while
        # holding the lock the status poll also takes) so a poll for this build
        # can't return the prior build's tail in the window before the worker
        # thread truncates the file. Truncate before the status flip so a failure
        # here surfaces as an error without wedging the slot in "running".
        _HANDOFF_LOG.parent.mkdir(parents=True, exist_ok=True)
        _HANDOFF_LOG.write_text("", encoding="utf-8")
        job_id = str(uuid.uuid4())
        _handoff_task = {"id": job_id, "status": "running"}
    threading.Thread(target=_run_handoff_build,
                     args=(job_id, req.board, req.limit, req.tailor),
                     daemon=True).start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/handoff/build-status/{job_id}")
def handoff_build_status(job_id: str) -> JSONResponse:
    with _handoff_lock:
        task = dict(_handoff_task) if _handoff_task and _handoff_task.get("id") == job_id else None
    if task is None:
        raise HTTPException(status_code=404, detail="Unknown build task")
    task.pop("id", None)
    # The captured build log — live while running, retained on the terminal state
    # — so the 🤝 panel can stream progress like the local pipeline run does.
    task["log_tail"] = tail_text(_HANDOFF_LOG, 40)
    return JSONResponse(task)


@app.get("/api/handoff/role-prompt/{num}")
def handoff_role_prompt(num: str) -> JSONResponse:
    """A paste-ready, agent-agnostic prompt for handing off ONE tracker role to
    the user's browser agent. This route gathers the UI-layer facts (row,
    report, cached resume); handoff.role_prompt renders them so the writeback
    contract lives beside its parser."""
    row = _find_role(num)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No triaged role #{num}.")
    url = data.extract_url(row.get("notes", ""))
    if not url:
        raise HTTPException(
            status_code=400,
            detail="This role has no posting URL in its tracker notes — nothing to hand off.",
        )
    from pipeline.resume_tailor import find_existing

    company = row.get("company", "")
    career_ops = _career_ops_local()
    # Number-based lookup (not the link target) — tolerant of report renames,
    # same convention as the skills launchpad.
    report = data.find_report_file(career_ops / "reports", row.get("report_num", ""))
    # profile defaults to the handoff dir's living PROFILE.md (the fact bank +
    # standing answers the agent tailors from) — not the raw onboarding YAML.
    prompt = handoff.role_prompt(
        company, row.get("role", ""), url,
        report=report,
        resume=find_existing(career_ops, company),
    )
    return JSONResponse({"company": company, "role": row.get("role", ""), "prompt": prompt})


class _NoCacheStaticFiles(StaticFiles):
    """Serve SPA assets with Cache-Control: no-cache so the browser revalidates
    every load (conditional GET -> 304 when unchanged). Without this a cached
    app.js / onboard.js lingers after a UI change until a manual hard refresh."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mount the SPA last so /api/* routes take precedence. html=True serves
# index.html at "/" and for unknown paths (client-side routing friendly).
if STATIC_DIR.exists():
    app.mount("/", _NoCacheStaticFiles(directory=str(STATIC_DIR), html=True), name="static")
