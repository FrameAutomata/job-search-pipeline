"""Filter jobs.csv to the most relevant rows.

Extracts keywords from the user's resume (YAKE — domain-agnostic statistical
extraction, works for any field) and scores each job by keyword and target-title
matches. Negative titles hard-exclude. No hardcoded vocabulary."""

import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yake
import yaml
from dotenv import load_dotenv

from pipeline import resume_text as _resume_text

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Resume files probed under resumes/ when RESUME_PATH is unset — derived from
# the import formats (IMPORT_SUFFIXES starts with .pdf, preserving the
# historical default) so adding a format can't leave this probe list behind.
_RESUME_PROBE_NAMES = tuple(f"resume{s}" for s in _resume_text.IMPORT_SUFFIXES)

JOBS_PATH = ROOT / "output" / "jobs.csv"
OUTPUT_PATH = ROOT / "output" / "filtered_jobs.csv"
KEYWORDS_CACHE_PATH = ROOT / "output" / "_keywords.json"

SCORE_BASE = 1
SCORE_SKILLS_BOOST = 2
SCORE_TITLE_MATCH = 5

SEARCH_FIELDS = ["title", "description", "skills"]

# Section headers commonly used to flag "claimed competencies". Terms inside
# this section get a 2x weight bump.
SKILLS_HEADER_RE = re.compile(
    r"^\s*(skills?|technical\s+skills|core\s+competencies|competencies|"
    r"certifications?|qualifications?|expertise)\b\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Generic resume-filler that matches almost every job description and adds no
# discriminator signal. Used two ways: (1) drop any single-token keyword that
# IS one of these, (2) drop any multi-token n-gram whose tokens are ALL in here.
# So "experience building" is dropped, but "building rest apis" is kept.
RESUME_NOISE = {
    "experience", "experienced", "experiences",
    "team", "teams", "work", "worked", "working", "works",
    "year", "years", "ability", "able", "abilities",
    "responsibilities", "responsible", "responsibility",
    "project", "projects", "summary", "education", "skills", "skill",
    "support", "supporting", "supported",
    "workflow", "workflows",
    "professional", "professionally",
    "building", "build", "builds", "built",
    "help", "helping", "helped",
    "various", "multiple", "including", "include", "includes",
    "develop", "developing", "developed", "develops",
    "use", "used", "using", "uses",
    "new", "different",
}


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_resume_text(resume_path: Path) -> str:
    """Extract resume text, dispatching on the file's format (PDF/DOCX/ODT/TXT).
    Thin delegate to pipeline.resume_text so scoring and the UI share one
    implementation."""
    return _resume_text.extract_resume_text(resume_path)


def _resolve_resume_path(resume_env: str, root: Path) -> Path:
    """Pick the resume file to score from.

    An explicit RESUME_PATH (absolute or repo-relative) is honored verbatim —
    returned even if missing, so the caller's not-found error names exactly what
    the user pointed at rather than silently falling back. Only when RESUME_PATH
    is unset/empty do we probe resumes/ for resume.pdf, then resume.docx, then
    resume.odt, so a user can drop a DOCX or ODT without editing .env. Falls back
    to resumes/resume.pdf (the historical default) when nothing is found."""
    env = (resume_env or "").strip()
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = (root / p).resolve()
        return p
    for name in _RESUME_PROBE_NAMES:
        candidate = (root / "resumes" / name).resolve()
        if candidate.exists():
            return candidate
    return (root / "resumes" / "resume.pdf").resolve()


def find_skills_section(text: str) -> str:
    """Return text from the first Skills/Certifications header to the next
    section header (or end of doc). Empty if no header found."""
    m = SKILLS_HEADER_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    next_hdr = re.search(r"\n[A-Z][A-Za-z &/]{3,40}\n", rest)
    end_offset = next_hdr.start() if next_hdr else len(rest)
    return text[m.start(): m.end() + end_offset]


def extract_skills_section_tokens(skills_chunk: str) -> set[str]:
    """Split the Skills section on common delimiters (commas, bullets, pipes,
    newlines, category labels) to get atomic skill tokens. These are explicit
    user claims and reliably catch discriminators YAKE may miss (e.g. "flutter",
    "java" — single resume mentions that don't rank in top-N statistically)."""
    if not skills_chunk:
        return set()
    body = skills_chunk.split("\n", 1)[1] if "\n" in skills_chunk else skills_chunk
    tokens: set[str] = set()
    for raw in re.split(r"[,;|•●◦\n]+", body):
        part = re.sub(r"^[A-Za-z &/]+:\s*", "", raw.strip())  # strip "Languages:" labels
        part = re.sub(r"^[*\-]\s*", "", part).strip().lower()
        if 2 <= len(part) <= 30 and not part.isdigit() and part not in RESUME_NOISE:
            tokens.add(part)
    return tokens


def _is_all_noise(kw: str) -> bool:
    """True if every whitespace-separated token in kw is in RESUME_NOISE."""
    parts = kw.split()
    return bool(parts) and all(p in RESUME_NOISE for p in parts)


def extract_keywords(resume_text: str) -> dict[str, int]:
    """YAKE 1–3 gram extraction + direct Skills-section token extraction."""
    extractor = yake.KeywordExtractor(lan="en", n=3, top=75, dedupLim=0.85)
    pairs = extractor.extract_keywords(resume_text)
    skills_chunk = find_skills_section(resume_text)
    skills_chunk_lower = skills_chunk.lower()
    skills_tokens = extract_skills_section_tokens(skills_chunk)

    keywords: dict[str, int] = {}

    for kw, _score in pairs:
        kw_norm = kw.strip().lower()
        if len(kw_norm) < 2 or _is_all_noise(kw_norm):
            continue
        weight = SCORE_SKILLS_BOOST if kw_norm in skills_chunk_lower else SCORE_BASE
        keywords[kw_norm] = max(keywords.get(kw_norm, 0), weight)

    # Skills-section atomic tokens — explicit user claims, always skills-boost.
    for tok in skills_tokens:
        keywords[tok] = max(keywords.get(tok, 0), SCORE_SKILLS_BOOST)

    return keywords


def _load_or_extract_keywords(resume_text: str, source_path: Path) -> dict[str, int]:
    """Extract keywords, caching to output/_keywords.json keyed by resume sha.
    Subsequent runs against the same resume skip the YAKE step entirely."""
    digest = hashlib.sha1(resume_text.encode("utf-8")).hexdigest()
    if KEYWORDS_CACHE_PATH.exists():
        try:
            cached = json.loads(KEYWORDS_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("sha") == digest:
                return {k: int(v) for k, v in cached.get("keywords", {}).items()}
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    keywords = extract_keywords(resume_text)
    KEYWORDS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        KEYWORDS_CACHE_PATH.write_text(
            json.dumps({"sha": digest, "source": source_path.name, "keywords": keywords}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return keywords


def parse_date_posted(val: str) -> datetime | None:
    if not val or val.strip().lower() in ("", "none", "nan", "nat"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            continue
    return None


def _compile_alternation(terms: list[str]) -> re.Pattern | None:
    """Compile a single \\b(?:t1|t2|...)\\b pattern, case-insensitive.
    Returns None for an empty list so callers can short-circuit cheaply."""
    pieces = sorted({t.lower() for t in terms if t}, key=len, reverse=True)
    if not pieces:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in pieces) + r")\b", re.IGNORECASE)


def _target_lookup(target_titles: list[str | None]) -> dict[str, str]:
    """Map a matched (lowercased) title term back to its configured casing.
    Drops falsy entries — a bare `-` in the YAML list parses to None — mirroring
    _compile_alternation's `if t` guard so both stay null-tolerant."""
    return {t.lower(): t for t in target_titles if t}


def _is_remote(row: dict) -> bool:
    """JobSpy writes is_remote as a stringified bool ("True"/"False"/"") or empty."""
    return str(row.get("is_remote") or "").strip().lower() in ("true", "1", "yes", "t")


def is_eligible(
    row: dict,
    negative_loc_pattern: re.Pattern | None,
    eligible_loc_pattern: re.Pattern | None,
    negative_desc_pattern: re.Pattern | None,
) -> bool:
    """Cheap eligibility gate, applied before scoring.

    Excludes a job when its description matches a negative-description term — a
    configured whole-word/phrase list, so it works for any country's vocabulary
    (e.g. a security clearance the candidate can't hold: "security clearance",
    "TS/SCI", "vetting", "polygraph"). Also excludes a non-remote job in a negative
    location or one that fails an eligible-locations allowlist. All matching is on
    word boundaries, so a short token like "US" matches the "US" in "Dallas, US"
    but not the "us" inside "Russia". Remote roles are location-independent and
    bypass the location checks (description terms still apply). Per-question
    work-auth/sponsorship matching is deferred to apply time."""
    if negative_desc_pattern is not None and negative_desc_pattern.search(row.get("description") or ""):
        return False

    if _is_remote(row):
        return True

    if negative_loc_pattern is None and eligible_loc_pattern is None:
        return True
    location = (row.get("location") or "").strip()
    if negative_loc_pattern is not None and location and negative_loc_pattern.search(location):
        return False
    if eligible_loc_pattern is not None and location and not eligible_loc_pattern.search(location):
        return False
    return True


def score_job(
    row: dict,
    keywords: dict[str, int],
    target_titles: list[str],
    negative_titles: list[str],
) -> tuple[int, list[str]] | None:
    """Convenience wrapper that compiles patterns each call.

    The pipeline runs the hot loop in `run()` with patterns compiled once.
    Tests and ad-hoc callers should use this entry point."""
    return _score_job(
        row,
        keywords,
        _compile_alternation(list(keywords.keys())),
        _compile_alternation(target_titles),
        _compile_alternation(negative_titles),
        _target_lookup(target_titles),
    )


def _score_job(
    row: dict,
    keyword_weights: dict[str, int],
    keyword_pattern: re.Pattern | None,
    target_pattern: re.Pattern | None,
    negative_pattern: re.Pattern | None,
    target_lookup: dict[str, str],
) -> tuple[int, list[str]] | None:
    """Score one job row using precompiled patterns. Each keyword contributes
    its weight at most once even if it appears multiple times in the text."""
    title = (row.get("title") or "").lower()

    if negative_pattern is not None and negative_pattern.search(title):
        return None

    matched: list[str] = []
    score = 0

    if keyword_pattern is not None:
        text = " ".join((row.get(f) or "") for f in SEARCH_FIELDS).lower()
        seen: set[str] = set()
        for hit in keyword_pattern.findall(text):
            if hit in seen:
                continue
            seen.add(hit)
            score += keyword_weights.get(hit, SCORE_BASE)
            matched.append(hit)

    if target_pattern is not None:
        for hit in set(target_pattern.findall(title)):
            score += SCORE_TITLE_MATCH
            matched.append(f"title:{target_lookup.get(hit, hit)}")

    return score, matched


def run(config_path: Path) -> Path:
    cfg = load_config(config_path)
    fcfg = cfg["filter"]
    min_score = fcfg.get("min_score", 5)
    target_titles = fcfg.get("target_titles") or []
    negative_titles = fcfg.get("negative_titles") or []
    overrides = fcfg.get("keyword_overrides") or {}
    max_age_hours = fcfg.get("max_age_hours")
    cutoff = datetime.now() - timedelta(hours=max_age_hours) if max_age_hours else None

    negative_locations = fcfg.get("negative_locations") or []
    eligible_locations = fcfg.get("eligible_locations") or []
    negative_description_terms = fcfg.get("negative_description_terms") or []
    negative_loc_pattern = _compile_alternation(negative_locations)
    eligible_loc_pattern = _compile_alternation(eligible_locations)
    negative_desc_pattern = _compile_alternation(negative_description_terms)

    if not JOBS_PATH.exists():
        raise FileNotFoundError(f"{JOBS_PATH} not found — run scrape first.")

    # Read the scrape's output BEFORE resolving the resume. Everything between
    # here and the scoring loop is expensive and none of it depends on the rows:
    # PDF/DOCX text extraction, a full YAKE pass (n=3, top=75), a _keywords.json
    # write the cloud never gets to reuse (the daily caches career-ops/** only).
    # Since #106 a scrape that returns zero rows truncates jobs.csv instead of
    # leaving the previous run's file behind, so "nothing to score" is now the
    # ordinary shape of a throttled morning rather than a rare one.
    with open(JOBS_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        # Truncate, don't just return: leaving yesterday's filtered_jobs.csv in
        # place is #106 one stage down — screen and bridge would re-process
        # those rows as today's results. Named as an empty scrape rather than
        # left to fall through to the min_score message below, which reads as a
        # threshold the user should lower.
        print(f"[filter] {JOBS_PATH.name} has no rows — nothing to filter")
        OUTPUT_PATH.write_text("", encoding="utf-8")
        return OUTPUT_PATH

    # The date and eligibility cuts need nothing from the resume, so they run
    # first and can rule out the resume work entirely.
    candidates = []
    too_old = 0
    ineligible = 0
    for row in rows:
        if cutoff:
            posted = parse_date_posted(row.get("date_posted") or "")
            if posted is not None and posted < cutoff:
                too_old += 1
                continue
        if not is_eligible(row, negative_loc_pattern, eligible_loc_pattern, negative_desc_pattern):
            ineligible += 1
            continue
        candidates.append(row)

    if not candidates:
        print(
            f"[filter] nothing left to score of {len(rows)} scraped "
            f"({ineligible} ineligible, {too_old} too old)"
        )
        OUTPUT_PATH.write_text("", encoding="utf-8")
        return OUTPUT_PATH

    # Resolve via _resolve_resume_path so an unset RESUME_PATH still discovers a
    # dropped resume.docx/resume.odt, not just resume.pdf. An explicitly empty
    # RESUME_PATH (e.g. a workflow `${{ vars.X || '' }}` pattern) is treated the
    # same as unset.
    resume_path = _resolve_resume_path(os.environ.get("RESUME_PATH", ""), ROOT)

    resume_txt = resume_path.with_suffix(".txt")
    if resume_txt.exists():
        print(f"[filter] loading resume text from {resume_txt.name}")
        resume_text = resume_txt.read_text(encoding="utf-8")
        source_path = resume_txt
    elif resume_path.exists():
        print(f"[filter] extracting keywords from {resume_path.name}")
        resume_text = extract_resume_text(resume_path)
        source_path = resume_path
    else:
        raise FileNotFoundError(
            f"Resume not found at {resume_path} (or {resume_txt.name}). "
            "Drop your resume (PDF, DOCX, or ODT) in resumes/ or set RESUME_PATH "
            "in .env."
        )

    if not resume_text.strip():
        print("[filter] WARNING: no text extracted from resume — is it a scanned "
              "PDF or an empty document?")

    keywords = _load_or_extract_keywords(resume_text, source_path)
    for kw, w in overrides.items():
        keywords[kw.lower()] = int(w)
    print(
        f"[filter] {len(keywords)} keywords extracted "
        f"(target_titles: {len(target_titles)}, negative_titles: {len(negative_titles)})"
    )

    keyword_pattern = _compile_alternation(list(keywords.keys()))
    target_pattern = _compile_alternation(target_titles)
    negative_pattern = _compile_alternation(negative_titles)
    target_lookup = _target_lookup(target_titles)

    jobs = []
    excluded = 0
    for row in candidates:
        result = _score_job(
            row, keywords, keyword_pattern, target_pattern, negative_pattern, target_lookup,
        )
        if result is None:
            excluded += 1
            continue
        score, matches = result
        row["relevance_score"] = score
        row["matched_keywords"] = ", ".join(matches)
        jobs.append(row)

    relevant = [j for j in jobs if j["relevance_score"] >= min_score]
    relevant.sort(key=lambda r: r["relevance_score"], reverse=True)

    if not relevant:
        print(
            f"[filter] no jobs scored >= {min_score} "
            f"(of {len(jobs)} scored, {excluded} negative-excluded, "
            f"{ineligible} ineligible, {too_old} too old)"
        )
        OUTPUT_PATH.write_text("", encoding="utf-8")
        return OUTPUT_PATH

    summary_cols = [
        "title", "company", "location", "is_remote",
        "min_amount", "max_amount", "interval", "job_url", "date_posted",
        "relevance_score", "matched_keywords",
    ]
    all_cols = summary_cols + [c for c in relevant[0].keys() if c not in summary_cols]

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols)
        writer.writeheader()
        writer.writerows(relevant)

    print(
        f"[filter] kept {len(relevant)} of {len(jobs)} "
        f"({excluded} negative-excluded, {ineligible} ineligible, {too_old} too old) "
        f"-> {OUTPUT_PATH}"
    )
    return OUTPUT_PATH


if __name__ == "__main__":
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path)
