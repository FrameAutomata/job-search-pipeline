"""Screen filtered_jobs.csv for liveness before bridge.

Runs between filter and bridge. Controlled by the `screen:` section in
config/search.yml.

  screen:
    liveness: false          # HTTP liveness check (default: false)
    liveness_timeout: 8      # per-request timeout in seconds (default: 8)

Liveness logic is a Python port of career-ops/liveness-core.mjs.
When disabled (or `screen:` is absent) the stage is a no-op.
"""

import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def check_liveness(url: str, timeout: int = 8) -> tuple[str, str]:
    import requests  # transitive dep via python-jobspy

    try:
        resp = requests.get(
            url, headers={"User-Agent": _UA}, timeout=timeout, allow_redirects=True
        )
        return classify_liveness(resp.status_code, str(resp.url), resp.text)
    except Exception as exc:
        return "uncertain", f"request error: {exc}"


def run(config_path: Path) -> int:
    """Screen filtered_jobs.csv in-place. Returns number of jobs dropped."""
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

    def _check(job: dict) -> tuple[str, str]:
        url = (job.get("job_url") or "").strip()
        if not url:
            return "uncertain", ""
        return check_liveness(url, liveness_timeout)

    kept: list[dict] = []
    dropped = 0

    max_workers = min(8, len(jobs))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        checks = list(pool.map(_check, jobs))

    for job, (result, reason) in zip(jobs, checks):
        title = (job.get("title") or "?")[:60]
        if result == "expired":
            dropped += 1
            print(f"  SKIP {title} -- liveness: {reason}")
        else:
            kept.append(job)

    with open(FILTERED_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(f"[screen] kept {len(kept)}, dropped {dropped} of {len(jobs)}")
    return dropped


if __name__ == "__main__":
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path.resolve())
