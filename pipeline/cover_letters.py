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


def build_prompt(profile: ApplyProfile, cv: str, job, report_text: str) -> tuple[str, str]:
    """(system, user) for one cover letter. Grounded in the CV + report so the
    model can't invent employers, titles, or credentials."""
    system = (
        "You write concise, specific, honest cover letters for a job applicant. "
        "Use ONLY facts found in the candidate's CV and the evaluation notes "
        "below — never invent employers, job titles, dates, degrees, or skills. "
        "Write in English, 3-4 short paragraphs: (1) the role and a genuine, "
        "specific hook; (2) two or three concrete achievements from the CV that "
        "match what this job needs; (3) a brief, warm close. No markdown headings, "
        "no bracketed placeholders, no buzzword filler. Sound like a real person. "
        "End with the candidate's name on its own line."
    )
    contact = ", ".join(x for x in (profile.full_name, profile.email,
                                    f"{profile.city}, {profile.country}".strip(", ")) if x)
    parts = [
        f"Candidate: {contact}",
        f"Applying to: {job.company} — {job.role}".rstrip(" —"),
        "",
        "=== CANDIDATE CV ===",
        cv or "(no CV on file)",
    ]
    if report_text:
        # The report carries the role summary, match analysis, and phrases the
        # evaluator tagged for cover-letter use — exactly the tailoring material.
        parts += ["", "=== EVALUATION NOTES FOR THIS JOB ===", report_text[:6000]]
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
    model = model or os.environ.get("BATCH_MODEL") or PROVIDER_DEFAULTS[provider]
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
    import sys
    co = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "career-ops"
    ms = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    run(co.resolve(), min_score=ms, force=True)
