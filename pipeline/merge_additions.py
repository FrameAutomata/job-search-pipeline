"""Merge whatever is pending in career-ops/batch/tracker-additions/ — after
recovering the rows a previous merge refused, and sanitizing every row first.

The step `run.sh`/`run.ps1` run once `batch-runner.sh` returns. That runner
merges its own additions as its last act, inside career-ops with no Python in
the process tree, so a row the agent CLI wrote with a bare `4.2` is refused and
archived into `merged/` before anything here can see it — and from there nothing
retries it. `run_merge_tracker` is the one place that knows how to pull such a
row back, repair it, and merge it; this module exists so the wrappers can reach
it after the runner, which nothing else in the pipeline does.

Also the right entry point for a manual retry: `python -m pipeline.merge_additions`.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from pipeline._batch_common import run_merge_tracker
from pipeline.stdio import line_buffer_stdout
from pipeline.tracker_layout import career_ops_dir

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    # The same .env orchestrate.py reads, so CAREER_OPS_PATH resolves the same way.
    load_dotenv(ROOT / ".env")
    career_ops = career_ops_dir()
    if not (career_ops / "merge-tracker.mjs").exists():
        print(f"[batch] no career-ops checkout at {career_ops} — nothing to merge into")
        return 0
    return 0 if run_merge_tracker(career_ops) else 1


if __name__ == "__main__":
    line_buffer_stdout()
    sys.exit(main())
