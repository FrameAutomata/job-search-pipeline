"""Read-side data access for the local UI.

Parses career-ops/data/applications.md (the tracker) into structured rows and
renders individual report markdown files to HTML. Pure functions, no FastAPI
import — so they're unit-testable without standing up a server.
"""

import re
from pathlib import Path

# Canonical applications.md column order (see career-ops AGENTS.md):
#   | # | Date | Company | Role | Score | Status | PDF | Report | Notes |
_COLUMNS = ["num", "date", "company", "role", "score", "status", "pdf", "report", "notes"]

# Pull the report number + relative path out of the Report cell, which holds a
# markdown link like: [042](reports/042-acme-2026-05-27.md)
_REPORT_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# A markdown table separator row: | --- | :--: | ... |
_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")


def _split_row(line: str) -> list[str]:
    """Split a markdown table row into trimmed cell values, dropping the
    leading/trailing pipe so empty edge columns don't shift positions."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_applications(applications_md: Path) -> list[dict]:
    """Parse applications.md into a list of row dicts.

    Each dict has the _COLUMNS keys plus a derived `report_num` and
    `report_path` extracted from the Report cell's markdown link, and a
    `score_value` float (parsed from the "X.X/5" Score cell, or None).
    Returns [] if the file is missing or has no data rows."""
    if not applications_md.exists():
        return []

    rows: list[dict] = []
    for line in applications_md.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        if _SEPARATOR_RE.match(line.strip()):
            continue
        cells = _split_row(line)
        if len(cells) < len(_COLUMNS):
            continue
        # Skip the header row.
        if cells[0].lower() in ("#", "num") and cells[1].lower() == "date":
            continue

        row = dict(zip(_COLUMNS, cells))

        # Derive report number + path from the Report link cell.
        m = _REPORT_LINK_RE.search(row.get("report", ""))
        row["report_num"] = m.group(1).strip() if m else ""
        row["report_path"] = m.group(2).strip() if m else ""

        # Parse the leading float out of "4.2/5" → 4.2 for sorting.
        row["score_value"] = _parse_score(row.get("score", ""))

        rows.append(row)

    return rows


def _parse_score(score_cell: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", score_cell or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def find_report_file(reports_dir: Path, report_num: str) -> Path | None:
    """Locate a report file by its number. Reports are named
    `{num}-{company-slug}-{date}.md`; the tracker stores the zero-padded num
    (e.g. "042"). Match on the leading numeric segment, tolerating padding
    differences (42 vs 042)."""
    if not report_num or not reports_dir.exists():
        return None
    try:
        wanted = int(report_num)
    except ValueError:
        return None
    for f in reports_dir.glob("*.md"):
        m = re.match(r"^(\d+)-", f.name)
        if m and int(m.group(1)) == wanted:
            return f
    return None


def render_report_html(report_path: Path) -> str:
    """Render a report's markdown to HTML. Falls back to a <pre> block if the
    `markdown` package isn't installed (keeps the UI usable without the
    optional dependency, just unstyled)."""
    text = report_path.read_text(encoding="utf-8")
    try:
        import markdown as _md
    except ImportError:
        import html as _html
        return f"<pre>{_html.escape(text)}</pre>"
    return _md.markdown(text, extensions=["tables", "fenced_code"])
