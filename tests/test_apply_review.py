"""Tests for the UI review-and-submit apply flow (server endpoints + worker).

The Playwright engine (browser.launch / linkedin.apply_to / submit_application)
is mocked — the same convention as the add-job LLM tests; the real form-walking
is verified manually via `--apply-mode dry-run`. These cover the server
orchestration: the task lifecycle (pending -> ready -> submitted/cancelled),
single-flight, the not-logged-in / not-easy-apply bails, and the
submit-marks-Applied write-back through the identity-anchored override channel.

Design under test (per the Phase-2 plan):
- POST /api/jobs/apply-async {num}: start a visible review session for tracker
  row `num`. 404 unknown num, 400 if it isn't a LinkedIn Easy Apply URL, 409 if
  a session is already active. Returns {job_id}.
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
from pipeline.apply.result import ApplyResult, APPLIED, EXPIRED, LOGIN_ISSUE  # noqa: E402


_TRACKER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-06-01 | Acme | Engineer | 4.5/5 | Evaluated | ❌ | [001](reports/001.md) | "
    "https://www.linkedin.com/jobs/view/123 — strong fit |\n"
    "| 2 | 2026-06-01 | Globex | Dev | 4.2/5 | Evaluated | ❌ | [002](reports/002.md) | "
    "https://boards.greenhouse.io/x/jobs/9 — offsite ATS |\n"
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

    monkeypatch.setattr(browser_mod, "launch", fake_launch)
    monkeypatch.setattr(browser_mod, "ensure_logged_in",
                        lambda page, *, headless, **kw: state["logged_in"])
    monkeypatch.setattr(linkedin_mod, "apply_to",
                        lambda page, job, eng, *, mode, resume_path=None:
                            state["calls"].append(("apply_to", mode)) or state["result"])
    monkeypatch.setattr(linkedin_mod, "submit_application",
                        lambda page: state["calls"].append(("submit", None)) or state["submit_result"],
                        raising=False)
    # Short hold so a worker left blocking on the decision Event (a test that
    # asserts "ready" but never submits/cancels) dies fast instead of lingering.
    monkeypatch.setenv("APPLY_HOLD_TIMEOUT", "2")
    return state


@pytest.fixture
def client(tmp_path, monkeypatch, engine):
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    (co / "data" / "applications.md").write_text(_TRACKER, encoding="utf-8")
    monkeypatch.setenv("CAREER_OPS_PATH", str(co))
    from pipeline.app import server
    importlib.reload(server)
    server._active_data_dir = None
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

    def test_non_linkedin_url_400(self, client, engine):
        # Row 2 is an off-site greenhouse ATS, not a LinkedIn Easy Apply URL.
        assert client.post("/api/jobs/apply-async", json={"num": "2"}).status_code == 400

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
