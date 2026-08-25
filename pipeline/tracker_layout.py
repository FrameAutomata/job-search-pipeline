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

import re

# The canonical layout, and the fallback for a table with no readable header.
CANONICAL_COLUMNS = [
    "num", "date", "company", "role", "score", "status", "pdf", "report", "notes",
]

# Header labels -> our column keys. Mirrors career-ops' tracker-aliases.json;
# the Spanish spellings are the ones its localized modes emit.
HEADER_ALIASES = {
    "#": "num", "num": "num", "n": "num",
    "date": "date", "fecha": "date",
    "company": "company", "empresa": "company",
    "via": "via",
    "role": "role", "puesto": "role", "rol": "role",
    "score": "score", "puntuacion": "score", "puntuación": "score",
    "status": "status", "estado": "status",
    "pdf": "pdf",
    "report": "report", "informe": "report",
    "notes": "notes", "notas": "notes",
    "url": "url",
}

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
    keys = [HEADER_ALIASES.get(c.replace("*", "").strip().lower(),
                               c.replace("*", "").strip().lower())
            for c in cells]
    if not all(k in keys for k in _ESSENTIAL):
        return None
    return keys


def header_columns(text: str) -> list[str]:
    """The column layout of a tracker's markdown table — from its header row
    when it has a readable one, else the canonical positional order."""
    for line in text.splitlines():
        if not line.lstrip().startswith("|") or SEPARATOR_RE.match(line.strip()):
            continue
        return detect_columns(split_row(line)) or list(CANONICAL_COLUMNS)
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
