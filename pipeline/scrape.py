"""Scrape job boards via JobSpy. Reads search params from config/search.yml,
writes results to output/jobs.csv."""

from pathlib import Path
import sys
import yaml
from jobspy import scrape_jobs

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "output" / "jobs.csv"


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(config_path: Path) -> Path:
    cfg = load_config(config_path)["search"]
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for term in cfg["search_terms"]:
        print(f"[scrape] searching: {term!r}")
        df = scrape_jobs(
            site_name=cfg["sites"],
            search_term=term,
            location=cfg.get("location"),
            country_indeed=cfg.get("country_indeed"),
            results_wanted=cfg.get("results_wanted", 50),
            hours_old=cfg.get("hours_old"),
        )
        all_rows.append(df)

    import pandas as pd
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
