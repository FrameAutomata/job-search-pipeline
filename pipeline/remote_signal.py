"""The remote-consistency guard: does a posting the board flagged remote SAY so?

An `is_remote: true` search pass returns rows Indeed and LinkedIn flag remote
whose description is plainly on-site somewhere else — patient-access roles in
Spartanburg SC, Detroit, Maine and Washington state; outreach roles in
Sacramento, NYC and Vermont — because the boards' remote flag is set by the
employer's checkbox, not by the JD. Those rows bypass every location check
(filter.is_eligible returns True for a remote row) and burn an evaluation each.

The guard is one question asked twice: **a remote claim needs a remote mention
in the JD.** `mentions_remote` is a deliberately simple word-boundary regex —
"remote", "work from home", "telecommute", "home office", a "distributed team",
"anywhere in the US" — and a hybrid JD that mentions remote work passes, which
is intended: the target is the posting that never says the word at all. A row
the JD contradicts is treated as ON-SITE: `is_remote` is rewritten to "False"
so every later reader sees the truth, and then

  - if the row is `remote_only` — returned by a remote pass and by NO non-remote
    pass (pipeline.scrape.mark_remote_only) — it is dropped, UNLESS its location
    is one of the user's own non-remote passes' (`local_pass_locations`):
    a local on-site role is exactly what the local pass exists to find, and
    dropping it because the remote pass found it first would be perverse;
  - otherwise it falls through to filter's ordinary location checks like any
    on-site row.

Two limits, both deliberate. A nearby-suburb on-site row the local pass missed
is still dropped: the local pass is radius-bounded and the remote pass is not,
so "Plano, TX" under a "Dallas, TX" pass is off-site to this guard — matching is
on the pass's city name (or a state/country-level pass location) as a whole
word, never on the state code, because widening it to the state re-admits the
far-away rows the guard exists to drop. And a genuinely remote JD restricted
to residents of one state ("must reside in South Carolina") is NOT addressed
here at all: it mentions remote, so it passes, and its state rule is for the
evaluator to read. `filter.remote_requires_mention: false` disables the guard
in both stages.

The rows judged are filter's — those with a description — and the screen stage
re-runs the same judgement after it backfills LinkedIn JDs, which arrive empty.
Same judgement, whole: a row screen turns on-site (ONSITE) is one filter
passed through the remote bypass, so screen also re-applies the location half
of filter.is_eligible — `location_eligible`, kept here so screen can call it
without importing filter — and drops the row those checks refuse, exactly as
filter would have had the JD been there. A row screen drops either way is
recorded in scan-history as `screened-offsite` (bridge.SCAN_HISTORY_STATUSES)
so the same far-away posting is not re-fetched every morning; bridge.load_seen
expires that status after sixty days so broadening the passes later can
resurrect it.

The `"True"`/`"False"`/`""` string readers live here, beside the regex, so
filter, screen and bridge share ONE reading of the two flags — the shape
bridge.is_easy_apply_row already set for the third pass-level flag. Stdlib
only, plus the two dependency-free leaves pipeline.sites (the one reading of a
pass's `is_remote` that the scraper uses) and pipeline.rowio (the dropped-rows
CSV); the jobspy-free UI venv can import this.
"""

import re
from pathlib import Path

from pipeline.rowio import read_rows, write_rows
from pipeline.sites import normalize_pass

ROOT = Path(__file__).resolve().parent.parent

# Every row the guard dropped this run, all columns, plus `dropped_by`
# (filter | screen). Overwritten by filter each run and extended by screen, so
# after a full run it holds the run's whole set; a --skip-filter run extends
# whatever the previous filter left. The stage log shows at most
# _LOG_LIMIT of them; this file is the rest.
DROPPED_PATH = ROOT / "output" / "remote-dropped.csv"
_LOG_LIMIT = 25

# One of these is missing from a JD the board called remote in the cases this
# guard exists for. Word-bounded and case-insensitive; `remote-first` is
# spelled out even though `\bremote\b` already covers it, so the list reads as
# the vocabulary it is. "virtual" is deliberately absent — "virtual interview",
# "virtual care" and "virtual desktop" are on-site JDs' words too.
REMOTE_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"remote(?:ly|-first)?"
    r"|work[ -]from[ -]home"
    r"|wfh"
    r"|telecommut(?:e|es|ed|ing)"
    r"|telework(?:s|ed|ing|ers?)?"
    r"|home[ -]based"
    r"|home\s+office"
    r"|fully\s+distributed"
    r"|distributed\s+team"
    r"|anywhere\s+in\s+the\s+(?:u\.?s\.?a?\.?|united\s+states|country)"
    r")\b",
    re.IGNORECASE,
)

# The verdicts judge_remote_row hands back for a row it rewrote. `None` is the
# third answer: the row was not flagged remote, has no description to judge
# yet, or its JD confirms the claim — leave it exactly as it was.
ONSITE = "onsite"      # rewritten to on-site; kept (not remote-only, or local)
OFFSITE = "offsite"    # rewritten to on-site; remote-only and far away — drop

_TRUE_CELLS = frozenset({"true", "1", "yes", "t"})


def mentions_remote(text) -> bool:
    """Whether `text` says, anywhere, that the work can be done remotely."""
    return bool(text) and REMOTE_SIGNAL_RE.search(str(text)) is not None


def _true_cell(value) -> bool:
    """A CSV bool cell. pandas writes the scraper's bool columns as "True"/
    "False"; an older jobs.csv, or a hand-written fixture, may carry "true",
    "1" or nothing at all — the same tolerance filter's reader always had."""
    return str(value if value is not None else "").strip().lower() in _TRUE_CELLS


def is_remote_str(row) -> bool:
    """The board's own remote flag on a row (`is_remote`), as written to CSV."""
    return _true_cell(row.get("is_remote"))


def remote_only_str(row) -> bool:
    """Whether every pass that returned this row was a remote pass
    (`remote_only`, pipeline.scrape.mark_remote_only). False when the column is
    absent — a jobs.csv from before the column existed, or a fixture."""
    return _true_cell(row.get("remote_only"))


def flagged_remote(row) -> bool:
    """A row with a remote claim to check: the board flagged it, or a remote
    pass — and only a remote pass — returned it."""
    return is_remote_str(row) or remote_only_str(row)


_FALSE_CELLS = frozenset({"false", "0", "no", "n", "off"})


def guard_enabled(cfg) -> bool:
    """`filter.remote_requires_mention`, default True — the one switch for the
    guard in both stages, read the same way by both. Tolerates a quoted
    `"false"`, which is a truthy Python string and would otherwise re-enable
    the guard on precisely the hand-edited config that meant to turn it off."""
    fcfg = (cfg.get("filter") if isinstance(cfg, dict) else None) or {}
    raw = fcfg.get("remote_requires_mention", True)
    if raw is None:
        # A key left blank (`remote_requires_mention:`) loads as None — a stub,
        # not an answer — so it keeps the default rather than reading as off.
        return True
    if isinstance(raw, str):
        return raw.strip().lower() not in _FALSE_CELLS
    return bool(raw)


def search_passes(cfg) -> list[dict]:
    """The per-pass mappings in a loaded search config: a `searches:` list, or
    the legacy single `search:` mapping — the shape rule pipeline.scrape.
    load_searches and pipeline.app.onboard.search_entries apply (the latter is
    the app-side mirror; tests/test_remote_signal.py holds the two to it).
    Non-mappings are skipped rather than refused: this reads a config a run has
    already loaded, so its shape errors were reported upstream."""
    if not isinstance(cfg, dict):
        return []
    if "searches" in cfg:
        entries = cfg["searches"]
        entries = entries if isinstance(entries, list) else []
    else:
        single = cfg.get("search")
        entries = [single] if single is not None else []
    return [e for e in entries if isinstance(e, dict)]


def local_pass_locations(cfg) -> list[str]:
    """The `location` of every NON-remote pass in `cfg`, in order, deduped.

    These are the places the user is actually willing to go, so an on-site row
    in one of them is kept by `judge_remote_row` even when a remote pass alone
    returned it. Read through normalize_pass so `is_remote: "true"` is a remote
    pass here exactly as it is to the scraper."""
    out: list[str] = []
    for p in search_passes(cfg):
        p = normalize_pass(p)
        if p.get("is_remote") is True:
            continue
        where = str(p.get("location") or "").strip()
        if where and where not in out:
            out.append(where)
    return out


def _place_name(pass_location: str) -> str:
    """What to look for in a row's location: the city of a "City, ST" pass, or
    the whole name of a state/country-level one ("Texas", "United States")."""
    return pass_location.split(",", 1)[0].strip()


def is_local_location(row_location, pass_locations) -> bool:
    """Whether a row's location is one of the user's non-remote passes'.

    True when the row's location contains the pass's city name as a whole word
    (case-insensitive) — "Dallas, TX" and "Dallas-Fort Worth Area" alike under
    a "Dallas, TX" pass — or the pass location is a state/country-level name
    ("Texas", "Canada") the row's location contains. Never the state code
    alone: a "Plano, TX" row is not local to a "Dallas, TX" pass, and widening
    to TX would re-admit "El Paso, TX", which is farther from Dallas than
    Little Rock is."""
    where = str(row_location or "")
    if not where.strip():
        return False
    for loc in pass_locations:
        name = _place_name(str(loc or ""))
        if len(name) < 2:
            continue
        if re.search(rf"\b{re.escape(name)}\b", where, re.IGNORECASE):
            return True
    return False


def compile_alternation(terms) -> re.Pattern | None:
    """Compile one \\b(?:t1|t2|...)\\b pattern, case-insensitive, from a config
    list. Returns None for an empty list so callers can short-circuit cheaply;
    drops falsy entries (a bare `-` in YAML parses to None). Filter's
    `_compile_alternation`, moved here so screen can compile the two location
    lists without importing filter."""
    pieces = sorted({str(t).lower() for t in (terms or []) if t}, key=len, reverse=True)
    if not pieces:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in pieces) + r")\b", re.IGNORECASE)


def location_eligible(row, negative_loc_pattern, eligible_loc_pattern) -> bool:
    """The location half of filter.is_eligible, for an ON-SITE row: False in a
    `negative_locations` place, or outside the `eligible_locations` allowlist.
    Word-bounded, so "US" matches the "US" in "Dallas, US" and not the "us" in
    "Russia". Deliberately NOT the whole gate — the remote bypass belongs to
    the caller (filter asks it first; screen asks it only of a row the guard
    has just turned on-site). `negative_description_terms` is knowingly NOT
    re-applied here, and that is a gap rather than a redundancy: filter runs
    the pattern over every row, but a LinkedIn row reaches filter with an
    empty description (`linkedin_fetch_description: false` is the documented
    default) and gets its JD only from screen's backfill, so on those rows the
    term list has never been tested against a JD at all. The location half IS
    re-asked because a row the guard turns on-site skipped it entirely through
    the remote bypass; re-asking the description half would need a third drop
    class in screen, with its own counter and scan-history question."""
    if negative_loc_pattern is None and eligible_loc_pattern is None:
        return True
    location = (row.get("location") or "").strip()
    if negative_loc_pattern is not None and location and negative_loc_pattern.search(location):
        return False
    if eligible_loc_pattern is not None and location and not eligible_loc_pattern.search(location):
        return False
    return True


def judge_remote_row(row: dict, pass_locations=()) -> str | None:
    """Apply the guard to one row. Returns None (left alone), ONSITE or OFFSITE.

    Rewrites `row["is_remote"]` to "False" on ONSITE and OFFSITE so downstream
    readers — filter.is_eligible, the UI, the work-order — see what the JD
    says rather than what the board's checkbox did. Idempotent: a row already
    rewritten is either no longer flagged (not remote-only) or judged the same
    way again (remote-only), so filter and screen can both run it."""
    if not flagged_remote(row):
        return None
    description = (row.get("description") or "").strip()
    if not description:
        return None        # nothing to judge yet — screen backfills LinkedIn JDs
    if mentions_remote(description):
        return None
    row["is_remote"] = "False"
    if not remote_only_str(row):
        return ONSITE
    if is_local_location(row.get("location"), pass_locations):
        return ONSITE
    return OFFSITE


def describe_dropped(row: dict) -> str:
    """The one-line log form of a dropped row: `company · title · location`."""
    return " · ".join(
        (str(row.get(k) or "").strip() or "?") for k in ("company", "title", "location")
    )


def report_dropped(rows: list[dict], stage: str) -> None:
    """Print the dropped rows, one line each, up to _LOG_LIMIT of them."""
    for row in rows[:_LOG_LIMIT]:
        print(f"[{stage}] dropped remote-pass on-site posting: {describe_dropped(row)}")
    if len(rows) > _LOG_LIMIT:
        print(f"[{stage}] … and {len(rows) - _LOG_LIMIT} more — see {DROPPED_PATH}")


def write_dropped(rows: list[dict], stage: str, *, extend: bool = False) -> Path:
    """Write the dropped rows to DROPPED_PATH, tagging each with `dropped_by`.

    `extend=False` (filter) overwrites — and truncates when nothing was
    dropped, so the previous run's rows never read as today's (rowio's
    contract). `extend=True` (screen) keeps what is already there and adds its
    own, deduped by job_url, so one file holds the run's whole set. The column
    set is the union, since filter's rows and screen's do not carry the same
    columns."""
    path = DROPPED_PATH
    tagged = [{**r, "dropped_by": stage} for r in rows]
    if extend:
        existing = read_rows(path)
        seen = {(r.get("job_url") or "").strip() for r in existing}
        tagged = existing + [r for r in tagged if (r.get("job_url") or "").strip() not in seen]
    fieldnames: list[str] = []
    for r in tagged:
        fieldnames += [k for k in r if k not in fieldnames]
    write_rows(path, tagged, fieldnames or None)
    return path
