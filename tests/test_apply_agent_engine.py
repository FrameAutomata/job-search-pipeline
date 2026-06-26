"""Contract tests for the agentic apply adapter (pipeline/apply/agent_engine.py).

The adapter wraps agent.run_agent so the agentic engine presents the same
apply_to()/submit_application() surface as the deterministic linkedin/indeed
engines. These tests mock run_agent (the real subprocess is integration, tested
in test_apply_agent.py) and pin the adapter's branching/mapping:

  - review/dry-run -> agent fills and STOPS (RESULT:READY) -> held APPLIED,
    submitted=False, with the agent's summary surfaced for the panel.
  - auto          -> one-shot fill+submit -> APPLIED, submitted=True.
  - failures/expired pass straight through (never masqueraded as held/applied).
  - submit_application runs a second turn that actually submits.
  - the agent is always pointed at the SESSION's CDP endpoint.
"""

from types import SimpleNamespace

import pytest

from pipeline.apply import agent_engine
from pipeline.apply.queue import ApplyJob
from pipeline.apply.result import (APPLIED, EXPIRED, READY, ApplyResult,
                                   CANCELLED, failed)


def _session(endpoint="http://localhost:9222"):
    return SimpleNamespace(page=object(), cdp_endpoint=endpoint)


def _job():
    return ApplyJob(num="1", company="Globex", role="Engineer",
                    url="https://boards.greenhouse.io/x/jobs/9", score=4.5)


def _answers(*, ats_password="", imap_configured=False, imap_env=None):
    # The adapter reads profile (incl. apply creds) + cv_text off the shared
    # AnswerEngine to build the prompt and wire the verification MCP; a stand-in
    # with those attributes is enough.
    profile = SimpleNamespace(
        ats_password=ats_password, imap_configured=imap_configured,
        imap_mcp_env=lambda: (imap_env if imap_env is not None else {}),
    )
    return SimpleNamespace(profile=profile, cv_text="", cover_letter_text="")


@pytest.fixture
def fake_run_agent(monkeypatch):
    """Patch run_agent to a recorder that returns a caller-chosen result; also
    stub build_prompt so the adapter doesn't need a real ApplyProfile."""
    calls = []

    def make(result: ApplyResult):
        def _fake(prompt, *, cdp_endpoint, dry_run=False, model=None, **kw):
            calls.append(SimpleNamespace(prompt=prompt, cdp_endpoint=cdp_endpoint,
                                         dry_run=dry_run, model=model,
                                         imap_env=kw.get("imap_env")))
            return result
        monkeypatch.setattr(agent_engine.agent, "run_agent", _fake)
        return calls

    monkeypatch.setattr(agent_engine.prompt, "build_prompt",
                        lambda *a, **k: "PROMPT-BODY")
    return make


class TestApplyToReview:
    def test_ready_maps_to_held_applied_with_summary(self, fake_run_agent):
        calls = fake_run_agent(ApplyResult(code=READY, reason="filled 8 fields"))
        r = agent_engine.apply_to(_session(), _job(), _answers(), mode="review")
        # Held: looks applied to the worker's "ready" branch, but NOT submitted.
        assert r.applied is True and r.submitted is False
        # The agent's summary is surfaced so the panel can show it.
        assert any("filled 8 fields" in a for pair in r.answers for a in pair)

    def test_review_runs_agent_in_dry_run_against_session_endpoint(self, fake_run_agent):
        calls = fake_run_agent(ApplyResult(code=READY))
        agent_engine.apply_to(_session("http://localhost:7000"), _job(),
                              _answers(), mode="review")
        assert len(calls) == 1
        assert calls[0].dry_run is True
        assert calls[0].cdp_endpoint == "http://localhost:7000"

    def test_failure_passes_through_not_held(self, fake_run_agent):
        fake_run_agent(failed("cloudflare_blocked"))
        r = agent_engine.apply_to(_session(), _job(), _answers(), mode="review")
        assert r.code == "failed" and r.reason == "cloudflare_blocked"
        assert r.applied is False

    def test_expired_passes_through(self, fake_run_agent):
        fake_run_agent(ApplyResult(code=EXPIRED))
        r = agent_engine.apply_to(_session(), _job(), _answers(), mode="review")
        assert r.code == EXPIRED  # worker marks the posting Discarded

    def test_cancel_before_run_skips_agent(self, fake_run_agent):
        calls = fake_run_agent(ApplyResult(code=READY))
        r = agent_engine.apply_to(_session(), _job(), _answers(), mode="review",
                                  should_cancel=lambda: True)
        assert r.code == CANCELLED
        assert calls == []  # the agent is never spawned once cancelled


class TestForwardsCredentials:
    def test_apply_to_passes_ats_password_and_verification_flag(self, monkeypatch):
        # Phase 6 wiring: the adapter forwards the profile's ATS password and
        # whether email verification is available into the prompt builder, which
        # activates the login/signup + read_verification_code instructions.
        captured = {}

        def fake_build_prompt(job, profile, **kw):
            captured.update(kw)
            return "PROMPT"

        monkeypatch.setattr(agent_engine.prompt, "build_prompt", fake_build_prompt)
        monkeypatch.setattr(agent_engine.agent, "run_agent",
                            lambda *a, **k: ApplyResult(code=READY))
        agent_engine.apply_to(_session(), _job(),
                              _answers(ats_password="atspw", imap_configured=True),
                              mode="review")
        assert captured.get("ats_password") == "atspw"
        assert captured.get("verification_available") is True


class TestForwardsImapEnv:
    def test_passes_imap_env_when_configured(self, fake_run_agent):
        # Phase 7 wiring: a configured IMAP inbox means the agent gets the
        # read_verification_code tool, fed creds via the subprocess env.
        calls = fake_run_agent(ApplyResult(code=READY))
        agent_engine.apply_to(
            _session(), _job(),
            _answers(imap_configured=True, imap_env={"APPLY_IMAP_HOST": "h"}),
            mode="review")
        assert calls[0].imap_env == {"APPLY_IMAP_HOST": "h"}

    def test_no_imap_env_when_unconfigured(self, fake_run_agent):
        calls = fake_run_agent(ApplyResult(code=READY))
        agent_engine.apply_to(_session(), _job(),
                              _answers(imap_configured=False), mode="review")
        assert calls[0].imap_env is None


class TestApplyToAuto:
    def test_auto_submits_one_shot(self, fake_run_agent):
        calls = fake_run_agent(ApplyResult(code=APPLIED, submitted=True))
        r = agent_engine.apply_to(_session(), _job(), _answers(), mode="auto")
        assert r.applied is True and r.submitted is True
        assert calls[0].dry_run is False  # auto = live submit


class TestSubmitApplication:
    def test_runs_submit_turn_and_marks_submitted(self, fake_run_agent):
        calls = fake_run_agent(ApplyResult(code=APPLIED, submitted=True))
        r = agent_engine.submit_application(_session("http://localhost:8001"))
        assert r.applied is True and r.submitted is True
        assert len(calls) == 1
        assert calls[0].dry_run is False
        assert calls[0].cdp_endpoint == "http://localhost:8001"
