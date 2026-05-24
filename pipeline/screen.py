"""Screen filtered_jobs.csv for liveness AND backfill missing job descriptions.

Runs between filter and bridge. Controlled by the `screen:` section in
config/search.yml.

  screen:
    liveness: false          # HTTP liveness check (default: false)
    liveness_timeout: 8      # per-request timeout in seconds (default: 8)

When liveness is on, the same HTTP response is also mined for a job
description. Any job row whose `description` column is empty (typical for
LinkedIn when `linkedin_fetch_description: false` in the scrape config) gets
populated from the page body before bridge runs. This lets us scrape LinkedIn
without paying for a per-job description fetch on thousands of jobs that get
filtered out — we only fetch for the ~dozens that survive scoring.

Liveness logic is a Python port of career-ops/liveness-core.mjs. When
disabled (or `screen:` is absent) the stage is a no-op.
"""

import csv
import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
FILTERED_PATH = ROOT / "output" / "filtered_jobs.csv"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ── Liveness patterns (port of career-ops/liveness-core.mjs) ─────────────────

_HARD_EXPIRED = [
    re.compile(r"job (is )?no longer available", re.I),
    re.compile(r"job.*no longer open", re.I),
    re.compile(r"position has been filled", re.I),
    re.compile(r"this job has expired", re.I),
    re.compile(r"job posting has expired", re.I),
    re.compile(r"no longer accepting applications", re.I),
    re.compile(r"this (position|role|job) (is )?no longer", re.I),
    re.compile(r"this job (listing )?is closed", re.I),
    re.compile(r"job (listing )?not found", re.I),
    re.compile(r"the page you are looking for doesn.t exist", re.I),
    re.compile(r"applications?\s+(?:(?:have|are|is)\s+)?closed", re.I),
    re.compile(r"closed on \d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I),
    re.compile(r"closed on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}", re.I),
    re.compile(r"diese stelle (ist )?(nicht mehr|bereits) besetzt", re.I),
    re.compile(r"offre (expir[eé]e|n.est plus disponible)", re.I),
]

_LISTING_PAGE = [
    re.compile(r"\d+\s+jobs?\s+found", re.I),
    re.compile(r"search for jobs page is loaded", re.I),
]

_EXPIRED_URL = re.compile(r"[?&]error=true", re.I)

_APPLY = [
    re.compile(r"\bapply\b", re.I),
    re.compile(r"\bsolicitar\b", re.I),
    re.compile(r"\bbewerben\b", re.I),
    re.compile(r"\bpostuler\b", re.I),
    re.compile(r"submit application", re.I),
    re.compile(r"easy apply", re.I),
    re.compile(r"start application", re.I),
    re.compile(r"ich bewerbe mich", re.I),
]

_MIN_CONTENT_CHARS = 300


# ── Description extraction ──────────────────────────────────────────────────

# Job descriptions get inlined into the LLM system prompt; an 8 KB ceiling
# keeps prompt size predictable without truncating realistic JDs.
_MAX_DESCRIPTION_CHARS = 8000

# Site-specific JD containers, tried before falling back to body extraction.
# Each pattern captures the inner HTML of the description block.
_SITE_DESCRIPTION_PATTERNS = [
    # LinkedIn guest job page — `show-more-less-html__markup` wraps the JD.
    re.compile(
        r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL | re.IGNORECASE,
    ),
    # Indeed full-page JD container.
    re.compile(
        r'<div[^>]*id="jobDescriptionText"[^>]*>(.*?)</div>',
        re.DOTALL | re.IGNORECASE,
    ),
    # Glassdoor JD container.
    re.compile(
        r'<div[^>]*class="[^"]*jobDescriptionContent[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL | re.IGNORECASE,
    ),
]

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.DOTALL | re.IGNORECASE)


def _clean_html(s: str) -> str:
    """Strip scripts/styles + all HTML tags, decode entities, collapse whitespace."""
    s = _SCRIPT_STYLE_RE.sub(" ", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WHITESPACE_RE.sub(" ", s).strip()


def extract_description(html_body: str) -> str:
    """Pull the visible job description text from a fetched page.

    Tries site-specific selectors (LinkedIn, Indeed, Glassdoor) first because
    they isolate the actual JD from page chrome. Falls back to the whole
    `<body>` so unknown sites still produce *something* usable."""
    if not html_body:
        return ""
    for pat in _SITE_DESCRIPTION_PATTERNS:
        m = pat.search(html_body)
        if m:
            cleaned = _clean_html(m.group(1))
            if cleaned:
                return cleaned[:_MAX_DESCRIPTION_CHARS]
    body = _BODY_RE.search(html_body)
    if body:
        cleaned = _clean_html(body.group(1))
        return cleaned[:_MAX_DESCRIPTION_CHARS]
    return _clean_html(html_body)[:_MAX_DESCRIPTION_CHARS]


# ── Liveness check ──────────────────────────────────────────────────────────

def classify_liveness(status: int, final_url: str, body: str) -> tuple[str, str]:
    """Return (result, reason): result is 'active', 'expired', or 'uncertain'."""
    if status in (404, 410):
        return "expired", f"HTTP {status}"
    if _EXPIRED_URL.search(final_url):
        return "expired", f"error redirect: {final_url}"
    for pat in _HARD_EXPIRED:
        if pat.search(body):
            return "expired", f"body: {pat.pattern}"
    if any(p.search(body) for p in _APPLY):
        return "active", "apply control visible"
    for pat in _LISTING_PAGE:
        if pat.search(body):
            return "expired", f"listing page: {pat.pattern}"
    if len(body.strip()) < _MIN_CONTENT_CHARS:
        return "expired", "insufficient content"
    return "uncertain", "content present, no apply control"


_MAX_REDIRECTS = 5


def fetch_and_classify(url: str, timeout: int = 8) -> tuple[str, str, str]:
    """Fetch the page and return (liveness_result, reason, html_body).

    The body is returned so the caller can also mine it for the job
    description without paying for a second HTTP request."""
    import requests  # transitive dep via python-jobspy

    try:
        session = requests.Session()
        session.max_redirects = _MAX_REDIRECTS
        resp = session.get(
            url, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True
        )
        final_scheme = urlparse(str(resp.url)).scheme.lower()
        if final_scheme not in ("http", "https"):
            return "uncertain", f"unexpected scheme after redirect: {final_scheme}", ""
        result, reason = classify_liveness(resp.status_code, str(resp.url), resp.text)
        return result, reason, resp.text
    except Exception as exc:
        return "uncertain", f"request error: {exc}", ""


def check_liveness(url: str, timeout: int = 8) -> tuple[str, str]:
    """Back-compat wrapper that returns just (result, reason). Existing tests
    and external callers use this; the description-backfill path calls
    `fetch_and_classify` directly to also receive the page body."""
    result, reason, _ = fetch_and_classify(url, timeout)
    return result, reason


# ── Main entry point ────────────────────────────────────────────────────────

def run(config_path: Path, career_ops_path: Path | None = None) -> int:
    """Screen filtered_jobs.csv in-place. Returns number of jobs dropped.

    If `career_ops_path` is provided, the run also:
      1. Loads URLs already known (scan-history.tsv + pipeline.md + applications.md)
         and drops them *before* any HTTP fetches. With 100-result scrapes
         on a daily cadence ~80% of rows are repeats — skipping them here is
         the biggest cost saving in the pipeline.
      2. Records URLs that fail the liveness check back to scan-history.tsv
         with status `screened-dead`, so future runs skip them too instead of
         re-fetching a guaranteed-dead URL each day.
    """
    import yaml
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    scfg = cfg.get("screen") or {}

    liveness_on = bool(scfg.get("liveness", False))
    liveness_timeout = int(scfg.get("liveness_timeout", 8))

    if not liveness_on:
        print("[screen] liveness disabled -- skipping (set screen.liveness: true to enable)")
        return 0

    if not FILTERED_PATH.exists() or FILTERED_PATH.stat().st_size == 0:
        print("[screen] no filtered_jobs.csv -- nothing to screen")
        return 0

    with open(FILTERED_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        jobs = list(reader)

    if not jobs:
        return 0

    # `description` may not be in fieldnames if the filter was run with a
    # minimal CSV. Ensure it's present so we can write back any backfills.
    if "description" not in fieldnames:
        fieldnames = list(fieldnames) + ["description"]

    # Early dedup against scan-history / pipeline / applications — these URLs
    # have been seen before, so an HTTP fetch and description extract would be
    # wasted work that bridge would discard anyway.
    skipped_seen = 0
    if career_ops_path is not None and career_ops_path.exists():
        # Local import to avoid a top-level cycle if bridge ever pulls from screen.
        from pipeline.bridge import load_seen_urls
        seen_urls = load_seen_urls(career_ops_path)
        if seen_urls:
            before = len(jobs)
            jobs = [j for j in jobs if (j.get("job_url") or "").strip() not in seen_urls]
            skipped_seen = before - len(jobs)
            if skipped_seen:
                print(f"[screen] skipping {skipped_seen} already-seen URL(s)", flush=True)

    if not jobs:
        # Everything was already seen — overwrite filtered_jobs.csv with header only.
        with open(FILTERED_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()
        print(f"[screen] all {skipped_seen} job(s) already seen — nothing new to screen")
        return 0

    def _check(job: dict) -> tuple[dict, str, str, str]:
        url = (job.get("job_url") or "").strip()
        if not url:
            return job, "uncertain", "", ""
        result, reason, body = fetch_and_classify(url, liveness_timeout)
        return job, result, reason, body

    kept: list[dict] = []
    dead_entries: list[dict] = []
    dropped = 0
    backfilled = 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_check, job) for job in jobs]
        for future in as_completed(futures):
            job, result, reason, body = future.result()
            title = (job.get("title") or "?")[:60]
            if result == "expired":
                dropped += 1
                print(f"  SKIP {title} -- liveness: {reason}", flush=True)
                url = (job.get("job_url") or "").strip()
                if url:
                    dead_entries.append({
                        "url": url,
                        "title": (job.get("title") or "").strip(),
                        "company": (job.get("company") or "").strip(),
                    })
                continue
            # Backfill missing description from the page body we already fetched.
            if not (job.get("description") or "").strip() and body:
                extracted = extract_description(body)
                if extracted:
                    job["description"] = extracted
                    backfilled += 1
            kept.append(job)

    with open(FILTERED_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    # Record dead URLs so we don't re-fetch them next run. Done after the pool
    # completes so we hit the TSV with a single appender, not 8 concurrent ones.
    if career_ops_path is not None and dead_entries:
        from pipeline.bridge import append_to_scan_history
        append_to_scan_history(
            career_ops_path, dead_entries, date.today().isoformat(), status="screened-dead",
        )

    print(
        f"[screen] kept {len(kept)}, dropped {dropped} of {len(jobs)} new "
        f"(backfilled: {backfilled}, skipped-seen: {skipped_seen})"
    )
    return dropped


if __name__ == "__main__":
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path.resolve())
