"""Can the candidate actually be where this role requires them to be?

A board's remote flag is the employer's checkbox and `pipeline.remote_signal`
is the guard for it — *does a posting the board called remote SAY so?* This is
the other geography question, asked one stage later and against a different
yardstick: **the posting's own stated work location against the candidate's
own commutable area.** A JD can be perfectly honest about requiring a body in
Chicago five days a week and still reach the top of a Dallas candidate's queue,
because the evaluator scores fit and nothing scores reach.

That is what it did (#180). On one real copy 20 of 61 queued roles required
presence in another metro, and because nothing demoted them they took the top
four slots by score (4.8, 4.8, 4.7, 4.5); the candidate worked top-down, hit a
wall on the first thing they opened, and stopped. The 21 DFW roles were there
the whole time, underneath. A queue that is two-thirds usable reads as unusable
when the unusable third is sorted to the front.

**Two halves, deliberately.** The evaluation prompt states the rule — a role
requiring recurring presence outside the candidate's commutable area is a hard
stop — and also emits `work_location` in the Machine Summary so the code can
act on it when the model ignores the rule. That is the shape `sanitize_addition`
already uses for the score cell, and for the same reason: this repo has learned
twice that a prompt rule is advisory until something checks it.

**Demote, never drop.** Someone may genuinely relocate, and two copies of this
template disagree about that. An out-of-area role keeps its score, its report
and its place in the queue; it sorts below every reachable role and carries a
label saying why. Dropping it would make this the second status that means two
things (#163).

The matching rule is **deliberately more permissive than
`remote_signal.is_local_location` alone**, and the asymmetry is the point. That
guard decides whether to DELETE a scraped row, so it errs strict and treats
"Plano, TX" as off-site to a "Dallas, TX" pass. This decides whether to BURY an
evaluated role, so it errs permissive: burying a reachable Fort Worth role is
the very harm #180 is about, inverted. So a role is out of area only on a
**confident** mismatch — its state differs from every non-remote search pass's
state AND its metro names none of their cities. Anything unreadable on either
side (a metro with no state, a pass location with no "City, ST" code, a role
the model left unstated) is left exactly where it was: the cost of a missed
demotion is today's behaviour, and the cost of a wrong one is a good local role
buried where nobody scrolls.

The commutable area is `remote_signal.local_pass_locations` — the user's own
non-remote search passes, which is a place they have already said they will go
to work, rather than a second answer parsed out of `PROFILE.md`'s prose. A
remote pass is excluded from it by that function and a `mode: remote` role is
never demoted by this one, so "open to fully remote US" needs no configuration
here at all.

Stdlib only, plus the dependency-free leaves `pipeline.remote_signal` and
`pipeline.search_config` — so the cloud digest, the local handoff build and the
jobspy-free UI venv can all import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from pipeline.remote_signal import is_local_location, local_pass_locations, mentions_remote
from pipeline.search_config import load_search_config

__all__ = [
    "MODE_ONSITE", "MODE_HYBRID", "MODE_REMOTE", "PRESENCE_MODES", "OUT_OF_AREA_TAG",
    "WORK_LOCATION_RE", "WorkLocation", "parse_work_location", "commutable_area",
    "commutable_states", "out_of_area", "area_label", "area_verdict",
    "location_mark", "work_location_from_notes",
]

MODE_ONSITE = "onsite"
MODE_HYBRID = "hybrid"
MODE_REMOTE = "remote"

# The two modes that put a body in a place. `hybrid` is here because one office
# day a week is still a commute: the rule is about the METRO, not the number of
# days, so hybrid IN the metro stays fine and hybrid in another one does not.
PRESENCE_MODES = frozenset({MODE_ONSITE, MODE_HYBRID})

# What every surface calls a demoted role, in ONE place: the digest's embed
# title and body, and the work-order's `Where` column and its header note. The
# module's whole claim is that one predicate keeps the surfaces agreeing, and
# three spellings of the answer would break that where the user can see it.
# Not ASCII, so it must never reach a console `print` — handoff's stage log runs
# on cp1252 Windows terminals, which raise on the glyph. File and webhook only.
OUT_OF_AREA_TAG = "⚠ Out of area"

# ── The tracker mark ────────────────────────────────────────────────────────
# The evaluation reads the posting once; every surface that ranks the role reads
# the tracker. So the FACT the evaluator established — where this job is — is
# recorded on the row, and each surface applies the predicate to it against its
# OWN commutable area. Storing the fact rather than the verdict is the whole
# point: "this posting is in Chicago" is fixed once the JD is read, while
# "the candidate cannot commute there" changes the day they widen their search
# or move, and a frozen verdict would go stale with nothing to refresh it.
#
# In Notes because that is where this repo already puts a fact it needs to
# survive the round trip — `liveness_closed_mark`'s reasoning applies unchanged:
# the mark rides the cached cloud tracker and every Refresh, where a new column
# would mean career-ops' merge-tracker, both tracker layouts and every reader
# learning a tenth cell, and a new state file would need its own cache path.
#
# The shape is one human sentence that parses back exactly: a mode word the
# same `_normalize_mode` reads, then the place, which carries its own state
# because `_state_of` recovers it. `_prepend_to_notes` owns the cell grammar
# (`req <id> — <url> — <mark> — <the model's sentence>`).
WORK_LOCATION_LABEL = "Work location"
# Anchored to the START of a clause — the beginning of the cell, or just after
# the ` — ` the Notes grammar joins with. The label is ordinary English, and the
# model writes that cell: a verdict sentence containing "...flexible work
# location: negotiable" would otherwise satisfy the idempotence guard (so OUR
# mark is never written, and the role becomes permanently undemotable) and then
# win the newest-mark read, since it sits after the mark we did write.
WORK_LOCATION_RE = re.compile(
    rf"(?:^|—)\s*{WORK_LOCATION_LABEL}:\s*(?P<value>[^—|]*?)\s*(?=—|\||$)", re.I)

_MODE_WORDS = {MODE_ONSITE: "On-site", MODE_HYBRID: "Hybrid", MODE_REMOTE: "Remote"}


# The prompt asks for exactly one of three tokens, and a model mostly obliges —
# but "on-site", "On Site" and "fully remote" are what it writes when it does
# not, and a mode we cannot read is a role we cannot judge. Hybrid is tested
# FIRST: "Hybrid (2 days remote)" and "Remote/Hybrid" both name a commute, and
# reading either as remote is the miss this module exists for. The remote case
# is deferred to `remote_signal.mentions_remote` rather than re-spelled here —
# that module is the one home for what "remote" is spelled like, and a second
# narrower copy would only drift the next time either is extended.
_MODE_STEMS = (
    (MODE_HYBRID, r"hybrid"),
    (MODE_ONSITE, r"on[\s_-]*site|in[\s_-]*office|in[\s_-]*person"
                  r"|on[\s_-]*premises?|office[\s_-]*based|on[\s_-]*location"),
)

# A place written "City, ST": the trailing two-letter token after a comma. Used
# on BOTH sides — a non-remote search pass's location (a real place the user
# typed; "Dallas, TX" in both copies on this template) and the model's `metro`
# when it filled that but left `state` empty. No state-code table is validated
# against, on purpose: three already exist in this repo with no guard between
# them (handoff's regex, onboard's set, setup-profile.mjs's), and a fourth copy
# would be the bug those are waiting to become. It is safe to skip because
# `local_pass_locations` has already excluded the remote passes, so "Remote, US"
# is not a value the pass side ever reads.
_CITY_ST_RE = re.compile(r",\s*([A-Za-z]{2})\s*$")

# The model's own `state` cell, which is what makes the metro machine-comparable
# without this module geocoding prose. Unvalidated for the same reason: garbage
# matches no pass state, and a non-match is "leave it alone".
_STATE_CELL_RE = re.compile(r"^[A-Za-z]{2}$")

# The metro is model-authored free text landing in a markdown table cell, so it
# gets the same treatment `_note_safe` gives the liveness reason — a `|` splits
# the row (`_strip_role_pipe`'s whole subject), and an em-dash is the Notes
# grammar's own separator, which would truncate the mark at read time because
# WORK_LOCATION_RE stops at one. Capped because a model that writes a sentence
# here should cost a cell, not a tracker row. Done HERE rather than by
# `_batch_common._note_safe`: this module cannot import that one (it imports
# this), and the producer of a mark is the right owner of its safety.
_MARK_UNSAFE_RE = re.compile(r"[|—\r\n\t]+")
_MARK_MAX = 60


@dataclass(frozen=True)
class WorkLocation:
    """The Machine Summary's `work_location`, read.

    `mode` is one of the three tokens or "" when the model stated none; `metro`
    and `state` are "" when it stated none. All three empty is the ordinary
    shape of a report written before #180, and reads as "unknown" — never as
    "remote", which would silently promote every legacy row. `remote` is kept
    distinct from unknown even though the predicate treats both as "leave it
    alone": the deferred case in #180 — a genuinely remote role restricted to
    residents of one state — needs to tell them apart.
    """
    mode: str = ""
    metro: str = ""
    state: str = ""

    @property
    def requires_presence(self) -> bool:
        return self.mode in PRESENCE_MODES


def _normalize_mode(raw) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    for mode, stem in _MODE_STEMS:
        if re.search(stem, text):
            return mode
    return MODE_REMOTE if mentions_remote(text) else ""


def _mark_safe(text) -> str:
    """Model-authored text made safe for a markdown cell and the Notes grammar."""
    clean = " ".join(_MARK_UNSAFE_RE.sub(" ", str(text or "")).split())
    return clean[:_MARK_MAX].rstrip(" ,")


def _state_of(place) -> str:
    """The two-letter state a "City, ST" place names, uppercased, else ""."""
    m = _CITY_ST_RE.search(str(place or "").strip())
    return m.group(1).upper() if m else ""


def parse_work_location(summary) -> WorkLocation:
    """`work_location` out of a parsed `## Machine Summary` mapping.

    Tolerant by design — this reads model output, and the fallback for anything
    unreadable is an empty WorkLocation, which demotes nothing. A bare string
    (`work_location: remote`) is read as the mode alone, since that is the
    shape a model reaches for when it compresses the block.

    An empty `state` falls back to the one the `metro` names, because the prompt
    tells the model to leave both blank "when the posting does not say" and a
    model that fills `metro: "Chicago, IL"` while leaving `state` empty is the
    likeliest way for an honest report to slip the gate — the failure #180 is
    about, surviving inside the fix for it.
    """
    if not isinstance(summary, dict):
        return WorkLocation()
    raw = summary.get("work_location")
    if isinstance(raw, str):
        return WorkLocation(mode=_normalize_mode(raw))
    if not isinstance(raw, dict):
        return WorkLocation()
    metro = str(raw.get("metro") or "").strip()
    state = str(raw.get("state") or "").strip()
    state = state.upper() if _STATE_CELL_RE.match(state) else _state_of(metro)
    return WorkLocation(mode=_normalize_mode(raw.get("mode")), metro=metro, state=state)


def commutable_area(path=None) -> list[str]:
    """The candidate's commutable places, from the search config in effect:
    every non-remote pass's `location`.

    Takes the config PATH rather than a loaded mapping because that is the whole
    composition both readers need, and spelling `load_search_config` at each of
    them was the duplication a bare alias for `local_pass_locations` would have
    left behind. Unreadable config → `[]`, which demotes nothing.
    """
    return local_pass_locations(load_search_config(path))


@lru_cache(maxsize=16)
def _confident_states(pass_locations: tuple) -> frozenset:
    """The commutable states, but ONLY when every pass yields one.

    A pass location this cannot read a state from is broader than the test —
    "United States" on a non-remote pass says the candidate will go anywhere,
    and comparing a Chicago role against the `{TX}` its sibling "Dallas, TX"
    pass contributes would demote a role that pass covers. So one unreadable
    pass turns the state half off for the whole config, which is the same answer
    the module already gives when NO pass is readable, and the same asymmetry it
    is built on: a missed demotion is today's behaviour, a wrong one buries a
    role the candidate could have taken.

    Cached because `data.parse_applications` asks once per tracker ROW for an
    answer that is a property of the config; pure, so it cannot go stale, and
    `remote_signal.local_pattern` does the same for the city half."""
    states = commutable_states(pass_locations)
    return frozenset(states) if len(states) == len(pass_locations or ()) else frozenset()


def commutable_states(pass_locations) -> set[str]:
    """The state codes the commutable places name, uppercased. Empty when none
    of them is written "City, ST" — which turns the state half of the test off
    (see `out_of_area`) rather than guessing at a state-level pass."""
    return {code for loc in (pass_locations or ()) if (code := _state_of(loc))}


def out_of_area(wl: WorkLocation, pass_locations) -> bool:
    """Whether a role demands presence somewhere the candidate has not said
    they can work — the one predicate every surface demotes on.

    True only on a confident mismatch, so every unreadable case answers False:
    a remote or unstated mode, a role with no readable state, a candidate with
    no "City, ST" pass to compare against. The state test comes first and the
    city test rescues it, which is what keeps a "Dallas-Fort Worth Area" metro
    local to a "Dallas, TX" pass whatever its state cell says.
    """
    if not wl.requires_presence or not wl.state:
        return False
    states = _confident_states(tuple(pass_locations or ()))
    if not states or wl.state in states:
        return False
    # is_local_location, not a second city test written here: it owns the
    # "Dallas-Fort Worth Area is local to a Dallas, TX pass" rule, and it caches
    # its own compiled pattern per pass list (remote_signal.local_pattern), so
    # asking it once per tracker row is no longer the expensive half it was.
    return not is_local_location(wl.metro, pass_locations)


def area_label(wl: WorkLocation) -> str:
    """The one-line reason a demoted role is where it is — for the digest row
    and the work-order table, so "why is this last" never needs the report.

    The same string `location_mark` writes after its label, by construction:
    an inline second mode→word map here rendered MODE_REMOTE as "On-site",
    unreachable only because `area_verdict` guards on `out_of_area` — a trap one
    refactor away, and the write path and the read path disagreeing about how a
    place is spelled is exactly the drift `OUT_OF_AREA_TAG` exists to prevent."""
    return _render(wl)


def area_verdict(wl: WorkLocation, pass_locations) -> str:
    """Predicate + label in one call: the demotion label, or `""` for a role
    that is reachable, remote or unreadable.

    One function rather than the predicate → label pair spelled at each reader,
    because the invariant that ties them — a label is set ONLY when the
    predicate fired — is what makes a non-empty label a safe stand-in for the
    verdict everywhere downstream. Readers restating it in two spellings is how
    they come to disagree.
    """
    return area_label(wl) if out_of_area(wl, pass_locations) else ""


def _render(wl: WorkLocation) -> str:
    """"On-site Chicago, IL" — the mode word and the place, in one place.

    The place written out must CARRY its state, because the state is the field
    the predicate tests and the mark is all a later reader gets back. A model
    that fills `metro: "Chicago"` with `state: "IL"` would otherwise round-trip
    to no state at all and the role would silently stop being demotable — the
    same gap `parse_work_location`'s fallback closes coming the other way."""
    word = _MODE_WORDS.get(wl.mode, "")
    if not word:
        return ""
    where = _mark_safe(wl.metro)
    if wl.state and _state_of(where) != wl.state:
        where = f"{where}, {wl.state}" if where else wl.state
    return f"{word} {where}" if where else word


def location_mark(wl: WorkLocation) -> str:
    """The Notes mark recording where a role is, or `""` when the evaluation
    did not say.

    `""` for an unknown mode is what keeps a legacy row legible as legacy: a
    row with no mark has never been judged, which is different from a row judged
    remote, and writing something for both would erase the distinction the whole
    backlog depends on. A remote role DOES get a mark — it is a fact the
    evaluator established, and recording it is what lets a later reader tell
    "remote" from "not looked at yet" (the deferred #180 case, a remote role
    restricted to one state's residents, needs exactly that).
    """
    rendered = _render(wl)
    return f"{WORK_LOCATION_LABEL}: {rendered}" if rendered else ""


def work_location_from_notes(notes) -> WorkLocation:
    """Read `location_mark` back out of a tracker row's Notes cell.

    The inverse of `location_mark`, and deliberately the same readers as the
    forward path — `_normalize_mode` on the leading word, `_state_of` on the
    rest — so a mode spelling that round-trips one way round-trips the other.
    No mark, or one nothing can be read from, is an empty WorkLocation: the
    shape of every row written before #180, which demotes nothing.

    **The LAST mark wins, and that is load-bearing.** merge-tracker's
    `mergeNotes` keeps the existing cell verbatim and FIRST, appending
    `Re-eval <date> (a→b): <the addition's whole cell>` after it — so a
    re-evaluated row carries every mark it has ever had, oldest first, and
    reading the first one returns the location of the posting this row used to
    be. Both directions are wrong and both are #180 itself: a role that moved to
    another metro rides the top of the queue, and one that went remote stays
    buried. `extract_url` was rewritten under #163 for precisely this shape ("the
    posting the row's NEWEST evaluation looked at"); this is the same rule, one
    cell over. An unreadable newest mark is "unknown" rather than a fall back to
    an older one: the older mark describes a posting this row no longer is.
    """
    m = None
    for m in WORK_LOCATION_RE.finditer(str(notes or "")):
        pass
    if m is None:
        return WorkLocation()
    word, _, where = m.group("value").strip().partition(" ")
    mode = _normalize_mode(word)
    if not mode:
        return WorkLocation()
    metro = where.strip()
    # A location the evaluation gave as a state and no metro is written bare
    # ("On-site IL"), and reading it back as a METRO would drop the state — the
    # one field the predicate tests, so the row would silently stop being
    # demotable. The mark is the only place the two can look alike.
    if _STATE_CELL_RE.match(metro):
        return WorkLocation(mode=mode, state=metro.upper())
    return WorkLocation(mode=mode, metro=metro, state=_state_of(metro))
