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

_ALIAS_FILE = "tracker-aliases.json"
_alias_cache: dict = {}


def career_ops_dir() -> Path:
    """The career-ops tree: CAREER_OPS_PATH, else the bundled ./career-ops."""
    return Path(os.environ.get("CAREER_OPS_PATH") or
                Path(__file__).resolve().parent.parent / "career-ops")


def header_aliases() -> dict:
    """career-ops' alias table when its checkout is readable, else the fallback.

    Cached per (path, mtime) so a long-running UI picks up a career-ops update
    without a restart, at the cost of one stat per parse."""
    path = career_ops_dir() / _ALIAS_FILE
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return _FALLBACK_ALIASES
    if _alias_cache.get("key") != key:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _FALLBACK_ALIASES
        if not isinstance(loaded, dict):
            return _FALLBACK_ALIASES
        # Union, not replacement: upstream's table is authoritative for what it
        # covers, and ours keeps `url`, which the pipeline writes and upstream
        # has no reason to name.
        merged = {**_FALLBACK_ALIASES,
                  **{str(k).lower(): str(v) for k, v in loaded.items()}}
        _alias_cache.update(key=key, aliases=merged)
    return _alias_cache["aliases"]

# Columns a header must resolve before we will map by name. Deliberately the
# same set as career-ops' REQUIRED_HEADER_FIELDS (tracker-parse.mjs): a row
# qualifies only by labelling the whole schema, because one telltale cell — a
# company genuinely named "Company" — would otherwise turn a data row into
# table furniture. Short of these we keep the positional read rather than
# half-map a table we misread.
_ESSENTIAL = ("num", "company", "role", "score", "status")

# A markdown table separator row: | --- | :--: | ... |
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")


def split_row(line: str) -> list[str]:
    """Split a markdown table row into trimmed cell values, dropping the
    leading/trailing pipe so empty edge columns don't shift positions."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def detect_columns(cells: list[str]) -> list[str] | None:
    """Map a candidate header row to our column keys, or None if it isn't one.

    An unrecognized header keeps its own lowercased label, so its slot still
    lines up for every column after it."""
    aliases = header_aliases()
    labels = [c.replace("*", "").strip().lower() for c in cells]
    keys = [aliases.get(label, label) for label in labels]
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
    for line in text.splitlines():
        if not line.lstrip().startswith("|") or SEPARATOR_RE.match(line.strip()):
            continue
        detected = detect_columns(split_row(line))
        if detected:
            return detected
    return list(CANONICAL_COLUMNS)


def is_header_row(cells: list[str]) -> bool:
    """True when this row is the table's header rather than data.

    Primarily the same detector the column mapping uses, so a localized header
    can't be mapped by one and read as a data row by the other. The second
    clause catches a header too partial to map — an abbreviated or legacy table
    that labels Company and Role but omits, say, Score. Requiring BOTH labels as
    whole cells is what keeps it off real data: it would take a company named
    "Company" holding a role named "Role"."""
    if detect_columns(cells) is not None:
        return True
    labels = {c.replace("*", "").strip().lower() for c in cells}
    return {"company", "role"} <= labels
