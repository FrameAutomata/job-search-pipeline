"""Submit pending batch-input.tsv jobs to the Anthropic Messages Batch API.

Reads career-ops context files and inlines them into the system prompt.
Each job gets an independent message request. Results arrive async (up to 24 h).
Poll for completion with pipeline/batch_retrieve.py.

State persisted in career-ops/batch/batch-api-state.json.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from pipeline._batch_common import (
    build_system_prompt,
    build_user_message,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    read_text,
)

ROOT = Path(__file__).resolve().parent.parent


def run(career_ops: Path, model: str | None = None, dry_run: bool = False) -> int:
    """Submit pending jobs to the Anthropic Batch API. Returns number of requests submitted."""
    model = model or os.environ.get("BATCH_MODEL", "claude-sonnet-4-6")
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

    batch_requests: list[dict] = []
    job_meta: dict[str, dict] = {}

    for row in pending:
        jid = str(row["id"]).strip()
        url = (row.get("url") or "").strip()
        company = (row.get("source") or "").strip()
        role = (row.get("notes") or "").strip()

        report_counter += 1
        tracker_counter += 1
        report_num = f"{report_counter:03d}"

        msg_meta = {
            "id": jid,
            "url": url,
            "company": company,
            "role": role,
            "report_num": report_num,
            "tracker_num": tracker_counter,
            "jd_text": read_text(career_ops / "batch" / "jds" / f"{jid}.txt"),
        }
        batch_requests.append({
            "custom_id": f"job-{jid}",
            "params": {
                "model": model,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": [{"role": "user", "content": build_user_message(msg_meta, today)}],
            },
        })

        job_meta[jid] = {
            "id": jid,
            "url": url,
            "company": company,
            "role": role,
            "report_num": report_num,
            "tracker_num": tracker_counter,
            "custom_id": f"job-{jid}",
            "status": "pending",
        }

    if dry_run:
        print(f"[batch-submit] dry-run: would submit {len(batch_requests)} request(s) via {model}")
        for meta in job_meta.values():
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
    state["submitted_at"] = datetime.utcnow().isoformat() + "Z"
    state["model"] = model
    state["status"] = "in_progress"
    if "jobs" not in state:
        state["jobs"] = {}
    state["jobs"].update(job_meta)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[batch-submit] state saved -> {state_path}")
    print("[batch-submit] run with --retrieve-batch once the batch is complete (up to 24 h)")

    return len(batch_requests)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    career_ops_path = Path(os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")).resolve()
    dry = "--dry-run" in sys.argv
    model_arg = None
    for i, a in enumerate(sys.argv):
        if a == "--model" and i + 1 < len(sys.argv):
            model_arg = sys.argv[i + 1]
    sys.exit(0 if run(career_ops_path, model=model_arg, dry_run=dry) >= 0 else 1)
