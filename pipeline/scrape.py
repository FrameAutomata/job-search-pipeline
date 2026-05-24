"""Scrape job boards via JobSpy. Reads search params from config/search.yml,
validates mutually exclusive Indeed/LinkedIn options, and writes output/jobs.csv."""

import sys
from pathlib import Path

import pandas as pd
import yaml
from jobspy import scrape_jobs

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
    "google_search_term",
]


def load_searches(path: Path) -> list[dict]:
    """Return a list of search configs. Supports `searches:` (list) and legacy `search:` (single)."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if "searches" in raw:
        return raw["searches"]
    return [raw["search"]]


def filter_passes(searches: list[dict], only_passes: list[str] | None) -> list[dict]:
    """Keep only searches whose `name:` matches one of `only_passes`.

    Matching is case-insensitive and whitespace-trimmed so the workflow can
    pass arbitrary casing. Returns all searches when `only_passes` is empty."""
    if not only_passes:
        return searches
    wanted = {p.strip().lower() for p in only_passes if p and p.strip()}
    if not wanted:
        return searches
    selected = [s for s in searches if (s.get("name") or "").strip().lower() in wanted]
    if not selected:
        available = ", ".join(repr(s.get("name", "")) for s in searches)
        raise ValueError(
            f"--only-pass matched no searches. Wanted: {sorted(wanted)}; available: {available}"
        )
    return selected


def validate_limitations(cfg: dict) -> None:
    """Raise ValueError if mutually exclusive JobSpy options are combined.

    Indeed / Glassdoor: only one of these groups may be active:
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

    if "indeed" in sites or "glassdoor" in sites:
        active = [hours_old, job_type or is_remote, easy_apply]
        if sum(active) > 1:
            raise ValueError(
                "Indeed/Glassdoor limitation: only ONE of the following groups "
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


def run(config_path: Path, only_passes: list[str] | None = None) -> Path:
    searches = filter_passes(load_searches(config_path), only_passes)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if only_passes:
        names = ", ".join(repr(s.get("name", "")) for s in searches)
        print(f"[scrape] running passes: {names}", flush=True)

    all_rows = []
    for cfg in searches:
        name = cfg.get("name", "pass")
        validate_limitations(cfg)
        optional = {k: cfg[k] for k in OPTIONAL_PARAMS if cfg.get(k) is not None}

        for term in cfg["search_terms"]:
            print(f"[scrape] [{name}] searching: {term!r}", flush=True)
            df = scrape_jobs(
                site_name=cfg["sites"],
                search_term=term,
                results_wanted=cfg.get("results_wanted", 50),
                **optional,
            )
            all_rows.append(df)

    combined = pd.concat(all_rows, ignore_index=True) if all_rows else None
    if combined is None or combined.empty:
        print("[scrape] no jobs returned")
        return OUTPUT_PATH

    before = len(combined)
    combined = combined.drop_duplicates(subset=["job_url"])
    print(f"[scrape] {before} rows -> {len(combined)} after dedup")

    combined.to_csv(OUTPUT_PATH, index=False)
    print(f"[scrape] wrote {OUTPUT_PATH}")
    return OUTPUT_PATH


if __name__ == "__main__":
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path)
