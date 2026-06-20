"""Route smoke tests for pipeline/app/server.py.

Skips entirely if FastAPI isn't installed (it's an optional UI dependency in
requirements-ui.txt, not part of the core pipeline deps)."""

import importlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Point the server at a tmp career-ops dir with one tracker row + report,
    then return a TestClient bound to a freshly-imported app."""
    career_ops = tmp_path / "career-ops"
    (career_ops / "data").mkdir(parents=True)
    (career_ops / "reports").mkdir(parents=True)
    (career_ops / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-05-27 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme.md) | APPLY |\n",
        encoding="utf-8",
    )
    (career_ops / "reports" / "001-acme.md").write_text(
        "# Acme — Eng\n\n**Score:** 4.2/5\n\nGreat match.", encoding="utf-8"
    )
    monkeypatch.setenv("CAREER_OPS_PATH", str(career_ops))

    # Re-import the server so its module-level paths pick up the env var. The
    # _career_ops() helper reads the env at request time, so a plain import is
    # fine, but reload keeps the test hermetic across runs.
    from pipeline.app import server
    importlib.reload(server)
    # Isolate mutable module state into tmp so tests don't leak into each other
    # or touch the real repo's .ui-cache. (status-overrides is isolated by the
    # autouse conftest fixture via data.STATUS_OVERRIDES_FILE.)
    server.PUSHED_OVERRIDES_FILE = tmp_path / ".ui-cache" / "pushed-overrides.json"
    server.UI_CACHE = tmp_path / ".ui-cache" / "latest"
    return TestClient(server.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["applications_md_exists"] is True


def test_list_jobs(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    payload = r.json()
    assert payload["source"] == "applications"
    jobs = payload["rows"]
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["score_value"] == 4.2
    assert jobs[0]["report_num"] == "001"


def test_get_report_renders(client):
    r = client.get("/api/reports/001")
    assert r.status_code == 200
    assert "Acme" in r.text
    assert "Great match" in r.text


def test_get_report_404_for_unknown(client):
    r = client.get("/api/reports/999")
    assert r.status_code == 404


def test_index_served(client):
    # The SPA shell should be served at root.
    r = client.get("/")
    assert r.status_code == 200
    assert "Triage" in r.text


def test_run_triggers_workflow(client, mocker):
    from pipeline.app import server
    trigger = mocker.patch.object(server.gh, "trigger_workflow")
    r = client.post("/api/run")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    trigger.assert_called_once_with(server.DAILY_WORKFLOW)


def test_run_surfaces_gh_error(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.gh, "trigger_workflow",
                        side_effect=server.gh.GhError("gh not authenticated"))
    r = client.post("/api/run")
    assert r.status_code == 502
    assert "not authenticated" in r.json()["detail"]


def test_refresh_404_when_no_runs(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.gh, "latest_successful_run", return_value=None)
    r = client.post("/api/refresh")
    assert r.status_code == 404


def test_refresh_downloads_and_repoints(client, tmp_path, mocker):
    from pipeline.app import server
    art = tmp_path / "dl" / "pipeline-output-7"
    (art / "data").mkdir(parents=True)
    (art / "reports").mkdir(parents=True)
    (art / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 9 | 2026-05-27 | Refreshed Co | Eng | 4.9/5 | Evaluated | ❌ | [009](reports/009-x.md) | APPLY |\n",
        encoding="utf-8",
    )
    mocker.patch.object(server.gh, "latest_successful_run",
                        return_value={"databaseId": 7, "createdAt": "t", "displayTitle": "Daily"})
    mocker.patch.object(server.gh, "download_artifact", return_value=art)
    r = client.post("/api/refresh")
    assert r.status_code == 200
    assert r.json()["run_id"] == 7
    # Refresh merges the artifact INTO local; /api/jobs (always local) shows it.
    jobs = client.get("/api/jobs").json()
    assert any(row["company"] == "Refreshed Co" for row in jobs["rows"])


def test_set_status_records_override(client):
    r = client.post("/api/status", json={"num": "1", "status": "Applied"})
    assert r.status_code == 200
    assert r.json()["pending"] == 1
    # /api/jobs should now overlay the pending status on that row.
    jobs = client.get("/api/jobs").json()
    assert jobs["pending"] == 1
    row = jobs["rows"][0]
    assert row["status_canonical"] == "Applied"
    assert row["pending"] is True


def test_set_status_rejects_unknown(client):
    r = client.post("/api/status", json={"num": "1", "status": "Bogus"})
    assert r.status_code == 400
    assert "Unknown status" in r.json()["detail"]


def test_refresh_offline_keeps_local_intact(client, mocker):
    # No credits / gh error: Refresh must not wipe or alter the local tracker —
    # the user keeps working against the last-synced data.
    from pipeline.app import server
    local_apps = server._career_ops_local() / "data" / "applications.md"
    before = local_apps.read_text(encoding="utf-8")
    mocker.patch.object(server.gh, "latest_successful_run",
                        side_effect=server.gh.GhError("billing limit reached"))
    r = client.post("/api/refresh")
    assert r.status_code == 502
    assert "last-synced" in r.json()["detail"]
    assert local_apps.read_text(encoding="utf-8") == before


def test_template_status_reports_available(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.self_update, "update_available",
                        return_value={"available": True, "template_sha": "abc123"})
    r = client.get("/api/template/status")
    assert r.status_code == 200
    assert r.json() == {"available": True, "template_sha": "abc123"}


def test_template_update_success(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.self_update, "apply_update",
                        return_value={"ok": True, "updated": True})
    r = client.post("/api/template/update")
    assert r.status_code == 200
    assert r.json()["updated"] is True


def test_template_update_conflict_returns_409(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.self_update, "apply_update",
                        return_value={"ok": False, "conflict": True, "error": "merge conflict"})
    r = client.post("/api/template/update")
    assert r.status_code == 409
    assert "conflict" in r.json()["detail"].lower()


def test_reset_requires_confirm(client, mocker):
    from pipeline.app import server
    rj = mocker.patch.object(server.reset, "reset_job_search")
    r = client.post("/api/reset", json={"confirm": "nope"})
    assert r.status_code == 400
    rj.assert_not_called()   # nothing wiped without the exact confirmation


def test_reset_runs_and_clears_cloud(client, mocker):
    from pipeline.app import server
    rj = mocker.patch.object(server.reset, "reset_job_search", return_value={"removed": ["a"], "count": 1})
    cc = mocker.patch.object(server.reset, "clear_cloud_caches", return_value={"deleted": ["pipeline-state-v1-1"]})
    r = client.post("/api/reset", json={"confirm": "RESET", "clear_cloud": True})
    assert r.status_code == 200
    rj.assert_called_once()
    cc.assert_called_once()
    assert r.json()["cloud"]["deleted"] == ["pipeline-state-v1-1"]


def test_reset_local_only(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.reset, "reset_job_search", return_value={"removed": []})
    cc = mocker.patch.object(server.reset, "clear_cloud_caches")
    r = client.post("/api/reset", json={"confirm": "RESET", "clear_cloud": False})
    assert r.status_code == 200
    cc.assert_not_called()


def test_reset_succeeds_even_if_cloud_clear_fails(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.reset, "reset_job_search", return_value={"removed": []})
    mocker.patch.object(server.reset, "clear_cloud_caches",
                        side_effect=server.gh.GhError("not authenticated"))
    r = client.post("/api/reset", json={"confirm": "RESET", "clear_cloud": True})
    assert r.status_code == 200
    assert "cloud_error" in r.json()


def test_push_status_400_when_nothing_pending(client):
    r = client.post("/api/push-status")
    assert r.status_code == 400


def test_push_status_refreshes_applies_and_dispatches(client, tmp_path, mocker):
    from pipeline.app import server
    # Pending change: mark role #1 Applied.
    client.post("/api/status", json={"num": "1", "status": "Applied"})

    # Fresh base from "the cloud" — includes a NEW row #2 the local copy lacks,
    # to prove the refresh-before-write guard preserves cloud-added rows.
    fresh = tmp_path / "fresh" / "pipeline-output-9"
    (fresh / "data").mkdir(parents=True)
    (fresh / "reports").mkdir(parents=True)
    (fresh / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-05-27 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme.md) | APPLY |\n"
        "| 2 | 2026-05-28 | NewCo | Dev | 3.9/5 | Evaluated | ❌ | [002](reports/002-newco.md) | new |\n",
        encoding="utf-8",
    )
    mocker.patch.object(server.gh, "latest_successful_run", return_value={"databaseId": 9})
    mocker.patch.object(server.gh, "download_artifact", return_value=fresh)
    trigger = mocker.patch.object(server.gh, "trigger_workflow")

    r = client.post("/api/push-status")
    assert r.status_code == 200
    body = r.json()
    assert body["pushed"] == 1
    assert body["base"] == "refreshed"

    # edit-tracker was dispatched with a status_overrides_json payload.
    trigger.assert_called_once()
    wf, fields = trigger.call_args.args[0], trigger.call_args.args[1]
    assert wf == server.EDIT_WORKFLOW
    import json
    overrides = json.loads(fields["status_overrides_json"])
    assert overrides == {"1": "Applied"}

    # Pending cleared after a successful push.
    assert client.get("/api/jobs").json()["pending"] == 0


def test_pushed_change_survives_post_push_reload_and_refresh(client, tmp_path, mocker):
    """A pushed status must stay visible across the post-push board reload AND a
    later Refresh, until a genuinely fresh pipeline run incorporates it. The bug:
    push edits the downloaded artifact copy, so the post-push /api/jobs reload
    self-cleans the pushed-override bridge against a copy push itself changed —
    and the next Refresh (re-downloading the still-unchanged cloud artifact) then
    shows the status reverting (it 'vanishes')."""
    from pipeline.app import server
    import json

    def artifact(name):
        # The cloud tracker still shows #1 as Evaluated (it hasn't incorporated
        # the pushed change yet — that happens on the next pipeline run).
        d = tmp_path / name / "pipeline-output-9"
        (d / "data").mkdir(parents=True)
        (d / "data" / "applications.md").write_text(
            "# Applications Tracker\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-05-27 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme.md) | APPLY |\n",
            encoding="utf-8")
        return d

    client.post("/api/status", json={"num": "1", "status": "Applied"})
    mocker.patch.object(server.gh, "latest_successful_run", return_value={"databaseId": 9})
    mocker.patch.object(server.gh, "download_artifact", return_value=artifact("push"))
    mocker.patch.object(server.gh, "trigger_workflow")
    assert client.post("/api/push-status").json()["pushed"] == 1

    client.get("/api/jobs")  # the UI reloads the board right after a push

    # User clicks Refresh to check the remote — re-downloads the (unchanged) artifact.
    mocker.patch.object(server.gh, "download_artifact", return_value=artifact("refresh"))
    client.post("/api/refresh")

    rows = {r["num"]: r for r in client.get("/api/jobs").json()["rows"]}
    assert rows["1"]["status_canonical"] == "Applied"        # must NOT have vanished
    pushed = json.loads((tmp_path / ".ui-cache" / "pushed-overrides.json").read_text())
    assert pushed == {"1": "Applied"}                        # bridge retained until a fresh run


def test_push_status_falls_back_to_local_when_refresh_fails(client, mocker):
    from pipeline.app import server
    client.post("/api/status", json={"num": "1", "status": "Interview"})
    # No runs available → refresh can't produce a fresh base.
    mocker.patch.object(server.gh, "latest_successful_run", return_value=None)
    trigger = mocker.patch.object(server.gh, "trigger_workflow")

    r = client.post("/api/push-status")
    assert r.status_code == 200
    assert r.json()["base"] == "local"
    import json
    overrides = json.loads(trigger.call_args.args[1]["status_overrides_json"])
    assert overrides == {"1": "Interview"}


def test_push_status_surfaces_gh_error(client, mocker):
    from pipeline.app import server
    client.post("/api/status", json={"num": "1", "status": "Applied"})
    mocker.patch.object(server.gh, "latest_successful_run", return_value=None)
    mocker.patch.object(server.gh, "trigger_workflow",
                        side_effect=server.gh.GhError("edit-tracker.yml not found"))
    r = client.post("/api/push-status")
    assert r.status_code == 502
    assert "edit-tracker" in r.json()["detail"]
    # The edit stays pending so it can be re-pushed once credits return.
    assert client.get("/api/jobs").json()["pending"] == 1


def _fresh_base_with_acme(tmp_path):
    """A cloud base containing only Acme/Eng (row 1) — used to make foreign-
    identity overrides resolve (Acme) or not (anything else)."""
    fresh = tmp_path / "fresh" / "pipeline-output-9"
    (fresh / "data").mkdir(parents=True)
    (fresh / "data" / "applications.md").write_text(
        "# Applications Tracker\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-05-27 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme.md) | x |\n",
        encoding="utf-8")
    return fresh


def test_push_clears_unresolved_discards_but_keeps_other_unresolved(client, tmp_path, mocker):
    """An unresolved DISCARD override (a closed role absent from the cloud
    tracker) is dropped on push — it's already applied locally, a closed role
    won't reappear to match later, and the cloud's own recheck is the backstop.
    An unresolved non-Discard (e.g. Applied) is still KEPT for a later push."""
    from pipeline.app import server, data
    import json
    data.record_status_override("1", "Applied", company="Acme", role="Eng")          # resolves
    data.record_status_override("90", "Discarded", company="GhostCo", role="Closed")  # unresolved Discard
    data.record_status_override("91", "Applied", company="OtherCo", role="Dev")       # unresolved non-Discard

    mocker.patch.object(server.gh, "latest_successful_run", return_value={"databaseId": 9})
    mocker.patch.object(server.gh, "download_artifact", return_value=_fresh_base_with_acme(tmp_path))
    mocker.patch.object(server.gh, "trigger_workflow")

    body = client.post("/api/push-status").json()
    assert body["pushed"] == 1            # Acme/Eng resolved + dispatched
    assert body["unresolved"] == 1        # only the kept Applied (91) — NOT the Discard
    remaining = json.loads(data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
    assert set(remaining) == {"91"}       # 1 pushed+cleared, 90 (Discard) cleared, 91 kept


def test_push_clears_unresolved_discards_when_nothing_resolves(client, tmp_path, mocker):
    """The real-world case: only unresolved Discards, none match the cloud base.
    They're still cleared (not kept forever to nag every push) and no empty
    edit-tracker run is dispatched."""
    from pipeline.app import server, data
    import json
    data.record_status_override("90", "Discarded", company="GhostCo", role="Closed")

    mocker.patch.object(server.gh, "latest_successful_run", return_value={"databaseId": 9})
    mocker.patch.object(server.gh, "download_artifact", return_value=_fresh_base_with_acme(tmp_path))
    trigger = mocker.patch.object(server.gh, "trigger_workflow")

    body = client.post("/api/push-status").json()
    assert body["pushed"] == 0
    assert body["unresolved"] == 0        # the stale Discard was cleared, not kept
    assert json.loads(data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8")) == {}
    trigger.assert_not_called()           # nothing resolved → no dispatch


# ── Onboarding (Phase 3) ───────────────────────────────────────────────────

def test_onboard_status_reports_readiness(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.gh, "current_repo", return_value="me/private")
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.gh, "list_secret_names", return_value=[
        "SEARCH_CONFIG_B64", "RESUME_TXT_B64", "CV_MD_B64", "PROFILE_YML_B64", "GEMINI_API_KEY",
    ])
    r = client.get("/api/onboard/status")
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == "me/private"
    assert body["ready"] is True


def test_onboard_status_not_ready_without_provider(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.gh, "current_repo", return_value="me/private")
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.gh, "list_secret_names", return_value=[
        "SEARCH_CONFIG_B64", "RESUME_TXT_B64", "CV_MD_B64", "PROFILE_YML_B64",
    ])
    assert client.get("/api/onboard/status").json()["ready"] is False


def _onboard_post(client, form):
    import json as _json
    return client.post(
        "/api/onboard",
        files={"resume": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"form": _json.dumps(form)},
    )


def test_onboard_writes_secrets_on_private_repo(client, tmp_path, mocker):
    from pipeline.app import server
    mocker.patch.object(server, "ROOT", tmp_path)  # don't write into the real repo
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.gh, "current_repo", return_value="me/private")
    mocker.patch.object(server.onboard, "extract_pdf_text", return_value="resume text body")
    gen = mocker.patch.object(server.onboard, "run_generation", return_value={"ok": True})
    mocker.patch.object(server.onboard, "collect_secret_blobs", return_value={
        "SEARCH_CONFIG_B64": "AA", "RESUME_TXT_B64": "BB",
        "CV_MD_B64": "CC", "PROFILE_YML_B64": "DD", "PROFILE_MD_B64": "EE",
    })
    set_secret = mocker.patch.object(server.gh, "set_secret")
    set_var = mocker.patch.object(server.gh, "set_variable")

    r = _onboard_post(client, {"name": "Jane", "provider": "gemini",
                               "api_key": "key-123", "batch_model": "gemini-2.5-flash"})
    assert r.status_code == 200
    written = r.json()["secrets_written"]
    assert "GEMINI_API_KEY" in written and "PROFILE_YML_B64" in written
    # Provider key written with the raw key; artifact secret with the blob.
    set_secret.assert_any_call("GEMINI_API_KEY", "key-123")
    set_secret.assert_any_call("PROFILE_YML_B64", "DD")
    set_var.assert_any_call("BATCH_PROVIDER", "gemini")
    set_var.assert_any_call("BATCH_MODEL", "gemini-2.5-flash")
    # Resume artifacts persisted under the (patched) ROOT.
    assert (tmp_path / "resumes" / "resume.pdf").exists()
    assert (tmp_path / "resumes" / "resume.txt").read_text(encoding="utf-8") == "resume text body"
    gen.assert_called_once()


def test_onboard_refuses_public_repo(client, tmp_path, mocker):
    from pipeline.app import server
    mocker.patch.object(server, "ROOT", tmp_path)
    mocker.patch.object(server.gh, "repo_visibility", return_value="PUBLIC")
    set_secret = mocker.patch.object(server.gh, "set_secret")
    r = _onboard_post(client, {"provider": "gemini", "api_key": "k"})
    assert r.status_code == 409
    assert "PUBLIC" in r.json()["detail"]
    set_secret.assert_not_called()  # wrote nothing


def test_onboard_rejects_unknown_provider(client, mocker):
    from pipeline.app import server
    # provider validated before any gh call
    r = _onboard_post(client, {"provider": "bogus", "api_key": "k"})
    assert r.status_code == 400
    assert "Unknown provider" in r.json()["detail"]


def test_onboard_rejects_unreadable_pdf(client, tmp_path, mocker):
    from pipeline.app import server
    mocker.patch.object(server, "ROOT", tmp_path)
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.onboard, "extract_pdf_text",
                        side_effect=Exception("not a pdf"))
    r = _onboard_post(client, {"provider": "gemini", "api_key": "k"})
    assert r.status_code == 400
    assert "could not read PDF" in r.json()["detail"]


def test_onboard_parse_resume_autofills_from_text(client, mocker):
    from pipeline.app import server
    # Mock PDF extraction; parse_resume_info runs for real on the text.
    mocker.patch.object(
        server.onboard, "extract_pdf_text",
        return_value="Jane Dev\njane@example.com | Dallas, TX | github.com/janedev\n",
    )
    r = client.post(
        "/api/onboard/parse-resume",
        files={"resume": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 200
    info = r.json()
    assert info["name"] == "Jane Dev"
    assert info["email"] == "jane@example.com"
    assert info["location"] == "Dallas, TX"
    assert info["github"] == "github.com/janedev"


def test_onboard_parse_resume_rejects_unreadable_pdf(client, mocker):
    from pipeline.app import server
    mocker.patch.object(server.onboard, "extract_pdf_text",
                        side_effect=Exception("not a pdf"))
    r = client.post(
        "/api/onboard/parse-resume",
        files={"resume": ("resume.pdf", b"%PDF junk", "application/pdf")},
    )
    assert r.status_code == 400
    assert "could not read PDF" in r.json()["detail"]


def test_onboard_load_config_returns_null_when_no_sidecar(client, tmp_path, mocker):
    # First-time setup: no sidecar, no persisted resume. UI should treat this
    # as "fresh wizard, nothing to prefill".
    from pipeline.app import server
    mocker.patch.object(server, "ROOT", tmp_path)
    r = client.get("/api/onboard/load-config")
    assert r.status_code == 200
    body = r.json()
    assert body["form"] is None
    assert body["has_resume"] is False


def test_onboard_load_config_returns_saved_payload(client, tmp_path, mocker):
    # After a successful onboard, the sidecar is written and load returns it
    # so the wizard can prefill. has_resume reflects the persisted PDF.
    from pipeline.app import server
    import json as _json
    mocker.patch.object(server, "ROOT", tmp_path)
    (tmp_path / ".ui-cache").mkdir()
    (tmp_path / ".ui-cache" / "onboarding.json").write_text(
        _json.dumps({"name": "Jane", "results_wanted": 5, "sites": ["indeed"]}),
        encoding="utf-8",
    )
    (tmp_path / "resumes").mkdir()
    (tmp_path / "resumes" / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
    r = client.get("/api/onboard/load-config")
    assert r.status_code == 200
    body = r.json()
    assert body["form"] == {"name": "Jane", "results_wanted": 5, "sites": ["indeed"]}
    assert body["has_resume"] is True


def test_onboard_writes_sidecar_after_successful_submit(client, tmp_path, mocker):
    # The sidecar mirrors what the wizard will need to prefill — every field
    # the user submitted, minus the API key (which lives only in Secrets).
    from pipeline.app import server
    import json as _json
    mocker.patch.object(server, "ROOT", tmp_path)
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.gh, "current_repo", return_value="me/private")
    mocker.patch.object(server.onboard, "extract_pdf_text", return_value="resume text")
    mocker.patch.object(server.onboard, "run_generation", return_value={"ok": True})
    mocker.patch.object(server.onboard, "collect_secret_blobs", return_value={
        "SEARCH_CONFIG_B64": "AA", "RESUME_TXT_B64": "BB",
        "CV_MD_B64": "CC", "PROFILE_YML_B64": "DD",
    })
    mocker.patch.object(server.gh, "set_secret")
    mocker.patch.object(server.gh, "set_variable")
    r = _onboard_post(client, {"name": "Jane", "provider": "gemini",
                               "api_key": "supersecret",
                               "results_wanted": 5, "sites": ["indeed"]})
    assert r.status_code == 200
    sidecar = tmp_path / ".ui-cache" / "onboarding.json"
    assert sidecar.exists()
    saved = _json.loads(sidecar.read_text(encoding="utf-8"))
    # API key MUST be excluded so the sidecar is safe on disk.
    assert "api_key" not in saved
    # Everything else round-trips so the next wizard visit can prefill.
    assert saved["name"] == "Jane"
    assert saved["results_wanted"] == 5
    assert saved["provider"] == "gemini"


def test_onboard_reuses_existing_resume_when_none_uploaded(client, tmp_path, mocker):
    # Edit-mode flow: the user is tweaking config and didn't re-upload the
    # resume. Server reuses resumes/resume.txt instead of erroring out.
    from pipeline.app import server
    import json as _json
    mocker.patch.object(server, "ROOT", tmp_path)
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    (resumes / "resume.pdf").write_bytes(b"%PDF-1.4 prior")
    (resumes / "resume.txt").write_text("existing resume text", encoding="utf-8")
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.gh, "current_repo", return_value="me/private")
    # extract_pdf_text MUST NOT be called — we're reusing the .txt directly.
    extract = mocker.patch.object(server.onboard, "extract_pdf_text",
                                  side_effect=AssertionError("unexpected PDF extract"))
    build = mocker.patch.object(server.onboard, "build_onboarding_json",
                                wraps=server.onboard.build_onboarding_json)
    mocker.patch.object(server.onboard, "run_generation", return_value={"ok": True})
    mocker.patch.object(server.onboard, "collect_secret_blobs", return_value={
        "SEARCH_CONFIG_B64": "AA", "RESUME_TXT_B64": "BB",
        "CV_MD_B64": "CC", "PROFILE_YML_B64": "DD",
    })
    mocker.patch.object(server.gh, "set_secret")
    mocker.patch.object(server.gh, "set_variable")

    # Post WITHOUT a resume file — just the form.
    r = client.post("/api/onboard", data={"form": _json.dumps({"results_wanted": 5})})
    assert r.status_code == 200, r.json()
    extract.assert_not_called()
    # The persisted resume text was the one fed to the generator.
    _, kwargs_payload = build.call_args.args, build.call_args.kwargs
    # build_onboarding_json(form, resume_text) — positional.
    assert build.call_args.args[1] == "existing resume text"


def test_onboard_errors_when_no_resume_anywhere(client, tmp_path, mocker):
    # First-time setup with no PDF upload AND no persisted resume: clear error.
    from pipeline.app import server
    import json as _json
    mocker.patch.object(server, "ROOT", tmp_path)
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    r = client.post("/api/onboard", data={"form": _json.dumps({})})
    assert r.status_code == 400
    assert "resume" in r.json()["detail"].lower()


def test_onboard_skips_provider_key_write_when_api_key_blank(client, tmp_path, mocker):
    # Edit-mode flow: user changed search settings but didn't re-paste their
    # API key. Provider/key writes should be skipped; artifact writes proceed.
    from pipeline.app import server
    mocker.patch.object(server, "ROOT", tmp_path)
    mocker.patch.object(server.gh, "repo_visibility", return_value="PRIVATE")
    mocker.patch.object(server.gh, "current_repo", return_value="me/private")
    mocker.patch.object(server.onboard, "extract_pdf_text", return_value="resume text")
    mocker.patch.object(server.onboard, "run_generation", return_value={"ok": True})
    mocker.patch.object(server.onboard, "collect_secret_blobs", return_value={
        "SEARCH_CONFIG_B64": "AA", "RESUME_TXT_B64": "BB",
        "CV_MD_B64": "CC", "PROFILE_YML_B64": "DD",
    })
    set_secret = mocker.patch.object(server.gh, "set_secret")
    set_var = mocker.patch.object(server.gh, "set_variable")
    r = _onboard_post(client, {"provider": "gemini", "api_key": ""})  # blank key
    assert r.status_code == 200
    written = r.json()["secrets_written"]
    # Artifact secrets written; provider key NOT.
    assert "PROFILE_YML_B64" in written
    assert "GEMINI_API_KEY" not in written
    # No provider/model variable writes either — nothing changed there.
    for call in set_secret.call_args_list:
        assert call.args[0] != "GEMINI_API_KEY"
    for call in set_var.call_args_list:
        assert call.args[0] != "BATCH_PROVIDER"


class TestStaticNoCache:
    """SPA assets must revalidate every load (no-cache), so a UI change isn't
    masked by a stale cached app.js/onboard.js until a manual hard refresh."""

    def test_spa_asset_no_cache(self, client):
        r = client.get("/app.js")
        assert r.status_code == 200
        assert "no-cache" in r.headers.get("cache-control", "").lower()

    def test_onboard_html_no_cache(self, client):
        r = client.get("/onboard")
        assert r.status_code == 200
        assert "no-cache" in r.headers.get("cache-control", "").lower()
