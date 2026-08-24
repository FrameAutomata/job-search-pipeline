"""Chain scrape -> filter -> screen -> bridge -> batch_prep. Each step can be skipped with a flag.

Applying is NOT a pipeline stage: the pipeline finds and evaluates roles, then
--handoff emits a work-order per site the scraper searches from
(output/handoff/next-roles-<site>.jsonl + .md) for whatever browser agent the
user prefers (Claude Cowork, OpenClaw, a local Agent-SDK runner, ...) to work
through — one site session at a time — with the user's own logged-in browser.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# The cloud-shared search config (populated from the SEARCH_CONFIG_B64 secret in
# the daily workflow) and the optional LOCAL override. A local run auto-prefers
# the override so a user can search different terms locally than the cloud daily,
# without touching the secret. The cloud checkout never has search.local.yml —
# it's gitignored and the daily only ever decodes the secret into search.yml.
SHARED_SEARCH_CONFIG = Path("config/search.yml")
LOCAL_SEARCH_CONFIG = Path("config/search.local.yml")


def resolve_search_config(explicit: str | Path | None, root: Path = ROOT) -> Path:
    """Pick the search config, resolved against `root`.

    Precedence: explicit --config > a *custom* SEARCH_CONFIG env >
    config/search.local.yml (if present) > config/search.yml.

    Note the "custom" qualifier: .env.example ships `SEARCH_CONFIG=./config/search.yml`,
    so nearly every local .env has the var set to the shared default. Honoring that
    literally would defeat the local override for everyone, so a SEARCH_CONFIG that
    resolves to the shared config/search.yml is treated as boilerplate (equivalent to
    unset) and the override still wins. A SEARCH_CONFIG pointing anywhere else is a
    deliberate choice and takes precedence. The cloud daily sets no SEARCH_CONFIG env
    (it relies on the default path), so it always uses the decoded search.yml.
    """
    def _resolve(p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (root / p).resolve()

    if explicit:
        return _resolve(explicit)
    shared = _resolve(SHARED_SEARCH_CONFIG)
    env = os.environ.get("SEARCH_CONFIG")
    if env and (env_path := _resolve(env)) != shared:
        return env_path
    local = _resolve(LOCAL_SEARCH_CONFIG)
    if local.exists():
        return local
    return shared


from pipeline import scrape, filter as filter_step, screen, bridge, batch_prep, batch_evaluate, notify  # noqa: E402
from pipeline._batch_common import env_float  # noqa: E402  (shared with batch_evaluate's timeout/budget knobs)
# Only the board vocabulary is needed at arg-parse time; handoff.run itself is
# still lazy-imported below to keep the hot pipeline path free of its tailoring
# deps. This module-level import is cheap (handoff pulls only _batch_common + app.data).
from pipeline.handoff import KNOWN_BOARDS  # noqa: E402


def _line_buffer_stdio() -> None:
    """Flush the stage log on every newline, however we were launched.

    Redirected to a file or a pipe, Python block-buffers stdout at 8KB — so a
    stage's progress lines sit unseen while it works, and the run looks hung on
    exactly the slow steps a reader is watching. Both callers that redirect us
    already compensate (pipeline/app/local_run.py sets PYTHONUNBUFFERED in the
    child env; daily-pipeline.yml sets it and passes `python -u`), which is why
    the ~90 unflushed prints across pipeline/ have never shown the symptom.

    Making it true here instead means the guarantee belongs to the program
    rather than to every caller remembering, and a `print` added to a stage
    tomorrow is correct without anyone thinking about buffering. The scattered
    `flush=True` calls stay: they still carry a stage module run directly
    (`python pipeline/scrape.py > log`), which never reaches this function.

    Guarded because sys.stdout is not always a TextIOWrapper — pytest's capture
    and some embedding hosts replace it with an object that has no reconfigure.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _line_buffer_stdio()
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
                    default=max(1, int(env_float("BATCH_CONCURRENCY", 3))),
                    help="Parallel workers for --evaluate-batch (default: 3, or the "
                         "BATCH_CONCURRENCY env var; floored at 1). Size to your "
                         "provider's limits — e.g. DeepInfra allows 200 concurrent "
                         "requests per model.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be submitted/evaluated without doing it")
    ap.add_argument("--handoff", action="store_true",
                    help="After evaluation, build one browser-agent work-order per site "
                         "(output/handoff/next-roles-<site>.jsonl + .md): every evaluated, "
                         "not-yet-handled role, ranked best-first within each site, for your "
                         "browser agent to apply through your own logged-in browser.")
    ap.add_argument("--handoff-board", choices=["both", *sorted(KNOWN_BOARDS)], default="both",
                    help="Restrict the hand-off to one site's session (default: both = "
                         "a session per site the scraper searches from)")
    ap.add_argument("--handoff-limit", type=int, default=None,
                    help="Cap EACH site's session at the top N roles (per-site, not a global total)")
    ap.add_argument("--handoff-tailor", action="store_true",
                    help="With --handoff: pre-tailor a candidate-named resume per "
                         "work-order row (needs resumes/resume.docx) so the agent "
                         "gets ready-to-upload files")
    ap.add_argument("--handoff-tailor-min-score", type=float, default=None,
                    help="With --handoff-tailor: only pre-tailor rows scoring >= "
                         "this (default: APPLY_TAILOR_MIN_SCORE env, else 4.0)")
    ap.add_argument("--recheck-liveness", action="store_true",
                    help="Re-check liveness of evaluated tracker roles and mark "
                         "closed/gone ones Discarded. Off by default; the daily "
                         "cloud workflow turns it on.")
    ap.add_argument("--recheck-timeout", type=int, default=8,
                    help="Per-request timeout (seconds) for --recheck-liveness (default: 8)")
    ap.add_argument("--recheck-drain", action="store_true",
                    help="With --recheck-liveness: loop budgeted sweeps (cooldown "
                         "between bursts) until the whole Evaluated backlog is "
                         "covered, instead of one budgeted sweep. For a manual "
                         "catch-up; the cloud workflow stays single-sweep.")
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

    config_path = resolve_search_config(args.config)
    if config_path == resolve(LOCAL_SEARCH_CONFIG):
        print(f"[config] using local search override ({LOCAL_SEARCH_CONFIG.name})")
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

    if args.recheck_liveness:
        # Re-check the tracker's still-open roles and Discard the ones whose
        # posting has closed. (Note: this cleans the TRACKER; the --handoff
        # work-order is built from the scored-queue export, which recheck does
        # not rewrite — the browser agent still verifies each posting live.)
        # recheck.run prints its own summary.
        from pipeline import recheck
        notify.notify("Pipeline", "Re-checking tracker liveness...")
        if args.recheck_drain:
            recheck.drain(career_ops, timeout=args.recheck_timeout)
        else:
            recheck.run(career_ops, timeout=args.recheck_timeout)

    if args.handoff:
        if args.dry_run:
            # --dry-run promises "print without doing it": the handoff stage
            # rewrites the tracker/work-order and --handoff-tailor spends real
            # LLM calls + renders, so it must not run during a rehearsal.
            print("[handoff] skipped (--dry-run)")
            return 0
        # Terminal stage: emit the work-order for the user's browser agent.
        # Lazy import keeps the hot pipeline path free of it. (handoff reads
        # HANDOFF_JOB_LOG itself; queue = the scored-export jsonl when present,
        # else career-ops' applications.md.)
        from pipeline import handoff
        notify.notify("Pipeline", "Building the browser-agent work-order...")
        rc = handoff.run(board=args.handoff_board, limit=args.handoff_limit,
                         tailor=args.handoff_tailor,
                         tailor_min_score=args.handoff_tailor_min_score,
                         career_ops=career_ops,
                         workers=args.batch_concurrency)
        if rc != 0:
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
