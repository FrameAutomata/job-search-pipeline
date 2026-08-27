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

2. A work-order per job site (``next-roles-<board>.jsonl`` + ``.md``) — the scored
   queue minus everything already in the tracker, partitioned by the site each
   posting URL belongs to and ranked best-first within each site, every row
   carrying a suggested resume base and an empty ``status`` column the agent
   writes back. One session per site lets the agent log into each site once and
   work its roles, then move on. The tracker (1) is shared across all sessions.

`reconcile()` builds (1) from JOB_LOG.md; `build_sessions()` builds (2) (over
`build_work_order()`, which produces the single deduped/ranked list). `run()`
wires them together as a re-runnable stage (orchestrate calls it directly);
`main()` is its argparse wrapper for `python -m pipeline.handoff`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from pipeline._batch_common import (
    atomic_write_text, env_float, env_int, normalize_company as _squeeze, read_text,
)
from pipeline.app import data as _data
from pipeline.tracker_layout import SEPARATOR_RE, split_row
from pipeline.stdio import line_buffer_stdout

ROOT = Path(__file__).resolve().parent.parent

# ── File-name conventions (shared by CLI + tests so they can't drift) ──────────
# The work-order is split into one session per site: next-roles-<board>.jsonl
# / .md (see work_order_paths). WORK_ORDER_JSONL is the LEGACY combined name —
# still read on writeback (an agent may be finishing a pre-upgrade file) and used
# as the "both"/generic fallback, but run() no longer writes it.
DEFAULT_TRACKER_NAME = "role-status.jsonl"
WORK_ORDER_JSONL = "next-roles.jsonl"
_WORK_ORDER_GLOB = "next-roles*.jsonl"   # matches per-site files AND the legacy one
HANDOFF_README = "HANDOFF-README.md"     # seeded agent-instructions file — a distinct name so it
                                         # never collides with a README the user's folder already has
HANDOFF_PROFILE = "PROFILE.md"           # the living master the agent qualifies + tailors against and
                                         # GROWS as it learns (seeded once from career-ops; never clobbered)
HANDOFF_RESUME_RUNBOOK = "RESUME-RUNBOOK.md"   # the recipe (schema + fill target + rules) a capable
                                               # agent follows to rebuild/override a résumé from PROFILE.md


def _is_combined(board: str) -> bool:
    """True for the "all sites" selector / legacy combined build (not one site)."""
    return board in ("", "both")


def _work_order_stem(board: str) -> str:
    return "next-roles" if _is_combined(board) else f"next-roles-{board}"


def work_order_paths(out_dir, board: str) -> tuple[Path, Path]:
    """The (jsonl, md) work-order pair for one site's session:
    next-roles-<board>.jsonl / .md. ("both"/"" → the legacy combined name.)"""
    out_dir, stem = Path(out_dir), _work_order_stem(board)
    return out_dir / f"{stem}.jsonl", out_dir / f"{stem}.md"


def _board_from_filename(name: str) -> str:
    """Inverse of work_order_paths for the jsonl name: 'next-roles-indeed.jsonl'
    → 'indeed'; the legacy 'next-roles.jsonl' → 'both'."""
    stem = Path(name).stem
    prefix = "next-roles-"
    return stem[len(prefix):] if stem.startswith(prefix) else "both"


def _work_order_jsonls(out_dir, *, include_legacy: bool = True) -> list[Path]:
    """The work-order jsonl files present in out_dir, sorted. include_legacy=False
    drops the combined next-roles.jsonl, leaving only the per-site sessions."""
    paths = sorted(Path(out_dir).glob(_WORK_ORDER_GLOB))
    return paths if include_legacy else [p for p in paths if p.name != WORK_ORDER_JSONL]

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

# A row is actionable ONLY while still "Evaluated" — an allowlist, matching
# role_select._PENDING_STATUSES. A denylist here failed OPEN: it silently
# admitted "Responded" (a real CANONICAL_STATES value) and any future status,
# re-applying to a role the employer already replied to (review bug M2).
_ACTIONABLE_STATUS = "Evaluated"


@dataclass
class TrackedRole:
    """One acted-on role in the status tracker."""
    key: str
    company: str
    role: str
    status: str
    board: str = ""          # a KNOWN_BOARDS tag (linkedin/indeed/glassdoor/…), or "" if unknown
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
    report: str = ""         # career-ops-relative eval report path; feeds tailoring

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
    report: str = ""         # career-ops-relative eval report path (proof-points for tailoring)


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


# A posting URL → board tag. Tags align to JobSpy's Site enum values (lowercase;
# "zip_recruiter" with an underscore) so the handoff's site vocabulary matches
# config/search.yml. The scraper's own `site` column is dropped at the bridge and
# the queue is rebuilt from applications.md (URL only), so the board is re-derived
# from the URL domain here. Substring match on a lowercased URL. Order matters
# only in that the first hit wins; the domains are disjoint in practice.
_BOARD_DOMAINS: tuple[tuple[str, str], ...] = (
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
    ("glassdoor.", "glassdoor"),          # glassdoor.com / .co.uk / .ca / ...
    ("ziprecruiter.com", "zip_recruiter"),
    ("bayt.com", "bayt"),
    ("naukri.com", "naukri"),
    ("bdjobs.com", "bdjobs"),
    ("workatastartup.com", "waas"),
)
# The catch-all: anything not recognized above (incl. Google-sourced employer/ATS
# links, which don't carry a stable job-board domain) gets its own session.
OTHER_BOARD = "other"
# Every value board_of() can emit — the site vocabulary the CLI/UI expose as
# --handoff-board choices. "both" is a selector over these, not a board itself.
KNOWN_BOARDS: frozenset[str] = frozenset(tag for _, tag in _BOARD_DOMAINS) | {OTHER_BOARD}

# Free-text synonyms for _board_from_text (JOB_LOG cells like "Indeed→ryan.wd1..").
# Kept parallel to _BOARD_DOMAINS so the URL and prose taggers agree on the vocab.
_BOARD_TEXT_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("linkedin", "linkedin"),
    ("indeed", "indeed"),
    ("glassdoor", "glassdoor"),
    ("ziprecruiter", "zip_recruiter"),
    ("zip recruiter", "zip_recruiter"),
    ("zip_recruiter", "zip_recruiter"),
    ("bayt", "bayt"),
    ("naukri", "naukri"),
    ("bdjobs", "bdjobs"),
    ("workatastartup", "waas"),
    ("work at a startup", "waas"),
    ("waas", "waas"),
)

# Human labels for the per-site work-order header / kickoff prompt.
_BOARD_LABELS: dict[str, str] = {
    "linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter", "bayt": "Bayt", "naukri": "Naukri",
    "bdjobs": "BDJobs", "waas": "Work-at-a-Startup", OTHER_BOARD: "other-site",
}


def board_of(url: str) -> str:
    """Map a URL to a board tag (linkedin, indeed, glassdoor, zip_recruiter,
    bayt, naukri, bdjobs, waas); an unrecognized domain → "other"."""
    u = (url or "").lower()
    for needle, tag in _BOARD_DOMAINS:
        if needle in u:
            return tag
    return OTHER_BOARD


def _board_from_text(text: str) -> str:
    """Best-effort board tag from a free-text cell like "Indeed→ryan.wd1...".
    Empty string when nothing matches (unknown, not "other")."""
    t = (text or "").lower()
    for needle, tag in _BOARD_TEXT_SYNONYMS:
        if needle in t:
            return tag
    return ""


def _board_label(board: str) -> str:
    return _BOARD_LABELS.get(board, board)


def _site_prefix(board: str) -> str:
    """A trailing-spaced human label for a per-site session header/prompt
    ('LinkedIn ', 'Indeed ', …); '' for the combined/legacy build."""
    return "" if _is_combined(board) else f"{_board_label(board)} "


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
        if stripped.startswith("|") and not SEPARATOR_RE.match(stripped):
            cols = split_row(stripped)
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
            report=str(o.get("report") or "").strip(),
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
        # Tracker statuses are authoritative here — emit only still-Evaluated
        # rows (anything acted on, incl. Responded, is never work-order
        # material, and keeping them out makes the "N scored" summary mean the
        # real pool).
        if row.get("status_canonical") != _ACTIONABLE_STATUS:
            continue
        out.append(QueueRole(
            num=str(row.get("num") or ""),
            score=float(score),
            company=str(row.get("company") or "").strip(),
            role=str(row.get("role") or "").strip(),
            url=url,
            status=str(row.get("status_canonical") or "").strip(),
            report=str(row.get("report_path") or "").strip(),
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


def load_all_writeback(out_dir) -> list[TrackedRole]:
    """Harvest agent-written statuses from EVERY work-order file in out_dir: each
    per-site next-roles-<board>.jsonl plus the legacy combined next-roles.jsonl
    (an agent may still be finishing one written before the per-site upgrade).
    Deduped by key via merge_tracked."""
    groups = [load_writeback(p) for p in _work_order_jsonls(out_dir)]
    return merge_tracked(*groups) if groups else []


def drop_late_writeback(items: list[WorkOrderItem], out_dir: Path, *,
                        late: list[TrackedRole] | None = None) -> tuple[list[WorkOrderItem], list[TrackedRole]]:
    """Re-read the on-disk work-orders right before overwriting them and drop any
    item whose row gained a status since the run started. A browser agent may
    be working the previous work-order(s) WHILE this run tailors for minutes —
    without this second read its statuses would be clobbered by the overwrite
    and the same roles re-emitted status-empty (double-apply risk). Reads every
    per-site file (and the legacy one) via load_all_writeback, unless a
    precomputed `late` is passed (run() shares one read across all its sessions).
    Returns (surviving items renumbered, the late statuses to fold into the tracker)."""
    if late is None:
        late = load_all_writeback(out_dir)
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


# Agent writeback statuses that surface in career-ops' applications.md (the UI
# Kanban / cloud tracker). handoff/claimed/drafted are transient or have no clean
# terminal state — they stay in role-status.jsonl for dedup only.
_TRACKER_STATUS = {"applied": "Applied", "skipped": "SKIP"}


def sync_tracker_statuses(harvested: list[TrackedRole], applications_md) -> int:
    """Reflect newly-actioned agent outcomes into career-ops' applications.md —
    the tracker the UI renders and the cloud maintains: applied -> Applied,
    skip -> SKIP. Only a role whose applications.md row is still ACTIONABLE
    (Evaluated) is transitioned, so this NEVER downgrades or clobbers a
    further-along status (Applied/Responded/Interview/Offer/Rejected/Discarded/
    SKIP) — however it was set (agent, Kanban drag, cloud Refresh) — and it is
    idempotent (an already-reflected row is no longer Evaluated). Rows are matched
    on the SAME normalized identity the handoff dedups on (role_key), so a
    legal-suffix / decoration difference between the writeback and the tracker
    (e.g. "Ryan, LLC / SWE - Remote" vs "Ryan / SWE") still resolves. Reuses the
    UI's record_status_changes, so the local Status cell is rewritten AND a
    pending cloud override — anchored on the tracker row's own identity, so it
    re-resolves against the cloud tracker — is queued (dispatched via the
    edit-tracker push). Handoff is local-only, so this only writes local state.
    Returns the number of tracker rows updated."""
    applications_md = Path(applications_md)
    if not applications_md.exists():
        return 0
    pending = [t for t in harvested if t.status in _TRACKER_STATUS]
    if not pending:
        return 0
    # Index the tracker by role_key -> row, reading each row's CURRENT status so
    # the write is gated on it (never trust role-status.jsonl, which non-handoff
    # channels don't update).
    tracker = {role_key(r["company"], r["role"]): r
               for r in _data.parse_applications(applications_md)}
    changes = []
    for t in pending:
        row = tracker.get(t.key)
        if row is None or row.get("status_canonical") != _ACTIONABLE_STATUS:
            continue                       # absent, or no longer actionable (don't clobber)
        changes.append((row["num"], _TRACKER_STATUS[t.status], row["company"], row["role"]))
    _data.record_status_changes(applications_md, changes)   # no-ops on []
    return len(changes)


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
        # canonicalizer so aliases ("Evaluada") count too. Exclude any KNOWN
        # acted-on state (Responded/Interview/Applied/… all canonicalize to a
        # non-blank value ≠ Evaluated); a blank/unset status stays fresh (the
        # export's fresh rows are Evaluated, and an unknown blank is not a known
        # acted-on state). Re-applying to a responded company is worse than a
        # missed fresh role.
        cs = _data.canonical_status(q.status)
        if cs and cs != _ACTIONABLE_STATUS:
            continue
        if board != "both" and q.board != board:
            continue
        key = role_key(q.company, q.role)
        if key in touched or key in seen or _fuzzy_touched(q):
            continue
        seen.add(key)
        fresh.append(q)

    fresh.sort(key=lambda q: q.score, reverse=True)
    # Non-positive limit means "no limit": --limit 0 must not empty the
    # work-order, and --limit -3 must not slice off the 3 lowest (review L1).
    if limit and limit > 0:
        fresh = fresh[:limit]

    return [
        WorkOrderItem(
            rank=i + 1, num=q.num, score=q.score, company=q.company, role=q.role,
            board=q.board, url=q.url, resume_base=suggest_resume_base(q.role),
            report=q.report,
        )
        for i, q in enumerate(fresh)
    ]


def build_sessions(
    queue: list[QueueRole],
    tracker: list[TrackedRole],
    limit: int | None = None,
) -> dict[str, list[WorkOrderItem]]:
    """Partition the fresh work-order into one session per site the scraper
    searches from — so a browser agent can log into each site once and work its
    roles. Reuses build_work_order (all the dedup/fuzzy/status logic and its
    tests) to get the globally-deduped, score-descending list, then groups by
    board and applies `limit` PER SESSION (each site capped independently),
    renumbering ranks 1..N within each session. A company::role key appears in
    exactly one session because build_work_order dedups globally first."""
    ranked = build_work_order(queue, tracker, board="both", limit=None)
    sessions: dict[str, list[WorkOrderItem]] = {}
    for item in ranked:                       # already score-descending
        sessions.setdefault(item.board, []).append(item)
    for board, items in sessions.items():
        # Same non-positive-means-no-limit guard as build_work_order (review L1).
        if limit and limit > 0:
            items = items[:limit]
        for rank, item in enumerate(items, start=1):
            item.rank = rank
        sessions[board] = items
    return sessions


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


def _make_tailor_fn(career_ops: Path, out_dir=None):
    """The real tailor adapter: builds each row's résumé from the handoff
    PROFILE.md via resume_content.generate_for_job (LLM → grounded content-JSON →
    one-page fit → cached PDF). out_dir is where PROFILE.md lives (defaults to the
    handoff dir). Lazy imports so the handoff stage itself never needs python-docx
    unless --tailor is used.

    All rows share ONE caller. The pacing reason this started as no longer
    applies — gemini_limits keys its limiter and budget on the MODEL now
    (`_pacer_for`), precisely so a caller built per row (or per UI request)
    can't hand each one its own full budget. What sharing still buys is the
    resolution itself: one provider/key check and one client, rather than one
    per row. Resolution is deferred to the first actual LLM invocation, so a
    fully cached run still works with no provider key configured."""
    import threading
    from pipeline import resume_content
    from pipeline import resume_tailor as rt
    from pipeline.role_select import ApplyJob

    out_dir = Path(out_dir) if out_dir else default_out_dir()
    lock = threading.Lock()
    cell: list = []

    def shared_caller(system: str, user: str) -> str:
        with lock:
            if not cell:
                cell.append(rt._resolve_caller(None, None))
        return cell[0](system, user)

    def tailor(item: WorkOrderItem):
        # report_path feeds generate_for_job's proof-point branch (report_base
        # defaults to career_ops, where the path is rooted) so tailoring uses the
        # evaluation report, not JD text alone.
        job = ApplyJob(num=item.num, company=item.company, role=item.role,
                       url=item.url, score=item.score, report_path=item.report)
        return resume_content.generate_for_job(career_ops, job, profile_dir=out_dir,
                                               caller=shared_caller)

    return tailor


def render_work_order_jsonl(items: list[WorkOrderItem]) -> str:
    """One compact JSON object per line, machine-consumable by any agent."""
    return "\n".join(json.dumps(asdict(i), ensure_ascii=False) for i in items) + ("\n" if items else "")


# The agent-facing writeback vocabulary, stated ONCE — the work-order .md, the
# batch kickoff prompt, and the per-role prompt all render from this, and
# load_writeback() is the parser that accepts it. A status added/renamed here
# and in STATUS_PRECEDENCE/load_writeback is the whole change — plus _TRACKER_STATUS
# if the new status is a terminal outcome that should surface in applications.md.
WRITEBACK_STATUSES = (
    ("claimed", "you are working on it now (claim-before-apply when sessions run in parallel)"),
    ("applied", "submitted successfully"),
    ("handoff", "prepped but blocked on the human (account, password, CAPTCHA, verification code)"),
    ("skip:<reason>", "evaluated and passed on (keep the reason short)"),
)


def _status_legend_md() -> list[str]:
    """The writeback vocabulary as markdown bullets — shared by the work-order
    header and the folder README so their legends can't diverge."""
    return [f"- `{token}` — {gloss}" for token, gloss in WRITEBACK_STATUSES]


# The closing promise the work-order header and the README both make — stated once
# so the load-bearing "we fold your statuses back" guarantee can't drift. Kept
# general (role-status.jsonl dedups ALL statuses); the applications.md reflection
# is a partial, separate mechanism (applied/skip only) and isn't claimed here.
_WRITEBACK_FOLD_NOTE = ("The next pipeline run folds these statuses into the tracker, "
                        "so recorded roles never reappear.")


def render_work_order_md(items: list[WorkOrderItem], *, board: str = "both",
                         total_queue: int, touched: int) -> str:
    """Human/agent-readable work-order for one site's session, with a short
    how-to header + status legend. Names THIS site's jsonl as the writeback
    target. Agent-agnostic — never names a specific browser agent."""
    jsonl_name = f"{_work_order_stem(board)}.jsonl"
    site = _site_prefix(board)
    lines = [
        f"# Work order — fresh {site}roles for a browser agent",
        "",
        f"{len(items)} fresh {site}roles (of {total_queue} scored; {touched} already handled and excluded).",
        "Work top-down. The score set reading order only — judge each role from the live posting.",
        "",
        "For each role: open the URL, qualify it against the profile, tailor the resume",
        f"(suggested base in the `resume base` column), apply, then record the outcome in `{jsonl_name}`",
        "by setting that row's `status` field:",
        "",
        *_status_legend_md(),
        "",
        _WRITEBACK_FOLD_NOTE,
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


def render_handoff_readme() -> str:
    """The standing agent-instructions file seeded into the handoff dir. Explains
    the work-order files + the writeback loop, generated from WRITEBACK_STATUSES so
    the status legend can't drift. Agent-agnostic — ships to every user."""
    return "\n".join([
        "# Browser-agent work-orders",
        "",
        "This folder is where the job-search pipeline drops **work-orders** for a",
        "browser agent to apply from. Work them; the pipeline reads your outcomes",
        "back on the next run.",
        "",
        "## Files",
        "- `next-roles-<site>.jsonl` / `.md` — one ranked work-order per job site",
        "  (linkedin, indeed, glassdoor, …). The `.jsonl` is what the agent edits;",
        "  the `.md` is a human-readable copy. Work one site at a time, top-down.",
        "- `role-status.jsonl` — the pipeline's dedup tracker. Don't hand-edit it.",
        "",
        "## Working a session",
        "For each row in a site's `next-roles-<site>.jsonl`: open its `url`, judge fit",
        "from the live posting (the score only sets reading order), tailor the resume,",
        "apply through the browser, then record the outcome in that row's `status`:",
        "",
        *_status_legend_md(),
        "",
        _WRITEBACK_FOLD_NOTE,
        "",
        "## Your profile — the living master",
        f"`{HANDOFF_PROFILE}` is the candidate's living master: identity, a",
        "metric-carrying fact bank, an honesty-rated skills inventory, and the",
        "standing answers to the questions applications keep asking (work",
        "authorization, compensation, location, voluntary disclosures). Qualify and",
        f"tailor every role against it. **Grow it:** when you learn a new fact or",
        "answer a new application question, write it back into the matching section",
        f"of `{HANDOFF_PROFILE}` so the next run is smarter — that's the whole point.",
        "",
        f"To rebuild or override a role's résumé, follow `{HANDOFF_RESUME_RUNBOOK}` (the",
        "content-JSON schema, the one-page fill target, and the tailoring rules).",
        "",
    ])


# The content-JSON schema shown in the seeded RESUME-RUNBOOK.md. Kept literal (not
# imported from resume_content._SCHEMA) so seeding never pulls in python-docx or the
# handoff↔resume_content import cycle — keep the two in sync if the schema changes.
_RESUME_SCHEMA_BLOCK = '''```json
{
  "name": "Full Name",
  "contact": "email · phone · City, ST · linkedin.com/in/… · github.com/…",
  "summary": "2-3 lines of prose",
  "skills": [{"label": "Languages", "items": "Python · Go · SQL"}],
  "experience": [{"org": "Employer", "dates": "2022 – Present", "role": "Title",
                  "loc": "City, ST", "bullets": ["a quantified achievement", "…"]}],
  "projects_heading": "Selected Projects",
  "projects": [{"org": "Project", "role": "stack / subtitle", "bullets": ["…"]}],
  "projects_first": false,
  "education": ["B.S. Field — School", "Certification"]
}
```'''


def render_resume_runbook() -> str:
    """The seeded résumé RUN_BOOK: how the pipeline builds each role's résumé from
    PROFILE.md, and how a capable agent can rebuild or override one. Docs only,
    agent-agnostic — the pipeline is the runnable builder; this is the recipe."""
    from pipeline.resume_fit import TARGET_HI, TARGET_LO
    band = f"{int(TARGET_LO * 100)}–{int(TARGET_HI * 100)}%"   # the aim band, single-sourced
    return "\n".join([
        "# Résumé build — how it works, and how to rebuild",
        "",
        "A role in a `next-roles-<site>.jsonl` work-order may carry a `resume_pdf`: a",
        f"one-page PDF the pipeline already tailored for that role from `{HANDOFF_PROFILE}`.",
        "Upload it as-is. This file explains how those are built so you can rebuild or",
        "override one when you want to.",
        "",
        "## The model",
        "A résumé is a **content-JSON** rendered to a **one page** PDF. The pipeline reads",
        f"`{HANDOFF_PROFILE}`, has an LLM produce a content-JSON tailored to the posting",
        "(using ONLY facts in the profile, keeping every metric verbatim, adding/removing",
        f"skills to match the JD), then renders it and auto-fits the layout so it fills",
        f"**{band} of one page**.",
        "",
        "## Content-JSON schema",
        _RESUME_SCHEMA_BLOCK,
        "- `skills[].items` is a `·`-separated string; `experience`/`projects` are ordered",
        "  best/most-relevant first; `projects_first: true` hoists projects above experience",
        "  (use it for AI or projects-forward roles).",
        "",
        "## Rules (the same ones the pipeline follows)",
        f"- Ground everything in `{HANDOFF_PROFILE}`. Never invent an employer, date, title,",
        "  or metric. Keep every quantified result verbatim.",
        "- Tailor emphasis to the JD: add the skills it asks for that the profile supports,",
        "  drop the irrelevant ones (honesty tiers — only Strong/Solid skills).",
        f"- Honor any candidate-specific tailoring rules in `{HANDOFF_PROFILE}` (which role to",
        "  lead with, phrasing preferences, and so on).",
        f"- One page, filled {band}. Prefer real substance over padding.",
        "",
        "## Rebuilding",
        "- **Re-run the pipeline** (rebuilds every role's résumé from the current profile and",
        "  refreshes the cached PDFs; a role is rebuilt when its role or the profile changes):",
        "  `./run.ps1 --skip-scrape --skip-filter --handoff --handoff-tailor`",
        "- **Build one yourself:** produce a content-JSON per the schema above from the",
        "  profile + the posting, render it to a one-page PDF with any docx/PDF tooling, and",
        f"  check it fills ~{band} of one page. Cached résumés live at",
        "  `career-ops/output/<Company> - resume.pdf`.",
        "",
    ])


# ── The living PROFILE.md ───────────────────────────────────────────────────────
# The browser agent's single source of truth, seeded once from what the user
# already told career-ops (cv.md + config/profile.yml) and grown by the agent from
# there. Structure mirrors a hand-built master profile: identity, a metric-carrying
# fact bank, an honesty-rated skills inventory, the standing form-answers, and the
# tailoring rules — so a role can be qualified and a résumé built without re-asking
# the user anything.

# The section titles that open a résumé's experience/projects body — matched as a
# whole line (bare or as a `## …` heading), case-insensitively. This is a
# best-effort list of COMMON section names, not one résumé's layout; when none
# matches, the fact bank falls back to the whole body, so experience/metrics are
# NEVER dropped — the list only makes the common case section more cleanly.
_EXPERIENCE_MARKERS = re.compile(
    r"^\s*#{0,6}\s*(AI ENGINEERING|ENGINEERING|PROFESSIONAL EXPERIENCE|WORK EXPERIENCE|"
    r"EXPERIENCE|EMPLOYMENT( HISTORY)?|WORK HISTORY|CAREER( HISTORY)?|"
    r"SELECTED PROJECTS|PROJECTS|OPEN SOURCE)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _cv_section_body(cv_md: str, name: str) -> str:
    """The lines under a `## <name>` (or bare `<NAME>`) heading, up to the next
    markdown heading or experience marker. Best-effort — an unrecognized layout
    returns "" (the content is still carried whole by _cv_experience_body)."""
    head = re.compile(rf"^\s*#{{0,6}}\s*{re.escape(name)}\b", re.IGNORECASE)
    stop = re.compile(r"^\s*#{1,6}\s+\S")
    out, capturing = [], False
    for ln in cv_md.splitlines():
        if not capturing:
            capturing = bool(head.match(ln))
            continue
        if stop.match(ln) or _EXPERIENCE_MARKERS.match(ln):
            break
        out.append(ln)
    return "\n".join(out).strip()


def _cv_experience_body(cv_md: str) -> str:
    """Everything from the first experience/projects marker to the end of the
    cv.md, verbatim (metrics live in these bullets — they must never be trimmed).
    No marker → the whole body, so nothing is ever silently dropped."""
    m = _EXPERIENCE_MARKERS.search(cv_md)
    return (cv_md[m.start():] if m else cv_md).strip()


# profile.yml is user-editable, so a section can be mis-authored (a list/scalar
# where a mapping is expected) — these coercions keep render_profile_md rendering
# instead of crashing (its OSError-only callers wouldn't catch a TypeError).
def _dsect(profile: dict, key: str) -> dict:
    """A profile.yml sub-section as a dict — a missing or non-dict value → {}."""
    v = profile.get(key)
    return v if isinstance(v, dict) else {}


def _slist(v) -> list[str]:
    """A YAML value as a list of non-empty strings: a list stays; a scalar becomes
    a one-item list (so `superpowers: Full-stack` doesn't render per-character)."""
    items = v if isinstance(v, list) else [v]
    return [str(x).strip() for x in items if x is not None and str(x).strip()]


def _as_bool(v):
    """A YAML bool-ish value as True/False, or None when it's genuinely unknown.
    Tolerates a quoted "false"/"no" (which YAML leaves a truthy string) so a
    hand-edit can't invert a yes/no answer."""
    if isinstance(v, bool) or v is None:
        return v
    s = str(v).strip().lower()
    return False if s in ("false", "no", "none", "n", "0", "") else \
        True if s in ("true", "yes", "y", "1") else None


def _profile_standing_answers(profile: dict) -> list[str]:
    """The application form-answers, drawn from profile.yml. Each is a markdown
    bullet; a missing value degrades to a fill-in prompt rather than vanishing, so
    the agent always sees the full question set."""
    wa = _dsect(profile, "work_authorization")
    comp = _dsect(profile, "compensation")
    loc = _dsect(profile, "location")
    vd = _dsect(profile, "voluntary_disclosures")

    def _ask(v, prompt="(add this)"):
        v = "" if v is None else str(v).strip()
        return v or prompt

    citizenship = _ask(wa.get("citizenship"), "")
    permit = _ask(wa.get("work_permit_type"), "")
    needs_sponsor = _as_bool(wa.get("requires_sponsorship"))
    sponsor = ("no sponsorship required" if needs_sponsor is False
               else "requires sponsorship" if needs_sponsor is True
               else _ask(loc.get("visa_status"), "(confirm sponsorship needs)"))
    work_auth = " ".join(p for p in (citizenship, permit) if p)
    work_auth = f"{work_auth} — {sponsor}" if work_auth else sponsor

    where = ", ".join(p for p in (_ask(loc.get("city"), ""), _ask(loc.get("state"), "")) if p)
    tz = _ask(loc.get("timezone"), "")
    flex = _ask(comp.get("location_flexibility") or loc.get("location_flexibility"), "")
    place = where + (f" ({tz})" if tz else "") + (f" — {flex}" if flex else "")

    comp_line = _ask(comp.get("target_range"), "(add a target range)")
    if comp.get("minimum"):
        comp_line += f" (minimum {comp['minimum']})"

    return [
        f"- **Work authorization:** {work_auth}",
        f"- **Compensation target:** {comp_line}",
        f"- **Location:** {place or '(add this)'}",
        f"- **Gender:** {_ask(vd.get('gender'))}",
        f"- **Race / ethnicity:** {_ask(vd.get('race_ethnicity'))}",
        f"- **Veteran status:** {_ask(vd.get('veteran_status'))}",
        f"- **Disability status:** {_ask(vd.get('disability_status'))}",
    ]


def _strip_h1(text: str) -> str:
    """Drop a single leading `# …` title line from an embedded source, so folding
    it into PROFILE.md doesn't plant a competing top-level heading."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):   # text.strip() already dropped any indent
        lines = lines[1:]
    return "\n".join(lines).strip()


_HEADING_RE = re.compile(r"(?m)^(#{1,6})(\s)")


def _demote_headings(text: str, by: int = 2) -> str:
    """Push every markdown heading in an embedded block `by` levels deeper (capped
    at H6), so the source's `##` sections nest UNDER the PROFILE.md subsection
    they're folded into instead of colliding with PROFILE.md's own headings."""
    return _HEADING_RE.sub(lambda m: "#" * min(6, len(m.group(1)) + by) + m.group(2), text)


# _profile.md sections that duplicate what profile.yml already renders (targets /
# exit story / location / comp) or aren't résumé material (negotiation). Matched
# by lowercased H2 title; ANY other section — including a user's custom one — is
# kept, so section-selecting never silently drops customization.
_PROFILE_MD_DROP = frozenset((
    "your target roles", "your exit narrative", "your location policy",
    "your comp targets", "negotiation scripts",
))


def _select_profile_md(profile_md: str) -> str:
    """The positioning-relevant slice of _profile.md — every `##` section except
    the ones in _PROFILE_MD_DROP (and their subsections). Keeps the unique bits
    (adaptive framing, deal-breakers, portfolio) without the boilerplate."""
    out, dropping = [], False
    for ln in _strip_h1(profile_md).splitlines():
        m = re.match(r"^\s*##\s+(.*\S)", ln)
        if m:
            dropping = m.group(1).strip().lower() in _PROFILE_MD_DROP
        if not dropping:
            out.append(ln)
    return "\n".join(out).strip()


def render_profile_md(*, cv_md: str = "", profile: dict | None = None,
                      article_digest: str = "", profile_md: str = "") -> str:
    """Assemble the seed for the living PROFILE.md from the grounded career-ops
    sources — cv.md (experience/skills), profile.yml (identity/standing answers),
    article-digest.md (hero metrics/proof points), _profile.md (positioning).
    Pure and defensive: any missing source degrades to a labelled scaffold so the
    agent always gets every section. All source text is embedded VERBATIM — the
    seed never trims a metric or a [TODO: confirm] flag."""
    profile = profile or {}
    cand = _dsect(profile, "candidate")
    narr = _dsect(profile, "narrative")
    targets = _slist(_dsect(profile, "target_roles").get("primary"))

    contact = " · ".join(str(v).strip() for v in (
        cand.get("email"), cand.get("phone"), cand.get("location"),
        cand.get("linkedin"), cand.get("github")) if v)
    superpowers = _slist(narr.get("superpowers"))
    summary = _cv_section_body(cv_md, "Professional Summary") or _cv_section_body(cv_md, "Summary")
    skills = _cv_section_body(cv_md, "Skills")
    experience = _cv_experience_body(cv_md)
    # Fold the two extra sources verbatim, but nest their headings (demote) and
    # drop _profile.md's sections that just duplicate profile.yml (section-select).
    positioning_ctx = _demote_headings(_select_profile_md(profile_md))
    proof_points = _demote_headings(_strip_h1(article_digest))

    lines = [
        f"# {cand.get('full_name') or '(your name)'} — candidate profile",
        "",
        "This is your **living master**: the single source of truth the browser",
        "agent qualifies roles and tailors résumés against. It was seeded from your",
        "résumé and onboarding answers; grow it as you go (see the last section).",
        "",
        "## Identity & contact",
        f"- **Name:** {cand.get('full_name') or '(add your name)'}",
        f"- **Contact:** {contact or '(add email · phone · location · links)'}",
        "",
        "## Positioning",
        f"- **Headline:** {narr.get('headline') or '(one line: who you are, what you build)'}",
        f"- **Exit story / motivation:** {narr.get('exit_story') or '(why you are looking)'}",
        f"- **Target roles:** {', '.join(targets) if targets else '(your primary role families)'}",
        "- **Superpowers:** " + ("; ".join(superpowers) if superpowers
                                  else "(what you do better than other candidates)"),
        *(["", "**From your résumé:**", "", summary] if summary else []),
        *(["", "**Targeting & positioning context (from _profile.md):**", "", positioning_ctx]
          if positioning_ctx else []),
        "",
        "## Role fact bank",
        "Your experience and projects, with the numbers that carry them. **Metrics",
        "are load-bearing — copy them into every résumé verbatim and never trim",
        "them.** Add framing variants (the same win, phrased for different job",
        "families) here as you learn what lands.",
        "",
        experience or "_(seed this from your résumé: one block per role/project, "
                      "each bullet keeping its concrete, quantified result.)_",
        *(["", "### Proof points & hero metrics (from article-digest.md)",
           "Keep any `[TODO: confirm]` figures flagged until you re-verify them.",
           "", proof_points] if proof_points else []),
        "",
        "## Skills inventory",
        "Rate each skill by honesty tier — **Strong** (current, lead with it) · "
        "**Solid** · **Lighter / older** (don't over-claim) · **Coursework only** · "
        "**Gated** (surface only for the roles that ask). Only add a skill to a "
        "résumé that is Solid+ here; the tier is the add/remove rule.",
        "",
        skills or "_(seed this from your résumé's Skills section, then tier each one.)_",
        "",
        "## Standing answers",
        "What applications keep asking — answer once here, reuse everywhere:",
        "",
        *_profile_standing_answers(profile),
        "",
        "## Tailoring rules",
        "- Pick the résumé base that matches the role family; lead with what the JD asks for.",
        "- Add or remove skills to mirror the JD — but only ones grounded in the inventory above.",
        "- Keep every quantified result. A specific number beats a vaguer 'tailored' line.",
        "- Fill the page: aim for a full one-pager, not a short one (details in the résumé kit).",
        "",
        "## How this profile grows",
        "Treat this file as append-only memory. When you learn a new fact, get a",
        "new metric, or answer an application question that isn't here yet, write it",
        "back into the matching section above — so every future run starts smarter.",
        "",
    ]
    return "\n".join(lines)


def _career_ops_dir(career_ops=None) -> Path:
    """The career-ops tree: an explicit path, else CAREER_OPS_PATH, else the
    bundled ./career-ops. One resolver shared by run(), main(), and the profile
    seed so they can never read from different trees."""
    if career_ops:
        return Path(career_ops)
    return Path(os.environ.get("CAREER_OPS_PATH") or ROOT / "career-ops")


def _read_or_empty(path) -> str:
    """read_text, but also swallow a non-FileNotFound OSError (an unreadable or
    directory path) and a non-UTF-8 body, so best-effort seeding degrades to a
    scaffold, never a crash. UnicodeDecodeError is a ValueError, not an OSError,
    so it used to escape this on the one input a user is most likely to have
    hand-edited in the wrong encoding."""
    try:
        return read_text(path)
    except (OSError, UnicodeDecodeError):
        return ""


def _load_yaml_or_empty(path) -> dict:
    """Parse a YAML file to a dict, tolerating a missing/unreadable file or
    malformed YAML (all of which just mean "no profile data yet")."""
    try:
        data = yaml.safe_load(_read_or_empty(path))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_profile_sources(career_ops=None) -> dict:
    """Read the seed inputs for render_profile_md from a career-ops tree — the four
    grounded sources: cv.md, config/profile.yml, article-digest.md (hero metrics /
    proof points), modes/_profile.md (positioning). Best-effort — missing/unreadable
    files yield empty inputs and render_profile_md falls back to its scaffold."""
    co = _career_ops_dir(career_ops)
    return {"cv_md": _read_or_empty(co / "cv.md"),
            "profile": _load_yaml_or_empty(co / "config" / "profile.yml"),
            "article_digest": _read_or_empty(co / "article-digest.md"),
            "profile_md": _read_or_empty(co / "modes" / "_profile.md")}


# ── CLI ────────────────────────────────────────────────────────────────────────
def default_out_dir() -> Path:
    """Where the work-order lands when no out_dir is given: HANDOFF_OUT_DIR,
    else output/handoff. The env exists so the files land somewhere the user's
    browser agent can actually reach (e.g. the folder a desktop-agent session
    is connected to). One resolver shared by run(), the CLI, orchestrate, and
    the UI endpoints, so they can never write/read different places."""
    env = (os.environ.get("HANDOFF_OUT_DIR") or "").strip()
    return Path(env) if env else ROOT / "output" / "handoff"


def resolve_profile_md(career_ops=None, out_dir=None) -> str:
    """The living PROFILE.md that drives evaluation (Commit 4), or "" when none
    exists. Resolution order:

      1. the handoff dir — HANDOFF_OUT_DIR (or an explicit out_dir), where the
         browser agent grows PROFILE.md and the résumé builder already reads it;
      2. career-ops/PROFILE.md — the target a later commit decodes the cloud
         PROFILE secret into, so the cloud evaluator sees the same master;
      3. "" — the caller (build_system_prompt) then falls back to the cv.md /
         profile.yml / _profile.md / article-digest.md seed fragments.

    _read_or_empty already strips whitespace and swallows a bad path, so a blank
    or unreadable file degrades to the next source rather than winning or crashing."""
    master = _read_or_empty(Path(out_dir or default_out_dir()) / HANDOFF_PROFILE)
    if master:
        return master
    return _read_or_empty(_career_ops_dir(career_ops) / HANDOFF_PROFILE)


def bootstrap_handoff_dir(out_dir, *, career_ops=None) -> Path:
    """Ensure the handoff directory exists and carries the two standing files the
    browser agent needs: the instructions README (HANDOFF-README.md) and the
    living master (PROFILE.md, seeded from career-ops). Non-clobbering — an
    existing file is left untouched (the folder accumulates the user's own work,
    and the agent grows PROFILE.md) — and idempotent, so it's safe to call on
    every run / at setup / when the UI sets the path. career-ops is read only when
    PROFILE.md actually needs seeding. Returns the README path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    readme = out_dir / HANDOFF_README
    if not readme.exists():
        atomic_write_text(readme, render_handoff_readme())
    runbook = out_dir / HANDOFF_RESUME_RUNBOOK
    if not runbook.exists():
        atomic_write_text(runbook, render_resume_runbook())
    profile = out_dir / HANDOFF_PROFILE
    if not profile.exists():
        atomic_write_text(profile, render_profile_md(**_load_profile_sources(career_ops)))
    return readme


def _writeback_contract(work_order: Path) -> str:
    """The closing paragraph every agent prompt ends with: where to record the
    outcome and the status vocabulary (rendered from WRITEBACK_STATUSES so the
    prompts can't drift from what load_writeback parses)."""
    statuses = "; ".join(f'"{token}" — {gloss}' for token, gloss in WRITEBACK_STATUSES)
    return (
        f"Record each outcome in {work_order} by setting that row's \"status\" "
        f"field: {statuses}. The pipeline folds these statuses into its tracker "
        "on the next run, so recorded roles never reappear."
    )


def kickoff_prompt(work_order: Path, board: str = "") -> str:
    """The paste-ready batch prompt for a browser agent working ONE site's
    session — names that session's work-order file (+ its .md sibling) and the
    writeback contract, and deliberately names no specific agent (this template
    ships to users of any of them)."""
    work_order = Path(work_order)
    site = _site_prefix(board)
    profile = work_order.parent / HANDOFF_PROFILE
    return (
        f"Work the {site}job-application work-order at:\n"
        f"  {work_order}\n"
        f"  (human-readable copy: {work_order.with_suffix('.md')})\n\n"
        f"Qualify and tailor every role against the candidate profile at {profile}.\n\n"
        "Go top to bottom. For each row: open its url, judge fit from the live "
        "posting (the score only sets reading order), tailor the resume "
        "(resume_base names a base; a non-empty resume_pdf is pre-tailored), "
        "and apply through the browser.\n\n"
        + _writeback_contract(work_order)
    )


def session_summaries(out_dir) -> list[dict]:
    """Enumerate the per-site sessions written to out_dir: one dict per NON-EMPTY
    next-roles-<site>.jsonl carrying its board, human label, file path, fresh
    count, and a paste-ready kickoff prompt. The UI reads results from this so the
    filename→session mapping lives in one place (not re-derived at each consumer)."""
    out: list[dict] = []
    for wo in _work_order_jsonls(out_dir, include_legacy=False):
        fresh = sum(1 for _ in _iter_jsonl(wo))
        if not fresh:
            continue
        board = _board_from_filename(wo.name)
        out.append({
            "board": board,
            "label": _board_label(board),
            "work_order": str(wo),
            "fresh": fresh,
            "kickoff": kickoff_prompt(wo, board=board),
        })
    return out


def role_prompt(company: str, role: str, url: str, *,
                report: Path | None = None,
                profile: Path | None = None,
                resume: Path | None = None) -> str:
    """The paste-ready prompt for handing ONE role to a browser agent. The
    caller (the UI route) gathers the facts/paths; this module renders them so
    the writeback contract and the appended-row schema — which must mirror the
    keys load_writeback() reads — live beside their parser."""
    # The role's writeback target — and its living profile — live in the same
    # handoff dir as its own site's session file.
    out_dir = default_out_dir()
    work_order = work_order_paths(out_dir, board_of(url))[0]
    profile = profile or out_dir / HANDOFF_PROFILE
    lines = [
        "Apply to this role through the browser, then record the outcome.",
        "",
        f"Company: {company}",
        f"Role: {role}",
        f"Posting: {url}",
    ]
    if report:
        lines.append(f"Evaluation report: {report}")
    lines.append(f"Candidate profile (qualify + tailor against it): {profile}")
    if resume:
        lines.append(f"Tailored resume: {resume}")
    else:
        lines.append("Resume: no tailored copy cached — tailor one from "
                     "resumes/resume.docx, or apply with your default resume.")
    fallback_row = json.dumps({"company": company, "role": role, "url": url,
                               "status": "applied"})
    lines += [
        "",
        _writeback_contract(work_order),
        f"If the role isn't listed there, append a JSON line: {fallback_row}",
    ]
    return "\n".join(lines)


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
    out_dir = Path(out_dir) if out_dir else default_out_dir()
    try:
        # Seed the agent README + PROFILE.md (from the SAME career-ops tree the
        # rest of run() reads), even on a no-queue run.
        bootstrap_handoff_dir(out_dir, career_ops=career_ops)
    except OSError as e:
        # Best-effort: a misconfigured/unwritable HANDOFF_OUT_DIR must not crash a
        # no-queue run (a real work-order write below surfaces the failure loudly).
        print(f"[handoff] could not prepare {out_dir} ({e})")
    co = _career_ops_dir(career_ops)
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
    # Fold in any statuses the agent wrote into the previous work-order(s) — one
    # per site now, plus a legacy combined file if one predates the upgrade —
    # before we overwrite them.
    writeback = load_all_writeback(out_dir)
    known = {norm_company(q.company) for q in queue if q.company}

    tracked = reconcile(job_log_text, existing, writeback=writeback, known_companies=known)
    write_tracker(tracker_path, tracked)

    # One session per site the scraper searches from (ranked + limited within
    # each site). A specific --board narrows the build to that one site.
    sessions = build_sessions(queue, tracked, limit=limit)
    if not _is_combined(board):
        sessions = {board: sessions.get(board, [])}

    if tailor:
        min_score = tailor_min_score if tailor_min_score is not None else env_float("APPLY_TAILOR_MIN_SCORE", 4.0)
        try:
            tailor_fn = _make_tailor_fn(co.resolve(), out_dir)
        except ImportError as e:
            print(f"[handoff] --tailor unavailable ({e}) — emitting the work-order without resume files")
        else:
            # Tailoring caches per company, so it must dedup ACROSS sessions:
            # flatten every session's rows in global score order and let
            # enrich_with_resumes pick the single best row per company. Rows are
            # mutated in place, so the session lists see the attached resume.
            all_items = sorted((i for items in sessions.values() for i in items),
                               key=lambda i: i.score, reverse=True)
            enrich_with_resumes(all_items, tailor_fn, min_score=min_score,
                                workers=max(1, workers or env_int("BATCH_CONCURRENCY", 3)))

    # A long tailor run leaves a window where a live agent session wrote new
    # statuses into an old work-order — drop those rows from each session and
    # fold the statuses into the tracker rather than clobbering (double-apply
    # risk). One read of every per-site file (+ the legacy one), shared across
    # sessions (load_all_writeback is directory-wide, not per-session).
    late = load_all_writeback(out_dir)
    for b in list(sessions):
        sessions[b], _ = drop_late_writeback(sessions[b], out_dir, late=late)
    if late:
        tracked = merge_tracked(tracked, late)   # already deduped by load_all_writeback
        write_tracker(tracker_path, tracked)

    # Reflect newly applied/skipped roles into career-ops' applications.md so they
    # surface in the UI Kanban and, via the queued override -> edit-tracker push,
    # the cloud. Gated on the tracker row still being Evaluated (never clobbers a
    # further-along status); role-status.jsonl stays the dedup ledger regardless.
    synced = sync_tracker_statuses(late, co / "data" / "applications.md")
    if synced:
        print(f"[handoff] applications.md: marked {synced} role(s) from agent writeback")

    # Write one next-roles-<site>.{jsonl,md} per session.
    written: set[Path] = set()
    for b, items in sessions.items():
        jsonl_path, md_path = work_order_paths(out_dir, b)
        atomic_write_text(jsonl_path, render_work_order_jsonl(items))
        atomic_write_text(md_path, render_work_order_md(
            items, board=b, total_queue=len(queue), touched=len(tracked)))
        written.update((jsonl_path, md_path))

    # Empty any leftover work-order file whose site produced nothing this run so
    # an agent never re-works a stale list. On a build-all run that's every
    # not-written file; a narrowed build must leave the OTHER sites' sessions
    # intact — but the legacy combined next-roles.jsonl is never a valid per-site
    # session, so it's always swept (else a pre-upgrade file lingers).
    combined = _is_combined(board)
    for stale in _work_order_jsonls(out_dir):
        if stale in written:
            continue
        if not combined and stale.name != WORK_ORDER_JSONL:
            continue
        atomic_write_text(stale, render_work_order_jsonl([]))
        atomic_write_text(stale.with_suffix(".md"), render_work_order_md(
            [], board=_board_from_filename(stale.name),
            total_queue=len(queue), touched=len(tracked)))

    # ASCII-only: Windows consoles often run cp1252, where fancy arrows crash print.
    total_fresh = sum(len(v) for v in sessions.values())
    print(f"[handoff] {len(queue)} scored -> {len(tracked)} tracked -> "
          f"{total_fresh} fresh across {len(sessions)} session(s)")
    print(f"[handoff] tracker:    {tracker_path}")
    for b in sorted(sessions):
        print(f"[handoff] session {b}: {work_order_paths(out_dir, b)[0]}")
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
    ap.add_argument("--bootstrap-dir", action="store_true",
                    help="Just create + seed the handoff directory (README) and exit — "
                         "no queue/build. Used by setup to prepare the folder up front.")
    ap.add_argument("--board", choices=["both", *sorted(KNOWN_BOARDS)], default="both",
                    help="Restrict the build to one site's session (default: both = "
                         "a session per site the scraper searches from)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap EACH site's session to the top N roles (per-site, not a global total)")
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
    if args.bootstrap_dir:
        readme = bootstrap_handoff_dir(args.out_dir or default_out_dir(),
                                       career_ops=args.career_ops)
        print(f"[handoff] seeded {readme}")
        return 0
    return run(
        queue_path=args.queue, job_log=args.job_log, tracker=args.tracker,
        out_dir=args.out_dir, board=args.board, limit=args.limit,
        tailor=args.tailor, tailor_min_score=args.tailor_min_score,
        career_ops=args.career_ops,
    )


if __name__ == "__main__":
    line_buffer_stdout()

    import sys
    sys.exit(main())
