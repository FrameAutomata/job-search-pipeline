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

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pipeline import resume_fit, resume_render, resume_tailor


@dataclass
class BuildResult:
    pdf: Path
    scale: float
    fit: resume_fit.FitResult

    def discard(self) -> None:
        """Remove the rendered PDF — for a result the caller has REJECTED (an
        overflow the trim loop retries, or one generate_for_job's two-page guard
        turns down). A result the caller accepts is consumed by `os.replace`
        into its final name instead, so either way nothing is left behind."""
        self.pdf.unlink(missing_ok=True)


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
    FitResult. Requires LibreOffice (resume_tailor.render_pdf).

    The search renders up to `steps`+2 docx/PDF pairs and exactly one of them
    is the answer. They go into a private scratch directory that is removed
    before this returns, whichever scale won: `out_dir` is career-ops/output/,
    the folder the UI serves résumés out of, and the non-chosen renders — some
    of them two pages long — used to be left there beside the product under
    near-identical names, ~13 files per résumé and a fresh set on every rebuild
    (#164). The scratch dir lives UNDER out_dir so the caller's `os.replace` of
    the chosen PDF into place stays a same-filesystem rename, i.e. atomic.

    What survives is the one chosen PDF, under a dot-prefixed name that neither
    sorts beside the product nor is served as one, and it is the caller's to
    consume (`os.replace`) or `discard()`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A fresh scratch dir per call (mkdtemp's unique name), so two fits in the
    # same out_dir — a parallel build, or two résumés — can never consume or
    # discard the other's PDF.
    with tempfile.TemporaryDirectory(prefix=".fit-", dir=out_dir,
                                     ignore_cleanup_errors=True) as td:
        scratch = Path(td)
        cache: dict[float, tuple[Path, resume_fit.Measurement]] = {}

        def measure_at(scale: float) -> resume_fit.Measurement:
            key = round(scale, 4)
            if key not in cache:
                docx = resume_render.render_docx(content, scratch / f"{key}.docx", scale=scale)
                pdf = resume_tailor.render_pdf(docx, scratch)
                if pdf is None:
                    raise RuntimeError("LibreOffice (soffice) is required to fit a résumé to one page")
                cache[key] = (pdf, resume_fit.measure(pdf))
            return cache[key][1]

        scale = _search_scale(measure_at, lo=lo, hi=hi, steps=steps)
        pdf, m = cache[round(scale, 4)]
        chosen = out_dir / f"{scratch.name}.pdf"
        os.replace(pdf, chosen)
    return BuildResult(pdf=chosen, scale=scale, fit=resume_fit.result_from(m, content))
