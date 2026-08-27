"""Read-side data access for the local UI.

Parses career-ops/data/applications.md (the tracker) into structured rows and
renders individual report markdown files to HTML. Pure functions, no FastAPI
import — so they're unit-testable without standing up a server.
"""

import json
import re
import shutil
import threading
from pathlib import Path

from pipeline._batch_common import (
    atomic_write_text,
    normalize_company,
    read_url_set,
    score_value,
)
from pipeline import tracker_layout
from pipeline.tracker_layout import (
    CANONICAL_COLUMNS,
    SEPARATOR_RE as _SEPARATOR_RE,
    is_score_cell,
    header_columns as _header_columns,
)

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


def _override_value(status: str, company: str | None, role: str | None):
    """An override-map value: a bare status string, or a {status, company, role}
    record carrying an identity anchor when company/role are known — so consumers
    re-resolve the right row even if the num differs across trackers."""
    if company or role:
        return {"status": status, "company": company or "", "role": role or ""}
    return status


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
    try:
        with _status_lock:
            overrides = load_status_overrides(p)
            overrides[str(num)] = _override_value(status, company, role)
            save_status_overrides(overrides, p)
    except OSError:
        pass


def record_status_changes(applications_md: Path, changes) -> None:
    """Batch dual-write: apply many (num, status, company, role) edits with a
    SINGLE read-modify-write of the tracker file and a SINGLE overrides-file
    update. The liveness re-check uses this to Discard many roles at once;
    record_status_change is the one-row case. Entries with an empty num are
    skipped; an all-empty/empty list is a no-op.

    Writes BOTH places that matter:
      1. The tracker file's Status cells (minimal in-place edits), so the file
         on disk / a local run reflects them.
      2. The UI's identity-anchored override channel, so the UI shows them
         immediately and the Push button carries them to the cloud tracker.

    The override carries company/role, not just num: the num came from whatever
    tracker the caller read (a refreshed cloud artifact, or the local copy), and
    the cloud tracker mints numbers independently — anchoring on identity marks
    the row actually acted on, never a different company sharing the num."""
    items = [(str(n).strip(), s, c, r) for (n, s, c, r) in changes if str(n).strip()]
    if not items:
        return
    applications_md = Path(applications_md)
    if applications_md.exists():
        text = applications_md.read_text(encoding="utf-8")
        new = text
        for num, status, _company, _role in items:
            new = set_status_in_text(new, num, status)
        if new != text:
            atomic_write_text(applications_md, new)
    try:
        with _status_lock:
            overrides = load_status_overrides()
            for num, status, company, role in items:
                overrides[num] = _override_value(status, company, role)
            save_status_overrides(overrides)
    except OSError:
        pass


def record_status_change(applications_md: Path, num: str, status: str, *,
                         company: str | None = None, role: str | None = None) -> None:
    """Record `status` for a single tracker row `num` (tracker cell + override).
    One-row convenience over record_status_changes; no-ops on an empty num. Used
    by the apply stage (submit -> Applied, closed posting -> Discarded)."""
    record_status_changes(applications_md, [(num, status, company, role)])


def resolve_num_by_identity(applications_md_text: str, company: str, role: str,
                            columns: list[str] | None = None) -> str | None:
    """Find the tracker row matching company (+ role when given) and return its
    num. Used to re-anchor an identity-carrying override onto the correct row of
    whatever tracker it's being applied to. Matches Company and Role through the
    table's own header mapping, and returns the num from its own column — the
    value is dispatched to edit-tracker.yml as the cloud row key, and
    detect_columns imposes no column ORDER. Returns None when no row matches."""
    want_company = normalize_company(company)
    if not want_company:
        return None
    want_role = normalize_company(role)
    # Read Company/Role by name, not by slot — a tracker migrated to the Via
    # layout puts the agency where Role used to sit. `columns` lets a caller
    # resolving many identities against one tracker derive the layout once.
    for columns, cells in tracker_layout.data_rows(applications_md_text):
        company_idx, role_idx = columns.index("company"), columns.index("role")
        if len(cells) < len(columns):
            continue
        if normalize_company(cells[company_idx]) == want_company and (
            not want_role or normalize_company(cells[role_idx]) == want_role
        ):
            # The num by name too — detect_columns imposes no column ORDER, so
            # cell 0 is not guaranteed to be `#`, and this value is dispatched
            # to edit-tracker.yml as the cloud row key.
            return cells[columns.index("num")].strip()
    return None


def resolve_overrides_for_push(applications_md_text: str, overrides: dict,
                               *, build_text: bool = True):
    """Build the cloud push payload from the pending overrides, applied onto the
    base tracker.

    Returns (new_text, cloud_payload, unresolved):
      - new_text: the base with each applied override's Status cell rewritten
        (identical to the input when build_text is False).
      - cloud_payload: {num: status} for edit-tracker.yml (always num-keyed).
      - unresolved: keys of identity-anchored overrides whose company/role isn't
        in THIS base. Those are NOT applied and NOT dispatched — falling back to
        the (foreign) num would mark a different company that merely shares it,
        and the caller must keep (not clear) them so they reach the right row on
        a later push once the company appears.

    build_text=False skips rewriting the (potentially large) merged tracker text
    when the caller won't persist it — e.g. a refreshed-artifact push only needs
    cloud_payload (the artifact copy is transient and isn't written). Status-cell
    edits don't affect identity resolution, so the payload is unchanged.
    """
    new_text = applications_md_text
    cloud_payload: dict[str, str] = {}
    unresolved: list[str] = []
    # The layout can't change under us — set_status_in_text only rewrites Status
    # cells — so derive it once rather than per override.
    columns = _header_columns(applications_md_text)
    for key, value in overrides.items():
        status = override_status(value)
        identity = override_identity(value)
        if identity:
            num = resolve_num_by_identity(new_text, *identity, columns=columns)
            if num is None:
                unresolved.append(key)
                continue
        else:
            num = key
        if build_text:
            new_text = set_status_in_text(new_text, num, status)
        cloud_payload[num] = status
    return new_text, cloud_payload, unresolved


# Canonical applications.md statuses (mirror of career-ops templates/states.yml
# + merge-tracker.mjs). The kanban board uses these as its columns.
# "Hired" is career-ops' 9th state (terminal — "offer accepted, job landed").
# It postdates this list, and an unknown status is NOT inert: the board falls
# back to `STATES.includes(s) ? s : "Evaluated"`, so a landed job rendered in
# the Evaluated column and the report pane's dropdown pre-selected Evaluated
# for it — one save away from downgrading it.
CANONICAL_STATES = [
    "Evaluated", "Applied", "Responded", "Interview", "Offer", "Rejected",
    "Discarded", "SKIP", "Hired",
]

# Map the aliases merge-tracker accepts (Spanish defaults + variants) onto the
# canonical English states, so a card written as "Evaluada" lands in the
# "Evaluated" column. Lowercased keys. Mirrors templates/states.yml.
_STATUS_ALIASES = {
    "evaluada": "Evaluated", "evaluar": "Evaluated", "condicional": "Evaluated",
    "hold": "Evaluated", "verificar": "Evaluated",
    "degerlendirildi": "Evaluated", "değerlendirildi": "Evaluated",
    "aplicado": "Applied", "aplicada": "Applied", "enviada": "Applied", "sent": "Applied",
    "basvuruldu": "Applied", "başvuruldu": "Applied",
    "respondido": "Responded",
    "yanit verildi": "Responded", "yanıt verildi": "Responded",
    "yanit_verildi": "Responded", "yanıt_verildi": "Responded",
    "entrevista": "Interview", "mulakat": "Interview", "mülakat": "Interview",
    "oferta": "Offer", "teklif": "Offer",
    "rechazado": "Rejected", "rechazada": "Rejected", "reddedildi": "Rejected",
    "descartado": "Discarded", "descartada": "Discarded",
    "cerrada": "Discarded", "cancelada": "Discarded",
    "iptal edildi": "Discarded", "iptal_edildi": "Discarded",
    "ıptal edildi": "Discarded", "ıptal_edildi": "Discarded",
    "no aplicar": "SKIP", "no_aplicar": "SKIP", "monitor": "SKIP",
    "geo blocker": "SKIP", "geo_blocker": "SKIP",
    "uygun degil": "SKIP", "uygun değil": "SKIP",
    "uygun_degil": "SKIP", "uygun_değil": "SKIP",
    "contratado": "Hired", "contratada": "Hired",
    "accepted": "Hired", "accept": "Hired",
    "kabul edildi": "Hired", "kabul_edildi": "Hired",
    "ise alindi": "Hired", "işe alındı": "Hired", "işe alindi": "Hired",
}


# career-ops ships the status vocabulary as DATA — templates/states.yml, which
# it calls the source of truth for "career-ops (writer) and dashboard (reader)".
# Read it rather than keeping a copy: the constant above went stale the moment
# upstream added `Hired`, and an unknown status is not inert — the board falls
# back to Evaluated, so a landed job rendered in the wrong column and the report
# pane was one save away from downgrading it.
#
# The constants above are the FALLBACK, not the source. career-ops is not always
# present (`run-ui.sh --data` points the UI at an extracted artifact with no
# checkout), and pyyaml — though a core requirement — is imported lazily so a
# UI-only install missing it degrades to the baked list instead of failing to
# start.
_STATES_FILE = "templates/states.yml"


def _parse_states(text: str):
    """(labels, alias->label) from states.yml, or None to decline the file."""
    import yaml   # lazy: a UI-only install without it degrades to the fallback
    entries = (yaml.safe_load(text) or {}).get("states") or []
    labels = [str(e["label"]) for e in entries if e.get("label")]
    if not labels:
        return None
    aliases = {str(a).strip().lower(): str(e.get("label") or "")
               for e in entries for a in (e.get("aliases") or [])}
    # Union both halves. Our alias map carries merge-tracker spellings the yaml
    # does not, and our labels must survive too: `canonical_status` can return a
    # baked label through a baked alias, and every consumer that compares against
    # a literal ("Evaluated" in handoff, "Discarded" in the push) would then be
    # testing against a vocabulary `canonical_states()` no longer contains.
    return ([*labels, *(s for s in CANONICAL_STATES if s not in labels)],
            {**_STATUS_ALIASES, **aliases})


def canonical_states() -> list[str]:
    """The status vocabulary in force — career-ops' when readable, else ours."""
    return _load_states()[0]


def _load_states() -> tuple[list[str], dict]:
    """career-ops ships the status vocabulary as DATA (templates/states.yml,
    "the source of truth for career-ops (writer) and dashboard (reader)"), so
    read it. The constants above are the fallback for an absent or unreadable
    checkout — see tracker_layout.load_contract, which owns that whole policy."""
    return tracker_layout.load_contract(
        _STATES_FILE, _parse_states, (CANONICAL_STATES, _STATUS_ALIASES))


def canonical_status(raw: str, vocabulary: tuple | None = None) -> str:
    """Map a raw status string to its canonical state. Unknown values pass
    through unchanged (so we never silently drop a status we don't recognize)."""
    clean = (raw or "").replace("*", "").strip()
    lower = clean.lower()
    # `vocabulary` lets a caller walking many rows resolve the contract once
    # instead of per row — the same hoist tracker_layout.data_rows does for the
    # column layout and the alias table.
    states, aliases = vocabulary if vocabulary is not None else _load_states()
    for s in states:
        if s.lower() == lower:
            return s
    return aliases.get(lower, clean)

# Canonical applications.md column order (see career-ops AGENTS.md):
#   | # | Date | Company | Role | Score | Status | PDF | Report | Notes |
# The layout a tracker actually uses is read from its header — see
# pipeline/tracker_layout.py, which both this module and bridge.py share.
_COLUMNS = list(CANONICAL_COLUMNS)

# Tracker-additions TSV column order — note status comes BEFORE score here
# (merge-tracker.mjs swaps them when merging into applications.md):
#   num \t date \t company \t role \t status \t score \t pdf \t report \t notes
_TRACKER_COLUMNS = ["num", "date", "company", "role", "status", "score", "pdf", "report", "notes"]

# Pull the report number + relative path out of the Report cell, which holds a
# markdown link like: [042](reports/042-acme-2026-05-27.md)
_REPORT_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# merge-tracker.mjs now normalizes the Report link relative to the tracker FILE
# (tracker-links.mjs:normalizeReportLink), and the pipeline seeds the tracker at
# career-ops/data/applications.md — so a link the pipeline emitted as
# `reports/042-x.md` comes back as `../reports/042-x.md`. The older
# merge-tracker copied the cell verbatim, so both shapes are now in circulation
# within one file. Every consumer of `report_path` (cover_letters, resume_tailor,
# resume_content, via handoff) resolves it as `career_ops / report_path`, which
# the `../` form escapes — and `read_text` returns "" on the miss, so tailoring
# and cover letters silently lose the evaluation report's proof points. Strip the
# ascent here, at the single point both parsers extract it, so the stored value
# is career-ops-relative whichever shape the file holds.
_REPORT_ASCENT_RE = re.compile(r"^(?:\.\./)+")


def _report_link(cell: str) -> tuple[str, str]:
    """(report_num, career-ops-relative report_path) from a Report cell."""
    m = _REPORT_LINK_RE.search(cell or "")
    if not m:
        return "", ""
    return m.group(1).strip(), _REPORT_ASCENT_RE.sub("", m.group(2).strip())

# Report link cell: [num](path). Used to re-anchor columns when extra cells
# shift the layout (e.g. LLM writes "Role | Remote" and the pipe splits the cell).
_REPORT_CELL_RE = re.compile(r"^\[[\w\d]+\]\([^)]+\)$")


def _realign_cells(cells: list[str], columns: list[str]) -> list[str]:
    """Recover correct column mapping when a row has extra cells.

    The LLM occasionally appends context to a role title with a bare pipe
    (e.g. "Software Engineer | Remote"), which merge-tracker.mjs writes
    verbatim into the markdown table and the pipe is interpreted as a cell
    separator. This shifts every subsequent column right.

    Strategy: anchor on the Report link cell (always [num](path)), scan the
    middle cells for a recognisable score (X.X/5) and a recognisable status
    (canonical value lookup), then reconstruct a clean row. Defaults status to
    "Evaluated" when none of the middle cells is a canonical value — which
    happens for compound-corrupted rows that have accumulated multiple extra
    score cells from re-evaluations.

    `columns` is the table's own layout, so a tracker carrying the optional Via
    column keeps its leading identity cells intact — the head to preserve and
    the Report anchor's expected slot both come from it, not from a hardcoded
    9-column assumption."""
    # The whole method is "anchor on the Report link, rebuild around it", so a
    # layout with no Report column has nothing to anchor on. detect_columns only
    # demands num/company/role/score/status (career-ops' own required set), so
    # such a layout is reachable — bail out rather than raise and take the whole
    # parse, and with it the UI's board, down with it.
    if "report" not in columns:
        return cells
    report_idx = columns.index("report")
    head = columns.index("role") + 1      # identity cells to keep verbatim
    for i, c in enumerate(cells):
        if _REPORT_CELL_RE.match(c) and i > report_idx:
            before = cells[head:i]   # cells between role and report
            score = next((v for v in before if is_score_cell(v)), "")
            known = canonical_states()
            status = next(
                (v for v in before
                 if not is_score_cell(v)
                 and canonical_status(v) in known),
                "Evaluated",      # safe default — batch rows always start here
            )
            # Rebuild the middle from the table's OWN column names, so a layout
            # that orders or omits them differently doesn't get the canonical
            # score/status/pdf tail stamped onto it.
            recovered = {"score": score, "status": status, "pdf": "null", "report": c}
            middle = [recovered.get(k, "") for k in columns[head:report_idx + 1]]
            notes_parts = cells[i + 1:]
            return (
                cells[:head]
                + middle
                + ([" | ".join(notes_parts)] if notes_parts else [""])
            )
    return cells



# A bare posting URL inside a tracker row's Notes cell (the link the bridge
# stores there). Trailing sentence punctuation is trimmed so "...a/b, fits"
# yields the clean URL.
_NOTES_URL_RE = re.compile(r"https?://\S+")


def extract_url(notes: str) -> str:
    """The first URL in a Notes cell, or "" if none. Shared by the apply queue
    (pick a posting to apply to) and the liveness re-check (re-fetch it)."""
    m = _NOTES_URL_RE.search(notes or "")
    return m.group(0).rstrip(".,);]") if m else ""


# Sibling of applications.md, written by pipeline/bridge.py — the URLs that came
# from an easy_apply search pass (Indeed SmartApply / LinkedIn Easy Apply).
_EASY_APPLY_URLS_FILE = "easy-apply-urls.txt"


def load_easy_apply_urls(data_dir: Path) -> set[str]:
    """The set of easy-apply URLs recorded by the bridge stage (empty if none)."""
    return read_url_set(data_dir / _EASY_APPLY_URLS_FILE)


def parse_applications(applications_md: Path) -> list[dict]:
    """Parse applications.md into a list of row dicts.

    Each dict has the _COLUMNS keys plus a derived `report_num` and
    `report_path` extracted from the Report cell's markdown link, a
    `score_value` float (parsed from the "X.X/5" Score cell, or None), and an
    `easy_apply` bool (its Notes URL is in the sibling easy-apply-urls.txt).
    Returns [] if the file is missing or has no data rows."""
    if not applications_md.exists():
        return []
    return parse_applications_text(
        applications_md.read_text(encoding="utf-8"),
        easy_apply_urls=load_easy_apply_urls(applications_md.parent),
    )


def parse_applications_text(text: str, *, easy_apply_urls: set[str] | None = None) -> list[dict]:
    """Parse applications.md *text* into row dicts (see parse_applications for the
    shape). Split out so the offline-tracker merge can parse in-memory cloud and
    local trackers without a file. `easy_apply_urls` tags the easy_apply flag;
    omitted for callers (like the merge) that don't need it."""
    easy_apply_urls = easy_apply_urls or set()
    vocabulary = _load_states()
    rows: list[dict] = []
    for columns, cells in tracker_layout.data_rows(text):
        if len(cells) < len(columns):
            continue
        if len(cells) > len(columns):
            cells = _realign_cells(cells, columns)

        row = dict(zip(columns, cells))

        # Derive report number + path from the Report link cell.
        row["report_num"], row["report_path"] = _report_link(row.get("report", ""))

        # Parse the leading float out of "4.2/5" → 4.2 for sorting.
        row["score_value"] = score_value(row.get("score", ""))
        row["status_canonical"] = canonical_status(row.get("status", ""), vocabulary)
        row["easy_apply"] = extract_url(row.get("notes", "")) in easy_apply_urls

        rows.append(row)

    return rows


def _row_identity(row: dict) -> str:
    """Cross-tracker identity key — normalized company::role, the same key the
    bridge and merge-tracker.mjs dedup on. Cloud and local trackers number rows
    independently, so the number can't be the identity."""
    return f"{normalize_company(row.get('company', ''))}::{normalize_company(row.get('role', ''))}"


def _report_int(value) -> int | None:
    """A report number as an int, or None if it isn't one."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _report_ints(values) -> set[int]:
    """Report numbers as a set of ints, dropping anything non-numeric.

    Numbers, not strings, because the pipeline mints them zero-padded
    (`f"{n:03d}"` in assign_job_numbers) while a renumber here emits `str(n)`.
    A string membership test therefore reads "43" as unclaimed against a cloud
    that holds "043" — so the very number this function's callers assign would
    be invisible to the next merge's collision check."""
    return {n for n in (_report_int(v) for v in values) if n is not None}


def _row_report_nums(rows: list[dict]) -> set[int]:
    """The report numbers a set of tracker rows link to."""
    return _report_ints(r["report_num"] for r in rows if r.get("report_num"))


def reconcile_trackers(
    cloud_md: str, local_md: str, cloud_report_nums: set[str],
    local_report_nums: set[str] = frozenset(),
) -> tuple[str, list[tuple[str, str]]]:
    """Merge the pulled cloud tracker with the durable local tracker.

    Cloud wins for any role present in both (it's the canonical pipeline output),
    so cloud rows — and their numbers and report links — are kept verbatim. Rows
    only in local (offline `Run local` results) are preserved, renumbered to sit
    after the cloud max so row numbers stay unique. A local-only row whose report
    number collides with a cloud report is given a fresh number (max(used)+1) and
    its Report link rewritten; the (old filename, new filename) pair is returned
    so the caller can move that one file. Triage edits live in the overrides
    overlay, not here.

    `cloud_report_nums` is the set of report numbers whose FILES came down in
    the artifact; the cloud tracker's own rows are consulted as well, since the
    artifact is a per-run delta and need not carry a file for every number the
    cloud has minted. `local_report_nums` is the set of report numbers that
    exist as files in the local reports dir — passed so a renumber can't target
    an existing local file (including one orphaned by a cloud-shared row).
    Returns (merged_md, renames)."""
    cloud_rows = parse_applications_text(cloud_md)
    local_rows = parse_applications_text(local_md)

    cloud_ids = {_row_identity(r) for r in cloud_rows}
    local_only = [r for r in local_rows if _row_identity(r) not in cloud_ids]
    if not local_only:
        return cloud_md, []

    # Which report numbers belong to the cloud. The artifact's report FILES are
    # no longer the whole answer: since issue #129 the daily artifact carries
    # only the reports that run minted, so a cloud report from a Refresh the
    # user missed (artifacts expire after a week) would look unclaimed, and a
    # local-only row holding that number would keep it — leaving the cloud row's
    # `[42](reports/42-….md)` link pointing at the local report. The cloud
    # tracker names every number it has ever used, so ask it too.
    cloud_claimed = _report_ints(cloud_report_nums) | _row_report_nums(cloud_rows)

    next_num = max(_report_ints(r.get("num") for r in cloud_rows), default=0)
    # A renumbered report must avoid every number already in use: cloud reports
    # (about to be copied in), every report referenced by a local row, and every
    # report file on local disk (orphans included). Seed the running max from all
    # three so an assigned number can't collide with any of them.
    reserved = cloud_claimed | _report_ints(local_report_nums) | _row_report_nums(local_rows)
    next_rep = max(reserved, default=0)

    # Emit rows in the CLOUD table's own layout, not the canonical order: rows
    # are appended to the cloud tracker, and a cloud tracker migrated to the Via
    # layout is 10 columns wide. A 9-cell row there is short, so the very next
    # parse drops it on the minimum-cell-count check — the local-only evaluation
    # would vanish from the UI and the handoff while its report sat on disk.
    columns = _header_columns(cloud_md)
    num_idx = columns.index("num")
    report_idx = columns.index("report") if "report" in columns else None

    renames: list[tuple[str, str]] = []
    new_lines: list[str] = []
    for row in local_only:
        next_num += 1
        cells = [row.get(c, "") for c in columns]
        cells[num_idx] = str(next_num)

        old_rep = row.get("report_num", "")
        if _report_int(old_rep) in cloud_claimed and report_idx is not None:
            next_rep += 1
            new_rep = str(next_rep)
            cells[report_idx] = _renumber_report_cell(cells[report_idx], old_rep, new_rep)
            # Name the file, not the number. A number can match two files in a
            # local reports dir — the cloud report a past Refresh copied in, and
            # a local-only one that happens to share it — and only this row's is
            # meant to move. Read the pair back out of the cell just rewritten,
            # so the rename and the link the tracker now carries cannot disagree.
            renames.append((Path(row["report_path"]).name,
                            Path(_report_link(cells[report_idx])[1]).name))
        new_lines.append("| " + " | ".join(cells) + " |")

    return _append_rows(cloud_md, new_lines), renames


def _renumber_report_cell(cell: str, old: str, new: str) -> str:
    """Rewrite a `[old](reports/old-slug.md)` Report cell to use `new`, in both
    the link text and the filename prefix."""
    def repl(m: re.Match) -> str:
        text, path = m.group(1).strip(), m.group(2).strip()
        if text == old:
            text = new
        # Both filename shapes _REPORT_FILE_RE accepts: `NNN-slug.md` and the
        # slug-less `NNN.md`. Missing the second one is not cosmetic — the
        # rename pair is read back out of this cell, so a path left un-rewritten
        # produces a (same, same) self-rename and the incoming cloud file
        # overwrites the local-only report the renumber existed to protect.
        path = re.sub(rf"(^|/){re.escape(old)}(-|\.md$)", rf"\g<1>{new}\g<2>", path)
        return f"[{text}]({path})"
    return _REPORT_LINK_RE.sub(repl, cell)


def _append_rows(md: str, new_lines: list[str]) -> str:
    """Insert table-row lines right after the last existing table row, so they
    land inside the markdown table rather than after any trailing prose."""
    lines = md.splitlines()
    last = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and not _SEPARATOR_RE.match(s):
            last = i
    if last == -1:  # no table found — just append
        return md.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
    lines[last + 1:last + 1] = new_lines
    return "\n".join(lines) + ("\n" if md.endswith("\n") else "")


_REPORT_FILE_RE = re.compile(r"^(\d+)(?:-.*)?\.md$")


def _report_numbers(reports_dir: Path) -> set[str]:
    """Leading report numbers of `NNN-slug.md` / `NNN.md` files in a reports dir."""
    if not reports_dir.is_dir():
        return set()
    nums = set()
    for f in reports_dir.glob("*.md"):
        m = _REPORT_FILE_RE.match(f.name)
        if m:
            nums.add(m.group(1))
    return nums


def sync_pulled_tracker(artifact_dir: Path, local_dir: Path) -> dict:
    """Merge a downloaded pipeline artifact into the durable local career-ops.

    The offline-first core: makes local always hold the latest cloud tracker
    while preserving local-only rows (offline `Run local` results). Steps, in
    order — the order matters so a report-number collision can't clobber a
    local-only report:
      1. reconcile applications.md (cloud wins shared; keep + renumber local-only)
      2. rename the colliding local-only report files BEFORE copying cloud in
      3. copy cloud reports/ into local (cloud canonical for shared numbers)
      4. copy cloud pipeline.md into local
      5. write the merged applications.md

    Only writes to local on success; the caller skips this entirely on a failed
    download, so local is never wiped when offline."""
    cloud_apps = artifact_dir / "data" / "applications.md"
    local_apps = local_dir / "data" / "applications.md"
    cloud_md = cloud_apps.read_text(encoding="utf-8") if cloud_apps.exists() else ""
    local_md = local_apps.read_text(encoding="utf-8") if local_apps.exists() else ""

    # A reports-only / malformed artifact (no usable cloud tracker) must NOT be
    # treated as "cloud has zero rows" — merging would renumber every local row
    # and write a header-less file over the durable tracker. Leave local intact.
    if not cloud_md.strip():
        return {"renames": [], "rows": len(parse_applications_text(local_md)),
                "skipped": "no-cloud-tracker"}

    cloud_reports = artifact_dir / "reports"
    local_reports = local_dir / "reports"
    merged, renames = reconcile_trackers(
        cloud_md, local_md,
        _report_numbers(cloud_reports), _report_numbers(local_reports))

    for old, new in renames:
        _rename_report_file(local_reports, old, new)
    if cloud_reports.is_dir():
        local_reports.mkdir(parents=True, exist_ok=True)
        for f in cloud_reports.glob("*.md"):
            shutil.copy2(f, local_reports / f.name)

    cloud_pipeline = artifact_dir / "data" / "pipeline.md"
    local_apps.parent.mkdir(parents=True, exist_ok=True)
    if cloud_pipeline.exists():
        shutil.copy2(cloud_pipeline, local_dir / "data" / "pipeline.md")

    atomic_write_text(local_apps, merged)
    return {"renames": renames, "rows": len(parse_applications_text(merged))}


def _rename_report_file(reports_dir: Path, old: str, new: str) -> None:
    """Move a renumbered row's report file from `old` to `new` — both basenames,
    as reconcile_trackers read them out of the row's own Report link. No-op if
    the source isn't present (the row can outlive its file).

    Named files rather than a `{num}-*.md` glob: a number matches two files
    whenever local holds both a synced cloud report and a local-only one that
    shares it, and moving the cloud one breaks the tracker row that links to it."""
    src = reports_dir / old
    if src.is_file():
        src.rename(reports_dir / new)


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
    # Same easy_apply tagging as parse_applications, so the apply button gating
    # works in this fallback path too (career-ops/data is a sibling of batch/).
    easy_apply_urls = load_easy_apply_urls(tracker_dir.parent.parent / "data")
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
            row["report_num"], row["report_path"] = _report_link(row.get("report", ""))
            row["score_value"] = score_value(row.get("score", ""))
            row["status_canonical"] = canonical_status(row.get("status", ""))
            row["easy_apply"] = extract_url(row.get("notes", "")) in easy_apply_urls
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

    The row and its Status cell are located through the table's OWN header, the
    same mapping every read path uses. This is the single MUTATING path, and it
    was the last one reading fixed slots: `resolve_num_by_identity` hands it a
    num it found by header name, so against a table whose `#` is not first the
    lookup succeeded and the write then matched nothing — the local edit silently
    no-opped while the cloud push still fired, leaving the two diverged.

    Returns the text unchanged if the row isn't found."""
    want = str(num).strip()
    columns = _header_columns(applications_md_text)
    # +1 because splitting on "|" puts an empty cell before the leading pipe.
    num_idx = columns.index("num") + 1
    status_idx_default = columns.index("status") + 1
    report_idx = columns.index("report") + 1 if "report" in columns else None
    out_lines = []
    changed = False
    for line in applications_md_text.splitlines():
        if not changed and line.lstrip().startswith("|"):
            # Split preserving structure: leading/trailing pipes produce empty
            # edge cells we must keep so indices stay aligned on re-join.
            parts = line.split("|")
            cells = [p.strip() for p in parts]
            if (len(parts) > max(num_idx, status_idx_default)
                    and cells[num_idx] == want and cells[num_idx] not in ("#", "")):
                # Anchor on the Report link where the layout has one: it is
                # always [num](path), so it survives extra cells the LLM may have
                # injected (e.g. "Role | Remote") shifting everything right.
                status_idx = status_idx_default
                if report_idx is not None:
                    offset = report_idx - status_idx_default
                    for pi, p in enumerate(parts):
                        if _REPORT_CELL_RE.match(p.strip()):
                            status_idx = pi - offset
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


# `NNN-RESERVED.md` is career-ops' report-number LOCK, not a report: a JSON body
# ({"pid","token","created_at"}) dropped to claim a number, which survives any
# run killed before the real report replaces it. It carries the `NNN-` prefix
# this resolver keys on, so a first-match glob handed the UI the lock and the
# report pane rendered raw JSON where the evaluation belongs — silently, reading
# as a corrupt report rather than the wrong file.
#
# Only this resolver skips locks. The other readers of a report number ask
# "is this number taken?" and must keep counting them — see the note on
# `_batch_common.max_report_num`.
_RESERVED_REPORT_SUFFIX = "-RESERVED.md"


def find_report_file(reports_dir: Path, report_num: str) -> Path | None:
    """Locate a report file by its number. Reports are named
    `{num}-{company-slug}-{date}.md`; the tracker stores the zero-padded num
    (e.g. "042"). Match on the leading numeric segment, tolerating padding
    differences (42 vs 042). Reservation locks are skipped (see above).

    Iterated in sorted order so that a number matching two REAL reports resolves
    to the same one on every request — `_rename_report_file` records when that
    happens ("a number matches two files"). That is stability, not correctness:
    the caller-supplied report path is the authority when one is available."""
    wanted = _report_int(report_num)
    if wanted is None or not reports_dir.exists():
        return None
    for f in sorted(reports_dir.glob("*.md")):
        if f.name.endswith(_RESERVED_REPORT_SUFFIX):
            continue
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
