"""Prepare career-ops/batch/batch-input.tsv from newly bridged offers.

Runs after bridge. Assigns sequential IDs continuing from whatever is already
in batch-input.tsv (so repeated runs append rather than overwrite), deduplicates
by URL against the existing file, and writes the TSV header if the file is new.
"""

import csv
import sys
from pathlib import Path

from pipeline._batch_common import read_batch_input
from pipeline.stdio import line_buffer_stdout

ROOT = Path(__file__).resolve().parent.parent
BATCH_INPUT = "batch/batch-input.tsv"
FIELDNAMES = ["id", "url", "source", "notes"]


def _load_existing(batch_input: Path) -> tuple[int, set[str]]:
    """Return (max_id, seen_urls) from an existing batch-input.tsv."""
    max_id = 0
    seen: set[str] = set()
    # Through the shared reader: the FORMAT is one fact, the question is ours.
    # An IO error propagates here deliberately — swallowing it would restart ids
    # at 1 and re-queue every job already in the file.
    for row in read_batch_input(batch_input):
        try:
            max_id = max(max_id, int(row.get("id") or 0))
        except ValueError:
            pass
        url = (row.get("url") or "").strip()
        if url:
            seen.add(url)
    return max_id, seen


def run(career_ops_path: Path, new_offers: list[dict]) -> int:
    """Append new_offers to batch-input.tsv. Returns number of rows added."""
    if not new_offers:
        return 0

    batch_input = career_ops_path / BATCH_INPUT
    batch_input.parent.mkdir(parents=True, exist_ok=True)

    max_id, seen_urls = _load_existing(batch_input)

    jds_dir = career_ops_path / "batch" / "jds"
    jds_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for offer in new_offers:
        url = (offer.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        max_id += 1
        rows.append({
            "id": max_id,
            "url": url,
            "source": (offer.get("company") or "").strip(),
            "notes": (offer.get("title") or "").strip(),
        })
        desc = (offer.get("description") or "").strip()
        if desc:
            (jds_dir / f"{max_id}.txt").write_text(desc, encoding="utf-8")

    if not rows:
        print("[batch-prep] no new offers to add to batch-input.tsv")
        return 0

    write_header = not batch_input.exists() or batch_input.stat().st_size == 0
    with open(batch_input, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, delimiter="\t")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)

    print(f"[batch-prep] added {len(rows)} offers to {batch_input}")
    return len(rows)


if __name__ == "__main__":
    line_buffer_stdout()

    import json
    career_ops = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "career-ops"
    offers = json.loads(sys.argv[2]) if len(sys.argv) > 2 else []
    run(career_ops.resolve(), offers)
