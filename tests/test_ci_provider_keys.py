"""The daily-pipeline workflow must forward an API key env var for every LLM
provider the evaluator can auto-detect.

A provider added to ``batch_evaluate._PROVIDER_KEYS`` but NOT wired into the
workflow's env silently fails in the cloud: the eval step never sees the key, so
either auto-detection skips the provider or an explicit ``BATCH_PROVIDER`` errors
with "unknown provider" — and the run produces no applications.md. This is
exactly what happened with ``deepseek``. This test keeps the code's provider list
and the CI wiring in lockstep so the next added provider can't repeat it.
"""
from pathlib import Path

import yaml

from pipeline.batch_evaluate import _PROVIDER_KEYS

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "daily-pipeline.yml"


def _all_env_pairs(workflow: dict) -> dict[str, str]:
    """Every env key -> value across all jobs and steps in the workflow."""
    pairs: dict[str, str] = {}
    for job in (workflow.get("jobs") or {}).values():
        for k, v in (job.get("env") or {}).items():
            pairs[k] = str(v)
        for step in (job.get("steps") or []):
            for k, v in (step.get("env") or {}).items():
                pairs[k] = str(v)
    return pairs


def test_daily_pipeline_forwards_every_provider_api_key():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = _all_env_pairs(workflow)
    missing = [
        (provider, key)
        for provider, key in _PROVIDER_KEYS.items()
        if f"secrets.{key}" not in env.get(key, "")
    ]
    assert not missing, (
        "daily-pipeline.yml must forward an API key for every provider in "
        "batch_evaluate._PROVIDER_KEYS, but these are not wired into the "
        "workflow env: " + ", ".join(f"{p} ({k})" for p, k in missing)
    )
