"""Read-side data access for the local UI.

Parses career-ops/data/applications.md (the tracker) into structured rows and
renders individual report markdown files to HTML. Pure functions, no FastAPI
import — so they're unit-testable without standing up a server.
"""

import re
from pathlib import Path

# Canonical applications.md statuses (mirror of career-ops templates/states.yml
# + merge-tracker.mjs). The kanban board uses these as its columns.
CANONICAL_STATES = [
    "Evaluated", "Applied", "Responded", "Interview", "Offer", "Rejected",
    "Discarded", "SKIP",
]

# Map the aliases merge-tracker accepts (Spanish defaults + variants) onto the
# canonical English states, so a card written as "Evaluada" lands in the
# "Evaluated" column. Lowercased keys.
_STATUS_ALIASES = {
    "evaluada": "Evaluated", "evaluar": "Evaluated", "condicional": "Evaluated",
    "hold": "Evaluated", "verificar": "Evaluated",
    "aplicado": "Applied", "aplicada": "Applied", "enviada": "Applied", "sent": "Applied",
    "respondido": "Responded",
    "entrevista": "Interview",
    "oferta": "Offer",
    "rechazado": "Rejected", "rechazada": "Rejected",
    "descartado": "Discarded", "descartada": "Discarded",
    "cerrada": "Discarded", "cancelada": "Discarded",
    "no aplicar": "SKIP", "no_aplicar": "SKIP", "monitor": "SKIP",
}


def canonical_status(raw: str) -> str:
    """Map a raw status string to its canonical state. Unknown values pass
    through unchanged (so we never silently drop a status we don't recognize)."""
    clean = (raw or "").replace("*", "").strip()
    lower = clean.lower()
    for s in CANONICAL_STATES:
        if s.lower() == lower:
            return s
    return _STATUS_ALIASES.get(lower, clean)

# Canonical applications.md column order (see career-ops AGENTS.md):
#   | # | Date | Company | Role | Score | Status | PDF | Report | Notes |
_COLUMNS = ["num", "date", "company", "role", "score", "status", "pdf", "report", "notes"]

# Tracker-additions TSV column order — note status comes BEFORE score here
# (merge-tracker.mjs swaps them when merging into applications.md):
#   num \t date \t company \t role \t status \t score \t pdf \t report \t notes
_TRACKER_COLUMNS = ["num", "date", "company", "role", "status", "score", "pdf", "report", "notes"]

# Pull the report number + relative path out of the Report cell, which holds a
# markdown link like: [042](reports/042-acme-2026-05-27.md)
_REPORT_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# A markdown table separator row: | --- | :--: | ... |
_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")

# Report link cell: [num](path). Used to re-anchor columns when extra cells
# shift the layout (e.g. LLM writes "Role | Remote" and the pipe splits the cell).
_REPORT_CELL_RE = re.compile(r"^\[[\w\d]+\]\([^)]+\)$")
# Expected 0-indexed position of the Report cell in a well-formed row.
_REPORT_COL_IDX = _COLUMNS.index("report")  # 7


def _realign_cells(cells: list[str]) -> list[str]:
    """Recover correct column mapping when a row has extra cells.

    The LLM occasionally appends context to a role title with a bare pipe
    (e.g. "Software Engineer | Remote"), which merge-tracker.mjs writes
    verbatim into the markdown table and the pipe is interpreted as a cell
    separator. This shifts every subsequent column right.

    Strategy: anchor on the Report link cell (always [num](path)), which is
    identifiable by regex and whose expected position is known. Work backward
    from it to recover score, status, and pdf; everything right of it is notes."""
    for i, c in enumerate(cells):
        if _REPORT_CELL_RE.match(c) and i > _REPORT_COL_IDX:
            before = cells[4:i]        # cells between role and report
            if len(before) < 3:
                break                  # not enough context; leave as-is
            score  = before[-3]
            status = before[-2]
            pdf    = before[-1]
            notes_parts = cells[i + 1:]
            return (
                cells[:4]
                + [score, status, pdf, c]
                + ([" | ".join(notes_parts)] if notes_parts else [""])
            )
    return cells


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
        if len(cells) > len(_COLUMNS):
            cells = _realign_cells(cells)

        row = dict(zip(_COLUMNS, cells))

        # Derive report number + path from the Report link cell.
        m = _REPORT_LINK_RE.search(row.get("report", ""))
        row["report_num"] = m.group(1).strip() if m else ""
        row["report_path"] = m.group(2).strip() if m else ""

        # Parse the leading float out of "4.2/5" → 4.2 for sorting.
        row["score_value"] = _parse_score(row.get("score", ""))
        row["status_canonical"] = canonical_status(row.get("status", ""))

        rows.append(row)

    return rows


def parse_tracker_additions(tracker_dir: Path) -> list[dict]:
    """Parse career-ops/batch/tracker-additions/*.tsv into row dicts.

    These are the raw per-evaluation rows the batch evaluator writes, one TSV
    line per file, before `merge-tracker.mjs` folds them into applications.md.
    We read them as a fallback so the UI shows results even when the merge
    step didn't run (e.g. node missing in the runner, or merge-tracker failed).

    Returns the same dict shape as parse_applications (with report_num,
    report_path, score_value), sorted by tracker number."""
    if not tracker_dir.exists():
        return []
    rows: list[dict] = []
    for f in tracker_dir.glob("*.tsv"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.rstrip("\n")
            if not line.strip():
                continue
            # maxsplit keeps the notes column intact even if it contains tabs.
            cells = line.split("\t", len(_TRACKER_COLUMNS) - 1)
            if len(cells) < len(_TRACKER_COLUMNS):
                continue
            row = dict(zip(_TRACKER_COLUMNS, [c.strip() for c in cells]))
            m = _REPORT_LINK_RE.search(row.get("report", ""))
            row["report_num"] = m.group(1).strip() if m else ""
            row["report_path"] = m.group(2).strip() if m else ""
            row["score_value"] = _parse_score(row.get("score", ""))
            row["status_canonical"] = canonical_status(row.get("status", ""))
            rows.append(row)
    rows.sort(key=lambda r: _safe_int(r.get("num")))
    return rows


def _safe_int(s) -> int:
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return 0


def set_status_in_text(applications_md_text: str, num: str, new_status: str) -> str:
    """Return applications.md text with the Status cell of row `num` replaced.

    Operates at the line level — finds the table row whose first cell (the #
    column) equals `num` and rewrites only its Status cell, leaving every other
    byte untouched. This avoids re-serializing the whole table (which could
    mangle notes containing special chars) and makes the change a minimal diff.

    Returns the text unchanged if the row isn't found."""
    want = str(num).strip()
    out_lines = []
    changed = False
    for line in applications_md_text.splitlines():
        if not changed and line.lstrip().startswith("|"):
            # Split preserving structure: leading/trailing pipes produce empty
            # edge cells we must keep so indices stay aligned on re-join.
            parts = line.split("|")
            # parts[0] is "" (before leading pipe). Data cells start at parts[1].
            cells = [p.strip() for p in parts]
            if len(parts) >= 10 and cells[1] == want and cells[1] not in ("#", ""):
                # Locate the Status cell by anchoring on the Report link, which
                # is always [num](path). Status sits 2 positions before it
                # (…|score|status|pdf|report|…), regardless of extra cells the
                # LLM may have injected (e.g. "Role | Remote").
                status_idx = 6  # fallback: correct for normal 9-cell rows
                for pi, p in enumerate(parts):
                    if _REPORT_CELL_RE.match(p.strip()):
                        status_idx = pi - 2
                        break
                parts[status_idx] = f" {new_status} "
                line = "|".join(parts)
                changed = True
        out_lines.append(line)
    text = "\n".join(out_lines)
    if applications_md_text.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


def load_jobs(career_ops: Path) -> dict:
    """Load tracker rows for the UI, preferring the merged applications.md and
    falling back to raw tracker-additions when it's missing or empty.

    Returns {"rows": [...], "source": "applications" | "tracker-additions" | "none"}
    so the UI can tell the user when it's showing unmerged eval output."""
    apps_md = career_ops / "data" / "applications.md"
    rows = parse_applications(apps_md)
    if rows:
        return {"rows": rows, "source": "applications"}

    tracker_dir = career_ops / "batch" / "tracker-additions"
    rows = parse_tracker_additions(tracker_dir)
    if rows:
        return {"rows": rows, "source": "tracker-additions"}

    return {"rows": [], "source": "none"}


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
