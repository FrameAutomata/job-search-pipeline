"""Tests for pipeline/batch_submit.py — covers dry-run, state shape, prompt-cache wiring."""

import csv
import json
from pathlib import Path

import pytest

from pipeline import batch_submit as submit_mod


def _make_career_ops(tmp_path: Path, jobs: list[dict] | None = None) -> Path:
    career_ops = tmp_path / "career-ops"
    batch_dir = career_ops / "batch"
    batch_dir.mkdir(parents=True)
    tsv = batch_dir / "batch-input.tsv"
    rows = jobs or [
        {"id": "1", "url": "https://a.com", "source": "Acme", "notes": "Eng"},
        {"id": "2", "url": "https://b.com", "source": "Globex", "notes": "Dev"},
    ]
    with open(tsv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "url", "source", "notes"], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    (career_ops / "cv.md").write_text("# CV", encoding="utf-8")
    return career_ops


class TestRunNoInput:
    def test_no_batch_input_returns_zero(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        assert submit_mod.run(career_ops, dry_run=True) == 0

    def test_no_cv_returns_zero(self, tmp_path):
        career_ops = _make_career_ops(tmp_path)
        (career_ops / "cv.md").unlink()
        # dry-run still triggers the CV check before printing
        assert submit_mod.run(career_ops, dry_run=True) == 0


class TestDryRun:
    def test_dry_run_returns_pending_count(self, tmp_path):
        career_ops = _make_career_ops(tmp_path)
        assert submit_mod.run(career_ops, dry_run=True) == 2

    def test_dry_run_skips_already_pending_or_completed(self, tmp_path):
        career_ops = _make_career_ops(tmp_path)
        state_path = career_ops / "batch" / "batch-api-state.json"
        state_path.write_text(json.dumps({
            "jobs": {"1": {"status": "completed"}, "2": {"status": "pending"}}
        }), encoding="utf-8")
        assert submit_mod.run(career_ops, dry_run=True) == 0


class TestSystemBlock:
    def test_system_block_marks_cache_control(self):
        block = submit_mod._system_block("hello")
        assert isinstance(block, list)
        assert block[0]["type"] == "text"
        assert block[0]["text"] == "hello"
        assert block[0]["cache_control"] == {"type": "ephemeral"}


class TestArgvParsing:
    def test_parse_argv_defaults(self):
        ns = submit_mod._parse_argv([])
        assert ns.model is None
        assert ns.dry_run is False

    def test_parse_argv_with_flags(self):
        ns = submit_mod._parse_argv(["--model", "claude-haiku-4-5-20251001", "--dry-run"])
        assert ns.model == "claude-haiku-4-5-20251001"
        assert ns.dry_run is True


class TestRunWithMockedAPI:
    """End-to-end run() with the Anthropic SDK mocked. Verifies the request payload,
    the cache_control wiring, and the persisted state shape."""

    def test_run_submits_and_persists_state(self, tmp_path, monkeypatch, mocker):
        career_ops = _make_career_ops(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        # Mock the anthropic SDK so we can intercept the create call without
        # importing the real module's create_batch path.
        fake_batch = mocker.MagicMock()
        fake_batch.id = "batch_abc"
        fake_batch.processing_status = "in_progress"

        fake_client = mocker.MagicMock()
        fake_client.messages.batches.create.return_value = fake_batch

        fake_anthropic = mocker.MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client

        # Use mocker.patch on the module's import-time lookup. batch_submit
        # imports anthropic lazily inside run(), so patch sys.modules.
        import sys as _sys
        monkeypatch.setitem(_sys.modules, "anthropic", fake_anthropic)

        n = submit_mod.run(career_ops, dry_run=False)
        assert n == 2

        # Verify the SDK got called with our prompt-cached system block.
        kwargs = fake_client.messages.batches.create.call_args.kwargs
        requests = kwargs["requests"]
        assert len(requests) == 2
        params = requests[0]["params"]
        assert isinstance(params["system"], list)
        assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert requests[0]["custom_id"] == "job-1"
        assert requests[1]["custom_id"] == "job-2"

        # State file should now hold batch_id and per-job metadata.
        state = json.loads((career_ops / "batch" / "batch-api-state.json").read_text(encoding="utf-8"))
        assert state["batch_id"] == "batch_abc"
        assert state["status"] == "in_progress"
        assert set(state["jobs"].keys()) == {"1", "2"}
        assert state["jobs"]["1"]["custom_id"] == "job-1"
        assert state["jobs"]["1"]["report_num"] == "001"
        assert state["jobs"]["2"]["report_num"] == "002"
        assert state["jobs"]["1"]["tracker_num"] == 1

    def test_run_missing_api_key_returns_zero(self, tmp_path, monkeypatch):
        career_ops = _make_career_ops(tmp_path)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Need anthropic importable for this code path
        import sys as _sys
        if "anthropic" not in _sys.modules:
            monkeypatch.setitem(_sys.modules, "anthropic", type("M", (), {"Anthropic": object})())
        assert submit_mod.run(career_ops, dry_run=False) == 0
