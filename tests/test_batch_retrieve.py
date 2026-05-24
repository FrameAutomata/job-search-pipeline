"""Tests for pipeline/batch_retrieve.py — covers polling, dry-run, no-state cases."""

import json
import sys as _sys
from pathlib import Path

import pytest

from pipeline import batch_retrieve as retrieve_mod


def _make_state(career_ops: Path, state: dict) -> None:
    state_path = career_ops / "batch" / "batch-api-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


class TestRunNoState:
    def test_no_state_returns_zero(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        assert retrieve_mod.run(career_ops) == 0

    def test_state_without_batch_id_returns_zero(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        _make_state(career_ops, {"jobs": {}})
        assert retrieve_mod.run(career_ops) == 0

    def test_already_completed_returns_zero(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        _make_state(career_ops, {"batch_id": "b1", "status": "completed", "jobs": {}})
        assert retrieve_mod.run(career_ops) == 0


class TestDryRun:
    def test_dry_run_reports_pending(self, tmp_path, capsys):
        career_ops = tmp_path / "career-ops"
        _make_state(career_ops, {
            "batch_id": "b1",
            "status": "in_progress",
            "jobs": {"1": {"status": "pending"}, "2": {"status": "completed"}},
        })
        n = retrieve_mod.run(career_ops, dry_run=True)
        assert n == 0
        captured = capsys.readouterr()
        assert "1 job(s) pending" in captured.out


class TestPolling:
    def test_polling_in_progress_returns_zero(self, tmp_path, monkeypatch, mocker):
        career_ops = tmp_path / "career-ops"
        _make_state(career_ops, {"batch_id": "b1", "status": "in_progress", "jobs": {}})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        fake_batch = mocker.MagicMock()
        fake_batch.processing_status = "in_progress"
        fake_batch.request_counts = mocker.MagicMock(
            processing=2, succeeded=0, errored=0, canceled=0, expired=0,
        )
        fake_client = mocker.MagicMock()
        fake_client.messages.batches.retrieve.return_value = fake_batch
        fake_anthropic = mocker.MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client
        monkeypatch.setitem(_sys.modules, "anthropic", fake_anthropic)

        assert retrieve_mod.run(career_ops) == 0
        # State is NOT marked completed when batch is still running.
        state = json.loads((career_ops / "batch" / "batch-api-state.json").read_text(encoding="utf-8"))
        assert state["status"] == "in_progress"

    def test_polling_ended_processes_succeeded(self, tmp_path, monkeypatch, mocker):
        career_ops = tmp_path / "career-ops"
        _make_state(career_ops, {
            "batch_id": "b1",
            "status": "in_progress",
            "jobs": {
                "1": {"id": "1", "status": "pending", "report_num": "001", "tracker_num": 1, "company": "Acme"},
            },
        })
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        fake_batch = mocker.MagicMock()
        fake_batch.processing_status = "ended"
        fake_batch.request_counts = mocker.MagicMock(
            processing=0, succeeded=1, errored=0, canceled=0, expired=0,
        )

        # A minimal successful result whose XML body has both <report> and <tracker_tsv>.
        text_response = (
            "<evaluation>"
            "<report># report body</report>"
            "<tracker_tsv>1\t2026-05-12\tAcme\tEng\tEvaluada\t4.0/5\tnull\t[001](r.md)\tAPPLY ok</tracker_tsv>"
            '<summary>{"score": 4.0, "id": "1", "report_num": "001"}</summary>'
            "</evaluation>"
        )
        fake_result = mocker.MagicMock()
        fake_result.custom_id = "job-1"
        fake_result.result.type = "succeeded"
        fake_result.result.message.content = [mocker.MagicMock(text=text_response)]

        fake_client = mocker.MagicMock()
        fake_client.messages.batches.retrieve.return_value = fake_batch
        fake_client.messages.batches.results.return_value = iter([fake_result])
        fake_anthropic = mocker.MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client
        monkeypatch.setitem(_sys.modules, "anthropic", fake_anthropic)

        # Avoid invoking the real merge-tracker.mjs in the assertion below.
        mocker.patch.object(retrieve_mod, "run_merge_tracker", return_value=False)

        n = retrieve_mod.run(career_ops)
        assert n == 1

        # Report and tracker files should now exist.
        reports = list((career_ops / "reports").glob("*.md"))
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8").startswith("# report")

        trackers = list((career_ops / "batch" / "tracker-additions").glob("*.tsv"))
        assert len(trackers) == 1

        state = json.loads((career_ops / "batch" / "batch-api-state.json").read_text(encoding="utf-8"))
        assert state["status"] == "completed"
        assert state["jobs"]["1"]["status"] == "completed"
        assert state["jobs"]["1"]["score"] == 4.0

    def test_polling_missing_api_key_returns_zero(self, tmp_path, monkeypatch):
        career_ops = tmp_path / "career-ops"
        _make_state(career_ops, {"batch_id": "b1", "status": "in_progress", "jobs": {}})
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        if "anthropic" not in _sys.modules:
            monkeypatch.setitem(_sys.modules, "anthropic", type("M", (), {"Anthropic": object})())
        assert retrieve_mod.run(career_ops) == 0
