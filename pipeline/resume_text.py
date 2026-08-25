"""Resume text extraction — format-dispatching over PDF / DOCX / ODT / TXT.

A single entry point both the keyword-scoring filter (pipeline/filter.py) and the
UI onboarding upload (pipeline/app/onboard.py) go through, so every resume format
is read identically. Extracted text is what feeds YAKE keyword scoring and the
cloud evaluator (resumes/resume.txt); the source format doesn't otherwise matter.

DOCX/ODT are preferred over PDF for import: resume tailoring
(pipeline/resume_tailor.py) slot-edits a DOCX directly, and editable formats give
cleaner text than a PDF's reconstructed layout. All four formats work for scoring.

Per-format libraries are imported lazily inside each extractor so importing this
module stays cheap (the UI's onboard module imports it at the top) and a missing
optional dep only bites the user who actually hands us that format.
"""

import sys
import tempfile
from pathlib import Path

from pipeline.stdio import line_buffer_stdout

# What the setup/UI offer as importable resume formats (no .txt — that's the
# generated sidecar, not something a user uploads).
IMPORT_SUFFIXES = (".pdf", ".docx", ".odt")


def _from_pdf(path: Path) -> str:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _from_docx(path: Path) -> str:
    # Paragraphs plus table cells — skills/tech are frequently laid out in a
    # borderless table, which doc.paragraphs alone would miss.
    from docx import Document
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _from_odt(path: Path) -> str:
    # Walk the body in document order (rather than getElementsByType, which
    # groups by type and would scramble the name-then-sections ordering the
    # contact-info parser relies on). Headings (text:h) and paragraphs (text:p)
    # are emitted as lines; teletype.extractText flattens nested spans/links.
    from odf.opendocument import load
    from odf import teletype
    doc = load(str(path))
    lines: list[str] = []

    def walk(node) -> None:
        for child in getattr(node, "childNodes", []):
            qname = getattr(child, "qname", None)
            if qname and qname[1] in ("p", "h"):
                lines.append(teletype.extractText(child))
            else:
                walk(child)

    walk(doc.text)
    return "\n".join(lines)


def _from_txt(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _extractor_for(suffix: str):
    """The extractor for a file suffix, or a clear ValueError so callers can
    surface "use PDF/DOCX/ODT" rather than a cryptic library error. Dispatch is
    by name through module globals, so tests can monkeypatch an extractor.
    `.txt` isn't an import format users pick, but the filter reads a resume.txt
    sidecar directly, so the dispatcher handles it too."""
    name = {".pdf": "_from_pdf", ".docx": "_from_docx",
            ".odt": "_from_odt", ".txt": "_from_txt"}.get(suffix)
    if name is None:
        raise ValueError(
            f"Unsupported resume format {suffix or '(none)'!r}. "
            f"Use one of: {', '.join(IMPORT_SUFFIXES)}."
        )
    return globals()[name]


# Formats extract_resume_text accepts (import formats + the .txt sidecar).
SUPPORTED_SUFFIXES = IMPORT_SUFFIXES + (".txt",)


def extract_resume_text(path: Path) -> str:
    """Extract plain text from a resume file, dispatching on its suffix."""
    path = Path(path)
    return _extractor_for(path.suffix.lower())(path)


def extract_resume_bytes(data: bytes, filename: str) -> str:
    """Extract text from in-memory resume bytes, dispatching on `filename`'s
    suffix (an upload is bytes + a client-supplied name, not a path on disk).

    Writes a temp file with the right suffix so the per-format libraries — which
    open by path — work unchanged."""
    suffix = Path(filename).suffix.lower()
    _extractor_for(suffix)   # reject unsupported formats BEFORE the temp write
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    try:
        return extract_resume_text(tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _main(argv: list[str]) -> int:
    """`python -m pipeline.resume_text <path>` — print extracted text to stdout.

    The Node setup script (setup-profile.mjs) shells out to this for DOCX/ODT so
    there's a single extraction implementation and no extra npm dependencies."""
    if len(argv) != 1:
        print("usage: python -m pipeline.resume_text <resume-file>", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(extract_resume_text(Path(argv[0])))
        return 0
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    line_buffer_stdout()

    raise SystemExit(_main(sys.argv[1:]))
