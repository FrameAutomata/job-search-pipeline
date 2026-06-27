"""Tests for the UI review-and-submit apply flow (server endpoints + worker).

The Playwright engine (browser.launch / linkedin.apply_to / submit_application)
is mocked — the same convention as the add-job LLM tests; the real form-walking
is verified manually via `--apply-mode dry-run`. These cover the server
orchestration: the task lifecycle (pending -> ready -> submitted/cancelled),
single-flight, the not-logged-in / not-easy-apply bails, and the
submit-marks-Applied write-back through the identity-anchored override channel.

Design under test (per the Phase-2 plan):
- POST /api/jobs/apply-async {num}: start a visible review session for tracker
  row `num`. 404 unknown num, 400 if the URL isn't navigable, 409 if a session is
  already active. LinkedIn/Indeed use deterministic engines; off-site ATS the
  agentic catch-all. Returns {job_id}.
- GET  /api/jobs/apply-status/{job_id}: {status, company, role, num, answers,
  needs_review, code, reason}.
- POST /api/jobs/apply-submit/{job_id}: only valid from "ready"; signals the
  worker to click Submit. 409 otherwise.
- POST /api/jobs/apply-cancel/{job_id}: signals the worker to close without
  submitting.

The worker holds ONE live browser open between fill and submit (Playwright is
thread-affine, so it blocks on a decision Event); submit clicks Submit and marks
the row Applied via record_status_override (carrying company/role identity).
"""

import contextlib
import importlib
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pipeline.apply import browser as browser_mod  # noqa: E402
from pipeline.apply import linkedin as linkedin_mod  # noqa: E402
from pipeline.apply import indeed as indeed_mod  # noqa: E402
from pipeline.apply import agent_engine as agent_engine_mod  # noqa: E402
from pipeline.apply.result import ApplyResult, APPLIED, EXPIRED, LOGIN_ISSUE, NEEDS_HUMAN  # noqa: E402


_TRACKER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-06-01 | Acme | Engineer | 4.5/5 | Evaluated | ❌ | [001](reports/001.md) | "
    "https://www.linkedin.com/jobs/view/123 — strong fit |\n"
    "| 2 | 2026-06-01 | Globex | Dev | 4.2/5 | Evaluated | ❌ | [002](reports/002.md) | "
    "https://boards.greenhouse.io/x/jobs/9 — offsite ATS |\n"
    "| 3 | 2026-06-01 | Initech | SRE | 4.8/5 | Evaluated | ❌ | [003](reports/003.md) | "
    "https://www.indeed.com/viewjob?jk=abc123 — indeed smartapply |\n"
)


@pytest.fixture
def engine(monkeypatch):
    """Mock the Playwright engine and record what the worker drives. `result`
    and `submit_result` are overridable per test; `calls` records submit clicks."""
    state = {
        "calls": [],
        "logged_in": True,
        "result": ApplyResult(
            code=APPLIED, reason="not submitted (mode=review)", submitted=False,
            answers=(("First name", "Tom"), ("Desired salary", "185000")),
        ),
        "submit_result": ApplyResult(code=APPLIED, submitted=True),
    }

    @contextlib.contextmanager
    def fake_launch(headless=False, user_data_dir=None):
        state["calls"].append(("launch", headless))
        try:
            yield object()           # a dummy page; the engine fns are mocked
        finally:
            state["calls"].append(("close", None))

    def fake_apply_to(page, job, eng, *, mode, resume_path=None, should_cancel=None):
        state["calls"].append(("apply_to", mode))
        # cancellable_fill: simulate a long multi-step fill that bails at the next
        # step boundary when the worker's should_cancel hook fires (#2).
        if state.get("cancellable_fill"):
            for _ in range(500):
                if should_cancel and should_cancel():
                    return ApplyResult(code="cancelled", reason="cancelled during fill")
                time.sleep(0.01)
        return state["result"]

    def fake_submit(page):
        state["calls"].append(("submit", None))
        fn = state.get("submit_fn")
        return fn(page) if fn else state["submit_result"]

    def fake_resume(page):
        # Continue-after-human turn; returns a held "ready" by default.
        state["calls"].append(("resume", None))
        return state.get("resume_result") or ApplyResult(
            code=APPLIED, reason="resumed after human", submitted=False,
            answers=(("First name", "Tom"),))

    @contextlib.contextmanager
    def fake_launch_indeed(headless=False, user_data_dir=None):
        state["calls"].append(("launch_indeed", headless))
        try:
            yield object()
        finally:
            state["calls"].append(("close", None))

    @contextlib.contextmanager
    def fake_launch_session(*, headless=False, cdp_port=None, user_data_dir=None,
                            channel="chrome"):
        state["calls"].append(("launch_session", headless))
        try:
            yield object()           # a stand-in Session; agent_engine is mocked
        finally:
            state["calls"].append(("close", None))

    monkeypatch.setattr(browser_mod, "launch", fake_launch)
    monkeypatch.setattr(browser_mod, "ensure_logged_in",
                        lambda page, *, headless, **kw: state["logged_in"])
    monkeypatch.setattr(linkedin_mod, "apply_to", fake_apply_to)
    monkeypatch.setattr(linkedin_mod, "submit_application", fake_submit, raising=False)
    # Indeed path: launch_indeed + is_logged_in_indeed + the indeed engine, all
    # mocked onto the same fakes so the worker can drive either platform.
    monkeypatch.setattr(browser_mod, "launch_indeed", fake_launch_indeed, raising=False)
    monkeypatch.setattr(browser_mod, "is_logged_in_indeed",
                        lambda page, **kw: state["logged_in"], raising=False)
    monkeypatch.setattr(indeed_mod, "apply_to", fake_apply_to)
    monkeypatch.setattr(indeed_mod, "submit_application", fake_submit, raising=False)
    # Agent path: launch_session (real Chrome + CDP) + the agentic engine, on the
    # same fakes so the worker drives any of the three platforms.
    monkeypatch.setattr(browser_mod, "launch_session", fake_launch_session, raising=False)
    monkeypatch.setattr(agent_engine_mod, "apply_to", fake_apply_to)
    monkeypatch.setattr(agent_engine_mod, "submit_application", fake_submit, raising=False)
    monkeypatch.setattr(agent_engine_mod, "resume_after_human", fake_resume, raising=False)
    # Short hold so a worker left blocking on the decision Event (a test that
    # asserts "ready" but never submits/cancels) dies fast instead of lingering.
    monkeypatch.setenv("APPLY_HOLD_TIMEOUT", "2")
    yield state
    # Drain any worker still blocked on the hold so daemon threads don't linger
    # past the test (the mocked browser closes instantly once they wake).
    try:
        from pipeline.app import server
        with server._apply_lock:
            for t in server._apply_tasks.values():
                ev = t.get("event")
                if ev is not None and t.get("status") in server._APPLY_ACTIVE:
                    t["decision"] = "cancel"
                    ev.set()
    except Exception:
        pass


@pytest.fixture
def client(tmp_path, monkeypatch, engine):
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    (co / "data" / "applications.md").write_text(_TRACKER, encoding="utf-8")
    monkeypatch.setenv("CAREER_OPS_PATH", str(co))
    from pipeline.app import server
    importlib.reload(server)
    return TestClient(server.app)


def _wait_status(client, job_id, targets, timeout=5.0):
    """Poll apply-status until status is in `targets` (or timeout). Returns the
    last status body."""
    deadline = time.time() + timeout
    body = {}
    while time.time() < deadline:
        body = client.get(f"/api/jobs/apply-status/{job_id}").json()
        if body.get("status") in targets:
            return body
        time.sleep(0.02)
    return body


class TestApplyAsyncStart:
    def test_unknown_num_404(self, client, engine):
        assert client.post("/api/jobs/apply-async", json={"num": "999"}).status_code == 404

    def test_indeed_url_admitted(self, client, engine):
        # An Indeed SmartApply URL is now accepted (not 400) — starts a session.
        r = client.post("/api/jobs/apply-async", json={"num": "3"})
        assert r.status_code == 200 and "job_id" in r.json()

    def test_indeed_worker_drives_indeed_engine_to_submitted(self, client, engine):
        job_id = client.post("/api/jobs/apply-async", json={"num": "3"}).json()["job_id"]
        assert _wait_status(client, job_id, {"ready", "failed"}).get("status") == "ready"
        client.post(f"/api/jobs/apply-submit/{job_id}")
        assert _wait_status(client, job_id, {"submitted", "failed"}).get("status") == "submitted"
        assert ("launch_indeed", False) in engine["calls"]   # drove the Indeed browser

    def test_offsite_url_admitted(self, client, engine):
        # Row 2 is an off-site greenhouse ATS — now driven by the agentic catch-all,
        # so it's admitted (was 400 before the agent engine was wired into the UI).
        r = client.post("/api/jobs/apply-async", json={"num": "2"})
        assert r.status_code == 200 and "job_id" in r.json()

    def test_agent_worker_drives_agent_engine_to_submitted(self, client, engine):
        # The off-site row runs through the agentic engine on its CDP browser, holds
        # at review, then submits — same review-hold lifecycle as the deterministic
        # engines, just a different launch + engine module.
        start = client.post("/api/jobs/apply-async", json={"num": "2"})
        assert start.status_code == 200
        job_id = start.json()["job_id"]
        assert _wait_status(client, job_id, {"ready", "failed"}).get("status") == "ready"
        client.post(f"/api/jobs/apply-submit/{job_id}")
        assert _wait_status(client, job_id, {"submitted", "failed"}).get("status") == "submitted"
        assert ("launch_session", False) in engine["calls"]   # drove the agent's CDP browser

    def test_valid_num_returns_job_id(self, client, engine):
        r = client.post("/api/jobs/apply-async", json={"num": "1"})
        assert r.status_code == 200
        assert r.json().get("job_id")


class TestReviewLifecycle:
    def _start(self, client):
        return client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]

    def test_reaches_ready_with_drafted_answers(self, client, engine):
        job_id = self._start(client)
        s = _wait_status(client, job_id, {"ready", "failed"})
        assert s["status"] == "ready"
        assert s["company"] == "Acme" and s["role"] == "Engineer" and s["num"] == "1"
        assert ["First name", "Tom"] in s["answers"]
        # Cancel to release the held worker.
        client.post(f"/api/jobs/apply-cancel/{job_id}")

    def test_submit_clicks_and_marks_applied(self, client, engine, tmp_path):
        import json
        from pipeline.app import data as app_data
        job_id = self._start(client)
        _wait_status(client, job_id, {"ready"})
        r = client.post(f"/api/jobs/apply-submit/{job_id}")
        assert r.status_code == 200
        s = _wait_status(client, job_id, {"submitted", "failed"})
        assert s["status"] == "submitted"
        assert ("submit", None) in engine["calls"]            # Submit was actually clicked
        # Marked Applied via the identity-anchored override channel.
        ov = json.loads(app_data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
        assert ov == {"1": {"status": "Applied", "company": "Acme", "role": "Engineer"}}
        # And in the local tracker copy.
        apps = tmp_path / "career-ops" / "data" / "applications.md"
        assert "| Applied |" in apps.read_text(encoding="utf-8")

    def test_cancel_does_not_submit_or_mark(self, client, engine):
        from pipeline.app import data as app_data
        job_id = self._start(client)
        _wait_status(client, job_id, {"ready"})
        assert client.post(f"/api/jobs/apply-cancel/{job_id}").status_code == 200
        s = _wait_status(client, job_id, {"cancelled", "failed"})
        assert s["status"] == "cancelled"
        assert ("submit", None) not in engine["calls"]        # never clicked Submit
        assert not app_data.STATUS_OVERRIDES_FILE.exists()

    def test_submit_before_ready_409(self, client, engine):
        # Make the engine never reach ready (not logged in) so the task fails fast,
        # then a submit on a non-ready task is rejected.
        engine["logged_in"] = False
        job_id = self._start(client)
        _wait_status(client, job_id, {"failed"})
        assert client.post(f"/api/jobs/apply-submit/{job_id}").status_code == 409


class TestNeedsHumanFlow:
    """The agent parks on a CAPTCHA it can't clear (RESULT:NEEDS_HUMAN); the worker
    holds, the user solves it in the open browser and hits Continue, then a resume
    turn finishes — surfaced for review like any other ready session."""

    def _start_agent(self, client):
        # Row 2 is an off-site greenhouse role -> the agentic engine.
        return client.post("/api/jobs/apply-async", json={"num": "2"}).json()["job_id"]

    def test_holds_for_human_then_resumes_to_ready(self, client, engine):
        engine["result"] = ApplyResult(code=NEEDS_HUMAN, reason="captcha at the door")
        job_id = self._start_agent(client)
        assert _wait_status(client, job_id, {"needs_human", "ready", "failed"})["status"] == "needs_human"
        assert client.post(f"/api/jobs/apply-continue/{job_id}").status_code == 200
        s = _wait_status(client, job_id, {"ready", "failed"})
        assert s["status"] == "ready"
        assert ("resume", None) in engine["calls"]          # the resume turn ran

    def test_cancel_during_needs_human(self, client, engine):
        engine["result"] = ApplyResult(code=NEEDS_HUMAN, reason="captcha")
        job_id = self._start_agent(client)
        _wait_status(client, job_id, {"needs_human"})
        assert client.post(f"/api/jobs/apply-cancel/{job_id}").status_code == 200
        assert _wait_status(client, job_id, {"cancelled", "failed"})["status"] == "cancelled"

    def test_continue_rejected_when_not_awaiting_human(self, client, engine):
        # A ready (LinkedIn Easy Apply) session isn't awaiting a human -> 409.
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        _wait_status(client, job_id, {"ready"})
        assert client.post(f"/api/jobs/apply-continue/{job_id}").status_code == 409
        client.post(f"/api/jobs/apply-cancel/{job_id}")


class TestAutoApply:
    """Manual-review OFF (auto): the worker runs the engine in mode='auto' so it
    fills AND submits in one turn — marking Applied with no review hold. The
    default (no auto flag) stays manual review. A wall still pauses for the user
    (a captcha can't be auto-cleared), so 'at your own risk' auto is for the roles
    that don't need a human."""

    def test_auto_submits_without_review_hold(self, client, engine, tmp_path):
        import json as _json
        from pipeline.app import data as app_data
        engine["result"] = ApplyResult(code=APPLIED, submitted=True)   # filled + submitted in one turn
        job_id = client.post("/api/jobs/apply-async", json={"num": "1", "auto": True}).json()["job_id"]
        s = _wait_status(client, job_id, {"submitted", "ready", "failed"})
        assert s["status"] == "submitted"                 # straight to submitted — no review hold
        assert ("apply_to", "auto") in engine["calls"]    # drove the engine in auto mode
        ov = _json.loads(app_data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
        assert ov.get("1", {}).get("status") == "Applied"   # marked Applied

    def test_default_is_manual_review(self, client, engine):
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        assert _wait_status(client, job_id, {"ready", "failed"})["status"] == "ready"
        client.post(f"/api/jobs/apply-cancel/{job_id}")
        assert ("apply_to", "review") in engine["calls"]  # default stays review


class TestApplyDesktopNotification:
    """A native OS toast (the same pipeline/notify.py plyer mechanism the pipeline
    run uses) fires when a role is ready for review or needs the user at a wall —
    so a batch you've stepped away from pulls you back with an OS notification +
    sound, not a browser beep."""

    def test_ready_fires_os_notification(self, client, engine, monkeypatch):
        import pipeline.notify
        calls = []
        monkeypatch.setattr(pipeline.notify, "notify", lambda title, msg: calls.append((title, msg)))
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        _wait_status(client, job_id, {"ready", "failed"})
        client.post(f"/api/jobs/apply-cancel/{job_id}")
        assert any("Acme" in msg for _, msg in calls)   # company-bearing OS notification

    def test_needs_human_fires_os_notification(self, client, engine, monkeypatch):
        import pipeline.notify
        calls = []
        monkeypatch.setattr(pipeline.notify, "notify", lambda title, msg: calls.append((title, msg)))
        engine["result"] = ApplyResult(code=NEEDS_HUMAN, reason="captcha")
        job_id = client.post("/api/jobs/apply-async", json={"num": "2"}).json()["job_id"]  # agent role
        _wait_status(client, job_id, {"needs_human", "failed"})
        client.post(f"/api/jobs/apply-cancel/{job_id}")
        assert any("Globex" in msg for _, msg in calls)


class TestApplyQueue:
    """GET /api/jobs/apply-queue — the ordered roles a batch-apply run would walk:
    Evaluated, score >= min_score, highest first, every navigable role (not just
    LinkedIn). Read-only; the SPA drives apply-async per role from this list."""

    def test_returns_evaluated_roles_highest_score_first(self, client, engine):
        body = client.get("/api/jobs/apply-queue?min_score=4.0").json()
        assert [r["num"] for r in body["roles"]] == ["3", "1", "2"]   # 4.8, 4.5, 4.2
        top = body["roles"][0]
        assert top["company"] == "Initech" and top["score"] == 4.8

    def test_respects_min_score(self, client, engine):
        body = client.get("/api/jobs/apply-queue?min_score=4.6").json()
        assert [r["num"] for r in body["roles"]] == ["3"]            # only the 4.8 clears

    def test_includes_offsite_and_indeed_not_just_linkedin(self, client, engine):
        # The batch admits every navigable role the apply engine can drive (matching
        # apply-async), so the greenhouse (#2) and indeed (#3) rows are in, not only
        # the LinkedIn one — i.e. the endpoint must NOT use queue.select's
        # linkedin_only default.
        nums = [r["num"] for r in client.get("/api/jobs/apply-queue?min_score=4.0").json()["roles"]]
        assert {"1", "2", "3"} == set(nums)


class TestLoginWallFlow:
    """The agent hits an ATS sign-in / account-creation wall it can't pass on its
    own (RESULT:LOGIN_ISSUE). In the UI a human IS present, so the worker holds the
    same way it does for a CAPTCHA — the user signs in / creates the account in the
    open browser and hits Continue, then a resume turn finishes. The held status
    carries code=login_issue so the SPA shows sign-in copy, not "a CAPTCHA needs
    you". A deterministic engine (no resume turn) still fails fast on LOGIN_ISSUE."""

    def _start_agent(self, client):
        # Row 2 is an off-site greenhouse role -> the agentic (resumable) engine.
        return client.post("/api/jobs/apply-async", json={"num": "2"}).json()["job_id"]

    def test_login_wall_holds_then_resumes_to_ready(self, client, engine):
        engine["result"] = ApplyResult(code=LOGIN_ISSUE, reason="create an account to apply")
        job_id = self._start_agent(client)
        s = _wait_status(client, job_id, {"needs_human", "ready", "failed"})
        assert s["status"] == "needs_human"
        assert s["code"] == LOGIN_ISSUE      # surfaced so the SPA shows sign-in (not CAPTCHA) copy
        assert client.post(f"/api/jobs/apply-continue/{job_id}").status_code == 200
        s = _wait_status(client, job_id, {"ready", "failed"})
        assert s["status"] == "ready"
        assert ("resume", None) in engine["calls"]          # the resume turn ran

    def test_cancel_during_login_wall(self, client, engine):
        engine["result"] = ApplyResult(code=LOGIN_ISSUE, reason="signup wall")
        job_id = self._start_agent(client)
        _wait_status(client, job_id, {"needs_human"})
        assert client.post(f"/api/jobs/apply-cancel/{job_id}").status_code == 200
        assert _wait_status(client, job_id, {"cancelled", "failed"})["status"] == "cancelled"

    def test_deterministic_login_issue_fails_fast(self, client, engine):
        # Regression guard: a deterministic engine (LinkedIn, row 1) has no resume
        # turn, so a LOGIN_ISSUE from its fill must FAIL — never enter the human
        # hold (which would call a resume_after_human the engine doesn't have).
        engine["logged_in"] = True
        engine["result"] = ApplyResult(code=LOGIN_ISSUE, reason="session expired mid-fill")
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        s = _wait_status(client, job_id, {"failed", "needs_human", "ready"})
        assert s["status"] == "failed" and s["code"] == LOGIN_ISSUE


class TestSingleFlightAndBails:
    def test_single_flight_409(self, client, engine):
        first = client.post("/api/jobs/apply-async", json={"num": "1"})
        assert first.status_code == 200
        _wait_status(client, first.json()["job_id"], {"ready"})
        assert client.post("/api/jobs/apply-async", json={"num": "1"}).status_code == 409
        client.post(f"/api/jobs/apply-cancel/{first.json()['job_id']}")

    def test_not_logged_in_fails_with_login_issue(self, client, engine):
        engine["logged_in"] = False
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        s = _wait_status(client, job_id, {"failed"})
        assert s["status"] == "failed" and s["code"] == LOGIN_ISSUE

    def test_not_easy_apply_bail(self, client, engine):
        engine["result"] = ApplyResult(code="not_easy_apply", reason="no apply CTA")
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        s = _wait_status(client, job_id, {"failed"})
        assert s["status"] == "failed" and s["code"] == "not_easy_apply"

    def test_closed_posting_marked_discarded(self, client, engine):
        # A posting no longer accepting applications (EXPIRED) is a distinct
        # "expired" outcome that marks the role Discarded via the same identity-
        # anchored channel as submit -> Applied.
        import json
        from pipeline.app import data as app_data
        engine["result"] = ApplyResult(code=EXPIRED, reason="no longer accepting applications")
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        s = _wait_status(client, job_id, {"expired", "failed"})
        assert s["status"] == "expired" and s["code"] == EXPIRED
        ov = json.loads(app_data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
        assert ov == {"1": {"status": "Discarded", "company": "Acme", "role": "Engineer"}}

    def test_unknown_job_id_status_404(self, client, engine):
        assert client.get("/api/jobs/apply-status/nope").status_code == 404


class TestCancelDuringFill:
    """#2: a Cancel while the form is still being filled (status pending) bails
    the fill via the worker's should_cancel hook and ends as 'cancelled', instead
    of the browser finishing the whole application before honoring the cancel."""

    def test_cancel_during_fill_bails(self, client, engine):
        engine["cancellable_fill"] = True   # mock apply_to loops until should_cancel
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        client.post(f"/api/jobs/apply-cancel/{job_id}")     # accepted while pending
        s = _wait_status(client, job_id, {"cancelled", "ready", "failed"})
        assert s["status"] == "cancelled"
        assert ("submit", None) not in engine["calls"]


class TestSubmitCancelGuards:
    """The worker transitions ready -> submitting before clicking Submit, so a
    Cancel racing an in-flight submit is rejected (#6); apply_cancel is accepted
    only from pending/ready and apply_submit only from ready (#6/#8)."""

    def _ready(self, client):
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        _wait_status(client, job_id, {"ready"})
        return job_id

    def test_cancel_during_submit_is_rejected(self, client, engine):
        import threading
        gate = threading.Event()
        def blocking_submit(page):
            gate.wait(timeout=5)
            return ApplyResult(code=APPLIED, submitted=True)
        engine["submit_fn"] = blocking_submit
        job_id = self._ready(client)
        assert client.post(f"/api/jobs/apply-submit/{job_id}").status_code == 200
        s = _wait_status(client, job_id, {"submitting"})         # transient, exposed
        assert s["status"] == "submitting"
        assert client.post(f"/api/jobs/apply-cancel/{job_id}").status_code == 409
        gate.set()
        assert _wait_status(client, job_id, {"submitted"})["status"] == "submitted"

    def test_cancel_rejected_from_terminal(self, client, engine):
        engine["logged_in"] = False
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        _wait_status(client, job_id, {"failed"})
        assert client.post(f"/api/jobs/apply-cancel/{job_id}").status_code == 409

    def test_submit_after_timeout_is_rejected(self, client, engine, monkeypatch):
        monkeypatch.setenv("APPLY_HOLD_TIMEOUT", "0.2")   # short hold -> times out
        job_id = client.post("/api/jobs/apply-async", json={"num": "1"}).json()["job_id"]
        assert _wait_status(client, job_id, {"timeout"})["status"] == "timeout"
        assert client.post(f"/api/jobs/apply-submit/{job_id}").status_code == 409


class TestApplyTaskEviction:
    """#4: terminal apply tasks are evicted so _apply_tasks doesn't grow without
    bound over the server's lifetime."""

    def test_terminal_tasks_capped(self, client, engine):
        from pipeline.app import server
        cap = server._APPLY_TASK_CAP
        for i in range(cap + 5):
            server._apply_tasks[f"old-{i}"] = {"status": "submitted"}
        client.post("/api/jobs/apply-async", json={"num": "1"})  # prunes on create
        assert len(server._apply_tasks) <= cap + 1   # at most `cap` terminal + 1 active
