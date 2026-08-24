"""Scrape job boards via JobSpy. Reads search params from config/search.yml,
validates mutually exclusive Indeed/LinkedIn options, and writes output/jobs.csv."""

import sys
from pathlib import Path

import pandas as pd
import yaml
from jobspy import scrape_jobs

from pipeline.sites import SUPPORTED_SITES, is_supported

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "output" / "jobs.csv"

# All optional JobSpy kwargs that map 1-to-1 from config keys.
# Passed through only when explicitly set (not None/missing).
OPTIONAL_PARAMS = [
    "location",
    "distance",
    "job_type",
    "is_remote",
    "easy_apply",
    "user_agent",
    "description_format",
    "offset",
    "hours_old",
    "verbose",
    "linkedin_fetch_description",
    "linkedin_company_ids",
    "country_indeed",
    "enforce_annual_salary",
    "ca_cert",
    "proxies",
]

def strip_unsupported_sites(searches: list[dict]) -> list[dict]:
    """Remove unsupported sites from every pass; drop passes with none left.

    Applied on the way into a run rather than only when a config is authored,
    so stale configs — e.g. a fork's old SEARCH_CONFIG_B64 cloud secret still
    listing glassdoor — degrade to a warning instead of wasted requests or a
    crash."""
    result = []
    for cfg in searches:
        sites = cfg.get("sites")
        if sites is None:
            result.append(cfg)
            continue
        kept, dropped = [], []
        for s in sites:
            (kept if is_supported(s) else dropped).append(s)
        name = cfg.get("name", "pass")
        if dropped:
            print(
                f"[scrape] [{name}] dropping unsupported sites: {', '.join(dropped)} "
                f"(supported: {', '.join(SUPPORTED_SITES)})",
                flush=True,
            )
        if not kept:
            print(f"[scrape] [{name}] skipping pass — no supported sites left", flush=True)
            continue
        result.append({**cfg, "sites": kept} if dropped else cfg)
    return result


def load_searches(path: Path) -> list[dict]:
    """Return a list of search configs. Supports `searches:` (list) and legacy `search:` (single)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if "searches" in raw:
        return raw["searches"]
    return [raw["search"]]


def filter_passes(
    searches: list[dict],
    only_passes: list[str] | None = None,
    *,
    easy_apply_only: bool = False,
    no_easy_apply: bool = False,
) -> list[dict]:
    """Return the subset of `searches` matching the given selectors.

    Selectors (any combination, applied as AND):
      - `only_passes`: keep passes whose `name:` matches (case-insensitive).
        Raises ValueError on no match — protects against `--only-pass` typos
        from the CLI.
      - `easy_apply_only`: keep only passes with `easy_apply: true`.
      - `no_easy_apply`: keep only passes without `easy_apply: true`.

    `easy_apply_only` and `no_easy_apply` are used by the cloud workflows to
    route passes to the right schedule by JobSpy field rather than pass name.
    When they filter to zero passes, this returns an empty list — the workflow
    treats that as "nothing to do for this run" and exits cleanly. That's
    different from `only_passes` (a CLI typo should be loud, but having no
    easy-apply passes configured is just a valid state)."""
    result = searches

    if only_passes:
        wanted = {p.strip().lower() for p in only_passes if p and p.strip()}
        if wanted:
            selected = [s for s in result if (s.get("name") or "").strip().lower() in wanted]
            if not selected:
                available = ", ".join(repr(s.get("name", "")) for s in result)
                raise ValueError(
                    f"--only-pass matched no searches. Wanted: {sorted(wanted)}; available: {available}"
                )
            result = selected

    if easy_apply_only and no_easy_apply:
        raise ValueError("easy_apply_only and no_easy_apply are mutually exclusive")

    if easy_apply_only:
        result = [s for s in result if s.get("easy_apply") is True]
    elif no_easy_apply:
        result = [s for s in result if s.get("easy_apply") is not True]

    return result


def mark_easy_apply(combined: pd.DataFrame) -> pd.DataFrame:
    """Collapse the per-pass easy_apply flag to a per-URL OR.

    Each row arrives carrying the easy_apply flag of the pass that produced it.
    A job returned by both a broad pass (False) and an easy_apply pass (True)
    must end up True regardless of which duplicate row `drop_duplicates` later
    keeps — so OR the flag across every row sharing a job_url before dedup.
    JobSpy gives no per-job "easy apply" signal, so this pass-level flag is the
    only thing that distinguishes (e.g.) Indeed SmartApply roles downstream."""
    if "easy_apply" not in combined.columns:
        combined["easy_apply"] = False
    flag = combined["easy_apply"].fillna(False).astype(bool)
    # transform drops NaN-keyed groups, so a row with a NaN job_url comes back
    # NaN and would promote the column to object dtype — fillna keeps it bool.
    combined["easy_apply"] = (
        flag.groupby(combined["job_url"]).transform("max").fillna(False).astype(bool)
    )
    return combined


def validate_limitations(cfg: dict) -> None:
    """Raise ValueError if mutually exclusive JobSpy options are combined.

    Indeed: only one of these groups may be active:
      Group A — hours_old
      Group B — job_type and/or is_remote
      Group C — easy_apply

    LinkedIn: only one of these may be active:
      hours_old  OR  easy_apply
    """
    sites = [s.lower() for s in cfg.get("sites", [])]
    hours_old   = cfg.get("hours_old")   is not None
    job_type    = cfg.get("job_type")    is not None
    is_remote   = cfg.get("is_remote")   is not None
    easy_apply  = cfg.get("easy_apply")  is not None

    if "indeed" in sites:
        active = [hours_old, job_type or is_remote, easy_apply]
        if sum(active) > 1:
            raise ValueError(
                "Indeed limitation: only ONE of the following groups "
                "may be set per search:\n"
                "  Group A — hours_old\n"
                "  Group B — job_type and/or is_remote\n"
                "  Group C — easy_apply\n"
                "Remove the conflicting options from config/search.yml."
            )

    if "linkedin" in sites:
        if hours_old and easy_apply:
            raise ValueError(
                "LinkedIn limitation: only ONE of [hours_old] or [easy_apply] "
                "may be set per search. Remove one from config/search.yml."
            )


def run(
    config_path: Path,
    only_passes: list[str] | None = None,
    *,
    easy_apply_only: bool = False,
    no_easy_apply: bool = False,
) -> Path:
    searches = strip_unsupported_sites(
        filter_passes(
            load_searches(config_path),
            only_passes,
            easy_apply_only=easy_apply_only,
            no_easy_apply=no_easy_apply,
        )
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not searches:
        # No matching passes — write an empty header-only CSV so downstream
        # stages no-op cleanly. This is the workflow-friendly path: e.g. the
        # easy-apply workflow on a user with no easy_apply pass configured.
        print(
            "[scrape] no searches matched the active filters — writing empty jobs.csv",
            flush=True,
        )
        OUTPUT_PATH.write_text("", encoding="utf-8")
        return OUTPUT_PATH

    if only_passes or easy_apply_only or no_easy_apply:
        names = ", ".join(repr(s.get("name", "")) for s in searches)
        print(f"[scrape] running passes: {names}", flush=True)

    all_rows = []
    for cfg in searches:
        name = cfg.get("name", "pass")
        validate_limitations(cfg)
        optional = {k: cfg[k] for k in OPTIONAL_PARAMS if cfg.get(k) is not None}

        pass_easy_apply = cfg.get("easy_apply") is True
        for term in cfg["search_terms"]:
            print(f"[scrape] [{name}] searching: {term!r}", flush=True)
            df = scrape_jobs(
                site_name=cfg["sites"],
                search_term=term,
                results_wanted=cfg.get("results_wanted", 50),
                **optional,
            )
            # Tag every row with the pass's easy_apply flag so it survives the
            # cross-pass merge; mark_easy_apply ORs it per URL below.
            df["easy_apply"] = pass_easy_apply
            all_rows.append(df)

    combined = pd.concat(all_rows, ignore_index=True) if all_rows else None
    if combined is None or combined.empty:
        print("[scrape] no jobs returned")
        return OUTPUT_PATH

    combined = mark_easy_apply(combined)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["job_url"])
    print(f"[scrape] {before} rows -> {len(combined)} after dedup")

    combined.to_csv(OUTPUT_PATH, index=False)
    print(f"[scrape] wrote {OUTPUT_PATH}")
    return OUTPUT_PATH


if __name__ == "__main__":
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path)
