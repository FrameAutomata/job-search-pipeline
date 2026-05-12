"""Filter jobs.csv to the most relevant rows.

Extracts keywords from the user's resume (YAKE — domain-agnostic statistical
extraction, works for any field) and scores each job by keyword and target-title
matches. Negative titles hard-exclude. No hardcoded vocabulary."""

import csv
import os
import re
import sys
from pathlib import Path

import pdfplumber
import yake
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

JOBS_PATH = ROOT / "output" / "jobs.csv"
OUTPUT_PATH = ROOT / "output" / "filtered_jobs.csv"

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


def extract_resume_text(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


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


def score_job(
    row: dict,
    keywords: dict[str, int],
    target_titles: list[str],
    negative_titles: list[str],
) -> tuple[int, list[str]] | None:
    title = (row.get("title") or "").lower()

    for neg in negative_titles:
        if re.search(r"\b" + re.escape(neg.lower()) + r"\b", title):
            return None

    text = " ".join((row.get(f) or "") for f in SEARCH_FIELDS).lower()
    matched: list[str] = []
    score = 0

    for kw, weight in keywords.items():
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            matched.append(kw)
            score += weight

    for target in target_titles:
        if re.search(r"\b" + re.escape(target.lower()) + r"\b", title):
            matched.append(f"title:{target}")
            score += SCORE_TITLE_MATCH

    return score, matched


def run(config_path: Path) -> Path:
    cfg = load_config(config_path)
    fcfg = cfg["filter"]
    min_score = fcfg.get("min_score", 5)
    target_titles = fcfg.get("target_titles") or []
    negative_titles = fcfg.get("negative_titles") or []
    overrides = fcfg.get("keyword_overrides") or {}

    if not JOBS_PATH.exists():
        raise FileNotFoundError(f"{JOBS_PATH} not found — run scrape first.")

    resume_env = os.environ.get("RESUME_PATH", "resumes/resume.pdf")
    resume_path = Path(resume_env)
    if not resume_path.is_absolute():
        resume_path = (ROOT / resume_path).resolve()
    if not resume_path.exists():
        raise FileNotFoundError(
            f"Resume PDF not found at {resume_path}. "
            "Drop your resume there or set RESUME_PATH in .env."
        )

    print(f"[filter] extracting keywords from {resume_path.name}")
    resume_text = extract_resume_text(resume_path)
    if not resume_text.strip():
        print("[filter] WARNING: no text extracted from resume — is it a scanned PDF?")
    keywords = extract_keywords(resume_text)
    for kw, w in overrides.items():
        keywords[kw.lower()] = int(w)
    print(
        f"[filter] {len(keywords)} keywords extracted "
        f"(target_titles: {len(target_titles)}, negative_titles: {len(negative_titles)})"
    )

    jobs = []
    excluded = 0
    with open(JOBS_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result = score_job(row, keywords, target_titles, negative_titles)
            if result is None:
                excluded += 1
                continue
            score, matches = result
            row["relevance_score"] = score
            row["matched_keywords"] = ", ".join(matches)
            jobs.append(row)

    relevant = [j for j in jobs if int(j["relevance_score"]) >= min_score]
    relevant.sort(key=lambda r: int(r["relevance_score"]), reverse=True)

    if not relevant:
        print(
            f"[filter] no jobs scored >= {min_score} "
            f"(of {len(jobs)} scored, {excluded} negative-excluded)"
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
        f"({excluded} negative-excluded) -> {OUTPUT_PATH}"
    )
    return OUTPUT_PATH


if __name__ == "__main__":
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path)
