"""Filter jobs.csv to the most relevant rows.

Extracts keywords from the user's resume (YAKE — domain-agnostic statistical
extraction, works for any field) and scores each job by keyword and target-title
matches. Negative titles hard-exclude. No hardcoded vocabulary."""

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

from pipeline import remote_signal
from pipeline import resume_text as _resume_text
from pipeline.rowio import read_rows, write_rows
from pipeline.stdio import line_buffer_stdout

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

JOBS_PATH = ROOT / "output" / "jobs.csv"
OUTPUT_PATH = ROOT / "output" / "filtered_jobs.csv"
KEYWORDS_CACHE_PATH = ROOT / "output" / "_keywords.json"

SCORE_BASE = 1
SCORE_SKILLS_BOOST = 2
SCORE_TITLE_MATCH = 5

SEARCH_FIELDS = ["title", "description", "skills"]

# Section headers commonly used to flag "claimed competencies". Terms inside
# this section get a 2x weight bump.
# Matched against a whole line, so this only ever fires on a standalone header.
# Two shapes the flat alternation used to miss, both common enough to have cost
# real resumes their skills boost:
#   - a qualifier in front ("CORE SKILLS" — the old list had core competencies
#     but not core skills, and `^\s*(skills?...)` required the line to START
#     with the noun);
#   - a conjoined tail ("CERTIFICATIONS & TRAINING" — `&` appears only in the
#     next-header lookahead below, never in the header itself).
# A miss is silent: find_skills_section returns "", the weight-2 boost quietly
# never applies, and the resume's most discriminating terms (EMR names, tools,
# certifications) score the same as prose.
SKILLS_HEADER_RE = re.compile(
    r"^\s*"
    r"(?:(?:core|key|technical|professional|relevant|additional|other)\s+"
    r"|areas\s+of\s+)?"
    r"(?:skills?|competencies|certifications?|qualifications?|expertise"
    r"|proficiencies)"
    r"(?:\s*(?:&|/|and)\s*[A-Za-z]+)*"
    r"\s*:?\s*$",
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


# Lives in pipeline.remote_signal now, so the screen stage can compile the two
# location lists without importing this module (yake, pandas); the old name
# stays for the callers and tests that reach it here.
_compile_alternation = remote_signal.compile_alternation


# A `target_titles` entry is matched LITERALLY against a posting's title, so one
# that groups alternatives — " / " with spaces, or a word-bounded "or" — can never
# match anything, and the stage log's `target_titles: N` still counts it (#160).
# Both real wizard-configured copies had every entry in that shape. The wizard
# now splits them; this is the warning for a config written any other way.
# "Coordinator" contains "or" and is not an alternative, hence the boundaries.
_GROUPED_TITLE_RE = re.compile(r"\s/\s|\bor\b", re.IGNORECASE)


def _warn_unmatchable_titles(target_titles: list[str | None]) -> list[str]:
    """Print one warning per grouped `target_titles` entry; return them."""
    grouped = [t for t in target_titles if t and _GROUPED_TITLE_RE.search(t)]
    for t in grouped:
        print(f'[filter] WARNING: target_titles entry "{t}" groups alternatives with '
              '" / " or " or " — no posting title contains that literally, so it can '
              'never earn the title bonus. List each alternative as its own entry.')
    return grouped


def _target_lookup(target_titles: list[str | None]) -> dict[str, str]:
    """Map a matched (lowercased) title term back to its configured casing.
    Drops falsy entries — a bare `-` in the YAML list parses to None — mirroring
    _compile_alternation's `if t` guard so both stay null-tolerant."""
    return {t.lower(): t for t in target_titles if t}


def _is_remote(row: dict) -> bool:
    """JobSpy writes is_remote as a stringified bool ("True"/"False"/"") or empty.
    One reader, shared with screen and bridge through pipeline.remote_signal, so
    the remote-consistency guard's rewrite of the cell is read back here the
    same way it was written."""
    return remote_signal.is_remote_str(row)


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

    # The location half is shared with screen (pipeline.remote_signal), which
    # re-asks it of a row the remote-consistency guard turns on-site after the
    # JD backfill — that row passed through the bypass above with no JD to
    # judge, so this is the check it never met.
    return remote_signal.location_eligible(row, negative_loc_pattern, eligible_loc_pattern)


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


def _no_results(message: str) -> Path:
    """Report `message`, truncate the output, and hand back its path.

    Every "nothing survived" exit in run() goes through here, so a fourth one
    can't be added without the truncation. Why it has to truncate — and why zero
    bytes rather than a header — is pipeline.rowio's contract.

    OUTPUT_PATH is read at call time, which is what the tests rebind.
    """
    print(message)
    write_rows(OUTPUT_PATH, [])
    return OUTPUT_PATH


def run(config_path: Path) -> Path:
    cfg = load_config(config_path)
    fcfg = cfg["filter"]
    min_score = fcfg.get("min_score", 5)
    target_titles = fcfg.get("target_titles") or []
    negative_titles = fcfg.get("negative_titles") or []
    _warn_unmatchable_titles(target_titles)
    overrides = fcfg.get("keyword_overrides") or {}
    max_age_hours = fcfg.get("max_age_hours")
    cutoff = datetime.now() - timedelta(hours=max_age_hours) if max_age_hours else None

    negative_locations = fcfg.get("negative_locations") or []
    eligible_locations = fcfg.get("eligible_locations") or []
    negative_description_terms = fcfg.get("negative_description_terms") or []
    negative_loc_pattern = _compile_alternation(negative_locations)
    eligible_loc_pattern = _compile_alternation(eligible_locations)
    negative_desc_pattern = _compile_alternation(negative_description_terms)

    # The remote-consistency guard (pipeline.remote_signal): a row the board
    # flagged remote whose JD never mentions remote work is treated as on-site,
    # and dropped when a remote pass alone returned it — unless it sits in one
    # of the user's own non-remote passes' locations. Read from the whole
    # config, not `filter:` alone, because the local-pass locations live under
    # `searches:`. `remote_requires_mention: false` disables it.
    remote_guard = remote_signal.guard_enabled(cfg)
    local_locations = remote_signal.local_pass_locations(cfg) if remote_guard else []

    # Deliberately louder than the no-op screen and bridge give the same
    # condition, and decided here rather than left implicit in the choice of
    # read: a *missing* jobs.csv means the scrape stage never ran, because a
    # scrape that ran and found nothing still leaves the file behind at zero
    # bytes (pipeline.rowio). "Run scrape first" is actionable; filtering
    # nothing in silence would read as a search that returned nothing.
    if not JOBS_PATH.exists():
        raise FileNotFoundError(f"{JOBS_PATH} not found — run scrape first.")

    # Take the cuts that need only the config BEFORE resolving the resume. The
    # win isn't speed — a YAKE pass on a two-page CV measures ~11ms, and
    # _keywords.json caches it locally. It's that the resume work can FAIL: the
    # gate used to sit above the CSV read, so a fork whose RESUME_TXT_B64 didn't
    # decode reddened the daily on precisely the days the scrape came back
    # empty, reporting two unrelated problems as one. Since #106 a zero-row
    # scrape truncates jobs.csv rather than leaving the previous run's file, so
    # that is the ordinary shape of a throttled morning rather than a rare one.
    #
    # A new cut that reads only config belongs in this loop, not the scoring one
    # below. The standing exception is negative_titles, which stays inside
    # _score_job because score_job() is the documented public entry point and
    # promises to apply it.
    rows = read_rows(JOBS_PATH)
    if not rows:
        # Named as an empty scrape rather than left to fall through to the
        # min_score message below, which reads as a threshold to lower.
        return _no_results(f"[filter] {JOBS_PATH.name} has no rows — nothing to filter")

    scraped = len(rows)
    candidates = []
    too_old = 0
    ineligible = 0
    offsite = 0
    offsite_rows: list[dict] = []
    for row in rows:
        if cutoff:
            posted = parse_date_posted(row.get("date_posted") or "")
            if posted is not None and posted < cutoff:
                too_old += 1
                continue
        # Before is_eligible, which lets a remote row bypass the location
        # checks: this is what decides whether the row IS remote. A judged row
        # has its is_remote rewritten to "False" and, when kept, meets the
        # location checks below as the on-site row it turned out to be. A row
        # with no description yet (LinkedIn) is left alone — the screen stage
        # backfills the JD and runs the same judgement.
        if remote_guard and remote_signal.judge_remote_row(row, local_locations) == remote_signal.OFFSITE:
            offsite += 1
            offsite_rows.append(row)
            continue
        if not is_eligible(row, negative_loc_pattern, eligible_loc_pattern, negative_desc_pattern):
            ineligible += 1
            continue
        candidates.append(row)

    # Always written, so a run that dropped nothing truncates the previous
    # run's list rather than leaving it to read as today's.
    remote_signal.report_dropped(offsite_rows, "filter")
    remote_signal.write_dropped(offsite_rows, "filter")
    if offsite:
        print(f"[filter] dropped {offsite} remote-pass row(s) whose description is "
              f"on-site elsewhere -> {remote_signal.DROPPED_PATH}")

    # The raw scrape is the largest file in the chain and most of it is already
    # discarded by here. Dropping the reference frees the non-survivors before
    # resume extraction, YAKE and scoring run, so peak memory tracks `candidates`
    # rather than everything scraped — which is what the streaming read used to
    # give us for free.
    del rows

    if not candidates:
        return _no_results(
            f"[filter] nothing left to score of {scraped} scraped "
            f"({ineligible} ineligible, {offsite} off-site, {too_old} too old)"
        )

    # Resolved in pipeline.resume_text so an unset RESUME_PATH still discovers a
    # dropped resume.docx/resume.odt, not just resume.pdf — and so the setup
    # wizard, which cannot import this module (yake, pandas), answers "is there a
    # resume?" the same way rather than matching the fixed filenames and calling a
    # RESUME_PATH resume absent (#145). An explicitly empty RESUME_PATH (e.g. a
    # workflow `${{ vars.X || '' }}` pattern) is treated the same as unset.
    resume_path = _resume_text.resolve_resume_path(os.environ.get("RESUME_PATH", ""), ROOT)

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
        return _no_results(
            f"[filter] no jobs scored >= {min_score} "
            f"(of {len(jobs)} scored, {excluded} negative-excluded, "
            f"{ineligible} ineligible, {offsite} off-site, {too_old} too old)"
        )

    summary_cols = [
        "title", "company", "location", "is_remote", "remote_only",
        "min_amount", "max_amount", "interval", "job_url", "date_posted",
        "relevance_score", "matched_keywords",
    ]
    all_cols = summary_cols + [c for c in relevant[0].keys() if c not in summary_cols]

    write_rows(OUTPUT_PATH, relevant, all_cols)

    print(
        f"[filter] kept {len(relevant)} of {len(jobs)} "
        f"({excluded} negative-excluded, {ineligible} ineligible, {offsite} off-site, "
        f"{too_old} too old) -> {OUTPUT_PATH}"
    )
    return OUTPUT_PATH


if __name__ == "__main__":
    line_buffer_stdout()

    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path)
