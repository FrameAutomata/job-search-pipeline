"""Browser-agent handoff: turn the scored queue + JOB_LOG.md into a work-order.

The pipeline's job is to FIND roles; a browser agent (Claude Cowork, OpenClaw,
a local Agent-SDK/`claude -p` runner, ...) does the APPLYING. This module is the
seam between the two. It is deliberately agent-agnostic: it emits a plain,
self-describing work-order that any browser agent can consume, and it never
drives a browser itself.

Two moving parts:

1. A structured status tracker (``role-status.jsonl``) — one JSON line per role
   that has been acted on, keyed by ``company::role`` (board/req-id/case
   normalized away). This is the machine source of truth for "what's done".
   It is seeded once from the historical prose in ``JOB_LOG.md`` and, going
   forward, the browser agent appends structured lines (or a work-order
   writeback) so we never again depend on parsing prose.

2. A work-order (``next-roles.jsonl`` + ``next-roles.md``) — the scored queue
   minus everything already in the tracker, ranked best-first, each row carrying
   a suggested resume base and an empty ``status`` column the agent writes back.

`reconcile()` builds (1) from JOB_LOG.md; `build_work_order()` builds (2).
`run()` wires them together as a re-runnable stage (orchestrate calls it
directly); `main()` is its argparse wrapper for `python -m pipeline.handoff`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline._batch_common import atomic_write_text, env_float, env_int, normalize_company as _squeeze
from pipeline.app import data as _data

ROOT = Path(__file__).resolve().parent.parent

# ── File-name conventions (shared by CLI + tests so they can't drift) ──────────
DEFAULT_TRACKER_NAME = "role-status.jsonl"
WORK_ORDER_JSONL = "next-roles.jsonl"
WORK_ORDER_MD = "next-roles.md"

# Terminal statuses a role can carry in the tracker. Any role present in the
# tracker (whatever its status) is "touched" and excluded from the work-order.
# Precedence resolves conflicts when the same role is seen from multiple sources
# (e.g. parsed as a prose skip AND present in the Applied table → applied wins).
STATUS_PRECEDENCE: dict[str, int] = {
    "applied": 5,     # submitted successfully
    "handoff": 4,     # prepped, blocked on the human (account/password/CAPTCHA/code) or external ATS
    "drafted": 3,     # written but not sent (e.g. a Work-at-a-Startup note left for review)
    "claimed": 2,     # in progress this session
    "skipped": 1,     # evaluated and passed on
}

RESUME_BASE_AI = "content_adhoc"       # AI / agentic / backend / full-stack / general SWE
RESUME_BASE_STANDARD = "content_standard"  # production-support / SRE / mainframe / devops / pure-frontend

# Queue rows already acted on (tracker vocabulary, canonicalized) — never
# re-emitted in a work-order.
_ACTED_ON_STATUSES = frozenset({"Applied", "Rejected", "Interview", "Offer", "Discarded", "SKIP"})


@dataclass
class TrackedRole:
    """One acted-on role in the status tracker."""
    key: str
    company: str
    role: str
    status: str
    board: str = ""          # "linkedin" | "indeed" | "waas" | ""
    url: str = ""
    reason: str = ""
    date: str = ""
    source: str = ""         # applied_table | waas_table | needs_thomas | to_finish | claimed | skipped_prose | writeback | tracker


@dataclass
class QueueRole:
    """One candidate row from evaluated-roles-by-score.jsonl."""
    num: str
    score: float
    company: str
    role: str
    url: str
    status: str = ""

    @property
    def board(self) -> str:
        return board_of(self.url)


@dataclass
class WorkOrderItem:
    """One row in the work-order handed to the browser agent."""
    rank: int
    num: str
    score: float
    company: str
    role: str
    board: str
    url: str
    resume_base: str
    status: str = ""         # agent writes back: claimed | applied | handoff | skip:<reason>
    resume_pdf: str = ""     # optional: pre-tailored resume file (--tailor enrichment)


# ── Normalization / keys ───────────────────────────────────────────────────────
# Trailing legal-entity suffixes dropped from company names so "Ryan, LLC",
# "Ryan LLC", and "Ryan" all collide. Applied repeatedly ("X Co Ltd" → "X").
_COMPANY_SUFFIX_RE = re.compile(
    r"[\s,.]+(?:llc|inc|incorporated|corp|corporation|ltd|limited|co|company|holdings|plc|lp|llp)\.?\s*$",
    re.IGNORECASE,
)
# A parenthetical/bracket group is req-id NOISE when it contains a digit
# ("(req R0019979)", "[210669]"); alphabetic qualifiers ("(Backend)") are real
# role scope and must survive so two distinct openings don't merge.
_BRACKET_GROUP_RE = re.compile(r"\[[^\]]*\]")
_PAREN_GROUP_RE = re.compile(r"\(([^)]*)\)")
# Trailing " - <decoration>" segments on role titles that don't change the
# role's identity: worktype tags ("- Remote", "- Remote Contract") and the
# per-city labels boards stamp on identical reposted reqs ("- Tempe, AZ, USA").
# A meaningful scope segment ("- Ads", "- NLP, ML") matches neither and stays.
_ROLE_DECOR_WORK_RE = re.compile(
    r"\s+[-–—]\s+"
    r"(?:remote|hybrid|on[- ]?site|onsite|contract|full[- ]?time|part[- ]?time|"
    r"us|usa|united states|opportunity|only)"
    r"(?:[\s/]+(?:remote|hybrid|contract|opportunity|only|us|usa|united states))*"
    r"\s*$",
    re.IGNORECASE,
)
# The city rule must be CASE-SENSITIVE with real state codes: under IGNORECASE
# a bare [A-Z]{2} matched any tech pair ("- NLP, ML", "- React, UI") and merged
# genuinely distinct roles (found in review, reproduced).
_US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|"
    "MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_ROLE_DECOR_CITY_RE = re.compile(
    r"\s+[-–—]\s+[A-Za-z][\w .']*,\s*(?:" + _US_STATES + r")(?:,\s*(?:USA|United States))?\s*$"
)


def norm_company(s: str) -> str:
    """Canonical company token: lowercased, trailing legal suffix (LLC/Inc/...)
    dropped, then squeezed to alphanumerics, so "Ryan, LLC" == "Ryan"."""
    s = (s or "").strip()
    # Trailing parenthetical qualifiers on companies ("Skill (staffing)") are
    # descriptive, not identity.
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    prev = None
    while prev != s:
        prev = s
        s = _COMPANY_SUFFIX_RE.sub("", s)
    return _squeeze(s)


def norm_role(s: str) -> str:
    """Canonical role token: lowercased, req/ID/bracket noise and trailing
    worktype/city decorations removed, squeezed to alphanumerics, so "Full
    Stack AI Engineer [R0019979]" == "Full Stack AI Engineer" and "SWE -
    Remote" == "SWE". Level markers (II/III/Senior) are preserved — they are
    distinct roles."""
    s = (s or "").strip()
    s = _BRACKET_GROUP_RE.sub(" ", s)

    def _drop_if_reqish(m: re.Match) -> str:
        return " " if any(ch.isdigit() for ch in m.group(1)) else f" {m.group(1)} "

    s = _PAREN_GROUP_RE.sub(_drop_if_reqish, s).strip()
    prev = None
    while prev != s:   # peel stacked decorations ("… - Remote - Tempe, AZ, USA")
        prev = s
        s = _ROLE_DECOR_WORK_RE.sub("", s).strip()
        s = _ROLE_DECOR_CITY_RE.sub("", s).strip()
    return _squeeze(s)


def role_key(company: str, role: str) -> str:
    """The dedup key: f"{norm_company}::{norm_role}". Board-, case-, and
    req-id-insensitive per RUN_BOOK's dedup rule."""
    return f"{norm_company(company)}::{norm_role(role)}"


def _stripped_key(company: str, role: str) -> str:
    """A secondary, fuzzier key with ALL parenthetical/bracket groups removed —
    used to match a role whose subtitle appears on only one side ("Software
    Engineer - Ads (SEM Infrastructure)" vs "Software Engineer - Ads"). Routes
    through norm_role so trailing decorations are peeled identically."""
    bare = _PAREN_GROUP_RE.sub(" ", _BRACKET_GROUP_RE.sub(" ", role or ""))
    return f"{norm_company(company)}::{norm_role(bare)}"


def _paren_texts(role: str) -> frozenset[str]:
    """The squeezed alphabetic parenthetical qualifiers of a role title.
    Digit-bearing groups are req-id noise and don't count as qualifiers."""
    out = set()
    for m in _PAREN_GROUP_RE.finditer(role or ""):
        text = m.group(1)
        if text and not any(ch.isdigit() for ch in text):
            token = _squeeze(text)
            if token:
                out.add(token)
    return frozenset(out)


def board_of(url: str) -> str:
    """Map a URL to a board tag: linkedin | indeed | waas | other."""
    u = (url or "").lower()
    if "linkedin.com" in u:
        return "linkedin"
    if "indeed.com" in u:
        return "indeed"
    if "workatastartup.com" in u:
        return "waas"
    return "other"


def _board_from_text(text: str) -> str:
    """Best-effort board tag from a free-text cell like "Indeed→ryan.wd1..."."""
    t = (text or "").lower()
    if "linkedin" in t:
        return "linkedin"
    if "indeed" in t:
        return "indeed"
    if "workatastartup" in t or "waas" in t or "work at a startup" in t:
        return "waas"
    return ""


# ── Parsing JOB_LOG.md (historical backfill) ───────────────────────────────────
# The log is written with em/en dashes between company and role. Some copies of
# the file circulate with UTF-8-as-cp1252 mojibake; normalize the common forms
# before parsing rather than failing silently on them.
_MOJIBAKE = {"â€”": "—", "â€“": "–", "â€œ": '"', "â€\x9d": '"', "â€˜": "'", "â€™": "'"}
_DASH_SEP_RE = re.compile(r"\s+[—–]\s+")
_BOLD_PAIR_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*\s*(.*)$")
_KEYWORD_LINE_RE = re.compile(
    r"^\s*-\s+(?:APPLY\s*/\s*)?(SKIPPED|DROPPED|SUBMITTED|HANDOFF|BLOCKED|CLAIMED)\b[A-Z/ ]*\s+(.*)$"
)
_KEYWORD_STATUS = {
    "SUBMITTED": "applied",
    "SKIPPED": "skipped",
    "DROPPED": "skipped",
    "HANDOFF": "handoff",
    "BLOCKED": "handoff",
    "CLAIMED": "claimed",
}
_DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
_REASON_CAP = 200
# Explicit skip verdicts inside a bold-pair bullet's trailing text. Lets us
# catch the (very common) session-run bullets that end "…Skip — reason." even
# though their section heading isn't skip-titled. Verdict words only — a bullet
# that merely describes a role (e.g. "worth a retry") stays untracked.
_SKIP_VERDICT_RE = re.compile(
    r"(?i)(?<![a-z])skip(?:ped|ping)?\b|expired on indeed|no longer accepting|"
    r"listing (?:closed|removed)|(?<![a-z])moot\b"
)


def _normalize_log_text(text: str) -> str:
    for bad, good in _MOJIBAKE.items():
        text = text.replace(bad, good)
    return text


def _split_pair(text: str) -> tuple[str, str] | None:
    """Split "Company — Role" on the first em/en dash separator. Returns None
    when there is no dash pair — precision-first, we never guess."""
    parts = _DASH_SEP_RE.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    company, role = parts[0].strip(), parts[1].strip()
    if not company or not role:
        return None
    return company, role


def _truncate_role(role: str) -> str:
    """Cut trailing annotation off a prose role: " [Indeed] 2026-06-30 (...)"."""
    cut = re.split(r"\s+\[|\s+\(|:|\s+20\d\d-\d\d-\d\d", role, maxsplit=1)[0]
    return cut.strip().strip("*~").strip()


def _clean_reason(text: str) -> str:
    reason = text.strip().strip(":—–- ").strip()
    reason = reason.lstrip("(").rstrip(")").strip()
    return reason[:_REASON_CAP]


def parse_job_log(text: str, known_companies: set[str] | None = None) -> list[TrackedRole]:
    """Extract every acted-on role from JOB_LOG.md.

    High-confidence structured sources (exact): the ``## Applied`` table, the
    Work-at-a-Startup table, the ``## Needs Thomas`` / ``## To finish manually``
    bullet lists (→ handoff), and ``## Claimed``.

    Best-effort prose source (precision-first, partial recall by design): skip
    decisions written as ``**Company — Role**`` bold pairs inside ``/skip/i``
    headed sections, or on lines explicitly marked ``SKIPPED``/``DROPPED``. A
    missed skip simply reappears in a later work-order and gets re-skipped
    (cheap); we never invent a skip for a company only mentioned in passing.
    ``known_companies`` (normalized), when provided, further constrains prose
    skips to companies actually present in the queue.
    """
    text = _normalize_log_text(text or "")
    tracked: list[TrackedRole] = []

    def emit(company: str, role: str, status: str, *, board: str = "", url: str = "",
             reason: str = "", date: str = "", source: str = "") -> None:
        company, role = company.strip().strip("*~").strip(), role.strip().strip("*~").strip()
        if not company or not role:
            return
        if status == "skipped" and known_companies is not None:
            if norm_company(company) not in known_companies:
                return
        key = role_key(company, role)
        if not key.split("::")[0] or not key.split("::")[1]:
            return
        tracked.append(TrackedRole(
            key=key, company=company, role=role, status=status, board=board,
            url=url, reason=_clean_reason(reason), date=date, source=source,
        ))

    h2 = ""
    h3 = ""
    for line in text.splitlines():
        if line.startswith("## ") and not line.startswith("###"):
            h2, h3 = line[3:].strip().lower(), ""
            continue
        if line.startswith("### "):
            h3 = line[4:].strip().lower()
            continue

        in_skip_section = "skip" in h2 or "skip" in h3
        stripped = line.strip()

        # ── Markdown table rows ────────────────────────────────────────────
        if stripped.startswith("|") and not _data._SEPARATOR_RE.match(stripped):
            cols = _data._split_row(stripped)
            if len(cols) >= 3 and cols[1].lower() != "company":
                date = _DATE_RE.search(cols[0] or "")
                date_str = date.group(1) if date else ""
                if h2.startswith("applied") and len(cols) >= 5:
                    # | Date | Company | Role | Location/Comp | Board | Method |
                    emit(cols[1], cols[2], "applied",
                         board=_board_from_text(cols[4]), date=date_str,
                         source="applied_table")
                elif "work at a startup" in h2:
                    # | Date | Company | Role | Location | Job ID | Note |
                    note = cols[5] if len(cols) >= 6 else ""
                    status = "applied" if "SUBMITTED" in note.upper() else "drafted"
                    emit(cols[1], cols[2], status, board="waas", date=date_str,
                         reason=note if status == "drafted" else "",
                         source="waas_table")
            continue

        # ── Bulleted lines ─────────────────────────────────────────────────
        if not stripped.startswith(("-", "*")):
            continue

        # A struck-through bullet is a RESOLVED item (its outcome lives
        # elsewhere, e.g. the Applied table) — never emit it from here.
        if re.match(r"^\s*[-*]\s+~~", line):
            continue

        date_m = _DATE_RE.search(stripped)
        date_str = date_m.group(1) if date_m else ""

        keyword = _KEYWORD_LINE_RE.match(line)
        if keyword:
            status = _KEYWORD_STATUS[keyword.group(1)]
            rest = keyword.group(2).replace("**", "").strip()
            pair = _split_pair(rest)
            if pair:
                company, role = pair
                emit(company, _truncate_role(role), status,
                     board=_board_from_text(stripped), date=date_str,
                     reason=rest[len(company):] if status == "skipped" else "",
                     source="skipped_prose" if status == "skipped" else "log_line")
            continue

        bold = _BOLD_PAIR_RE.match(line)
        if not bold:
            continue
        pair = _split_pair(bold.group(1))
        if not pair:
            continue
        company, role = pair
        trailing = bold.group(2)

        # No _truncate_role here: the bold markers already bound the role
        # exactly, and its " (" cut would strip real qualifiers ("(Backend)") —
        # which the fuzzy matcher then over-matches against the sibling role
        # (review bug, reproduced). Truncation is for keyword lines only, where
        # the role runs into trailing "[board] date (commentary)" annotations.
        if h2.startswith("needs thomas"):
            emit(company, role, "handoff", reason=trailing,
                 board=_board_from_text(stripped), date=date_str, source="needs_thomas")
        elif h2.startswith("to finish manually"):
            emit(company, role, "handoff", reason=trailing,
                 board=_board_from_text(stripped), date=date_str, source="to_finish")
        elif h2.startswith("claimed"):
            emit(company, role, "claimed", board=_board_from_text(stripped),
                 date=date_str, source="claimed")
        elif in_skip_section or _SKIP_VERDICT_RE.search(trailing):
            emit(company, role, "skipped", reason=trailing,
                 board=_board_from_text(stripped), date=date_str, source="skipped_prose")

    return merge_tracked(tracked)


# ── Queue + tracker IO ─────────────────────────────────────────────────────────
def _iter_jsonl(path: Path):
    """Yield parsed objects from a jsonl file, tolerating blank/garbled lines."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    for line in raw.splitlines():
        line = line.strip().strip("\x00")
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def load_queue(path: Path) -> list[QueueRole]:
    """Read evaluated-roles-by-score.jsonl (tolerant of blank/garbled lines)."""
    out: list[QueueRole] = []
    for o in _iter_jsonl(path):
        try:
            score = float(o.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        out.append(QueueRole(
            num=str(o.get("num") or ""),
            score=score,
            company=str(o.get("company") or "").strip(),
            role=str(o.get("role") or "").strip(),
            url=str(o.get("url") or "").strip(),
            status=str(o.get("status") or "").strip(),
        ))
    return out


def load_queue_from_tracker(career_ops: Path) -> list[QueueRole]:
    """Build the queue straight from career-ops' applications.md — the tracker
    every --evaluate-batch run writes. This is the default queue source when no
    scored-export jsonl exists (nothing in the repo produces one; it's an
    optional out-of-band artifact), so --handoff works on a fresh install.
    Rows without a score or URL are skipped — they aren't actionable rows."""
    tracker = Path(career_ops) / "data" / "applications.md"
    if not tracker.exists():
        return []
    out: list[QueueRole] = []
    for row in _data.parse_applications(tracker):
        score = row.get("score_value")
        url = _data.extract_url(row.get("notes", ""))
        if score is None or not url:
            continue
        # Tracker statuses are authoritative here — emit only actionable rows
        # (an Applied/Interview/Discarded row is never work-order material, and
        # keeping them out makes the "N scored" summary mean the real pool).
        if row.get("status_canonical") in _ACTED_ON_STATUSES:
            continue
        out.append(QueueRole(
            num=str(row.get("num") or ""),
            score=float(score),
            company=str(row.get("company") or "").strip(),
            role=str(row.get("role") or "").strip(),
            url=url,
            status=str(row.get("status_canonical") or "").strip(),
        ))
    return out


def load_tracker(path: Path) -> list[TrackedRole]:
    """Read role-status.jsonl (empty list if it does not exist yet)."""
    out: list[TrackedRole] = []
    for o in _iter_jsonl(path):
        key = str(o.get("key") or "")
        status = str(o.get("status") or "")
        if not key or not status:
            continue
        out.append(TrackedRole(
            key=key,
            company=str(o.get("company") or ""),
            role=str(o.get("role") or ""),
            status=status,
            board=str(o.get("board") or ""),
            url=str(o.get("url") or ""),
            reason=str(o.get("reason") or ""),
            date=str(o.get("date") or ""),
            source=str(o.get("source") or "tracker"),
        ))
    return out


def write_tracker(path: Path, roles: list[TrackedRole]) -> None:
    """Write role-status.jsonl deterministically (stable sort for clean diffs)."""
    lines = [
        json.dumps(asdict(t), ensure_ascii=False)
        for t in sorted(roles, key=lambda t: t.key)
    ]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def load_writeback(work_order_jsonl: Path) -> list[TrackedRole]:
    """Read agent-written statuses back out of a completed next-roles.jsonl,
    so the next reconcile folds them into the tracker."""
    out: list[TrackedRole] = []
    for o in _iter_jsonl(work_order_jsonl):
        raw_status = str(o.get("status") or "").strip()
        if not raw_status:
            continue
        company = str(o.get("company") or "").strip()
        role = str(o.get("role") or "").strip()
        if not company or not role:
            continue
        low = raw_status.lower()
        reason = ""
        if low.startswith("skip"):
            status = "skipped"
            _, _, reason = raw_status.partition(":")
        elif low in STATUS_PRECEDENCE:
            status = low
        else:
            continue  # unknown token — leave for a human to look at
        url = str(o.get("url") or "")
        out.append(TrackedRole(
            key=role_key(company, role), company=company, role=role,
            status=status, board=board_of(url), url=url,
            reason=_clean_reason(reason), source="writeback",
        ))
    return out


def drop_late_writeback(items: list[WorkOrderItem], out_dir: Path) -> tuple[list[WorkOrderItem], list[TrackedRole]]:
    """Re-read the on-disk work-order right before overwriting it and drop any
    item whose row gained a status since the run started. A browser agent may
    be working the previous work-order WHILE this run tailors for minutes —
    without this second read its statuses would be clobbered by the overwrite
    and the same roles re-emitted status-empty (double-apply risk). Returns
    (surviving items renumbered, the late statuses to fold into the tracker)."""
    late = load_writeback(Path(out_dir) / WORK_ORDER_JSONL)
    if not late:
        return items, []
    late_keys = {t.key for t in late}
    kept = [i for i in items if role_key(i.company, i.role) not in late_keys]
    for rank, item in enumerate(kept, start=1):
        item.rank = rank
    return kept, late


# ── Reconcile ──────────────────────────────────────────────────────────────────
def merge_tracked(*groups: list[TrackedRole]) -> list[TrackedRole]:
    """Merge tracked-role lists, deduping by key. On a key collision the
    highest-STATUS_PRECEDENCE status wins (applied > handoff > drafted >
    claimed > skipped); on a tie the earlier entry wins but empty detail
    fields are backfilled from the later one."""
    by_key: dict[str, TrackedRole] = {}
    for group in groups:
        for t in group:
            cur = by_key.get(t.key)
            if cur is None:
                by_key[t.key] = t
                continue
            if STATUS_PRECEDENCE.get(t.status, 0) > STATUS_PRECEDENCE.get(cur.status, 0):
                winner, loser = t, cur
            else:
                winner, loser = cur, t
            for field in ("board", "url", "reason", "date", "source"):
                if not getattr(winner, field) and getattr(loser, field):
                    setattr(winner, field, getattr(loser, field))
            by_key[t.key] = winner
    return list(by_key.values())


def reconcile(
    job_log_text: str,
    existing: list[TrackedRole],
    writeback: list[TrackedRole] | None = None,
    known_companies: set[str] | None = None,
) -> list[TrackedRole]:
    """Fold JOB_LOG.md + any agent writeback into the existing tracker."""
    parsed = parse_job_log(job_log_text, known_companies=known_companies)
    return merge_tracked(existing, parsed, writeback or [])


# ── Work-order ─────────────────────────────────────────────────────────────────
# Titles routed to the non-AI base per RUN_BOOK Step 5: production-support /
# SRE / mainframe / devops / pure-frontend. Everything else uses the AI base.
_STANDARD_BASE_RE = re.compile(
    r"production\s*support|prod\s*support|site\s*reliability|\bsre\b|mainframe|"
    r"dev\s*ops|devops|front[\s-]*end|frontend",
    re.IGNORECASE,
)


def suggest_resume_base(role: str) -> str:
    """RUN_BOOK Step 5 base picker: content_standard for production-support /
    SRE / mainframe / devops / pure-frontend titles, else content_adhoc."""
    return RESUME_BASE_STANDARD if _STANDARD_BASE_RE.search(role or "") else RESUME_BASE_AI


def build_work_order(
    queue: list[QueueRole],
    tracker: list[TrackedRole],
    board: str = "both",
    limit: int | None = None,
) -> list[WorkOrderItem]:
    """The scored queue minus every key already in the tracker, board-filtered,
    ranked by score descending, numbered from 1, each with a resume-base hint."""
    touched = {t.key for t in tracker}
    # Secondary index: paren-stripped key → the qualifier sets seen for it.
    # A queue role matches a tracked one when the stripped keys agree AND the
    # parenthetical qualifiers don't CONFLICT (one side subtitle-free matches;
    # "(Backend)" vs "(Frontend)" stays two distinct roles).
    touched_stripped: dict[str, list[frozenset[str]]] = {}
    for t in tracker:
        touched_stripped.setdefault(_stripped_key(t.company, t.role), []).append(
            _paren_texts(t.role))

    def _fuzzy_touched(q: QueueRole) -> bool:
        qualifier_sets = touched_stripped.get(_stripped_key(q.company, q.role))
        if qualifier_sets is None:
            return False
        q_parens = _paren_texts(q.role)
        return any(not q_parens or not t_parens or q_parens == t_parens
                   for t_parens in qualifier_sets)

    fresh: list[QueueRole] = []
    seen: set[str] = set()
    for q in queue:
        if not q.company or not q.role:
            continue
        # The queue's own status column is a second signal (rows can carry
        # tracker statuses from the export) — honor it via the tracker's
        # canonicalizer so aliases ("Aplicada") count too. Anything acted on
        # is out: re-applying to an Interview/Offer company is worse than a
        # missed fresh role.
        if _data.canonical_status(q.status) in _ACTED_ON_STATUSES:
            continue
        if board != "both" and q.board != board:
            continue
        key = role_key(q.company, q.role)
        if key in touched or key in seen or _fuzzy_touched(q):
            continue
        seen.add(key)
        fresh.append(q)

    fresh.sort(key=lambda q: q.score, reverse=True)
    if limit is not None:
        fresh = fresh[:limit]

    return [
        WorkOrderItem(
            rank=i + 1, num=q.num, score=q.score, company=q.company, role=q.role,
            board=q.board, url=q.url, resume_base=suggest_resume_base(q.role),
        )
        for i, q in enumerate(fresh)
    ]


def enrich_with_resumes(items: list[WorkOrderItem], tailor_fn, *, min_score: float,
                        workers: int = 1) -> list[WorkOrderItem]:
    """Optionally attach a pre-tailored, candidate-named resume file to each row
    scoring >= min_score. `tailor_fn(item) -> str|Path|None` does the actual
    tailoring (see _make_tailor_fn). A tailoring failure or decline NEVER drops
    or blocks a row — the agent just tailors that one itself.

    Rows are independent (LLM call + JD fetch + render each), so `workers` > 1
    runs them through a bounded thread pool — same shape as batch_evaluate's
    worker pool. Default 1 keeps library callers/tests deterministic.

    ONE row per company: the tailor cache is company-keyed
    (career-ops/output/<Company> - resume.*), so tailoring two roles at the
    same company would alias one file — both rows pointing at a resume tailored
    for whichever role generated last (and racing its work files under the
    pool). Only the best-ranked row per company gets a pre-tailored file; the
    agent tailors any sibling rows itself."""
    eligible: list[WorkOrderItem] = []
    seen_companies: set[str] = set()
    for i in items:                      # items arrive rank-ordered (best first)
        if i.score < min_score:
            continue
        company = norm_company(i.company)
        if company in seen_companies:
            continue
        seen_companies.add(company)
        eligible.append(i)

    def _one(item: WorkOrderItem) -> None:
        try:
            path = tailor_fn(item)
        except Exception as e:
            print(f"[handoff] tailor failed for {item.company} ({type(e).__name__}: {e}) — "
                  "row kept without a resume file")
            return
        if path:
            item.resume_pdf = str(path)

    if workers <= 1 or len(eligible) <= 1:
        for item in eligible:
            _one(item)
        return items

    # Prime on the first row synchronously so the tailor's one-time setup (the
    # source-docx one-page baseline render, provider detection) warms once
    # instead of racing across the pool.
    _one(eligible[0])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_one, eligible[1:]))
    return items


def _make_tailor_fn(career_ops: Path):
    """The real tailor adapter: wraps pipeline.resume_tailor.generate_for_job
    (cached per company, one-page verified, hand-edits win). Lazy imports so the
    handoff stage itself never needs python-docx unless --tailor is used.

    All rows share ONE caller: gemini_limits' free-tier pacer lives inside the
    caller instance, so per-row resolution would give every pool worker its own
    rate limiter (429 storms — review bug). Resolution is deferred to the first
    actual LLM invocation, so a fully cached run still works with no provider
    key configured."""
    import threading
    from pipeline import resume_tailor as rt
    from pipeline.role_select import ApplyJob

    lock = threading.Lock()
    cell: list = []

    def shared_caller(system: str, user: str) -> str:
        with lock:
            if not cell:
                cell.append(rt._resolve_caller(None, None))
        return cell[0](system, user)

    def tailor(item: WorkOrderItem):
        job = ApplyJob(num=item.num, company=item.company, role=item.role,
                       url=item.url, score=item.score)
        return rt.generate_for_job(career_ops, job, caller=shared_caller)

    return tailor


def render_work_order_jsonl(items: list[WorkOrderItem]) -> str:
    """One compact JSON object per line, machine-consumable by any agent."""
    return "\n".join(json.dumps(asdict(i), ensure_ascii=False) for i in items) + ("\n" if items else "")


def render_work_order_md(items: list[WorkOrderItem], *, total_queue: int, touched: int) -> str:
    """Human/agent-readable work-order with a short how-to header + status
    legend. Agent-agnostic — never names a specific browser agent."""
    lines = [
        "# Work order — fresh roles for a browser agent",
        "",
        f"{len(items)} fresh roles (of {total_queue} scored; {touched} already handled and excluded).",
        "Work top-down. The score set reading order only — judge each role from the live posting.",
        "",
        "For each role: open the URL, qualify it against the profile, tailor the resume",
        f"(suggested base in the `resume base` column), apply, then record the outcome in `{WORK_ORDER_JSONL}`",
        "by setting that row's `status` field:",
        "",
        "- `claimed` — you are working on it now (claim-before-apply when sessions run in parallel)",
        "- `applied` — submitted successfully",
        "- `handoff` — prepped but blocked on the human (account, password, CAPTCHA, verification code)",
        "- `skip:<reason>` — evaluated and passed on (keep the reason short)",
        "",
        "The next pipeline run folds these statuses into the tracker, so recorded roles never reappear.",
        "",
        "| # | Score | Company | Role | Board | Resume base | URL |",
        "|---|-------|---------|------|-------|-------------|-----|",
    ]
    for i in items:
        lines.append(
            f"| {i.rank} | {i.score:g} | {i.company} | {i.role} | {i.board} | {i.resume_base} | {i.url} |"
        )
    lines.append("")
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────
def run(
    *,
    queue_path: Path | None = None,
    job_log: Path | None = None,
    tracker: Path | None = None,
    out_dir: Path | None = None,
    board: str = "both",
    limit: int | None = None,
    tailor: bool = False,
    tailor_min_score: float | None = None,
    career_ops: Path | None = None,
    workers: int | None = None,
) -> int:
    """Reconcile then build: read queue + JOB_LOG + tracker, write the updated
    tracker and the work-order (jsonl + md). Re-runnable; idempotent given the
    same inputs. This is the programmatic entry point orchestrate calls; main()
    is its argparse wrapper."""
    queue_path = Path(queue_path) if queue_path else ROOT / "output" / "evaluated-roles-by-score.jsonl"
    out_dir = Path(out_dir) if out_dir else ROOT / "output" / "handoff"
    co = Path(career_ops) if career_ops else Path(os.environ.get("CAREER_OPS_PATH") or ROOT / "career-ops")
    if job_log is None:
        env_log = (os.environ.get("HANDOFF_JOB_LOG") or "").strip()
        job_log = Path(env_log) if env_log else None

    # Queue source: the scored-export jsonl when present (an optional
    # out-of-band artifact), else the tracker itself — applications.md is what
    # every --evaluate-batch run writes, so a fresh install works end to end.
    if queue_path.exists():
        queue = load_queue(queue_path)
    else:
        queue = load_queue_from_tracker(co)
        if not queue:
            print(f"[handoff] no queue: neither {queue_path} nor "
                  f"{co / 'data' / 'applications.md'} has scored roles")
            return 1
        print(f"[handoff] queue source: {co / 'data' / 'applications.md'} "
              f"(no scored-export jsonl at {queue_path})")

    tracker_path = Path(tracker) if tracker else out_dir / DEFAULT_TRACKER_NAME
    job_log_text = job_log.read_text(encoding="utf-8") if job_log and job_log.exists() else ""
    existing = load_tracker(tracker_path)
    # Fold in any statuses the agent wrote into the previous work-order before
    # we overwrite it.
    writeback = load_writeback(out_dir / WORK_ORDER_JSONL)
    known = {norm_company(q.company) for q in queue if q.company}

    tracked = reconcile(job_log_text, existing, writeback=writeback, known_companies=known)
    write_tracker(tracker_path, tracked)

    items = build_work_order(queue, tracked, board=board, limit=limit)

    if tailor:
        min_score = tailor_min_score if tailor_min_score is not None else env_float("APPLY_TAILOR_MIN_SCORE", 4.0)
        try:
            tailor_fn = _make_tailor_fn(co.resolve())
        except ImportError as e:
            print(f"[handoff] --tailor unavailable ({e}) — emitting the work-order without resume files")
        else:
            # Per-row tailoring is LLM/fetch/render-bound and independent. The
            # pool width follows the eval stage's knob (same default) unless
            # the caller threads its own through.
            enrich_with_resumes(items, tailor_fn, min_score=min_score,
                                workers=max(1, workers or env_int("BATCH_CONCURRENCY", 3)))

    # A long tailor run leaves a window where a live agent session wrote new
    # statuses into the old work-order — fold those in rather than clobbering.
    items, late = drop_late_writeback(items, out_dir)
    if late:
        tracked = merge_tracked(tracked, late)
        write_tracker(tracker_path, tracked)

    atomic_write_text(out_dir / WORK_ORDER_JSONL, render_work_order_jsonl(items))
    atomic_write_text(out_dir / WORK_ORDER_MD,
                      render_work_order_md(items, total_queue=len(queue), touched=len(tracked)))

    # ASCII-only: Windows consoles often run cp1252, where fancy arrows crash print.
    print(f"[handoff] {len(queue)} scored -> {len(tracked)} tracked -> {len(items)} fresh")
    print(f"[handoff] tracker:    {tracker_path}")
    print(f"[handoff] work-order: {out_dir / WORK_ORDER_JSONL}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Argparse wrapper over run() for `python -m pipeline.handoff`."""
    ap = argparse.ArgumentParser(
        prog="handoff",
        description="Build the browser-agent work-order from the scored queue.",
    )
    ap.add_argument("--queue", type=Path, default=None,
                    help="Scored queue jsonl (default: output/evaluated-roles-by-score.jsonl)")
    ap.add_argument("--job-log", type=Path, default=None,
                    help="JOB_LOG.md to reconcile historical statuses from "
                         "(default: the HANDOFF_JOB_LOG env var, if set)")
    ap.add_argument("--tracker", type=Path, default=None,
                    help=f"Status tracker path (default: <out-dir>/{DEFAULT_TRACKER_NAME})")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Directory for the work-order files (default: output/handoff)")
    ap.add_argument("--board", choices=["linkedin", "indeed", "both"], default="both")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the work-order to the top N roles")
    ap.add_argument("--tailor", action="store_true",
                    help="Pre-tailor a candidate-named resume per row (reuses the "
                         "resume-tailoring stage; needs resumes/resume.docx) so the "
                         "work-order ships ready-to-upload files")
    ap.add_argument("--tailor-min-score", type=float, default=None,
                    help="Only pre-tailor rows scoring >= this "
                         "(default: APPLY_TAILOR_MIN_SCORE env, else 4.0)")
    ap.add_argument("--career-ops", type=Path, default=None,
                    help="career-ops install for --tailor caching "
                         "(default: CAREER_OPS_PATH env, else ./career-ops)")
    args = ap.parse_args(argv)
    return run(
        queue_path=args.queue, job_log=args.job_log, tracker=args.tracker,
        out_dir=args.out_dir, board=args.board, limit=args.limit,
        tailor=args.tailor, tailor_min_score=args.tailor_min_score,
        career_ops=args.career_ops,
    )


if __name__ == "__main__":
    import sys
    sys.exit(main())
