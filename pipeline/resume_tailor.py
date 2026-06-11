"""Per-job tailored resumes by slot-editing a copy of the candidate's own .docx.

The candidate's real resume (resumes/resume.docx, or RESUME_DOCX_PATH) is the
template — a copy is made per company and ONLY designated text slots are
rewritten by the LLM: the summary paragraph, each bullet ('List Paragraph'
style), and the value side of each "Label: values" skills line. Everything else
(name, contact, section headers, company/date lines, education) is structurally
untouchable, so the LLM cannot invent sections, employers, or dates, and the
document keeps the candidate's exact formatting.

One-page guarantee, in layers:
1. Per-slot length budgets — each replacement may not exceed the original's
   character count (plus a small tolerance), so pagination can barely move.
2. Deterministic verification — LibreOffice headless renders the edited copy to
   PDF and the page count is compared against the PRISTINE copy's page count
   (a baseline, so renderer quirks can't cause false failures). One LLM
   "shorten" retry on overflow; if it still overflows, the tailored resume is
   discarded and the default resume is used. Never silently send two pages.
3. The verified PDF is what gets uploaded (the docx is kept beside it).

Like cover letters, generation is lazy (only when a job actually reaches its
resume-upload step), score-gated by the caller, and cached per company in
career-ops/output/<Company> - resume.docx/.pdf — a hand-edited file wins if
newer. python-docx / LibreOffice are optional local-only deps: any missing
piece degrades to the default resume, never an error.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pipeline._batch_common import read_text

ROOT = Path(__file__).resolve().parent.parent

# Reuse the cover-letter filename conventions (same output dir, same sanitizer).
from pipeline.cover_letters import _safe_company  # noqa: E402

# Section headers we recognize (lowercased, exact match after whitespace
# collapse). Anything matching is protected and switches the current section.
_HEADERS = {
    "professional summary": "summary", "summary": "summary", "about": "summary",
    "skills": "skills", "technical skills": "skills", "core skills": "skills",
    "projects & open-source": "projects", "projects & open source": "projects",
    "projects": "projects", "open source": "projects",
    "professional experience": "experience", "experience": "experience",
    "work experience": "experience", "employment": "experience",
    "education & certifications": "education", "education": "education",
    "certifications": "education",
}

# Tolerance over the original slot length, per kind. Generous enough to let the
# model actually retarget (weave in the JD's terminology) — pagination is
# enforced by the LibreOffice render check downstream, not by budgets alone.
# The summary gets extra headroom: it's the most tailorable slot and a rewrite
# rejected for length means no retargeting where it matters most.
_TOLERANCE = {"summary": 0.30, "bullet": 0.20, "skills": 0.20}

# Cap on JD text fed to the prompt.
_JD_MAX = 5000


@dataclass
class Slot:
    id: str
    kind: str            # "summary" | "bullet" | "skills"
    para_index: int
    text: str            # current editable text (for skills: the values only)
    label: str = ""      # for skills: the bold "Label: " prefix (not editable)

    @property
    def max_chars(self) -> int:
        return int(len(self.text) * (1 + _TOLERANCE.get(self.kind, 0.20))) + 5


# ── docx slot extraction / patching ──────────────────────────────────────────

def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _is_header(text: str) -> str | None:
    return _HEADERS.get(_norm(text).lower())


def extract_slots(doc) -> list[Slot]:
    """Classify the document's paragraphs into editable slots. DEFAULT-PROTECT:
    only positively-identified patterns become slots; anything unrecognized is
    left alone, so an unusual resume shape degrades to less tailoring, never to
    a corrupted document."""
    slots: list[Slot] = []
    section = ""          # no slots before the first recognized header
    for i, p in enumerate(doc.paragraphs):
        text = _norm(p.text)
        if not text:
            continue
        header = _is_header(text)
        if header:
            section = header
            continue
        style = ""
        try:
            style = p.style.name if p.style is not None else ""
        except Exception:
            pass
        if style == "List Paragraph" and section:
            slots.append(Slot(id=f"s{i}", kind="bullet", para_index=i, text=p.text))
            continue
        if section == "skills" and len(p.runs) >= 2 and p.runs[0].bold \
                and p.runs[0].text.rstrip().endswith(":"):
            label = p.runs[0].text
            values = "".join(r.text for r in p.runs[1:])
            if values.strip():
                slots.append(Slot(id=f"s{i}", kind="skills", para_index=i,
                                  text=values, label=label))
            continue
        if section == "summary":
            slots.append(Slot(id=f"s{i}", kind="summary", para_index=i, text=p.text))
    return slots


def _split_skills(values: str) -> list[str]:
    """Split a skills list on commas NOT inside parentheses, so a compound
    skill like "AWS (ECS, DynamoDB)" stays one token."""
    return [t.strip() for t in re.split(r",(?![^(]*\))", values) if t.strip()]


def _skill_in_resume(token: str, resume_text: str) -> bool:
    """Whether a skill token is grounded in the resume — the deterministic guard
    against JD keyword-stuffing (the prompt forbids inventing skills, but models
    add them anyway: "JavaScript" when only TypeScript is on the resume).

    Word-boundary match. A '/'-compound ("Linux/Unix") is alternative naming —
    ANY part present suffices. A parenthetical ("AWS (ECS, DynamoDB)") enumerates
    sub-skill claims — the head AND every listed item must be grounded."""
    m = re.match(r"(.+?)\s*\((.+)\)\s*$", token)
    if m:
        head, inner = m.group(1), m.group(2)
        return (_skill_in_resume(head.strip(), resume_text)
                and all(_skill_in_resume(p.strip(), resume_text)
                        for p in inner.split(",") if p.strip()))
    for part in re.split(r"\s*/\s*", token):
        part = part.strip()
        if part and re.search(rf"(?<!\w){re.escape(part)}(?!\w)", resume_text, re.IGNORECASE):
            return True
    return False


def _fit_prose(new: str, max_chars: int) -> str | None:
    """Fit a prose replacement to its budget by trimming at a clause boundary
    instead of rejecting the whole rewrite (a rejected summary = no retargeting
    where it matters most). None when trimming would gut it."""
    if len(new) <= max_chars:
        return new
    for sep in (". ", "; ", " — ", ", "):
        cut = new.rfind(sep, 0, max_chars + 1)
        if cut >= int(max_chars * 0.6):
            trimmed = new[:cut].rstrip(" ;,")
            return trimmed if trimmed.endswith(".") else trimmed + "."
    return None


def _fit_skills(tokens: list[str], max_chars: int) -> str | None:
    """Fit a skills list to budget by dropping trailing (least relevant — the
    model front-loads) tokens."""
    while tokens and len(", ".join(tokens)) > max_chars:
        tokens.pop()
    return ", ".join(tokens) if tokens else None


def apply_replacements(doc, slots: list[Slot], replacements: dict[str, str],
                       resume_text: str = "") -> tuple[int, list[str]]:
    """Patch replacements into the document. Skills tokens not grounded in the
    resume are dropped (anti-keyword-stuffing); over-budget replacements are
    trimmed at a clause/token boundary rather than discarded; what can't be
    fitted is rejected (the original text stays). Returns (changed, rejected_ids)."""
    by_id = {s.id: s for s in slots}
    changed, rejected = 0, []
    for sid, new in replacements.items():
        slot = by_id.get(sid)
        if slot is None or not isinstance(new, str):
            continue
        new = new.strip()
        if slot.kind == "skills":
            tokens = _split_skills(new)
            if resume_text:
                tokens = [t for t in tokens if _skill_in_resume(t, resume_text)]
            fitted = _fit_skills(tokens, slot.max_chars)
        else:
            fitted = _fit_prose(new, slot.max_chars) if new else None
        if not fitted or fitted == slot.text.strip():
            if new and new != slot.text.strip():
                rejected.append(sid)
            continue
        p = doc.paragraphs[slot.para_index]
        if slot.kind == "skills":
            # Keep the bold label run; rewrite the value runs.
            p.runs[1].text = fitted
            for r in p.runs[2:]:
                r.text = ""
        else:
            if not p.runs:
                continue
            p.runs[0].text = fitted
            for r in p.runs[1:]:
                r.text = ""
        changed += 1
    return changed, rejected


def _strip_metadata(doc, author: str) -> None:
    """Reset document metadata so the copy doesn't leak edit history; the
    author is the candidate (it's their resume)."""
    try:
        cp = doc.core_properties
        cp.author = author or ""
        cp.last_modified_by = author or ""
        cp.title = ""
        cp.comments = ""
        cp.revision = 1
    except Exception:
        pass


# ── LLM prompt / response ────────────────────────────────────────────────────

# Corrective-retry feedback blocks (appended to the system prompt verbatim).
_OVERFLOW_FEEDBACK = (
    "The previous attempt OVERFLOWED one page: shorten aggressively — every "
    "replacement noticeably shorter than the original."
)
_TITLE_FEEDBACK = (
    "Your previous summary was REJECTED: it opened with the target job's title. "
    "Begin the summary with the original's own descriptor (keep its opening "
    "words) and tailor the REST of it toward the job."
)


def build_prompt(slots: list[Slot], full_resume_text: str, job,
                 report_text: str, jd_text: str = "",
                 feedback: str = "") -> tuple[str, str]:
    system = (
        "You tailor a resume toward one specific job by rewriting ONLY the text "
        "slots provided.\n"
        "OBJECTIVE: someone comparing the result to the original must immediately "
        "see it was written for THIS job — emphasis, ordering, and terminology "
        "aligned to the job's requirements. Timid micro-edits (swapping a word, "
        "trimming an article) are a FAILURE.\n"
        "HARD RULES:\n"
        "- Use ONLY facts already present in the resume below. Never invent or "
        "embellish employers, titles, dates, tools, metrics, degrees, or "
        "certifications. You may reorder, rephrase, emphasize, cut, and adopt "
        "the job's terminology for work the resume already demonstrates.\n"
        "- LENGTH: each replacement MUST be at most max_chars characters for its "
        "slot — longer is rejected.\n"
        "- PROSE HONESTY: the summary MUST begin with the original's own "
        "descriptor (e.g. 'Software engineer (~3 yrs)'), NEVER with the target "
        "job's title — a summary opening with the job title is rejected by a "
        "validator. Never attach a duration to a specific employer unless the "
        "original does (writing '3+ years at <employer>' when the original says "
        "'~3 yrs' across all roles is a fabrication); never add seniority labels "
        "(Expert, Senior, Lead, Principal) the original doesn't use.\n"
        "REQUIRED EDITS:\n"
        "- The summary slot: ALWAYS rewrite it to open toward this role — lead "
        "with the candidate's experience most relevant to the job's domain and "
        "stack, using the job's own vocabulary where truthful.\n"
        "- Every skills slot: reorder the comma-separated values so the job's "
        "technologies come first; you may drop the least relevant to make room; "
        "never add one that is not in the resume.\n"
        "- Bullets: rewrite each one where shifting emphasis or adopting the "
        "job's terminology makes the relevance obvious; front-load matching "
        "technologies. Leave a bullet unchanged only if it is already ideal.\n"
        + (f"- {feedback}\n" if feedback else "")
        + 'Reply with ONLY a JSON object: {"slot_id": "replacement text", ...}'
    )
    payload = [{"id": s.id, "kind": s.kind, **({"label": s.label.strip()} if s.label else {}),
                "text": s.text, "max_chars": s.max_chars} for s in slots]
    user = "\n\n".join(filter(None, [
        f"Target job: {job.company} — {job.role}".rstrip(" —"),
        ("=== JOB DESCRIPTION ===\n" + jd_text[:_JD_MAX]) if jd_text else "",
        ("=== EVALUATION NOTES (requirements / matches) ===\n" + report_text[:6000])
        if report_text else "",
        "=== FULL RESUME (source of truth — facts may only come from here) ===\n"
        + full_resume_text[:8000],
        "=== SLOTS TO REWRITE ===\n" + json.dumps(payload, ensure_ascii=False),
        "JSON:",
    ]))
    return system, user


def _leads_with_role_title(new: str, original: str, role: str) -> bool:
    """True when a summary replacement opens by retitling the candidate as the
    target job's title (e.g. 'Application Support Engineer with ...'). The title
    must appear as a CONSECUTIVE phrase in the opening words — scattered matches
    ('Engineer with ... production support') are legitimate prose, not a
    retitle. Allowed when the ORIGINAL summary already opens with the same
    phrase (a 'Software engineer' applying to 'Software Engineer' roles)."""
    def norm(t: str) -> str:
        return " ".join(re.findall(r"[a-z]+", (t or "").lower()))
    phrase = norm(role)
    if len(phrase.split()) < 2:    # single-word titles are too generic to judge
        return False
    head = " ".join(norm(new).split()[:10])
    if phrase not in head:
        return False
    orig_head = " ".join(norm(original).split()[:10])
    return phrase not in orig_head


def enforce_prose_rules(replacements: dict[str, str], slots: list[Slot],
                        job) -> tuple[dict[str, str], list[str]]:
    """Deterministic backstop for prose rules the prompt alone doesn't hold:
    drop a summary replacement that retitles the candidate as the target job's
    title (the original summary stays — decline beats fabricate). Returns the
    filtered replacements and human-readable notes for the stats line."""
    notes: list[str] = []
    out = dict(replacements)
    for s in slots:
        if s.kind != "summary" or s.id not in out:
            continue
        if _leads_with_role_title(out[s.id], s.text, getattr(job, "role", "")):
            del out[s.id]
            notes.append(f"{s.id} dropped: summary opens with the target job title")
    return out, notes


def parse_replacements(raw: str) -> dict[str, str]:
    """Tolerant parse of the model's JSON reply (code fences / prose around it)."""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


# ── LibreOffice render + page count ──────────────────────────────────────────

def find_soffice() -> str | None:
    env = os.environ.get("SOFFICE_PATH")
    if env and Path(env).exists():
        return env
    hit = shutil.which("soffice")
    if hit:
        return hit
    for p in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice", "/usr/local/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        if Path(p).exists():
            return p
    return None


def render_pdf(docx_path: Path, out_dir: Path) -> Path | None:
    """Convert a docx to PDF with LibreOffice headless. Uses a dedicated user
    profile so it works even while the user has LibreOffice open. Returns the
    PDF path, or None when LibreOffice is unavailable / conversion fails."""
    soffice = find_soffice()
    if not soffice:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = ROOT / "output" / ".lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--norestore",
             f"-env:UserInstallation={profile.resolve().as_uri()}",
             "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
            check=True, capture_output=True, timeout=120,
        )
    except Exception:
        return None
    pdf = out_dir / (Path(docx_path).stem + ".pdf")
    return pdf if pdf.exists() else None


def page_count(pdf_path: Path) -> int | None:
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


_BASELINE: dict[tuple[str, float], int] = {}


def _baseline_pages(source_docx: Path) -> int | None:
    """Page count of the PRISTINE resume under the same renderer — the bar the
    tailored copy must not exceed. Cached per (path, mtime)."""
    key = (str(source_docx), source_docx.stat().st_mtime)
    if key in _BASELINE:
        return _BASELINE[key]
    with tempfile.TemporaryDirectory() as td:
        pdf = render_pdf(source_docx, Path(td))
        pages = page_count(pdf) if pdf else None
    if pages is not None:
        _BASELINE[key] = pages
    return pages


# ── orchestration ────────────────────────────────────────────────────────────

def source_docx() -> Path | None:
    env = os.environ.get("RESUME_DOCX_PATH")
    p = Path(env) if env else ROOT / "resumes" / "resume.docx"
    return p if p.exists() else None


def resume_paths(career_ops: Path, company: str) -> tuple[Path, Path]:
    base = Path(career_ops) / "output" / f"{_safe_company(company)} - resume"
    return base.with_suffix(".docx"), base.with_suffix(".pdf")


def find_existing(career_ops: Path, company: str) -> Path | None:
    """An already-tailored resume for this company (PDF preferred, docx when
    there's no renderer). None if neither exists."""
    docx_p, pdf_p = resume_paths(career_ops, company)
    if pdf_p.exists():
        return pdf_p
    if docx_p.exists():
        return docx_p
    return None


def _jd_text(career_ops: Path, report_base: Path | None, job) -> str:
    """The job's description text — the strongest tailoring signal (the report
    discusses fit but doesn't carry the JD's keyword surface). Sources, in
    order: the batch-cached JD file (local career-ops, then the refreshed
    artifact), else a live fetch via the LinkedIn guest endpoint. "" when
    unavailable; the report alone still works."""
    num = str(getattr(job, "num", "") or "").strip()
    if num:
        for base in (career_ops, report_base):
            if base is None:
                continue
            p = Path(base) / "batch" / "jds" / f"{num}.txt"
            if p.exists():
                return read_text(p)[:_JD_MAX]
    try:
        from pipeline.screen import (
            extract_description, fetch_and_classify, linkedin_guest_jd_url,
        )
        guest = linkedin_guest_jd_url(getattr(job, "url", "") or "")
        if guest:
            _, _, body = fetch_and_classify(guest, timeout=8)
            return extract_description(body)[:_JD_MAX]
    except Exception:
        pass
    return ""


def _resolve_caller(provider: str | None, model: str | None):
    from pipeline.batch_evaluate import resolve_caller
    from pipeline.apply.answers import thinking_disabled
    return resolve_caller(provider, model, lead_env="TAILOR_MODEL",
                          disable_thinking=thinking_disabled())


def generate_for_job(career_ops: Path, job, *, caller=None,
                     provider: str | None = None, model: str | None = None,
                     report_base: Path | None = None, force: bool = False) -> Path | None:
    """Return the path of a one-page tailored resume for this job (PDF when a
    renderer exists, else the docx), generating it on first request. None on any
    failure — the caller falls back to the default resume."""
    career_ops = Path(career_ops)
    if not force:
        existing = find_existing(career_ops, job.company)
        if existing:
            return existing

    src = source_docx()
    if src is None:
        return None
    try:
        from docx import Document
    except ImportError:
        print("[tailor] python-docx not installed — using the default resume "
              "(pip install python-docx)")
        return None

    docx_out, pdf_out = resume_paths(career_ops, job.company)
    docx_out.parent.mkdir(parents=True, exist_ok=True)

    report_path = getattr(job, "report_path", "") or ""
    report_text = read_text(Path(report_base or career_ops) / report_path) if report_path else ""
    jd_text = _jd_text(career_ops, report_base, job)

    if caller is None:
        caller = _resolve_caller(provider, model)
    from pipeline.batch_evaluate import _call_with_retry

    baseline = _baseline_pages(src)   # None when LibreOffice is missing

    # Two attempts: the initial pass plus ONE corrective retry. The retry's
    # feedback is whichever rule was violated — a prose violation (summary
    # opened with the job title) or a page overflow. A second violation of
    # either kind isn't retried again: prose violations are dropped by the
    # validator (the original text stays), overflow falls back to the default.
    feedback = ""
    for attempt in (0, 1):
        shutil.copyfile(src, docx_out)
        doc = Document(str(docx_out))
        slots = extract_slots(doc)
        if not slots:
            docx_out.unlink(missing_ok=True)
            return None
        full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        system, user = build_prompt(slots, full_text, job, report_text, jd_text,
                                    feedback=feedback)
        try:
            raw = _call_with_retry(caller, system, user, max_attempts=6, base_delay=1.0)
        except Exception:
            docx_out.unlink(missing_ok=True)
            return None
        reps = parse_replacements(raw)
        reps, notes = enforce_prose_rules(reps, slots, job)
        if notes and attempt == 0:
            print(f"[tailor] {job.company}: {'; '.join(notes)} — retrying with feedback")
            feedback = _TITLE_FEEDBACK
            continue
        changed, rejected = apply_replacements(doc, slots, reps, full_text)
        print(f"[tailor] {job.company}: {len(reps)} rewrite(s) returned, {changed} applied"
              + (f", {len(rejected)} rejected ({', '.join(rejected[:4])})" if rejected else "")
              + (f" | {'; '.join(notes)}" if notes else ""))
        if not changed:
            docx_out.unlink(missing_ok=True)
            return None
        _strip_metadata(doc, getattr(job, "candidate_name", "") or _author_name(career_ops))
        doc.save(str(docx_out))

        pdf = render_pdf(docx_out, docx_out.parent)
        if pdf is None:
            # No renderer: budgets are the only guard; upload the docx itself.
            return docx_out
        pages = page_count(pdf)
        if pages is not None and baseline is not None and pages <= baseline:
            return pdf
        if pages == 1:           # no baseline (renderer appeared late) but fits
            return pdf
        print(f"[tailor] {job.company}: tailored resume is {pages} page(s) vs "
              f"baseline {baseline} — "
              f"{'retrying shorter' if attempt == 0 else 'falling back to default resume'}")
        pdf.unlink(missing_ok=True)
        feedback = _OVERFLOW_FEEDBACK

    docx_out.unlink(missing_ok=True)
    return None


def _author_name(career_ops: Path) -> str:
    try:
        from pipeline.apply.profile import ApplyProfile
        return ApplyProfile.load(Path(career_ops)).full_name
    except Exception:
        return ""


if __name__ == "__main__":
    # Manual test:
    #   python -m pipeline.resume_tailor "<Company>" "<Role>" [report.md] [job_url]
    import sys
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from pipeline.apply.queue import ApplyJob
    company = sys.argv[1] if len(sys.argv) > 1 else "Test Company"
    role = sys.argv[2] if len(sys.argv) > 2 else "Software Engineer"
    report = sys.argv[3] if len(sys.argv) > 3 else ""
    url = sys.argv[4] if len(sys.argv) > 4 else ""
    job = ApplyJob(num="", company=company, role=role, url=url, score=None,
                   report_path=report)
    out = generate_for_job(ROOT / "career-ops", job, force=True)
    print(f"-> {out}" if out else "-> failed (see messages above)")
