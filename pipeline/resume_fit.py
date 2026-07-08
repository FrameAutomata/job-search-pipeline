"""One-page fill gate (Commit 3b): measure how far down page 1 a rendered résumé
PDF reaches, and turn it into a verdict the build/tailor loop iterates against.

Mirrors the Cowork check_fit: aim 92–96% on exactly one page; the actual pass band
is 0.88 ≤ fill ≤ 0.985. Fill is measured with pdfplumber (lowest word bottom ÷
page height); docx→PDF is resume_tailor's job. The gate only measures and reports —
it never edits; the 3c loop reacts to the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Aim band (the printed target) plus the actual pass thresholds.
TARGET_LO, TARGET_HI, WARN_LO = 0.92, 0.96, 0.88
TIGHT = 0.985                 # above this, real risk of spilling to page 2
BOTTOM_MARGIN_IN = 0.5        # whitespace below the last line that's just bottom margin
LINE_IN = 0.20                # ~one body line, for the "N more lines" hint


@dataclass(frozen=True)
class Measurement:
    pages: int
    fill: float               # lowest-word bottom ÷ page height, on page 1
    blank_in: float           # inches of whitespace below the last line


@dataclass
class FitResult:
    ok: bool
    code: int                 # 0 = ok, 2 = underfull, 3 = overfull/tight
    verdict: str
    fill: float
    pages: int
    notes: list = field(default_factory=list)


def measure(pdf_path) -> Measurement:
    """Page count + how far down page 1 the text reaches (as a fraction of page
    height) + the blank inches beneath it."""
    import pdfplumber
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = len(pdf.pages)
        page = pdf.pages[0]
        height = page.height
        bottom = max((w["bottom"] for w in page.extract_words()), default=0.0)
    if not height:
        return Measurement(pages, 0.0, 0.0)
    return Measurement(pages, bottom / height, (height - bottom) / 72.0)


def _verdict(pages: int, fill: float) -> tuple[int, str]:
    """The pure gate decision — one page in [WARN_LO, TIGHT] is OK, anything else
    is under/overfull. I/O-free so it's unit-testable without a PDF."""
    if pages > 1:
        return 3, "OVERFULL"
    if fill > TIGHT:
        return 3, "TIGHT"
    if fill < WARN_LO:
        return 2, "UNDERFULL"
    return 0, "OK"


def _content_lint(content: dict | None) -> list[str]:
    """Structural warnings independent of the measured fill — chiefly a thin flex
    (projects) section, the documented #1 cause of an underfilled page."""
    if not content:
        return []
    projects = content.get("projects")
    projects = projects if isinstance(projects, list) else []
    bullets = sum(len(p.get("bullets") or []) for p in projects if isinstance(p, dict))
    # Warn when a projects section EXISTS but is thin (the documented #1 underfill
    # cause). Gate on the actual section, not the heading/order flags — render keys
    # the section off `projects`, so the flags both mis-fire (flag set, no section)
    # and miss a genuinely-thin default-heading section.
    if projects and (len(projects) < 2 or bullets < 3):
        return ["Flex (projects) section is THIN — expand to ~2 entries / 3–4 "
                "bullets; it's the #1 fill lever."]
    return []


def check_fit(pdf_path, content: dict | None = None) -> FitResult:
    """Measure the PDF, decide the verdict, and attach remediation notes (plus a
    content-lint when the content-JSON is passed)."""
    return result_from(measure(pdf_path), content)


def result_from(m: Measurement, content: dict | None = None) -> FitResult:
    """Turn a Measurement into a verdict + remediation notes. Split out from
    check_fit so the fit-search can reuse a measurement it already took."""
    code, verdict = _verdict(m.pages, m.fill)
    if verdict == "OVERFULL":
        notes = ["Spills onto page 2 — trim until it fits one page."]
    elif verdict == "TIGHT":
        notes = ["Very tight — trim a little to avoid spilling."]
    elif verdict == "UNDERFULL":
        usable = max(0.0, m.blank_in - BOTTOM_MARGIN_IN)
        more = max(1, round(usable / LINE_IN))
        notes = [f"Underfull — about {usable:.1f} in blank (~{more} more lines). Add substance."]
    else:
        notes = ["OK — one full page."]
    notes.extend(_content_lint(content))
    return FitResult(ok=(code == 0), code=code, verdict=verdict,
                     fill=m.fill, pages=m.pages, notes=notes)
