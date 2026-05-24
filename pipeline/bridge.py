"""Bridge filtered_jobs.csv into career-ops's data/pipeline.md.

Mirrors scan.mjs's output format:
  - Appends `- [ ] {url} | {company} | {title}` lines under `## Pendientes`
  - Records new entries in `data/scan-history.tsv` for dedup
  - Skips URLs already in scan-history.tsv, pipeline.md, or applications.md

The user then runs `/career-ops pipeline` in their AI CLI to evaluate the queue."""

import csv
import html
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from pipeline._batch_common import parse_date_posted

ROOT = Path(__file__).resolve().parent.parent
FILTERED_PATH = ROOT / "output" / "filtered_jobs.csv"

PIPELINE_MD = "data/pipeline.md"
SCAN_HISTORY = "data/scan-history.tsv"
APPLICATIONS_MD = "data/applications.md"

# Full job descriptions can be tens of KB. We keep the structured copy in
# career-ops/batch/jds/{id}.txt and only embed a preview in pipeline.md.
DESCRIPTION_PREVIEW_CHARS = 500


def _parse_applications_md(text: str) -> tuple[set[str], set[str]]:
    """Walk applications.md once. Return (urls, company::role pairs)."""
    urls: set[str] = set()
    roles: set[str] = set()

    # URLs can appear anywhere — links, table cells, raw markdown. Use the same
    # cheap regex the original code used.
    urls.update(re.findall(r"https?://[^\s|)]+", text))

    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        # Markdown separator row: |---|---|...
        if re.match(r"^\|[\s|:\-]+\|?\s*$", line):
            continue
        # Strip optional leading/trailing pipes before splitting so empty
        # columns at the ends don't shift positions.
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        cols = [c.strip() for c in stripped.split("|")]
        # Expected layout: # | Date | Company | Role | URL | Status
        if len(cols) < 4:
            continue
        company, role = cols[2].lower(), cols[3].lower()
        # Skip header row
        if company == "company" and role == "role":
            continue
        if company and role:
            roles.add(f"{company}::{role}")

    return urls, roles


def load_seen(career_ops: Path) -> tuple[set[str], set[str]]:
    """Return (seen_urls, seen_company_roles) merged across all dedup sources."""
    urls: set[str] = set()
    roles: set[str] = set()

    hist = career_ops / SCAN_HISTORY
    if hist.exists():
        with open(hist, "r", encoding="utf-8") as f:
            next(f, None)  # header
            for line in f:
                url = line.split("\t", 1)[0].strip()
                if url:
                    urls.add(url)

    pipe = career_ops / PIPELINE_MD
    if pipe.exists():
        text = pipe.read_text(encoding="utf-8")
        urls.update(re.findall(r"- \[[ x]\] (https?://\S+)", text))

    apps = career_ops / APPLICATIONS_MD
    if apps.exists():
        text = apps.read_text(encoding="utf-8")
        app_urls, app_roles = _parse_applications_md(text)
        urls.update(app_urls)
        roles.update(app_roles)

    return urls, roles


# Back-compat aliases — tests call these directly.
def load_seen_urls(career_ops: Path) -> set[str]:
    return load_seen(career_ops)[0]


def load_seen_company_roles(career_ops: Path) -> set[str]:
    return load_seen(career_ops)[1]


def _safe_description(raw: str) -> str:
    """Truncate to a preview length and escape HTML, including stripping any
    literal </details> tokens that would break the surrounding block."""
    text = raw.strip()
    if len(text) > DESCRIPTION_PREVIEW_CHARS:
        text = text[:DESCRIPTION_PREVIEW_CHARS].rstrip() + "..."
    # html.escape covers &, <, >, ", '. Belt-and-suspenders on </details> in
    # case any future change loosens the escape.
    text = html.escape(text, quote=True)
    return text.replace("</details>", "&lt;/details&gt;")


def append_to_pipeline(career_ops: Path, offers: list[dict]) -> None:
    pipe = career_ops / PIPELINE_MD
    pipe.parent.mkdir(parents=True, exist_ok=True)

    def format_offer(o: dict) -> str:
        lines = [f"- [ ] {o['url']} | {o['company']} | {o['title']}"]
        if o.get("description"):
            desc = _safe_description(o["description"])
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


def append_to_scan_history(
    career_ops: Path,
    entries: list[dict],
    today: str,
    status: str = "added",
) -> None:
    """Append rows to scan-history.tsv.

    `status` is the value written in the final column. Use the default
    "added" for bridge's normal flow; pre-screen records use "screened-dead"
    so future runs can skip URLs that already failed liveness.

    Each entry must have `url`, `title`, `company`."""
    hist = career_ops / SCAN_HISTORY
    hist.parent.mkdir(parents=True, exist_ok=True)
    if not hist.exists():
        hist.write_text("url\tfirst_seen\tportal\ttitle\tcompany\tstatus\n", encoding="utf-8")
    with open(hist, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e['url']}\t{today}\tjobspy\t{e['title']}\t{e['company']}\t{status}\n")


def run(career_ops_path: Path) -> list[dict]:
    if not FILTERED_PATH.exists() or FILTERED_PATH.stat().st_size == 0:
        print("[bridge] no filtered_jobs.csv — run filter first (or nothing passed the threshold)")
        return []

    if not career_ops_path.exists():
        raise FileNotFoundError(
            f"career-ops not found at {career_ops_path}. "
            "Run setup.ps1/setup.sh or set CAREER_OPS_PATH in .env."
        )

    seen_urls, seen_roles = load_seen(career_ops_path)

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
        return []

    # Newest first; unparseable / missing dates sort to the end.
    def sort_key(offer: dict) -> tuple:
        parsed = parse_date_posted(offer.get("date_posted") or "")
        return (0, parsed) if parsed is not None else (1, datetime.min)

    new_offers.sort(key=sort_key, reverse=True)

    today = date.today().isoformat()
    append_to_pipeline(career_ops_path, new_offers)
    append_to_scan_history(career_ops_path, new_offers, today)
    print(f"[bridge] added {len(new_offers)} offers to {career_ops_path / PIPELINE_MD}")
    return new_offers


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")
    )
    run(path.resolve())
