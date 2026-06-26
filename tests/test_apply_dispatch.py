"""CLI dispatch routing for the agentic catch-all (pipeline/apply.run + _apply_jobs).

The deterministic engines and the real browser are mocked (same convention as
test_apply_review.py). These pin three things the catch-all needs:

  1. run() SELECTS off-site ("agent") jobs — sites includes "agent".
  2. the dispatch loop PROCESSES the agent group instead of silently dropping it.
  3. the agent branch opens a CDP-exposing session (browser.launch_session) and
     drives agent_engine.apply_to — with no LinkedIn/Indeed login gate (the
     agent signs itself in).
"""

import contextlib
from types import SimpleNamespace

from pipeline import apply as apply_pkg
from pipeline.apply import agent_engine as agent_engine_mod
from pipeline.apply import browser as browser_mod
from pipeline.apply import linkedin as linkedin_mod
from pipeline.apply import queue as queue_mod
from pipeline.apply.queue import ApplyJob
from pipeline.apply.result import APPLIED, DEFER, ApplyResult, failed

_LI = "https://www.linkedin.com/jobs/view/1"
_GH = "https://boards.greenhouse.io/x/jobs/9"


def _job(num, url, score=4.5):
    return ApplyJob(num=num, company=f"Co{num}", role="Engineer", url=url, score=score)


class TestRunSelectsAgent:
    def test_select_includes_agent_site(self, tmp_path, monkeypatch):
        captured = {}

        def fake_select(career_ops, **kw):
            captured.update(kw)
            return []

        monkeypatch.setattr(queue_mod, "select", fake_select)
        apply_pkg.run(tmp_path, refresh=False)
        assert "agent" in captured.get("sites", ())


class TestRunDispatchesAgentGroup:
    def test_agent_group_is_processed_not_dropped(self, tmp_path, monkeypatch):
        # A LinkedIn job and an off-site greenhouse job. The greenhouse job must
        # reach _apply_jobs under the "agent" site, not vanish.
        monkeypatch.setattr(queue_mod, "select",
                            lambda career_ops, **kw: [_job("1", _LI), _job("2", _GH)])
        seen = []

        def fake_apply_jobs(site, jobs, engine, **kw):
            seen.append((site, [j.num for j in jobs]))
            return (0, 0, 0)

        monkeypatch.setattr(apply_pkg, "_apply_jobs", fake_apply_jobs)
        apply_pkg.run(tmp_path, refresh=False)

        assert "agent" in {site for site, _ in seen}
        assert [nums for site, nums in seen if site == "agent"] == [["2"]]


class TestApplyJobsAgentBranch:
    def test_opens_cdp_session_and_drives_agent_engine(self, tmp_path, monkeypatch):
        launched = {}

        @contextlib.contextmanager
        def fake_launch_session(*, headless=False, cdp_port=None, **kw):
            launched["cdp_port"] = cdp_port
            yield SimpleNamespace(page=object(),
                                  cdp_endpoint=f"http://localhost:{cdp_port}")

        monkeypatch.setattr(browser_mod, "launch_session", fake_launch_session,
                            raising=False)

        calls = []

        def fake_agent_apply(session, job, engine, *, mode, resume_path=None,
                             should_cancel=None):
            calls.append(SimpleNamespace(session=session, mode=mode, num=job.num))
            return ApplyResult(code=APPLIED, submitted=False,
                               answers=(("Agent summary", "filled"),))

        monkeypatch.setattr(agent_engine_mod, "apply_to", fake_agent_apply)

        # Neutralize the deterministic path so a mis-route can't open a real
        # browser; the test asserts the AGENT path was taken instead.
        @contextlib.contextmanager
        def fake_launch(headless=False, user_data_dir=None):
            yield object()

        monkeypatch.setattr(browser_mod, "launch", fake_launch)
        monkeypatch.setattr(browser_mod, "ensure_logged_in",
                            lambda page, *, headless, **kw: True)
        monkeypatch.setattr(linkedin_mod, "apply_to",
                            lambda *a, **k: failed("wrong_engine"))

        engine = apply_pkg.build_engine(tmp_path, provider=None, model=None)
        applied, held, failures = apply_pkg._apply_jobs(
            "agent", [_job("2", _GH)], engine,
            career_ops=tmp_path, report_root=tmp_path,
            applications_md=tmp_path / "data" / "applications.md",
            mode="review", headless=True, provider=None, model=None,
            tailor_min_score=99.0,
        )

        assert launched.get("cdp_port") is not None  # a CDP endpoint was opened
        assert len(calls) == 1 and calls[0].num == "2"
        assert calls[0].mode == "review"
        assert calls[0].session.cdp_endpoint.startswith("http://localhost:")
        assert (applied, held, failures) == (0, 1, 0)  # held for review


class TestDeferTarget:
    """Which engine a result hands a job off to (None = keep it here)."""

    def test_agent_defer_uses_deferred_to(self):
        r = ApplyResult(code=DEFER, deferred_to="indeed")
        assert apply_pkg._defer_target("agent", r) == "indeed"

    def test_deterministic_no_form_defers_to_agent(self):
        # The inverse handoff: a LinkedIn/Indeed engine that finds no fast-apply
        # form (apply-on-company-site) routes to the agentic catch-all.
        assert apply_pkg._defer_target("indeed", ApplyResult(code="not_easy_apply")) == "agent"
        assert apply_pkg._defer_target("linkedin", ApplyResult(code="no_easy_apply_button")) == "agent"

    def test_agent_no_form_does_not_defer_to_itself(self):
        assert apply_pkg._defer_target("agent", ApplyResult(code="not_easy_apply")) is None

    def test_applied_does_not_defer(self):
        assert apply_pkg._defer_target("linkedin",
                                       ApplyResult(code=APPLIED, submitted=True)) is None


class TestRedispatch:
    def test_deferred_job_runs_under_target_engine(self, tmp_path, monkeypatch):
        # An off-site job the agent recognizes as Indeed SmartApply defers to the
        # deterministic engine, which re-runs it in a second round.
        monkeypatch.setattr(queue_mod, "select", lambda co, **kw: [_job("1", _GH)])
        seen = []

        def fake_apply_jobs(site, jobs, engine, *, deferrals=None, **kw):
            seen.append((site, [j.num for j in jobs]))
            if site == "agent" and deferrals is not None:   # round 1
                deferrals.append((jobs[0], "indeed",
                                  ApplyResult(code=DEFER, deferred_to="indeed")))
            return (0, 0, 0)

        monkeypatch.setattr(apply_pkg, "_apply_jobs", fake_apply_jobs)
        apply_pkg.run(tmp_path, refresh=False)
        # round 1 ran the job at agent; round 2 re-dispatched it to indeed.
        assert seen == [("agent", ["1"]), ("indeed", ["1"])]

    def test_second_defer_is_not_redispatched(self, tmp_path, monkeypatch):
        # Loop guard: a ping-ponging job is dispatched at most twice (no round 3).
        monkeypatch.setattr(queue_mod, "select", lambda co, **kw: [_job("1", _GH)])
        calls = []

        def fake_apply_jobs(site, jobs, engine, *, deferrals=None, **kw):
            calls.append(site)
            if deferrals is not None:   # only the first round collects deferrals
                deferrals.append((jobs[0], "indeed",
                                  ApplyResult(code=DEFER, deferred_to="indeed")))
            return (0, 0, 0)

        monkeypatch.setattr(apply_pkg, "_apply_jobs", fake_apply_jobs)
        apply_pkg.run(tmp_path, refresh=False)
        assert calls == ["agent", "indeed"]   # round 1 + round 2 only — no round 3
