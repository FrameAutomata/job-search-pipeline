"""Bridge filtered_jobs.csv into career-ops's data/pipeline.md.

Mirrors scan.mjs's output format:
  - Appends `- [ ] {url} | {company} | {title}` lines under `## Pendientes`
  - Records new entries in `data/scan-history.tsv` for dedup
  - Skips URLs already in scan-history.tsv, pipeline.md, or applications.md

The user then runs `/career-ops pipeline` in their AI CLI to evaluate the queue."""

import csv
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILTERED_PATH = ROOT / "output" / "filtered_jobs.csv"

PIPELINE_MD = "data/pipeline.md"
SCAN_HISTORY = "data/scan-history.tsv"
APPLICATIONS_MD = "data/applications.md"


def load_seen_urls(career_ops: Path) -> set[str]:
    seen = set()

    hist = career_ops / SCAN_HISTORY
    if hist.exists():
        with open(hist, "r", encoding="utf-8") as f:
            next(f, None)  # header
            for line in f:
                url = line.split("\t", 1)[0].strip()
                if url:
                    seen.add(url)

    pipe = career_ops / PIPELINE_MD
    if pipe.exists():
        text = pipe.read_text(encoding="utf-8")
        seen.update(re.findall(r"- \[[ x]\] (https?://\S+)", text))

    apps = career_ops / APPLICATIONS_MD
    if apps.exists():
        text = apps.read_text(encoding="utf-8")
        seen.update(re.findall(r"https?://[^\s|)]+", text))

    return seen


def load_seen_company_roles(career_ops: Path) -> set[str]:
    seen = set()
    apps = career_ops / APPLICATIONS_MD
    if not apps.exists():
        return seen
    text = apps.read_text(encoding="utf-8")
    # Markdown table: | # | Date | Company | Role | ...
    for m in re.finditer(r"\|[^|]+\|[^|]+\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|", text):
        company, role = m.group(1).strip().lower(), m.group(2).strip().lower()
        if company and role and company != "company":
            seen.add(f"{company}::{role}")
    return seen


def append_to_pipeline(career_ops: Path, offers: list[dict]) -> None:
    pipe = career_ops / PIPELINE_MD
    pipe.parent.mkdir(parents=True, exist_ok=True)

    def format_offer(o: dict) -> str:
        """Format offer as checkbox link with optional collapsible description."""
        lines = [f"- [ ] {o['url']} | {o['company']} | {o['title']}"]
        if o.get("description"):
            # Escape HTML special chars in description for safety
            desc = o["description"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"  <details><summary>Description</summary>\n\n  {desc}\n\n  </details>")
        return "\n".join(lines)

    block = "\n" + "\n".join(format_offer(o) for o in offers) + "\n"

    if not pipe.exists():
        pipe.write_text(f"# Pipeline\n\n## Pendientes\n{block}\n", encoding="utf-8")
        return

    text = pipe.read_text(encoding="utf-8")
    marker = "## Pendientes"
    idx = text.find(marker)
    if idx == -1:
        proc_idx = text.find("## Procesadas")
        insert_at = proc_idx if proc_idx != -1 else len(text)
        new_block = f"\n{marker}\n{block}\n"
        text = text[:insert_at] + new_block + text[insert_at:]
    else:
        after = idx + len(marker)
        next_section = text.find("\n## ", after)
        insert_at = next_section if next_section != -1 else len(text)
        text = text[:insert_at] + block + text[insert_at:]

    pipe.write_text(text, encoding="utf-8")


def append_to_scan_history(career_ops: Path, offers: list[dict], today: str) -> None:
    hist = career_ops / SCAN_HISTORY
    hist.parent.mkdir(parents=True, exist_ok=True)
    if not hist.exists():
        hist.write_text("url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n", encoding="utf-8")
    with open(hist, "a", encoding="utf-8") as f:
        for o in offers:
            f.write(f"{o['url']}\t{today}\tjobspy\t{o['title']}\t{o['company']}\tadded\n")


def run(career_ops_path: Path) -> int:
    if not FILTERED_PATH.exists() or FILTERED_PATH.stat().st_size == 0:
        print("[bridge] no filtered_jobs.csv — run filter first (or nothing passed the threshold)")
        return 0

    if not career_ops_path.exists():
        raise FileNotFoundError(
            f"career-ops not found at {career_ops_path}. "
            "Run setup.ps1/setup.sh or set CAREER_OPS_PATH in .env."
        )

    seen_urls = load_seen_urls(career_ops_path)
    seen_roles = load_seen_company_roles(career_ops_path)

    new_offers = []
    with open(FILTERED_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("job_url") or "").strip()
            title = (row.get("title") or "").strip()
            company = (row.get("company") or "").strip()
            if not url or not title or not company:
                continue
            if url in seen_urls:
                continue
            key = f"{company.lower()}::{title.lower()}"
            if key in seen_roles:
                continue
            seen_urls.add(url)
            seen_roles.add(key)

            description = (row.get("description") or "").strip()
            date_posted_str = (row.get("date_posted") or "").strip()

            new_offers.append({
                "url": url,
                "title": title,
                "company": company,
                "description": description,
                "date_posted": date_posted_str,
            })

    if not new_offers:
        print("[bridge] no new offers to add (all duplicates)")
        return 0

    # Sort by date_posted descending (newest first), fallback to empty string for missing dates
    def sort_key(offer: dict) -> tuple:
        date_str = offer.get("date_posted") or ""
        try:
            return (0, datetime.strptime(date_str, "%Y-%m-%d"))
        except (ValueError, TypeError):
            # Unparseable or missing dates sort to end
            return (1, datetime.min)

    new_offers.sort(key=sort_key, reverse=True)

    today = date.today().isoformat()
    append_to_pipeline(career_ops_path, new_offers)
    append_to_scan_history(career_ops_path, new_offers, today)
    print(f"[bridge] added {len(new_offers)} offers to {career_ops_path / PIPELINE_MD}")
    print("[bridge] next: cd into career-ops and run /career-ops pipeline in your AI CLI")
    return len(new_offers)


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")
    )
    run(path.resolve())
