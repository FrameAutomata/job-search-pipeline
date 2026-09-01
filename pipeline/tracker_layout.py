"""The one contract for "which cell of a tracker row is which".

career-ops' `data/applications.md` has two supported layouts. The canonical one
is nine columns:

    | # | Date | Company | Role | Score | Status | PDF | Report | Notes |

and the other inserts an optional `Via` column (the agency a role comes through)
after Company, migrated in with `node merge-tracker.mjs --migrate-via`:

    | # | Date | Company | Via | Role | Score | Status | PDF | Report | Notes |

Reading either one positionally works for exactly one of them. Against the other,
every column past the insertion point is off by one — and the cell that lands in
`role` is the agency name, which is the worst possible place for the error:
`company::role` is the key bridge dedups on, handoff's `role_key`, the
résumé-base picker, and the role a tailored résumé is built for. career-ops'
own readers avoid this by mapping columns from the header row
(`tracker-parse.mjs:detectColumns`), and its `tracker-aliases.json` exists
because two of its readers once disagreed about what a header row even is.

So this module is the single answer here too. It is a dependency-free leaf
(stdlib only) on the same terms as `sites.py` and `rowio.py`, because both
`pipeline/bridge.py` (a core stage) and `pipeline/app/data.py` (the UI's
parser) need it and neither should have to reach through the other to get it —
the two previously disagreed about which tables have a readable header at all,
which is the same class of misalignment one layer up.

`header_columns` is deliberately tolerant in one direction only: it returns the
canonical positional order for a table with no readable header, so a
merge-seeded file and a hand-written one both keep working. It never guesses a
*partial* mapping — a header short of the essentials is treated as absent
rather than half-applied.
"""

import json
import os
import re
from pathlib import Path

# The canonical layout, and the fallback for a table with no readable header.
CANONICAL_COLUMNS = [
    "num", "date", "company", "role", "score", "status", "pdf", "report", "notes",
]

# career-ops ships the alias table as DATA — tracker-aliases.json, which it calls
# "the ONE shared table" and loads from both its Node and web readers. So read
# it rather than keeping a copy: a hand-mirror of a file sitting on disk is
# wrong the day it is written. This one was. It shipped missing seven aliases
# career-ops actually emits (`location`, `materials`, `apply link`, the
# follow-up spellings) and inventing eight Spanish ones it never emits.
#
# The baked map below is the FALLBACK, not the source: career-ops is not always
# present (`run-ui.sh --data` points the UI at an extracted artifact with no
# checkout), and a missing alias table must degrade to "read the canonical
# layout positionally", never to a crash.
_FALLBACK_ALIASES = {
    "#": "num", "num": "num",
    "date": "date",
    "company": "company",
    "via": "via",
    "role": "role",
    "score": "score",
    "status": "status",
    "pdf": "pdf",
    "report": "report",
    "notes": "notes",
}

_ROOT = Path(__file__).resolve().parent.parent

# Resolving the path is ~2.4us of pathlib work per call, and the loaders below
# are reached once per tracker ROW. Memoize on the raw env value, the only input.
_dir_cache: dict[str, Path] = {}


def career_ops_dir() -> Path:
    """The career-ops tree: CAREER_OPS_PATH, else the bundled ./career-ops.

    A RELATIVE CAREER_OPS_PATH resolves against the repo root, not the process
    CWD — the same rule orchestrate.py and the UI server already use, and the
    reason this belongs in the leaf rather than in a sixth private copy.
    `.env.example` ships `CAREER_OPS_PATH=./career-ops` and `run-ui.sh` never
    cds, so a CWD-relative read meant that launching the UI from anywhere but the
    repo root found no career-ops at all: the loaders below fell back to their
    baked constants in silence, and reading the contracts career-ops ships — the
    entire point of them — simply did not happen.

    `os.environ.get(VAR) or DEFAULT`, not `get(VAR, DEFAULT)`: the latter returns
    "" for a set-but-empty var, which resolves to the CWD."""
    raw = os.environ.get("CAREER_OPS_PATH") or "career-ops"
    hit = _dir_cache.get(raw)
    if hit is None:
        p = Path(raw)
        hit = p if p.is_absolute() else (_ROOT / p).resolve()
        _dir_cache[raw] = hit
    return hit


_ALIAS_FILE = "tracker-aliases.json"
_contract_cache: dict[str, tuple[int, object]] = {}


def load_contract(relpath: str, parse, fallback):
    """Read a career-ops contract file, cached until its mtime changes.

    The shared shape behind every "career-ops ships this as data" read: resolve
    under the checkout, stat, parse on change, and fall back to a baked constant
    on anything unreadable — an absent checkout (`run-ui.sh --data` points the UI
    at an extracted artifact), a malformed file, a schema change, a missing
    parser dependency. It never raises: a contract we cannot read must degrade to
    the constant, never take the caller down.

    `parse(text)` returns the loaded value, or None to decline it.

    Two performance rules are load-bearing, because callers reach this once per
    tracker ROW, not once per parse. The stat is on a memoized path STRING —
    rebuilding the Path was 76% of the call — and a declined parse caches the
    fallback rather than re-reading a file that will fail again on the next row."""
    key = f"{career_ops_dir()}/{relpath}"
    try:
        mtime = os.stat(key).st_mtime_ns
    except OSError:
        return fallback
    entry = _contract_cache.get(key)
    if entry is not None and entry[0] == mtime:
        return entry[1]
    try:
        with open(key, encoding="utf-8") as fh:
            value = parse(fh.read())
    except Exception:
        value = None
    if value is None:
        value = fallback
    _contract_cache[key] = (mtime, value)
    return value



def _parse_aliases(text: str):
    loaded = json.loads(text)
    if not isinstance(loaded, dict) or not loaded:
        return None
    # Union, not replacement: upstream's table is authoritative for what it
    # covers, and ours keeps `url`, which the pipeline writes and upstream has
    # no reason to name.
    return {**_FALLBACK_ALIASES, **{str(k).lower(): str(v) for k, v in loaded.items()}}


def header_aliases() -> dict:
    """career-ops' alias table when its checkout is readable, else the fallback."""
    return load_contract(_ALIAS_FILE, _parse_aliases, _FALLBACK_ALIASES)


# Columns a header must resolve before we will map by name. Deliberately the
# same set as career-ops' REQUIRED_HEADER_FIELDS (tracker-parse.mjs): a row
# qualifies only by labelling the whole schema, because one telltale cell — a
# company genuinely named "Company" — would otherwise turn a data row into
# table furniture. Short of these we keep the positional read rather than
# half-map a table we misread.
_ESSENTIAL = ("num", "company", "role", "score", "status")

# A markdown table separator row: | --- | :--: | ... |
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")

# The shape merge-tracker.mjs reads as a score (tracker-parse.mjs's
# SCORE_CELL_RE plus its sentinels). One definition: the WRITE side uses it to
# leave a swapped status/score pair alone, the READ side to pick the score out
# of a corrupted row, and two spellings had them disagreeing on `4./5`.
_SCORE_CELL_RE = re.compile(r"^\d+(?:\.\d+)?/5$")
SCORE_SENTINELS = frozenset({"N/A", "DUP", "—", "-"})


def is_score_cell(cell: str) -> bool:
    """True when merge-tracker would read this cell as the score."""
    clean = (cell or "").replace("*", "").strip()
    return bool(_SCORE_CELL_RE.match(clean)) or clean.upper() in SCORE_SENTINELS


def split_row(line: str) -> list[str]:
    """Split a markdown table row into trimmed cell values, dropping the
    leading/trailing pipe so empty edge columns don't shift positions."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _labels(cells: list[str]) -> list[str]:
    return [c.replace("*", "").strip().lower() for c in cells]


def detect_columns(cells: list[str], aliases: dict | None = None) -> list[str] | None:
    """Map a candidate header row to our column keys, or None if it isn't one.

    An unrecognized header keeps its own lowercased label, so its slot still
    lines up for every column after it. `aliases` lets a caller walking many
    rows resolve the table once instead of per row."""
    aliases = aliases if aliases is not None else header_aliases()
    keys = [aliases.get(label, label) for label in _labels(cells)]
    if not all(k in keys for k in _ESSENTIAL):
        return None
    return keys


def header_columns(text: str) -> list[str]:
    """The column layout of a tracker's markdown table — from its header row
    when it has a readable one, else the canonical positional order.

    Scans for a mappable header rather than judging the first table row it
    meets: a tracker file may open with some other table (a legend, a summary),
    and giving up there would silently fall back to the positional order — which
    against a Via-layout tracker is exactly the off-by-one this module exists to
    prevent. Only a row that labels the whole schema qualifies, so an unrelated
    table cannot be mistaken for the tracker's own header."""
    aliases = header_aliases()
    for line in text.splitlines():
        if not line.lstrip().startswith("|") or SEPARATOR_RE.match(line.strip()):
            continue
        detected = detect_columns(split_row(line), aliases)
        if detected:
            return detected
    return list(CANONICAL_COLUMNS)


def is_header_row(cells: list[str], aliases: dict | None = None) -> bool:
    """True when this row is the table's header rather than data.

    A data row's first cell is its `#`, a number — so a numeric lead settles it
    without touching the alias table at all, which is the case that runs once per
    row. Past that: the same detector the column mapping uses, so a localized
    header can't be mapped by one and read as a data row by the other, plus a
    literal Company+Role clause for a header too partial to map (an abbreviated
    or legacy table that omits, say, Score — which `detect_columns` rejects, so
    `header_columns` would fall back to positional and the header would parse as
    a job). Requiring BOTH labels as whole cells is what keeps that off real
    data: it would take a company named "Company" holding a role named "Role"."""
    if not cells:
        return False
    labels = _labels(cells)
    if labels[0].isdigit():
        return False
    if detect_columns(cells, aliases) is not None:
        return True
    return {"company", "role"} <= set(labels)


def data_rows(text: str):
    """Walk a tracker's markdown table: yields (columns, cells) per DATA row.

    One walk for every reader — the UI's parser, bridge's dedup, the merge's
    lost-addition check — because four hand-rolled copies of "skip the pipes,
    skip the separator, skip the header" is how the readers came to disagree
    about which tables even have a header. The layout and the alias table are
    resolved ONCE here rather than per row, which is what keeps a 300-row parse
    from making 600 stat() calls. Callers keep their own width guard, since they
    genuinely differ on how short a row is too short."""
    columns = header_columns(text)
    aliases = header_aliases()
    for line in text.splitlines():
        if not line.lstrip().startswith("|") or SEPARATOR_RE.match(line.strip()):
            continue
        cells = split_row(line)
        if not is_header_row(cells, aliases):
            yield columns, cells


# The `[N]` of a report link. Two places carry one, and merge-tracker reads both
# (`extractReportNum`): the Report cell, then — for a customized tracker with no
# Report column, where its own `buildRow` puts the link instead — a report link
# in Notes. The Notes pattern is scoped to `reports/` so a posting URL sitting in
# the same prose cell cannot be read as a report number.
_REPORT_NUM_RE = re.compile(r"\[(\d+)\]")
_NOTES_REPORT_NUM_RE = re.compile(r"\[(\d+)\]\([^)]*reports/[^)]+\)")


def report_num(report_cell: str, notes_cell: str = "") -> str:
    """The report number a tracker row claims, unpadded, or "".

    Unpadded because this answers "WHICH report is this row", and the pipeline
    mints numbers zero-padded (`f"{n:03d}"`) while other writers emit `str(n)` —
    so `[003]` and `[3]` have to be one key or an identity check on them silently
    fails. Deliberately NOT the same reader as `app/data.py:_report_link`, which
    answers "parse this link into its parts" and must keep the number verbatim
    beside the path it feeds (report rendering, and a renumber that rewrites the
    cell by string). Same distinction as `max_report_num` vs `find_report_file`:
    consistency between them would be a bug, not a tidy-up.

    `[000]` reads as ABSENT, not as report zero, mirroring merge-tracker's own
    `if (reportNum && …)` — its `parseInt` makes 0 falsy, so it never matches on
    it. Numbering starts at 1, and `000` is what `write_job_result` writes when
    it has no number at all; treating that as an identity would let two
    numberless additions at one company be read as the same evaluation."""
    m = (_REPORT_NUM_RE.search(report_cell or "")
         or _NOTES_REPORT_NUM_RE.search(notes_cell or ""))
    return str(int(m.group(1))) if m and int(m.group(1)) else ""
