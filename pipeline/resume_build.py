"""Adaptive one-page fit (Commit 3b): render a content-JSON at the layout scale
that fills the page.

Fill is content × layout size, so no single fixed typography fills the page across
candidates with different amounts of content. fit_to_page deterministically
searches the largest scale that still fits one page within the aim band — a
content-rich résumé stays tight (Cowork density), a lighter one scales up to fill —
with NO LLM and no padding. The 3c loop supplies the (grounded, tailored) content;
this decides how big to render it. docx→PDF is resume_tailor's job.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pipeline import resume_fit, resume_render, resume_tailor


@dataclass
class BuildResult:
    pdf: Path
    scale: float
    fit: resume_fit.FitResult


def _search_scale(measure_at, *, lo: float = 0.9, hi: float = 1.35, steps: int = 6,
                  ok=None) -> float:
    """The largest scale that renders the fullest acceptable one-pager: one page,
    fill up to the aim ceiling (TARGET_HI, which sits a margin below the TIGHT /
    overflow threshold). Fill rises monotonically with scale, so this bisects the
    boundary. If even `hi` is acceptable the content is light → use `hi`; if even
    `lo` isn't the content is heavy → use `lo` (the 3c loop then trims). `measure_at`
    renders+measures at a scale; pure, so it's testable without LibreOffice."""
    ok = ok or (lambda m: m.pages == 1 and m.fill <= resume_fit.TARGET_HI)
    if ok(measure_at(hi)):
        return hi
    if not ok(measure_at(lo)):
        return lo
    best = lo
    for _ in range(steps):
        mid = (lo + hi) / 2
        if ok(measure_at(mid)):
            best, lo = mid, mid
        else:
            hi = mid
    return best


def fit_to_page(content: dict, out_dir, *, lo: float = 0.9, hi: float = 1.35,
                steps: int = 6) -> BuildResult:
    """Render `content` at the fitted scale and return the chosen PDF + scale +
    FitResult. Requires LibreOffice (resume_tailor.render_pdf)."""
    out_dir = Path(out_dir)
    # A content-derived prefix keeps fits of DIFFERENT résumés in the same out_dir
    # from colliding on the same rounded-scale filename (and overwriting a prior
    # BuildResult's PDF).
    tag = hashlib.sha1(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:10]
    cache: dict[float, tuple[Path, resume_fit.Measurement]] = {}

    def measure_at(scale: float) -> resume_fit.Measurement:
        key = round(scale, 4)
        if key not in cache:
            docx = resume_render.render_docx(content, out_dir / f"_fit_{tag}_{key}.docx", scale=scale)
            pdf = resume_tailor.render_pdf(docx, out_dir)
            if pdf is None:
                raise RuntimeError("LibreOffice (soffice) is required to fit a résumé to one page")
            cache[key] = (pdf, resume_fit.measure(pdf))
        return cache[key][1]

    scale = _search_scale(measure_at, lo=lo, hi=hi, steps=steps)
    pdf, m = cache[round(scale, 4)]
    return BuildResult(pdf=pdf, scale=scale, fit=resume_fit.result_from(m, content))
