"""Read-side data access for the local UI.

Parses career-ops/data/applications.md (the tracker) into structured rows and
renders individual report markdown files to HTML. Pure functions, no FastAPI
import — so they're unit-testable without standing up a server.
"""

import json
import re
import threading
from pathlib import Path

from pipeline._batch_common import atomic_write_text, normalize_company

# The UI's pending-status-changes channel: {row key: status-or-record}. Kanban
# drags and the apply stage's auto-submits both write here; /api/jobs overlays
# it onto the rows and the Push button sends it to the cloud tracker. Defined
# once — both server.py and the apply stage go through the accessors below
# rather than re-deriving the path or re-implementing read/modify/write.
#
# A value is EITHER a plain status string (kanban drags — the row is known by
# its num) OR a record {"status", "company", "role"} (apply auto-submits — the
# num came from whatever tracker the apply run read, which may not be the one
# the override is later applied against, so it carries an identity anchor the
# consumers re-resolve to the correct row). See override_status/override_identity.
STATUS_OVERRIDES_FILE = (
    Path(__file__).resolve().parent.parent.parent / ".ui-cache" / "status-overrides.json"
)

# One in-process lock around every read/modify/write of the override file, so
# concurrent server requests (a kanban drag + a push) can't lose each other's
# update. Cross-process torn reads are handled separately by atomic_write_text
# (a reader sees either the whole old file or the whole new one, never a
# half-written one that would parse as {} and wipe the user's pending triage).
_status_lock = threading.Lock()


def load_status_overrides(path: Path | None = None) -> dict:
    """Read the override map. Tolerates a missing/corrupt file (returns {}),
    and guards against a non-dict top-level so a malformed file can't poison
    callers that index it."""
    p = path or STATUS_OVERRIDES_FILE
    try:
        overrides = json.loads(p.read_text(encoding="utf-8"))
        return overrides if isinstance(overrides, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_status_overrides(overrides: dict, path: Path | None = None) -> None:
    """Atomically persist the override map. Best-effort on OSError."""
    p = path or STATUS_OVERRIDES_FILE
    try:
        atomic_write_text(p, json.dumps(overrides, indent=2))
    except OSError:
        pass


def override_status(value) -> str:
    """The status string from an override value, whichever shape it is."""
    if isinstance(value, dict):
        return str(value.get("status", ""))
    return str(value)


def override_identity(value) -> tuple[str, str] | None:
    """The (company, role) identity anchor from an override value, or None for
    a plain-string (num-keyed) override."""
    if isinstance(value, dict):
        company = (value.get("company") or "").strip()
        role = (value.get("role") or "").strip()
        if company or role:
            return company, role
    return None


def clear_status_overrides(keys, path: Path | None = None) -> None:
    """Remove the given keys from the override map (used after a push). Re-reads
    under the lock and removes ONLY those keys, so anything written between the
    push's snapshot and now (e.g. an apply auto-submit) survives — unlike a
    blanket overwrite-with-{} which would silently drop it."""
    p = path or STATUS_OVERRIDES_FILE
    try:
        with _status_lock:
            overrides = load_status_overrides(p)
            for k in keys:
                overrides.pop(str(k), None)
            save_status_overrides(overrides, p)
    except OSError:
        pass


def override_matches_row(value, row: dict) -> bool:
    """Whether an identity-anchored override targets this tracker row (matched
    by normalized company, and role when the anchor carries one). Plain-string
    overrides are matched by num at the call site, not here."""
    identity = override_identity(value)
    if not identity:
        return False
    company, role = identity
    if normalize_company(row.get("company", "")) != normalize_company(company):
        return False
    want_role = normalize_company(role)
    return not want_role or normalize_company(row.get("role", "")) == want_role


def record_status_override(num: str, status: str, path: Path | None = None,
                           *, company: str | None = None, role: str | None = None) -> None:
    """Record a pending status change in the UI's override file — the same
    channel a kanban drag uses. Best-effort: a failure here must never break
    the caller (the tracker-file write is the primary record).

    Pass company/role when the num's tracker identity is uncertain (the apply
    stage, whose num comes from a refreshed-or-local tracker that may use
    different numbering than the one this override is later applied against):
    the value then carries an identity anchor so consumers mark the RIGHT row,
    not whichever row coincidentally shares the num."""
    p = path or STATUS_OVERRIDES_FILE
    value: object = status
    if company or role:
        value = {"status": status, "company": company or "", "role": role or ""}
    try:
        with _status_lock:
            overrides = load_status_overrides(p)
            overrides[str(num)] = value
            save_status_overrides(overrides, p)
    except OSError:
        pass


def resolve_num_by_identity(applications_md_text: str, company: str, role: str) -> str | None:
    """Find the tracker row matching company (+ role when given) and return its
    num. Used to re-anchor an identity-carrying override onto the correct row of
    whatever tracker it's being applied to. Matches on the first four cells
    (num, date, company, role), which are stable even when a stray pipe in the
    role shifts later columns. Returns None when no row matches."""
    want_company = normalize_company(company)
    if not want_company:
        return None
    want_role = normalize_company(role)
    for line in applications_md_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        if _SEPARATOR_RE.match(line.strip()):
            continue
        cells = _split_row(line)
        if len(cells) < len(_COLUMNS):
            continue
        if cells[0].lower() in ("#", "num"):
            continue
        if normalize_company(cells[2]) == want_company and (
            not want_role or normalize_company(cells[3]) == want_role
        ):
            return cells[0].strip()
    return None


def resolve_overrides_for_push(applications_md_text: str, overrides: dict):
    """Build the cloud push payload from the pending overrides, applied onto the
    base tracker.

    Returns (new_text, cloud_payload, unresolved):
      - new_text: the base with each applied override's Status cell rewritten.
      - cloud_payload: {num: status} for edit-tracker.yml (always num-keyed).
      - unresolved: keys of identity-anchored overrides whose company/role isn't
        in THIS base. Those are NOT applied and NOT dispatched — falling back to
        the (foreign) num would mark a different company that merely shares it,
        and the caller must keep (not clear) them so they reach the right row on
        a later push once the company appears.
    """
    new_text = applications_md_text
    cloud_payload: dict[str, str] = {}
    unresolved: list[str] = []
    for key, value in overrides.items():
        status = override_status(value)
        identity = override_identity(value)
        if identity:
            num = resolve_num_by_identity(new_text, *identity)
            if num is None:
                unresolved.append(key)
                continue
        else:
            num = key
        new_text = set_status_in_text(new_text, num, status)
        cloud_payload[num] = status
    return new_text, cloud_payload, unresolved


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
# Score cell: X.X/5
_SCORE_CELL_RE = re.compile(r"^\d+\.?\d*/5$")


def _realign_cells(cells: list[str]) -> list[str]:
    """Recover correct column mapping when a row has extra cells.

    The LLM occasionally appends context to a role title with a bare pipe
    (e.g. "Software Engineer | Remote"), which merge-tracker.mjs writes
    verbatim into the markdown table and the pipe is interpreted as a cell
    separator. This shifts every subsequent column right.

    Strategy: anchor on the Report link cell (always [num](path)), scan the
    middle cells for a recognisable score (X.X/5) and a recognisable status
    (canonical value lookup), then reconstruct a clean 9-cell row. Defaults
    status to "Evaluated" when none of the middle cells is a canonical value —
    which happens for compound-corrupted rows that have accumulated multiple
    extra score cells from re-evaluations."""
    for i, c in enumerate(cells):
        if _REPORT_CELL_RE.match(c) and i > _REPORT_COL_IDX:
            before = cells[4:i]   # cells between role and report
            score = next((v for v in before if _SCORE_CELL_RE.match(v)), "")
            status = next(
                (v for v in before
                 if not _SCORE_CELL_RE.match(v)
                 and canonical_status(v) in CANONICAL_STATES),
                "Evaluated",      # safe default — batch rows always start here
            )
            notes_parts = cells[i + 1:]
            return (
                cells[:4]
                + [score, status, "null", c]
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
