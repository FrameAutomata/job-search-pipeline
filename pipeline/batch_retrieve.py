"""Retrieve completed Anthropic Batch API results and write reports + tracker lines.

Polls the batch status. If complete, parses XML-tagged responses, writes:
  career-ops/reports/{num}-{company}-{date}.md
  career-ops/batch/tracker-additions/{id}.tsv

Then runs `node merge-tracker.mjs` in career-ops to merge the tracker additions.
State is read/written from career-ops/batch/batch-api-state.json.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline._batch_common import (
    atomic_write_text,
    load_state,
    run_merge_tracker,
    write_job_result,
)

ROOT = Path(__file__).resolve().parent.parent


def run(career_ops: Path, dry_run: bool = False) -> int:
    """Poll batch status and process results if complete. Returns jobs processed."""
    state_path = career_ops / "batch" / "batch-api-state.json"
    state = load_state(state_path)
    if not state.get("batch_id"):
        print("[batch-retrieve] no batch-api-state.json or no batch_id — nothing to retrieve")
        return 0

    batch_id = state["batch_id"]

    if state.get("status") == "completed":
        print(f"[batch-retrieve] batch {batch_id} already fully retrieved")
        return 0

    if dry_run:
        pending = sum(1 for j in state.get("jobs", {}).values() if j.get("status") == "pending")
        print(f"[batch-retrieve] dry-run: batch {batch_id} — {pending} job(s) pending retrieval")
        return 0

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
    batch = client.messages.batches.retrieve(batch_id)

    counts = batch.request_counts
    print(
        f"[batch-retrieve] {batch_id}: {batch.processing_status} — "
        f"processing={counts.processing} succeeded={counts.succeeded} "
        f"errored={counts.errored} canceled={counts.canceled} expired={counts.expired}"
    )

    if batch.processing_status != "ended":
        print("[batch-retrieve] batch not yet complete — try again later")
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    reports_dir = career_ops / "reports"
    tracker_dir = career_ops / "batch" / "tracker-additions"
    reports_dir.mkdir(parents=True, exist_ok=True)
    tracker_dir.mkdir(parents=True, exist_ok=True)

    processed = failed = 0

    for result in client.messages.batches.results(batch_id):
        job_id = result.custom_id.removeprefix("job-")
        job_meta = state.get("jobs", {}).get(job_id, {"id": job_id})

        if result.result.type == "succeeded":
            text = result.result.message.content[0].text
            out = write_job_result(text, job_meta, reports_dir, tracker_dir, today)
            if out["report_file"]:
                print(f"  [{job_id}] report -> {out['report_file']}")
            else:
                print(f"  [{job_id}] WARNING: no <report> tag in response")
            if not out["tracker_file"]:
                print(f"  [{job_id}] WARNING: no <tracker_tsv> tag in response")
            state["jobs"][job_id]["status"] = "completed"
            state["jobs"][job_id]["report"] = f"reports/{out['report_file']}" if out["report_file"] else None
            if out["summary"].get("score") is not None:
                state["jobs"][job_id]["score"] = out["summary"]["score"]
            processed += 1

        elif result.result.type == "errored":
            err = result.result.error
            msg = getattr(err, "message", str(err))
            print(f"  [{job_id}] ERROR: {err.type} — {msg}")
            state["jobs"][job_id]["status"] = "failed"
            state["jobs"][job_id]["error"] = f"{err.type}: {msg}"
            failed += 1

        else:
            print(f"  [{job_id}] {result.result.type} — skipped")
            state["jobs"][job_id]["status"] = result.result.type
            failed += 1

        atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))

    state["status"] = "completed"
    state["retrieved_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    atomic_write_text(state_path, json.dumps(state, indent=2, ensure_ascii=False))
    print(f"[batch-retrieve] done — processed={processed} failed={failed}")

    if processed > 0:
        run_merge_tracker(career_ops)

    return processed


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    career_ops_path = Path(os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")).resolve()
    sys.exit(0 if run(career_ops_path, dry_run="--dry-run" in sys.argv) >= 0 else 1)
