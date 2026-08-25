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

import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pipeline.stdio import line_buffer_stdout

from pipeline.rowio import read_rows, write_rows

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

# A closed posting's URL signal. `error=true` is the generic redirect; LinkedIn
# 302s a closed /jobs/view/ human page to a search page tagged
# `expired_jd_redirect` (the non-guest fallback path still sees this).
_EXPIRED_URL = re.compile(r"[?&]error=true|expired_jd_redirect", re.I)

# HTTP statuses that mean "LinkedIn rate-limited / sign-in-walled this fetch",
# not "the posting is gone". A burst of guest-endpoint fetches trips the per-IP
# limiter, which answers 403/429/999 with the same anti-bot sign-in chrome a
# real live JD carries — so this is decided on STATUS, never body, and must
# resolve to `throttled` (couldn't read it), never active/expired.
_THROTTLE_STATUSES = frozenset({403, 429, 999})

# Anti-bot interstitials (Cloudflare "Just a moment...", hCaptcha walls) render
# a short challenge page INSTEAD of the posting — and serve it with HTTP 200,
# so the status check above never sees them. Without this the body is short and
# carries no apply control, so it falls through to `insufficient content` and is
# recorded `expired` — which screen writes to scan-history.tsv as
# `screened-dead`, permanently filtering out a live job. Upstream's
# liveness-core.mjs added the same guard for the same reason.
#
# These are matched against the WHOLE page, including a real JD's prose, so they
# are spelled to be unambiguous: "Ray ID" needs Cloudflare's hex id after it (an
# ML-infra JD can say "Ray" on its own), and the check runs only after the apply
# control has had its say — a posting that says "it takes just a moment to
# apply" is a live posting, and a challenge page never carries an apply control.
# A false positive here is not free: `throttled` holds the row every run, so the
# job is never evaluated at all.
_BOT_CHALLENGE = [
    re.compile(r"just a moment\s*(\.{3}|…|</title>)", re.I),
    re.compile(r"performing security verification", re.I),
    re.compile(r"checking your browser before", re.I),
    re.compile(r"verify you are (a |not a )?human", re.I),
    re.compile(r"enable javascript and cookies to continue", re.I),
    re.compile(r"attention required.*cloudflare", re.I),
    re.compile(r"\bray id:?\s*[0-9a-f]{8,}", re.I),
    re.compile(r"\bcf-ray\b", re.I),
    re.compile(r"please complete the security check", re.I),
]

# A server-side failure is the site being broken, not the posting being gone.
# 5xx bodies are short error pages with no apply control, so they used to reach
# the `insufficient content` branch and be recorded `expired` — the same
# permanent, irreversible outcome as a real 404, for what is usually a blip.
_SERVER_ERROR_MIN = 500

# classify_each retries a throttled fetch this many times (a transient limit may
# clear on a re-fetch) with linear backoff between attempts.
_THROTTLE_RETRIES = 2
_THROTTLE_BACKOFF = 1.5

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

# Matches the numeric job ID in a LinkedIn /jobs/view/ URL. Handles both the
# bare-ID form (/jobs/view/4419521927) and the slug form
# (/jobs/view/software-engineer-at-acme-4419521927). The non-greedy prefix
# lets any slug be consumed before the trailing ID is captured.
_LINKEDIN_VIEW_RE = re.compile(
    r"linkedin\.com/jobs/view/(?:[^?#/]*?)(\d+)(?:[/?#]|$)", re.IGNORECASE
)


def linkedin_guest_jd_url(url: str) -> str | None:
    """Map a LinkedIn /jobs/view/{id} URL to the public guest job-posting API
    endpoint, which returns the full JD HTML without authentication.

    The regular /jobs/view/ page is login-walled when fetched from a
    datacenter IP — it inconsistently returns a sign-in preview that passes
    the liveness check but carries no extractable JD. The guest endpoint
    (jobs-guest/jobs/api/jobPosting/{id}) returns the complete JD reliably and
    is the same mechanism JobSpy uses for linkedin_fetch_description. Returns
    None for non-LinkedIn or unparseable URLs so callers fall back to the
    original URL."""
    m = _LINKEDIN_VIEW_RE.search(url or "")
    if not m:
        return None
    return f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}"


# Every job key is interpolated into the jobData GraphQL query string, so this
# charset gate doubles as the injection guard.
_INDEED_JK_CHARSET = re.compile(r"[0-9A-Za-z]+")


def indeed_job_key(url: str | None) -> str | None:
    """The Indeed job key — the first top-level `jk` query param — from a URL
    on any *.indeed.com host (viewjob, m./country subdomains, rc/clk tracking
    redirects), or None if this isn't an Indeed posting URL.

    Parsed with urllib rather than a regex scan: a scan can be hijacked by a
    jk= embedded in another param's VALUE (e.g. a redirect target), and a wrong
    key comes back absent from jobData — which reads as "removed" and would
    discard a live role. Host-checked so a jk-shaped param on another site (or
    a lookalike domain) never counts.

    The key — not the page — is Indeed's liveness handle: the posting page is
    Cloudflare-walled to a plain fetch, but the jobData GraphQL API answers by
    job key with a definitive `expired` flag. See fetch_indeed_expiry."""
    try:
        parts = urlparse(url or "")
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if host != "indeed.com" and not host.endswith(".indeed.com"):
        return None
    vals = parse_qs(parts.query).get("jk")
    key = vals[0] if vals else None
    return key if key and _INDEED_JK_CHARSET.fullmatch(key) else None


def is_liveness_verifiable(url: str | None) -> bool:
    """Whether a posting URL has a working unauthenticated liveness path.

    That's LinkedIn /jobs/view/ URLs (mappable to the guest JD endpoint) and
    Indeed URLs carrying a `jk` job key (checkable via the jobData GraphQL API —
    the Cloudflare wall only guards the website, not the API the scraper already
    uses). Glassdoor still serves a JS / anti-bot interstitial to a plain HTTP
    fetch — no JD, no apply control, no closed-marker — so liveness can't be
    determined and the re-check skips it rather than burning a fetch on a page
    it can't classify."""
    return linkedin_guest_jd_url(url) is not None or indeed_job_key(url) is not None


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
    """Return (result, reason): result is 'active', 'expired', 'throttled', or
    'uncertain'. `throttled` is a rate-limit/sign-in wall we couldn't read —
    distinct from a confirmed-gone `expired` and from a transient `uncertain`."""
    if status in (404, 410):
        return "expired", f"HTTP {status}"
    # Status-based throttle decision comes BEFORE any body/URL inspection: a
    # 403/429/999 wall carries the same apply chrome a live JD does, so trusting
    # the body here would misread a wall as active (or its emptiness as expired).
    if status in _THROTTLE_STATUSES:
        return "throttled", f"HTTP {status} (rate-limited / sign-in wall)"
    if status >= _SERVER_ERROR_MIN:
        return "uncertain", f"HTTP {status} (server error — not a removed posting)"
    if _EXPIRED_URL.search(final_url):
        return "expired", f"error redirect: {final_url}"
    for pat in _HARD_EXPIRED:
        if pat.search(body):
            return "expired", f"body: {pat.pattern}"
    if any(p.search(body) for p in _APPLY):
        return "active", "apply control visible"
    # No apply control. Before reading that absence as "gone", rule out a
    # 200-served anti-bot wall — its body is the challenge, not the posting.
    for pat in _BOT_CHALLENGE:
        if pat.search(body):
            return "throttled", f"anti-bot challenge: {pat.pattern}"
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


def classify_each(items, url_of, *, timeout: int = 8, max_workers: int = 8):
    """Fetch + classify each item's URL in parallel, yielding (item, result,
    reason, body) as each completes.

    `url_of(item)` returns the item's posting URL. LinkedIn /jobs/view/ URLs are
    fetched through the guest endpoint (the regular page is login-walled from
    datacenter IPs); the CSV/tracker URL is never mutated, only the fetch target.
    An item with no URL yields ('uncertain', '', ''); a fetch that raises is
    caught and yields ('uncertain', 'fetch error: ...', '') so one bad URL can't
    abort the whole sweep. Shared by the scrape-time screen and the tracker
    liveness re-check (pipeline/recheck.py) so the parallel-fetch + guest-mapping
    mechanics live in exactly one place."""
    def _one(item):
        url = (url_of(item) or "").strip()
        if not url:
            return item, "uncertain", "", ""
        fetch_url = linkedin_guest_jd_url(url) or url
        try:
            # Retry a `throttled` result (transient rate-limit) with linear
            # backoff; a raised error is NOT retried (it's an immediate
            # uncertain) and a conclusive result returns at once.
            for attempt in range(_THROTTLE_RETRIES + 1):
                result, reason, body = fetch_and_classify(fetch_url, timeout)
                if result != "throttled" or attempt == _THROTTLE_RETRIES:
                    return item, result, reason, body
                time.sleep(_THROTTLE_BACKOFF * (attempt + 1))
        except Exception as exc:
            return item, "uncertain", f"fetch error: {exc}", ""

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_one, it) for it in items]
        for fut in as_completed(futures):
            yield fut.result()


# ── Indeed liveness via the jobData GraphQL API ──────────────────────────────

_INDEED_GRAPHQL_URL = "https://apis.indeed.com/graphql"

# Keys per jobData request. 25 keeps each query small; a full multi-hundred-role
# backlog is still only a handful of requests (less traffic than one scrape pass).
_INDEED_BATCH_SIZE = 25


def _jobdata_query(keys) -> str:
    """The jobData GraphQL query for a batch of job keys. Only `key` + `expired`
    are requested — liveness needs none of the JD payload."""
    joined = ", ".join(f'"{k}"' for k in keys)
    return ("query GetJobData { jobData(input: {jobKeys: [" + joined + "]}) "
            "{ results { job { key expired } } } }")


def fetch_indeed_expiry(keys, *, timeout: int = 8) -> dict[str, bool]:
    """One batched jobData lookup on Indeed's GraphQL API → {jk: expired}.

    Same endpoint + embedded mobile-app credentials the JobSpy scraper uses.
    The headers come from jobspy so the credentials can't drift; the endpoint
    URL and the jobData query are OURS (jobspy only ships a jobSearch query),
    so a jobspy upgrade won't fix them if Indeed ever moves the API. Indeed's
    Cloudflare wall guards the website, not this API, so it works where a page
    fetch can't — including datacenter IPs. A job key the API no longer knows
    is silently OMITTED from the results; absence semantics belong to the
    caller (classify_indeed_each treats absence-from-a-non-empty-batch as
    removed).

    Raises RuntimeError on ANY transport- or shape-level failure (HTTP error,
    GraphQL errors, malformed/non-JSON payload) so the classifier has exactly
    one failure signal to map to `throttled` — a broken read must never look
    like a verdict."""
    import requests  # transitive dep via python-jobspy
    from jobspy.indeed.constant import api_headers

    headers = api_headers.copy()
    # jobspy threads this header per search country, but jobData KEY lookups are
    # country-agnostic — probed identical results (same keys, same expired
    # flags) under US/DE/GB — so a constant is safe even for country-subdomain
    # keys (de.indeed.com, …) the verifiability gate admits.
    headers["indeed-co"] = "US"
    resp = requests.post(
        _INDEED_GRAPHQL_URL, headers=headers, json={"query": _jobdata_query(keys)},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"jobData HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"jobData non-JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("jobData response is not a JSON object")
    if payload.get("errors"):
        first = (payload["errors"][0] or {}).get("message", "unknown")
        raise RuntimeError(f"jobData GraphQL error: {first}")
    results = ((payload.get("data") or {}).get("jobData") or {}).get("results")
    if results is None:
        raise RuntimeError("jobData response missing data.jobData.results")
    expiry: dict[str, bool] = {}
    for res in results:
        job = (res or {}).get("job") or {}
        key = job.get("key")
        if key is not None:
            expiry[key] = bool(job.get("expired"))
    return expiry


def classify_indeed_each(items, key_of, *, timeout: int = 8,
                         chunk_size: int = _INDEED_BATCH_SIZE):
    """Classify each item's Indeed posting via batched jobData lookups, yielding
    (item, result, reason, body) — the same shape classify_each yields, so the
    tracker re-check consumes both transports with one accounting loop. `body`
    is always '' (there is no page to mine on this path).

    Verdicts: a returned expired=False is `active`; expired=True is `expired`;
    a key ABSENT from a batch that returned others is `expired` too (the API
    silently omits keys it no longer knows — verified with a bogus-key probe);
    absent from an EMPTY batch is `uncertain` (indistinguishable from a silently
    rejected query, and uncertain never discards). A failed batch yields
    `throttled` for that chunk only — no read happened, so the re-check leaves
    those roles unstamped and retries them next run; later chunks still run.
    ImportError is the exception: a missing dependency is permanent for this
    process, so it propagates instead of posing as a retryable throttle.
    There's no in-call retry (unlike the LinkedIn throttle backoff): a batch
    failure is batch-wide, and the re-check's staleness state IS the retry
    mechanism. Items whose key_of() is falsy yield `uncertain` unqueried."""
    keyed = []   # (item, key) pairs — the key is computed once, not per phase
    for item in items:
        key = key_of(item)
        if key:
            keyed.append((item, key))
        else:
            yield item, "uncertain", "no Indeed job key", ""
    step = max(1, chunk_size)
    for i in range(0, len(keyed), step):
        chunk = keyed[i:i + step]
        try:
            expiry = fetch_indeed_expiry([key for _, key in chunk], timeout=timeout)
        except ImportError:
            # jobspy/requests absent (e.g. a UI-only venv): permanent, so a
            # `throttled` verdict would re-queue the backlog forever behind a
            # "will retry" that never can. Surface the real failure.
            raise
        except Exception as exc:
            for item, _ in chunk:
                yield item, "throttled", f"jobData request failed: {exc}", ""
            continue
        for item, key in chunk:
            if key in expiry:
                if expiry[key]:
                    yield item, "expired", "marked expired on Indeed", ""
                else:
                    yield item, "active", "listed live on Indeed", ""
            elif expiry:
                yield item, "expired", "removed from Indeed (absent from jobData)", ""
            else:
                yield item, "uncertain", "empty jobData response", ""


def classify_liveness_each(items, url_of, *, timeout: int = 8, max_workers: int = 8):
    """Site-routed liveness classification, yielding (item, result, reason,
    body) for every item: Indeed postings (a jk-bearing URL) go through the
    batched jobData API (their pages are Cloudflare-walled to a plain fetch),
    everything else through the per-URL page fetch. Callers never partition by
    site themselves; the routing lives here, beside is_liveness_verifiable, and
    the two must agree — a new verifiable site means extending BOTH the gate
    and this dispatch. `max_workers` paces only the page-fetch pool; the Indeed
    batches are already few (~items/25 requests)."""
    # One materializing pass: url_of/indeed_job_key run once per item, and a
    # one-shot iterable (generator) is safe — a second scan of `items` would
    # find it exhausted and silently drop whichever partition is built second.
    tagged = [(it, indeed_job_key(url_of(it))) for it in items]
    fetched = [it for it, key in tagged if not key]
    indeed = [it for it, key in tagged if key]
    yield from classify_each(fetched, url_of, timeout=timeout, max_workers=max_workers)
    yield from classify_indeed_each(
        indeed, lambda it: indeed_job_key(url_of(it)), timeout=timeout)


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

    # One guard where there were two: read_rows reads missing, zero-byte and
    # header-only alike as "no rows", which is the only distinction this stage
    # ever drew between them.
    jobs = read_rows(FILTERED_PATH)
    if not jobs:
        # Converge the file on the one shape a producer writes. A header-only
        # filtered_jobs.csv — left by a pre-rowio run, or by a tool that wrote a
        # header — reads as no rows here but is not zero bytes on disk, so
        # without this it stays that way through every later --skip-filter run
        # and the invariant is permanently violated in the one place it matters.
        # Nothing is created if the file is simply absent.
        if FILTERED_PATH.exists() and FILTERED_PATH.stat().st_size:
            write_rows(FILTERED_PATH, [])
        print("[screen] no rows in filtered_jobs.csv -- nothing to screen")
        return 0

    # A filter run on a minimal CSV may have no `description` column at all,
    # and the backfill below sets the key on some rows only. Normalizing it onto
    # every row here means write_rows' own default — the first row's keys — is
    # already the right column set, so there is no fieldnames local to thread
    # through the eighty lines between the read and the write.
    for job in jobs:
        job.setdefault("description", "")

    # Early dedup against scan-history / pipeline / applications — these URLs
    # have been seen before, so an HTTP fetch and description extract would be
    # wasted work that bridge would discard anyway.
    skipped_seen = 0
    if career_ops_path is not None and career_ops_path.exists():
        # Local import to avoid a top-level cycle if bridge ever pulls from screen.
        from pipeline.bridge import append_easy_apply_urls, is_easy_apply_row, load_seen_urls
        # Record easy-apply URLs from the FULL set, before the dedup drop below.
        # An already-tracked SmartApply role is removed here as a repeat and never
        # reaches bridge, so this is the only stage that can persist its flag for
        # the UI's apply-button gating.
        # append_easy_apply_urls strips/dedups/skips blanks itself.
        append_easy_apply_urls(
            career_ops_path,
            [j.get("job_url") for j in jobs if is_easy_apply_row(j)],
        )
        seen_urls = load_seen_urls(career_ops_path)
        if seen_urls:
            before = len(jobs)
            jobs = [j for j in jobs if (j.get("job_url") or "").strip() not in seen_urls]
            skipped_seen = before - len(jobs)
            if skipped_seen:
                print(f"[screen] skipping {skipped_seen} already-seen URL(s)")

    if not jobs:
        # Everything was already seen. Truncated, not header-only: a header is
        # not zero bytes, so bridge's "did upstream produce anything" test saw a
        # file with content and went looking for rows that were never there.
        #
        # Kept as its own exit rather than falling through to the identical
        # write at the end (`kept` would also be empty): the count in this
        # message is the one a reader wants when a whole run is a repeat, and
        # the tail's "kept 0, dropped 0 of 0 new" does not carry it.
        write_rows(FILTERED_PATH, [])
        print(f"[screen] all {skipped_seen} job(s) already seen — nothing new to screen")
        return 0

    kept: list[dict] = []
    dead_entries: list[dict] = []
    dropped = 0
    held = 0
    backfilled = 0

    # classify_each fetches each job_url in parallel (LinkedIn -> guest endpoint)
    # and yields as results land; the same helper drives the tracker liveness
    # re-check (pipeline/recheck.py).
    for job, result, reason, body in classify_each(
        jobs, lambda j: j.get("job_url") or "", timeout=liveness_timeout
    ):
        title = (job.get("title") or "?")[:60]
        if result == "expired":
            dropped += 1
            print(f"  SKIP {title} -- liveness: {reason}")
            url = (job.get("job_url") or "").strip()
            if url:
                dead_entries.append({
                    "url": url,
                    "title": (job.get("title") or "").strip(),
                    "company": (job.get("company") or "").strip(),
                })
            continue
        if result == "throttled":
            # Rate-limited / sign-in wall — we couldn't actually read it. HOLD it
            # for the next run rather than finalizing on no signal: don't keep it
            # (its body is the wall, not a JD, so it'd reach evaluation with an
            # empty description) and don't record it screened-dead (it's not
            # gone). Recording neither means its URL never enters scan-history,
            # so the next scrape re-finds and re-checks it — same "retry next
            # run" stance the tracker re-check (pipeline/recheck.py) takes.
            held += 1
            print(f"  HOLD {title} -- {reason} (retry next run)")
            continue
        # Backfill missing description from the page body we already fetched
        # (throttled bodies are held above, so anything here is a real page).
        if not (job.get("description") or "").strip() and body:
            extracted = extract_description(body)
            if extracted:
                job["description"] = extracted
                backfilled += 1
        kept.append(job)

    # `kept` is empty when every job was dropped or held, which wrote the same
    # header-only file as the all-seen exit above until write_rows made every
    # "produced nothing" exit in the chain agree on one shape.
    write_rows(FILTERED_PATH, kept)

    # Record dead URLs so we don't re-fetch them next run. Done after the pool
    # completes so we hit the TSV with a single appender, not 8 concurrent ones.
    if career_ops_path is not None and dead_entries:
        from pipeline.bridge import append_to_scan_history
        append_to_scan_history(
            career_ops_path, dead_entries, date.today().isoformat(), status="screened-dead",
        )

    print(
        f"[screen] kept {len(kept)}, dropped {dropped} of {len(jobs)} new "
        f"(held: {held}, backfilled: {backfilled}, skipped-seen: {skipped_seen})"
    )
    return dropped


if __name__ == "__main__":
    line_buffer_stdout()

    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path.resolve())
