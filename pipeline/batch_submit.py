"""Submit pending batch-input.tsv jobs to the Anthropic Messages Batch API.

Reads career-ops context files and inlines them into the system prompt.
Each job gets an independent message request. Results arrive async (up to 24 h).
Poll for completion with pipeline/batch_retrieve.py.

The system prompt is sent as a cacheable block (`cache_control: ephemeral`) so
the large CV+profile context is paid for once and reused across every job in
the batch — typically a 90%+ discount on those tokens.

State persisted in career-ops/batch/batch-api-state.json.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline._batch_common import (
    MAX_TOKENS,
    assign_job_numbers,
    atomic_write_text,
    build_system_prompt,
    build_user_message,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    read_text,
)

ROOT = Path(__file__).resolve().parent.parent


def _system_block(prompt: str) -> list[dict]:
    """Wrap the system prompt as a single cacheable block. The Anthropic API
    accepts either a string or a list of content blocks for `system`; the list
    form is required to attach cache_control."""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def run(career_ops: Path, model: str | None = None, dry_run: bool = False) -> int:
    """Submit pending jobs to the Anthropic Batch API. Returns number of requests submitted."""
    # Treat empty-string env var as "use default" — the GHA workflow's
    # `BATCH_MODEL: ${{ vars.BATCH_MODEL || '' }}` pattern injects "" when the
    # variable isn't set, and `os.environ.get(VAR, DEFAULT)` would return ""
    # instead of DEFAULT in that case.
    model = model or os.environ.get("BATCH_MODEL") or "claude-sonnet-4-6"
    today = datetime.now().strftime("%Y-%m-%d")

    batch_input = career_ops / "batch" / "batch-input.tsv"
    state_path = career_ops / "batch" / "batch-api-state.json"
    reports_dir = career_ops / "reports"
    applications_md = career_ops / "data" / "applications.md"

    if not batch_input.exists():
        print("[batch-submit] no batch-input.tsv found — nothing to submit")
        return 0

    state = load_state(state_path)
    # Include "pending" to avoid re-submitting jobs already sent to the API
    pending = load_pending(batch_input, state, done_statuses=frozenset({"pending", "completed"}))

    if not pending:
        print("[batch-submit] all jobs already submitted or no new jobs — nothing to do")
        return 0

    print(f"[batch-submit] {len(pending)} pending job(s) to submit via {model}")

    cv = read_text(career_ops / "cv.md")
    if not cv:
        print("error: career-ops/cv.md not found — cannot evaluate without a CV", file=sys.stderr)
        return 0

    system_prompt = build_system_prompt(
        cv,
        read_text(career_ops / "config" / "profile.yml"),
        read_text(career_ops / "modes" / "_profile.md"),
        read_text(career_ops / "article-digest.md"),
    )

    report_counter = max_report_num(reports_dir, state)
    tracker_counter = max_tracker_num(applications_md, state)

    jobs = assign_job_numbers(pending, state, report_counter, tracker_counter, career_ops)

    system_block = _system_block(system_prompt)
    batch_requests: list[dict] = []
    for meta in jobs:
        jid = meta["id"]
        state["jobs"][jid]["custom_id"] = f"job-{jid}"
        batch_requests.append({
            "custom_id": f"job-{jid}",
            "params": {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": system_block,
                "messages": [{"role": "user", "content": build_user_message(meta, today)}],
            },
        })

    if dry_run:
        print(f"[batch-submit] dry-run: would submit {len(batch_requests)} request(s) via {model}")
        for meta in jobs:
            print(f"  [{meta['id']}] {meta['company'] or '?'} / {meta['role'] or '?'} -> report {meta['report_num']}")
        return len(batch_requests)

    try:
        import anthropic as _anthropic
    except ImportError:
        print("error: anthropic package missing. Run: pip install anthropic", file=sys.stderr)
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 0

    client = _anthropic.Anthropic(api_key=api_key)
    batch = client.messages.batches.create(requests=batch_requests)

    print(f"[batch-submit] batch submitted: {batch.id}")
    print(f"[batch-submit] status: {batch.processing_status}")

    state["batch_id"] = batch.id
    state["submitted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state["model"] = model
    state["status"] = "in_progress"

    atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
    print(f"[batch-submit] state saved -> {state_path}")
    print("[batch-submit] run with --retrieve-batch once the batch is complete (up to 24 h)")

    return len(batch_requests)


def _parse_argv(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Submit pending jobs to the Anthropic Batch API.")
    ap.add_argument("--model", default=None, help="overrides BATCH_MODEL env var")
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args(argv)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    args = _parse_argv(sys.argv[1:])
    career_ops_path = Path(os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")).resolve()
    sys.exit(0 if run(career_ops_path, model=args.model, dry_run=args.dry_run) >= 0 else 1)
