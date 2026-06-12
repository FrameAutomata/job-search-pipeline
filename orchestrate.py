"""Chain scrape -> filter -> screen -> bridge -> batch_prep. Each step can be skipped with a flag."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from pipeline import scrape, filter as filter_step, screen, bridge, batch_prep, batch_evaluate, notify  # noqa: E402


def _env_float(name: str, default: float) -> float:
    """A float env override that can NEVER crash startup: this runs at
    argparse-setup time on every invocation (even runs that never touch the
    flag), so a malformed or set-but-empty value must warn and fall back, not
    raise and brick every scheduled pipeline run."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[orchestrate] ignoring invalid {name}={raw!r} (using {default})")
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the job-search pipeline.")
    ap.add_argument("--skip-scrape", action="store_true", help="Reuse existing output/jobs.csv")
    ap.add_argument("--skip-filter", action="store_true", help="Reuse existing output/filtered_jobs.csv")
    ap.add_argument("--skip-screen", action="store_true", help="Skip liveness and LLM fit screening")
    ap.add_argument("--skip-bridge", action="store_true", help="Don't push to career-ops")
    ap.add_argument("--skip-batch-prep", action="store_true", help="Don't write batch-input.tsv")
    ap.add_argument("--evaluate-batch", action="store_true",
                    help="Evaluate jobs synchronously via any LLM provider (auto-detected from env keys)")
    ap.add_argument("--batch-provider", type=str, default=None,
                    help="LLM provider for --evaluate-batch: anthropic|gemini|openai|groq|ollama")
    ap.add_argument("--batch-model", type=str, default=None,
                    help="Model name (overrides BATCH_MODEL env var)")
    ap.add_argument("--batch-concurrency", type=int,
                    default=int(_env_float("BATCH_CONCURRENCY", 3)),
                    help="Parallel workers for --evaluate-batch (default: 3, or the "
                         "BATCH_CONCURRENCY env var). Size to your provider's limits — "
                         "e.g. DeepInfra allows 200 concurrent requests per model.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be submitted/evaluated without doing it")
    ap.add_argument("--apply", action="store_true",
                    help="Auto-apply to evaluated LinkedIn Easy Apply jobs (local only — "
                         "needs a logged-in browser). Off by default.")
    ap.add_argument("--apply-mode", choices=["review", "dry-run", "auto"], default="review",
                    help="review: fill the form and stop before Submit (default); "
                         "dry-run: rehearse only; auto: submit unattended (at your own risk).")
    ap.add_argument("--apply-min-score", type=float, default=4.0,
                    help="Only apply to jobs scoring >= this (default: 4.0)")
    ap.add_argument("--apply-limit", type=int, default=0,
                    help="Max applications to attempt this run (0 = no cap)")
    ap.add_argument("--apply-url", type=str, default=None,
                    help="Apply to a single specific job URL, bypassing the tracker "
                         "queue (one-off apply, or to reproduce a specific posting).")
    ap.add_argument("--apply-refresh", action=argparse.BooleanOptionalAction, default=True,
                    help="Pull the latest tracker from the most recent GitHub pipeline "
                         "artifact before applying (default on; --no-apply-refresh to use "
                         "the local applications.md). Falls back to local when offline.")
    ap.add_argument("--apply-tailor-min-score", type=float,
                    default=_env_float("APPLY_TAILOR_MIN_SCORE", 4.0),
                    help="Jobs scoring >= this get a per-job tailored resume (slot-edited "
                         "copy of resumes/resume.docx, one-page verified). Default 4.0; "
                         "set high (e.g. 99) to always use the default resume.")
    ap.add_argument("--headless", action="store_true",
                    help="Run the apply browser headless (only works once you've logged in once)")
    ap.add_argument("--config", type=Path, default=None, help="Path to search.yml")
    pass_group = ap.add_mutually_exclusive_group()
    pass_group.add_argument("--only-pass", type=str, default=None,
                            help="Comma-separated list of search `name:` values to run "
                                 "(case-insensitive). Errors loudly on no match (typo protection).")
    pass_group.add_argument("--easy-apply-only", action="store_true",
                            help="Only run passes with `easy_apply: true`. No-ops if none configured.")
    pass_group.add_argument("--no-easy-apply", action="store_true",
                            help="Skip passes with `easy_apply: true`. Used by the daily cloud workflow.")
    args = ap.parse_args()

    only_passes: list[str] | None = None
    if args.only_pass:
        only_passes = [p.strip() for p in args.only_pass.split(",") if p.strip()]

    def resolve(p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (ROOT / p).resolve()

    # `os.environ.get(VAR) or DEFAULT` instead of `get(VAR, DEFAULT)` — the
    # latter returns "" when the env var is set to an empty string, which
    # silently uses the CWD as the career-ops path. The `or` form treats
    # unset and empty-string both as "use the default".
    career_ops = resolve(os.environ.get("CAREER_OPS_PATH") or "career-ops")

    config_path = args.config or resolve(os.environ.get("SEARCH_CONFIG") or "config/search.yml")
    if not config_path.exists():
        print(f"error: config not found at {config_path}", file=sys.stderr)
        print("hint: copy config/search.example.yml -> config/search.yml and edit", file=sys.stderr)
        return 1

    notify.notify("Job Search Pipeline", "Starting scrape stage...")

    if not args.skip_scrape:
        notify.notify("Pipeline", "Scraping job boards...")
        scrape.run(
            config_path,
            only_passes=only_passes,
            easy_apply_only=args.easy_apply_only,
            no_easy_apply=args.no_easy_apply,
        )
        notify.notify("Pipeline", "Scraping complete")

    if not args.skip_filter:
        notify.notify("Pipeline", "Filtering jobs...")
        filter_step.run(config_path)
        notify.notify("Pipeline", "Filtering complete")

    if not args.skip_screen:
        screen.run(config_path, career_ops_path=career_ops)

    new_offers: list[dict] = []
    if not args.skip_bridge:
        notify.notify("Pipeline", "Updating career-ops...")
        new_offers = bridge.run(career_ops)
        notify.notify("Pipeline", f"Bridge complete -- {len(new_offers)} new offers")

    if not args.skip_batch_prep and new_offers:
        batch_prep.run(career_ops, new_offers)

    notify.notify("Pipeline", f"Pipeline complete! {len(new_offers)} offers queued")

    if args.evaluate_batch:
        batch_evaluate.run(
            career_ops,
            provider=args.batch_provider,
            model=args.batch_model,
            concurrency=args.batch_concurrency,
            dry_run=args.dry_run,
        )

    if args.apply:
        # Local import: the apply stage pulls in Playwright lazily, so the rest
        # of the pipeline never pays for it.
        from pipeline import apply
        notify.notify("Pipeline", "Applying to qualified jobs...")
        apply_mode = "dry-run" if args.dry_run else args.apply_mode
        apply.run(
            career_ops,
            mode=apply_mode,
            min_score=args.apply_min_score,
            limit=args.apply_limit,
            headless=args.headless,
            refresh=args.apply_refresh,
            target_url=args.apply_url,
            provider=args.batch_provider,
            model=args.batch_model,
            tailor_min_score=args.apply_tailor_min_score,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
