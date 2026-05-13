"""Chain scrape -> filter -> bridge. Each step can be skipped with a flag."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from pipeline import scrape, filter as filter_step, bridge, notify  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the job-search pipeline.")
    ap.add_argument("--skip-scrape", action="store_true", help="Reuse existing output/jobs.csv")
    ap.add_argument("--skip-filter", action="store_true", help="Reuse existing output/filtered_jobs.csv")
    ap.add_argument("--skip-bridge", action="store_true", help="Don't push to career-ops")
    ap.add_argument("--config", type=Path, default=None, help="Path to search.yml")
    args = ap.parse_args()

    def resolve(p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (ROOT / p).resolve()

    config_path = args.config or resolve(os.environ.get("SEARCH_CONFIG", "config/search.yml"))
    if not config_path.exists():
        print(f"error: config not found at {config_path}", file=sys.stderr)
        print("hint: copy config/search.example.yml -> config/search.yml and edit", file=sys.stderr)
        return 1

    notify.notify("Job Search Pipeline", "Starting scrape stage...")

    if not args.skip_scrape:
        notify.notify("Pipeline", "🔍 Scraping job boards...")
        scrape.run(config_path)
        notify.notify("Pipeline", "✅ Scraping complete")

    if not args.skip_filter:
        notify.notify("Pipeline", "🔎 Filtering jobs...")
        filter_step.run(config_path)
        notify.notify("Pipeline", "✅ Filtering complete")

    if not args.skip_bridge:
        notify.notify("Pipeline", "📝 Updating career-ops...")
        career_ops = resolve(os.environ.get("CAREER_OPS_PATH", "career-ops"))
        count = bridge.run(career_ops)
        notify.notify("Pipeline", f"✅ Pipeline complete! Added {count} jobs")
    else:
        notify.notify("Pipeline", "✅ Pipeline complete!")

    return 0


if __name__ == "__main__":
    sys.exit(main())
