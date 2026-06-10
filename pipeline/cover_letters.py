"""Pre-generate tailored cover letters for high-fit pending jobs.

Runs ahead of the apply stage (not during it) so the slow free-text generation
stays out of the time-sensitive browser session, and so each letter is a file
you can read and edit before anything is submitted. Draws on what career-ops
already produced — your CV (cv.md) and the job's evaluation report (its
proof-point phrases) — and writes `career-ops/output/<company> - cover.md`,
which the apply engine's `_find_tailored_cover_letter` picks up automatically.

Reuses the pipeline's multi-provider LLM caller (same provider/model as
--evaluate-batch). Opt-in via orchestrate's --cover-letters; skips jobs whose
letter already exists unless force=True. Never invents experience — the prompt
constrains the model to facts from the CV/report."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pipeline._batch_common import atomic_write_text, read_text
from pipeline.apply import queue
from pipeline.apply.profile import ApplyProfile

ROOT = Path(__file__).resolve().parent.parent

# Windows-illegal filename characters (plus control chars); collapsed to spaces.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_company(company: str) -> str:
    name = _ILLEGAL.sub(" ", company or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name or "company"


def cover_path(out_dir: Path, company: str) -> Path:
    """The file the generator writes and the applier looks for: '<Company> -
    cover.md'. The applier matches any .md/.txt whose name contains the company
    slug and 'cover', so this naming is what links the two stages."""
    return Path(out_dir) / f"{_safe_company(company)} - cover.md"


def cover_pdf_path(career_ops: Path, company: str) -> Path:
    """The PDF rendered from the cover letter (for forms that want an upload)."""
    return cover_path(Path(career_ops) / "output", company).with_suffix(".pdf")


def build_prompt(profile: ApplyProfile, cv: str, job, report_text: str) -> tuple[str, str]:
    """(system, user) for one cover letter. Grounded in the CV + report so the
    model can't invent employers, titles, or credentials."""
    system = (
        "You write concise, specific, HONEST cover letters. Use ONLY facts found "
        "in the candidate's CV and details below. Never invent or embellish: no "
        "made-up employers, titles, dates, degrees, skills, or metrics — AND no "
        "claims about location, relocation, travel, on-site availability, "
        "willingness, security clearances, or start dates unless they appear "
        "verbatim in the candidate details. The evaluation notes may flag gaps, "
        "blockers, or location/seniority requirements — do NOT rebut them and do "
        "NOT fabricate anything to satisfy them; simply omit what you can't "
        "truthfully support. If you're unsure whether something is true, leave it "
        "out. Write in English, 3-4 short paragraphs: (1) the role and a genuine "
        "hook grounded in the CV; (2) two or three concrete achievements from the "
        "CV that match the job; (3) a brief, warm close expressing real interest "
        "(no logistical promises about travel or availability). No markdown "
        "headings, no bracketed placeholders, no buzzword filler. End with the "
        "candidate's name on its own line."
    )
    contact = ", ".join(x for x in (profile.full_name, profile.email,
                                    f"{profile.city}, {profile.country}".strip(", ")) if x)
    parts = [
        f"Candidate: {contact}",
        "(The location above is the ONLY location/availability fact you know — do "
        "not state or imply any other location, travel, relocation, or on-site "
        "availability.)",
        f"Applying to: {job.company} — {job.role}".rstrip(" —"),
        "",
        "=== CANDIDATE CV ===",
        cv or "(no CV on file)",
    ]
    if report_text:
        # The report carries proof-point phrases the evaluator tagged for cover-
        # letter use. It ALSO contains gap/blocker analysis — the system prompt
        # forbids fabricating mitigations for those.
        parts += ["", "=== EVALUATION NOTES (use positives only; do not address gaps) ===",
                  report_text[:6000]]
    parts += ["", "Write the cover letter now."]
    return system, "\n".join(parts)


def _resolve_caller(provider: str | None, model: str | None):
    from pipeline.batch_evaluate import _build_caller, _detect_provider, PROVIDER_DEFAULTS
    from pipeline.apply.answers import thinking_disabled
    provider = provider or _detect_provider()
    if not provider:
        raise RuntimeError(
            "no LLM provider configured for cover letters — set a provider key "
            "(DEEPINFRA_API_KEY, etc.) or BATCH_PROVIDER in .env"
        )
    # Model precedence for cover letters: COVER_MODEL (a quality-first chain,
    # since a letter is recruiter-facing) → APPLY_MODEL → BATCH_MODEL → default.
    # Each may be a comma-separated failover chain.
    model = (model or os.environ.get("COVER_MODEL") or os.environ.get("APPLY_MODEL")
             or os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider])
    # A cover letter is prose, not a reasoning task — disable thinking so the
    # model writes directly (faster, and avoids the truncated/garbled tails MiMo
    # produces when it spends the token budget thinking).
    return _build_caller(provider, model, disable_thinking=thinking_disabled())


def find_existing(career_ops: Path, company: str) -> str:
    """Text of an existing tailored cover letter for this company, if any.

    Broad match (so a hand-written letter under a slightly different name is
    still found): any .md/.txt in career-ops/output (or APPLY_COVER_DIR) whose
    filename contains the company slug and 'cover'. Most recent wins."""
    base = os.environ.get("APPLY_COVER_DIR")
    cdir = Path(base) if base else Path(career_ops) / "output"
    slug = re.sub(r"[^a-z0-9]+", "", (company or "").lower())
    if not cdir.exists() or not slug:
        return ""
    matches = []
    for p in cdir.glob("*"):
        if p.suffix.lower() not in (".txt", ".md"):
            continue
        name = re.sub(r"[^a-z0-9]+", "", p.stem.lower())
        if slug in name and "cover" in name:
            matches.append(p)
    if not matches:
        return ""
    return read_text(max(matches, key=lambda p: p.stat().st_mtime))


def render_pdf(text: str, pdf_path: Path) -> bool:
    """Render plain cover-letter text to a simple PDF via headless Chromium
    (Playwright is already an apply dependency — no extra package). Returns False
    if Playwright/Chromium isn't available or rendering fails (caller then skips
    the upload rather than erroring)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    import html as _html
    paras = [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    body = "\n".join(f"<p>{_html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)
    doc = ("<!doctype html><html><head><meta charset='utf-8'><style>"
           "body{font-family:Georgia,'Times New Roman',serif;font-size:11pt;"
           "line-height:1.5;color:#111;} p{margin:0 0 12pt;}"
           "</style></head><body>" + body + "</body></html>")
    try:
        Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(doc, wait_until="load")
                page.pdf(path=str(pdf_path), format="Letter",
                         margin={"top": "1in", "bottom": "1in", "left": "1in", "right": "1in"})
            finally:
                browser.close()
        return True
    except Exception:
        return False


def ensure_cover_pdf(career_ops: Path, company: str) -> Path | None:
    """Return a PDF of this company's cover letter, rendering it from the saved
    .md text if one doesn't exist yet. None when there's no letter text or the
    render fails. Used for forms whose cover-letter field is a file upload."""
    career_ops = Path(career_ops)
    text = find_existing(career_ops, company)
    if not text:
        return None
    pdf = cover_pdf_path(career_ops, company)
    if pdf.exists():
        return pdf
    return pdf if render_pdf(text, pdf) else None


def generate_for_job(career_ops: Path, job, *, caller=None,
                     provider: str | None = None, model: str | None = None,
                     force: bool = False) -> str:
    """Return a tailored cover letter for one job — the existing file if present
    (unless force), otherwise generate one, save it to
    career-ops/output/<company> - cover.md, and return its text. "" on failure.

    This is the request-gated entry point: the apply engine calls it only when a
    form actually has a cover-letter field, so we never generate unrequested
    letters. `caller` lets the applier reuse its already-built LLM caller."""
    career_ops = Path(career_ops)
    if not force:
        existing = find_existing(career_ops, job.company)
        if existing:
            return existing
    if caller is None:
        caller = _resolve_caller(provider, model)

    profile = ApplyProfile.load(career_ops)
    cv = read_text(career_ops / "cv.md")
    report_path = getattr(job, "report_path", "") or ""
    report_text = read_text(career_ops / report_path) if report_path else ""
    system, user = build_prompt(profile, cv, job, report_text)
    from pipeline.batch_evaluate import _call_with_retry
    try:
        text = _call_with_retry(caller, system, user, max_attempts=6, base_delay=1.0).strip()
    except Exception:
        return ""
    if not text:
        return ""
    atomic_write_text(cover_path(career_ops / "output", job.company), text + "\n")
    return text


def run(
    career_ops: Path,
    *,
    min_score: float = 4.0,
    limit: int = 0,
    force: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> int:
    """Bulk pre-generate cover letters for pending jobs scoring >= min_score.

    NOTE: this generates a letter for every high-fit job regardless of whether
    that job's form actually asks for one — so it's a power-user pre-warm, not
    the default flow. The apply engine generates lazily (only when a form has a
    cover-letter field). Standalone: `python -m pipeline.cover_letters`."""
    career_ops = Path(career_ops)
    jobs = queue.select(career_ops, min_score=min_score, limit=limit, linkedin_only=False)
    if not jobs:
        print(f"[cover] no pending jobs scoring >= {min_score}")
        return 0

    caller = None
    written = skipped = failed = 0
    print(f"[cover] {len(jobs)} pending job(s) >= {min_score} | output -> {career_ops / 'output'}")
    for job in jobs:
        if not force and find_existing(career_ops, job.company):
            skipped += 1
            continue
        if caller is None:
            caller = _resolve_caller(provider, model)
        text = generate_for_job(career_ops, job, caller=caller, force=force)
        if text:
            written += 1
            print(f"[cover]   + {cover_path(career_ops / 'output', job.company).name}")
        else:
            failed += 1
            print(f"[cover]   ! {job.company} / {job.role}")

    print(f"[cover] done — {written} written, {skipped} already existed, {failed} failed")
    return written


if __name__ == "__main__":
    # python -m pipeline.cover_letters [career-ops-path] [min-score]
    # Standalone invocation regenerates (force) so it's easy to re-test output.
    # Load .env here — unlike the in-pipeline path, this bypasses orchestrate.py
    # (which normally loads it), so provider keys wouldn't otherwise be seen.
    import sys
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    co = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "career-ops"
    ms = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    run(co.resolve(), min_score=ms, force=True)
