"""One-off: recover `work_location` for roles evaluated before #180 shipped.

#180 made the evaluator state where a role is (`work_location` in the Machine
Summary) and record it on the tracker row (`Work location:` in Notes), so every
ranker can demote a role the candidate cannot physically reach. It is
forward-looking: no report written before it carries the field, so an existing
backlog sorts exactly as it did — which on the copy that prompted the issue was
210 open roles, 32 of them in other metros and 24 of those above the daily
digest's score bar.

This reads what those reports DO say — Block A's `Remote policy` line — and
writes the mark for the rows it can read confidently.

**Prose parsing, deliberately, and deliberately only here.** #180 rejected this
for the permanent path: the line is free text in two layouts and two LANGUAGES
with no schema, and a rule built on it would rot. As a ONE-OFF over reports
already on disk the risk profile is different — the corpus is fixed, `--apply`
is a separate decision from the default dry run, and the alternative is
re-evaluating the backlog and rewriting scores and reports that are fine.

**It records the FACT, not the verdict**, exactly as the live path does: the
mark says where the job is, and `data.parse_applications` decides per read
whether that is reachable. So widening a search later re-judges these rows too.

**Every rule below is a refusal**, because the asymmetry from
`pipeline/work_location.py` applies with more force to a migration: a missed
mark leaves a row exactly as it is today, while a wrong one buries a role the
candidate could have taken, on evidence they cannot see. So this declines a
sentence that hedges, that negates its own mode, that names two states, that
names a state only as part of an employer's name, or that gives a mode with no
place. On the first real corpus 72 of 210 rows were declined and almost all of
them were local ("On-site (Dallas office)"), where no mark is already right.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from pipeline._batch_common import _report_location_mark, read_text, resolve_report
from pipeline.app import data
from pipeline.remote_signal import REMOTE_SIGNAL_RE
from pipeline.stdio import line_buffer_stdout
from pipeline.work_location import (
    MODE_HYBRID, MODE_ONSITE, MODE_REMOTE, WORK_LOCATION_RE, WorkLocation,
    _MODE_STEMS, _state_of, area_verdict, commutable_area, location_mark,
)

# Block A's row, in both layouts career-ops' reports use: a bullet
# (`- **Remote policy:** X`) and a table cell (`| **Remote policy** | X |`).
# The colon migrates in and out of the bold, and the casing varies.
#
# `Politica Remota` because the report template is bilingual — its headings are
# Spanish (`# Evaluacion`, `## A) Resumen del Rol`) and the model sometimes
# carries that through to this row. Only 9 of 689 reports in the first real
# corpus, but one was a 4.8 in-person role in Portland, MAINE at the top of the
# queue, which is the whole bug.
_REMOTE_POLICY_RE = re.compile(
    r"\*\*\s*(?:Remote\s*Policy|Politica\s*Remota)\s*:?\s*\*\*\s*:?\s*\|?\s*([^\n|]+)",
    re.I)

# US state NAMES, for prose that spells them out. #180's own module validates
# against no state table — a fourth copy of one is the bug three existing copies
# are waiting to become — but that rule is about the LIVE path, where both sides
# of the comparison are cells we wrote. Here the input is prose nobody wrote to
# a schema.
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_STATE_CODES = frozenset(_STATE_NAMES.values())

# "City, ST" inside the prose, where ST is a REAL state code. Two uppercase
# letters after a comma is not a state test — this is prose, and it matched
# "Tuesdays, IT", "Mon-Fri, AM" and "shifts, HR", each of which then became a
# confident mark whose state demoted the role. The code is checked against
# `_STATE_NAMES`' own values, so one table answers both directions.
_PLACE_RE = re.compile(
    r"((?:[A-Z][A-Za-z.'-]*[ ]){0,3}[A-Z][A-Za-z.'-]*),\s*([A-Z]{2})(?![-\w])")

# Two-letter tokens that are state codes AND everyday words in this corpus.
# `(?![-\w])` above already kills "PA-C"; these are the bare ones, where only
# what PRECEDES the comma can tell a place from a schedule or a credential:
# "Mon-Fri, ID badge required" is not Idaho, "AM and PM shifts, IN house" is not
# Indiana. Health and education postings are full of both.
_NOT_A_PLACE_RE = re.compile(
    r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|am|pm|m-f|mon-fri|"
    r"[\d\s:.-]+|shifts?|hours?|days?|full|part)$", re.I)

# A spelled-out state AND the place it belongs to, with the "<place>, " prefix
# REQUIRED. Optional, it matched a state name anywhere in the sentence — and
# this corpus is health, education and outreach employers whose NAMES are state
# names: "George Washington University Hospital" became `On-site WA`, "Virginia
# Mason Medical Center" `On-site VA`, "Indiana University Health, Dallas clinic"
# `On-site IN`. Each produced the guaranteed-demotion shape, a bare state with
# no metro that `out_of_area` compares and `is_local_location` cannot rescue, so
# a Dallas role was buried on the strength of its employer's name. Requiring the
# comma costs two real wins that lacked one and removes the whole class.
# Longest first, so "west virginia" is not read as "virginia".
_STATE_NAME_RE = re.compile(
    r"([A-Z][A-Za-z.\-' ]{2,30}),\s*\b("
    + "|".join(re.escape(n) for n in sorted(_STATE_NAMES, key=len, reverse=True))
    + r")\b", re.I)

# The evaluator saying it does not know. One real report reads "Unspecified in
# truncated snippet (University of Miami typically on-site)", which the
# state-name match would happily turn into `On-site FL`.
_UNSURE_RE = re.compile(
    r"\b(?:unspecified|not\s+(?:specified|stated|explicitly|listed|clear)|unclear"
    r"|unknown|not\s+detailed|likely|presum\w+|typically)\b", re.I)

# A mode token inside a negation. "Not on-site; fully remote (HQ Chicago, IL)"
# and "No on-site requirement - 100% remote, based in Chicago, IL" put the
# ON-SITE word first, so the earlier-signal rule below read them as on-site and
# demoted a remote role to its headquarters' metro — the failure that rule was
# added to prevent, arriving through the other door. A sentence that negates a
# mode is not evidence for it, and not reliably evidence for the opposite
# either, so the line is declined.
_NEGATED_MODE_RE = re.compile(
    r"\b(?:not|no|non|zero|never|without|rather\s+than|instead\s+of)\b[^.;]{0,40}?"
    r"(?:on[\s_-]*site|in[\s_-]*person|in[\s_-]*office|hybrid|remote)", re.I)

# The evaluator's own aside about the CANDIDATE, which routinely names the
# candidate's home state inside the job's location line: "On-site / Tucson, AZ
# (Note: Candidate is in Dallas, TX; relocation may be required)". That second
# state is the person's, not the job's — it made nine correct demotions look
# like two-state ambiguity and decline. Removed before any place is read, so
# the sentence that remains is about the ROLE.
# Narrow first: the aside is often INSIDE the same parenthetical as the real
# location — "On-site (Springfield, MA - note: candidate is in Dallas, TX)" —
# so removing the whole group would take the job's own metro with it.
_CANDIDATE_CLAUSE_RE = re.compile(
    r"[\*\-–—;,]?\s*\**\s*note\s*:?[^.);]*\bcandidate\b[^.);]*"
    r"|\bcandidate\s+(?:is|was|will\s+be|would|remains)\b[^.;)]*", re.I)
# ...then any parenthetical that STILL mentions them carried nothing else.
_CANDIDATE_GROUP_RE = re.compile(r"\([^()]*\bcandidate\b[^()]*\)", re.I)

# A place capture can start mid-phrase ("On-site / Hybrid in Brooklyn, NY"),
# which renders as "Hybrid Hybrid in Brooklyn, NY" once the mode is prepended.
_LEADING_MODE_RE = re.compile(
    r"^(?:on[\s_-]*site|hybrid|remote|in[\s_-]*person|in[\s_-]*office|field[\s_-]*based)"
    r"(?:\s+(?:in|at|near))?\b", re.I)

# A mode offered rather than required.
_OPTIONAL_RE = re.compile(
    r"\b(?:optional|available|possible|if\s+(?:desired|preferred|near)|may|can\s+be|"
    r"negotiable|upon\s+request|for\s+those)\b", re.I)

# "On-site" as an AMENITY, not a work mode. Postings advertise on-site parking,
# an on-site gym, an on-site clinic — and "On-site parking provided; position is
# fully remote (Boston, MA HQ)" put the on-site token FIRST, so earlier-signal-
# wins read a remote role as on-site in Boston. Nothing negates here and nothing
# hedges, so neither of the guards above sees it; the tell is the noun.
_ONSITE_AMENITY_RE = re.compile(
    r"\s*(?:parking|gym|fitness|cafeteria|caf[eé]|daycare|child[\s-]?care|clinic"
    r"|pharmacy|laundry|dining|amenit\w*|meals?|coffee)\b", re.I)

# The mode stems, needed INDIVIDUALLY here because prose carries both signals and
# their order is the only thing that separates them, where `_normalize_mode`
# returns only the winner. Taken from that module's own table rather than
# re-spelled: it defers the remote half to `REMOTE_SIGNAL_RE` for exactly this
# reason, and a second copy of the on-site half would drift the next time either
# is extended.
_STEM_RE = {mode: re.compile(stem, re.I) for mode, stem in _MODE_STEMS}


def read_policy(report_text: str) -> str:
    m = _REMOTE_POLICY_RE.search(report_text or "")
    return m.group(1).strip() if m else ""


def mode_from_prose(prose: str) -> str:
    """The work mode a `Remote policy` SENTENCE describes.

    `work_location._normalize_mode` reads a three-token cell the model was asked
    for; this reads a sentence it wrote freely, where both signals routinely
    appear. Two rules, in order:

    **Hybrid wins outright**, as in the live module — a hybrid role requires
    presence whatever else the sentence says, and "Hybrid (Tumwater, WA duty
    station, eligible to telework 2 days per week)" is a real, correct demotion
    that a "mentions remote, so skip" rule would throw away.

    **Otherwise the EARLIER signal wins.** "Fully Remote (< 10% on-site)" is a
    remote role qualified by a rare exception; "On-site (Dallas) with some remote
    flexibility" is the reverse. `_normalize_mode`'s fixed order read the first
    as on-site, harmless only while such rows carry no place — give one a
    "Chicago, IL" and it becomes a wrong demotion. (Negated forms put the
    on-site word first too; `location_from_policy` declines those outright,
    because order cannot tell them apart.)
    """
    text = str(prose or "")
    hy = _STEM_RE[MODE_HYBRID].search(text)
    rem0 = REMOTE_SIGNAL_RE.search(text)
    # Hybrid wins outright — a hybrid role requires presence whatever else the
    # sentence says — EXCEPT when it is offered as an option inside a remote
    # role: "Remote (optional hybrid access to the Chicago, IL office)" is not a
    # commute, and marking it one demotes a role the candidate can do.
    if hy and not (rem0 and rem0.start() < hy.start()
                   and _OPTIONAL_RE.search(text, max(0, hy.start() - 24), hy.end() + 24)):
        return MODE_HYBRID
    on = _first_onsite(text)
    rem = REMOTE_SIGNAL_RE.search(text)
    if on and rem:
        return MODE_ONSITE if on.start() < rem.start() else MODE_REMOTE
    if on:
        return MODE_ONSITE
    return MODE_REMOTE if rem else ""


def _first_onsite(text: str):
    """The first on-site token that is a WORK MODE, skipping amenities."""
    for m in _STEM_RE[MODE_ONSITE].finditer(text):
        if not _ONSITE_AMENITY_RE.match(text, m.end()):
            return m
    return None


def _clean_place(text: str) -> str:
    return _LEADING_MODE_RE.sub("", (text or "").strip()).strip(" -/,")


def role_prose(prose: str) -> str:
    """The policy line with the evaluator's asides about the candidate removed."""
    text = _CANDIDATE_CLAUSE_RE.sub(" ", str(prose or ""))
    return " ".join(_CANDIDATE_GROUP_RE.sub(" ", text).split())


def _place_in(prose: str) -> WorkLocation | None:
    """The single work place a policy sentence names, or None.

    None when it names none, or names MORE THAN ONE state: "HQ in Chicago, IL;
    role based in Dallas, TX" is a real shape, and taking the first match
    demoted a Dallas role to Chicago. Two states in one sentence is ambiguity,
    and this module resolves ambiguity by declining."""
    text = str(prose or "")
    codes = {m.group(2).upper() for m in _PLACE_RE.finditer(text)
             if not _NOT_A_PLACE_RE.match(m.group(1).strip())}
    codes |= {_STATE_NAMES[m.group(2).lower()] for m in _STATE_NAME_RE.finditer(text)}
    codes = {c for c in codes if c in _STATE_CODES}
    if len(codes) != 1:
        return None
    m = next((x for x in _PLACE_RE.finditer(text)
              if x.group(2).upper() in codes
              and not _NOT_A_PLACE_RE.match(x.group(1).strip())), None)
    if m:
        # Group 1 is the CITY only — the pattern was narrowed to capitalised
        # words so a clause like "reports to the Dallas hub but based in
        # Chicago, IL" cannot drag "Dallas" into the metro and have
        # `is_local_location` falsely rescue it. Rejoin the code so the metro
        # still round-trips through `_state_of` as "City, ST".
        city = _clean_place(m.group(1))
        metro = f"{city}, {m.group(2).upper()}" if city else ""
        return WorkLocation(metro=metro, state=_state_of(metro) or m.group(2).upper())
    n = _STATE_NAME_RE.search(text)
    if not n:
        return None
    code, where = _STATE_NAMES[n.group(2).lower()], _clean_place(n.group(1))
    return WorkLocation(metro=f"{where}, {code}" if where else "", state=code)


def location_from_policy(prose: str) -> WorkLocation:
    """The `Remote policy` prose as a WorkLocation, or an empty one.

    Empty whenever the sentence hedges, negates its own mode, names no readable
    mode, or names no single place for a presence mode — always the cheap
    direction. The hedging guard covers EVERY mode, including remote: below the
    remote early-return it gated on-site and hybrid only, by accident of where it
    sat, so "Unspecified (likely fully remote)" earned a confident
    `Work location: Remote`."""
    text = role_prose(prose)
    if _UNSURE_RE.search(text) or _NEGATED_MODE_RE.search(text):
        return WorkLocation()
    mode = mode_from_prose(text)
    if not mode:
        return WorkLocation()
    if mode == MODE_REMOTE:
        return WorkLocation(mode=mode)
    place = _place_in(text)
    return WorkLocation(mode=mode, metro=place.metro, state=place.state) if place else WorkLocation()


def mark_for_report(report_text: str) -> str:
    """The `Work location:` mark for one report.

    The report's own structured `work_location` FIRST, through the live path's
    `_report_location_mark` — "the one place a report is turned into that mark,
    so the writer and the merge-time repair cannot produce two shapes", and this
    must not become a third. `plan` selects on an unmarked tracker ROW, which is
    not the same set as a pre-#180 report: a `--batch` row merged before the
    mark shipped, or one whose Notes an older merge-tracker replaced, has the
    field in its report already. Prose is the fallback, not the first resort."""
    return _report_location_mark(report_text) or location_mark(
        location_from_policy(read_policy(report_text)))


def plan(career_ops: Path, commutable) -> tuple[list[dict], Counter]:
    """(rows to mark, why-skipped tally). Reads only; writes nothing."""
    tracker = career_ops / "data" / "applications.md"
    rows = data.parse_applications_text(read_text(tracker))
    out, tally = [], Counter()
    for row in rows:
        if row.get("status_canonical") != "Evaluated":
            continue
        tally["open"] += 1
        if WORK_LOCATION_RE.search(row.get("notes", "")):
            tally["marked"] += 1
            continue
        found = resolve_report(career_ops, row.get("report_path", ""),
                               num_text=row.get("report_num", ""),
                               company=row.get("company", ""))
        if found is None:
            tally["no_report"] += 1
            continue
        text = read_text(found)
        if not read_policy(text) and not _report_location_mark(text):
            tally["no_policy"] += 1
            continue
        mark = mark_for_report(text)
        if not mark:
            tally["declined"] += 1
            continue
        wl_verdict = area_verdict(_wl_of(mark), commutable)
        out.append({"num": str(row.get("num") or ""), "company": row.get("company", ""),
                    "role": row.get("role", ""), "score": row.get("score_value"),
                    "mark": mark, "verdict": wl_verdict,
                    "policy": read_policy(text)})
    return out, tally


def _wl_of(mark: str) -> WorkLocation:
    """The WorkLocation a mark encodes — read back through the live reader, so a
    mark this module writes and a mark the evaluator writes are judged the same."""
    from pipeline.work_location import work_location_from_notes
    return work_location_from_notes(mark)


def apply_marks(career_ops: Path, planned: list[dict]) -> int:
    """Write each row's mark into its Notes cell, in one read-modify-write.

    Through `data.append_note_in_text`, which maps columns by HEADER NAME and
    anchors on the Report link. An inline positional editor (`cells[-2]` for
    Notes, `cells[1]` for `#`) is the bug this repo has already shipped once —
    `apply_cloud_overrides` "replaced an inline copy that read fixed slots
    (`parts[6]` for Status: one cell off on a Via-layout tracker)". career-ops
    supports trailing user columns (`apply link`, `follow-up`, `url` are in its
    own `tracker-aliases.json`), and on any tracker carrying one, `[-2]` writes
    the mark into a user's cell, the demotion never fires, and the idempotence
    guard reads that same wrong cell so the row is skipped forever after.

    Appends rather than prepends, which is what the helper does: the reader takes
    the LAST mark in the cell (newest wins, #163's rule) and `extract_url` takes
    the first URL, so neither cares, and one writer for the Notes grammar beats
    two."""
    tracker = career_ops / "data" / "applications.md"
    text = read_text(tracker)
    changed = 0
    for p in planned:
        if not p["num"]:
            continue
        after = data.append_note_in_text(text, p["num"], p["mark"])
        if after != text:
            text, changed = after, changed + 1
    if changed:
        # read_text strips, so restore the trailing newline the file had.
        data.atomic_write_text(tracker, text if text.endswith("\n") else text + "\n")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.backfill_work_location",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--career-ops", type=Path, default=Path("career-ops"))
    ap.add_argument("--config", type=Path, default=None,
                    help="search config naming the commutable metros")
    ap.add_argument("--apply", action="store_true",
                    help="write the marks (default: dry run, print only)")
    ap.add_argument("--show", type=int, default=40, help="rows to print (0 = all)")
    args = ap.parse_args(argv)

    co = args.career_ops.resolve()
    commutable = commutable_area(args.config)
    if not commutable:
        print("[backfill] no commutable metros in the search config — nothing could be "
              "judged reachable or not. Aborting rather than marking blind.")
        return 1
    planned, tally = plan(co, commutable)
    demote = sum(1 for p in planned if p["verdict"])
    print(f"[backfill] {co}")
    print(f"[backfill] commutable: {', '.join(commutable)}")
    print(f"[backfill] open {tally['open']} | already marked {tally['marked']} | "
          f"no report {tally['no_report']} | no policy line {tally['no_policy']} | "
          f"declined {tally['declined']}")
    print(f"[backfill] would mark {len(planned)}, of which {demote} demote\n")
    shown = sorted(planned, key=lambda p: (not p["verdict"], -(p["score"] or 0)))
    for p in shown[:args.show or None]:
        print(f"  [{'DEMOTE' if p['verdict'] else '  ok  '}] {p['score'] or '-':>4}  "
              f"#{p['num']:>4}  {p['company'][:26]:28} {p['mark']}")
        if p["verdict"]:
            print(f"            from: {p['policy'][:96]}")
    if not args.apply:
        print("\n[backfill] dry run — nothing written. Re-run with --apply to write.")
        return 0
    n = apply_marks(co, planned)
    print(f"\n[backfill] wrote {n} mark(s) into {co / 'data' / 'applications.md'}")
    # NOT "press Push in the UI": that channel carries the pending STATUS
    # overrides a Kanban drag queues, and this writes none — Push answers 400
    # ("No pending status changes to push"), and the next Refresh, where cloud
    # rows win verbatim, would erase every mark written here. A full-file
    # replacement is the mechanism that matches what this did, and a real
    # tracker is far past the ~64KB dispatch-input cap, so it goes by release
    # asset. Said here because getting this wrong loses the whole run silently.
    print("[backfill] These are NOT pushed by the UI's Push button (that channel "
          "carries status overrides, and a Refresh would erase these).")
    print("[backfill] Replace the cloud copy with the whole file, e.g.:")
    print(f"    gh release create tracker-backfill-$(date +%s) "
          f"{co / 'data' / 'applications.md'}")
    print("    gh workflow run edit-tracker.yml -f applications_md_release_tag=<tag>")
    print("    # then delete the release; the asset holds your tracker")
    return 0


if __name__ == "__main__":
    line_buffer_stdout()
    raise SystemExit(main())
