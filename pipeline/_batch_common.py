"""Shared utilities for batch job evaluation (submit, evaluate, retrieve)."""

import csv
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from pipeline.tracker_layout import (
    SCORE_SENTINELS, data_rows, header_columns, is_score_cell, split_row,
    # Aliased: `report_num` is also the name of the number itself in
    # write_job_result and below, and a local shadowing a callable is a
    # TypeError waiting for whoever adds the next call.
    report_num as row_report_num,
)

MAX_TOKENS = 8192


def read_url_set(path: Path) -> set[str]:
    """Read a newline-delimited URL file into a set of stripped, non-blank
    lines. Missing file → empty set."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def parse_date_posted(val: str) -> datetime | None:
    """Parse a date_posted string. Tolerates both date and datetime forms."""
    if not val or val.strip().lower() in ("", "none", "nan", "nat"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except ValueError:
            continue
    return None


def atomic_write_text(path: Path, content: str) -> None:
    """Write content atomically — write to a tmp sibling then os.replace.
    os.replace is atomic on POSIX and Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return default


def tail_text(path: Path, tail_lines: int, max_bytes: int = 16384) -> str:
    """The last `tail_lines` lines of a log file, reading at most `max_bytes`
    from the end rather than the whole file — cheap enough to poll a live,
    growing log. Returns "" before the file exists or if it can't be read.
    Shared by the UI's local-run and handoff-build log streams."""
    if tail_lines <= 0:
        return ""   # else [-0:] would slice the WHOLE window, not zero lines
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            raw = f.read()
    except OSError:
        return ""
    return "\n".join(raw.decode("utf-8", errors="replace").splitlines()[-tail_lines:])


def env_float(name: str, default: float) -> float:
    """A float env override that can NEVER crash startup: a malformed or
    set-but-empty value warns and falls back rather than raising. Shared by
    orchestrate's flag defaults and batch_evaluate's timeout/budget knobs so
    they parse env the same way."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[config] ignoring invalid {name}={raw!r} (using {default})")
        return default


def env_int(name: str, default: int) -> int:
    """The integer sibling of env_float — a never-crash int env override
    (malformed / set-but-empty warns and falls back)."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] ignoring invalid {name}={raw!r} (using {default})")
        return default


def thinking_disabled() -> bool:
    """Reasoning/thinking is unnecessary for short tailoring output and cover
    letters (and slows/garbles vLLM-served reasoning models like MiMo/Qwen3), so
    disable it by default for those use cases. Set APPLY_ENABLE_THINKING=true to
    keep it on (e.g. if a provider rejects the toggle)."""
    return os.environ.get("APPLY_ENABLE_THINKING", "").strip().lower() not in ("1", "true", "yes")


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a process id (used by the cross-process
    eval lock and the UI's local-run orphan guard).

    On Windows os.kill(pid, 0) would SEND CTRL_C_EVENT, so use OpenProcess +
    GetExitCodeProcess — OpenProcess succeeds on a zombie while any handle to
    it is held, so only an exit code of STILL_ACTIVE means actually running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def has_pending_tracker_additions(tracker_dir: Path) -> bool:
    """True when career-ops/batch/tracker-additions holds un-merged TSVs.

    merge-tracker.mjs MOVES each TSV into tracker-additions/merged/ once it has
    folded it into applications.md, so a top-level *.tsv means "evaluated but
    not yet merged" — the signal that lets an interrupted run heal on a later
    invocation even when it processes zero new jobs."""
    try:
        return any(tracker_dir.glob("*.tsv"))
    except OSError:
        return False


def normalize_company(s: str) -> str:
    """Lowercase + strip everything non-alphanumeric — the canonical company
    key used for identity matching (mirrors merge-tracker.mjs's normalizeCompany).
    One definition so filename lookup, cover-letter caching, and tracker
    re-anchoring can't drift apart."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# ── Cross-process lock ────────────────────────────────────────────────────────
# One mechanism for every pid-based guard (the batch-eval single-flight lock and
# the UI local-run orphan guard). A lock file holds "pid timestamp". It is
# reclaimable only when its holder is DEAD or its timestamp is older than
# max_age — and a live holder REFRESHES its timestamp (heartbeat) so a long,
# progressing run never looks stale and is never stolen. The timestamp is the
# pid-reuse safety valve: if the holder died and the OS recycled its pid to an
# unrelated live process, the frozen timestamp eventually goes stale and the
# lock is reclaimed instead of wedging forever.

# An empty lock file is a racer caught between the O_EXCL create and its pid
# write — NOT free. Only treat it as reclaimable once it has sat empty past this
# grace window (i.e. the writer crashed mid-create).
_EMPTY_LOCK_GRACE = 5.0


def read_process_lock(path: Path) -> tuple[int, float]:
    """(pid, timestamp) from a lock file; (0, 0.0) when absent/empty/unparseable."""
    try:
        parts = path.read_text(encoding="utf-8").split()
        return int(parts[0]), (float(parts[1]) if len(parts) > 1 else 0.0)
    except (OSError, ValueError, IndexError):
        return 0, 0.0


def write_process_lock(path: Path, pid: int) -> None:
    """Atomically record `pid` as the lock holder with a fresh timestamp. Used
    to register a CHILD process (the local-run orchestrate subprocess) as the
    holder, and internally by acquire/refresh."""
    try:
        atomic_write_text(path, f"{pid} {time.time()}")
    except OSError:
        pass


def _lock_reclaimable(path: Path, max_age: float) -> bool:
    """True when no LIVE, non-stale holder owns the lock."""
    pid, ts = read_process_lock(path)
    if pid == 0:
        # Empty/unparseable: a racer mid-create (don't steal) unless it has sat
        # untouched past the grace window (writer crashed mid-create).
        try:
            return (time.time() - path.stat().st_mtime) > _EMPTY_LOCK_GRACE
        except OSError:
            return True
    if pid == os.getpid():
        return True
    if not pid_alive(pid):
        return True
    # Live holder: reclaim only if its heartbeat went stale — a genuine,
    # progressing run keeps its timestamp fresh.
    return ts > 0 and (time.time() - ts) > max_age


def acquire_process_lock(path: Path, *, max_age: float) -> bool:
    """Atomically take a self-lock (the current process becomes the holder).
    O_CREAT|O_EXCL makes the create-if-absent atomic so two simultaneous starts
    can't both pass a read-then-write check. Returns False when a live, non-stale
    holder owns it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()} {time.time()}".encode("utf-8")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _lock_reclaimable(path, max_age):
                return False
            try:
                path.unlink()       # reclaim a dead/stale lock, then retry O_EXCL
            except OSError:
                pass
            continue
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True
    return False


def refresh_process_lock(path: Path) -> None:
    """Heartbeat: rewrite our timestamp so a long live run never looks stale.
    No-op unless we own the lock."""
    pid, _ = read_process_lock(path)
    if pid == os.getpid():
        write_process_lock(path, os.getpid())


def release_process_lock(path: Path) -> None:
    """Delete the lock, but only if we still own it (a stale-takeover may have
    replaced our pid, and we must not delete another process's lock)."""
    pid, _ = read_process_lock(path)
    if pid == os.getpid():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def process_lock_active(path: Path, *, max_age: float) -> bool:
    """True if a LIVE, non-stale holder owns the lock — the non-acquiring probe
    behind is_running()-style checks (where the holder is a CHILD pid, not us)."""
    if not path.exists():
        return False
    return not _lock_reclaimable(path, max_age)


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"jobs": {}}


def max_report_num(reports_dir: Path, state: dict) -> int:
    """Highest report number already claimed, so the next one is free.

    A `NNN-RESERVED.md` lock counts, deliberately: claiming the number is the
    lock's whole purpose, so skipping it would hand out a number career-ops has
    already reserved. This is the OPPOSITE of the rule in
    `pipeline/app/data.py:find_report_file`, which must skip locks because it
    resolves a number to a file to render. Do not "make them consistent" —
    they answer different questions.
    """
    max_num = 0
    if reports_dir.exists():
        for f in reports_dir.glob("*.md"):
            m = re.match(r"^(\d+)-", f.name)
            if m:
                max_num = max(max_num, int(m.group(1)))
    for job in state.get("jobs", {}).values():
        try:
            max_num = max(max_num, int(job.get("report_num", 0)))
        except (ValueError, TypeError):
            pass
    return max_num


def max_tracker_num(applications_md: Path, state: dict) -> int:
    max_num = 0
    if applications_md.exists():
        # Through the shared walk: the `#` is not guaranteed to be the first
        # cell (detect_columns imposes no column order), and a header or a
        # separator row must not be read as data.
        for columns, cells in data_rows(applications_md.read_text(encoding="utf-8")):
            num_idx = columns.index("num")
            if len(cells) > num_idx:
                try:
                    max_num = max(max_num, int(cells[num_idx]))
                except ValueError:
                    pass
    for job in state.get("jobs", {}).values():
        try:
            max_num = max(max_num, int(job.get("tracker_num", 0)))
        except (ValueError, TypeError):
            pass
    return max_num


def extract_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_json_loose(text: str) -> dict | None:
    """Try parsing `text` as JSON. If that fails, try each balanced {...}
    block found in the text and return the first one that parses."""
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Walk the string finding balanced { ... } blocks (depth-aware), so we
    # don't over-capture when the response contains multiple JSON-like spans.
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                try:
                    return json.loads(text[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    start = -1
                    continue
    return None


# Tracker-additions TSV schema — note STATUS comes before SCORE here, the
# opposite of applications.md (merge-tracker.mjs swaps them when merging).
# Mirrors what the LLM is told to emit in _profile.md. Named rather than
# counted because this module writes the file and `pipeline/app/data.py` reads
# it back: two spellings of one layout is how a column insertion upstream gets
# fixed in one reader and silently misread by the other.
ADDITION_COLUMNS = [
    "num", "date", "company", "role", "status", "score", "pdf", "report", "notes",
]
_TRACKER_TSV_COLUMNS = len(ADDITION_COLUMNS)
# Every cell the sanitizers below reach for, resolved from that list rather than
# written as a literal — the guards only check the TOTAL, so a column inserted
# upstream would leave them silently rewriting the wrong cells.
_ROLE_IDX = ADDITION_COLUMNS.index("role")
_STATUS_IDX = ADDITION_COLUMNS.index("status")
_SCORE_IDX = ADDITION_COLUMNS.index("score")
_REPORT_IDX = ADDITION_COLUMNS.index("report")
_NOTES_IDX = ADDITION_COLUMNS.index("notes")

# The Report cell is `[N](path)`, and it is the one cell whose shape identifies
# it — which makes it the anchor for deciding whether a row's nine columns are
# where they should be. `app/data.py:_realign_cells` anchors on the same cell for
# the same reason.
_REPORT_CELL_RE = re.compile(r"^\[\w+\]\([^)]+\)$")


def _row_parts(tracker_tsv: str) -> list[str] | None:
    """The cells of an addition row whose nine columns are IN PLACE, else None.

    One classifier, shared by every positional sanitizer, so they cannot disagree
    about which rows to touch — which is exactly what went wrong twice. With
    `== 9` guards, career-ops' documented "9 columns plus a trailing `url`" row
    skipped the score normalization while `_strip_role_pipe`, which had no guard,
    still rewrote it: the file changed, the repair was counted and logged, and
    the row was refused for its score anyway. With `>= 9` guards, a row carrying
    an EARLY extra cell (a tab inside the company name) had `N/A` written into
    its status and a URL prepended to its report link — and merge-tracker, now
    seeing exactly one score-shaped cell, accepted the garbage where it had been
    refusing it, so the loss guard saw the row "land". A width alone cannot
    tell those two ten-cell rows apart. The Report cell can: a trailing extra
    leaves it at index 7, an inserted one shifts it.

    Nine cells are taken as-is — that is our own prompt's shape, and the chain
    has always operated on it without an anchor check. Fewer than nine is
    declined by EVERY step, `_strip_role_pipe` included, so a short row cannot
    change on disk and be reported as repaired."""
    # A whitespace-only row splits to nine EMPTY cells — `_restore_trailing_cells`
    # pads eight tabs to nine — and has nothing to sanitize; writing a URL into
    # it manufactures a half-populated row out of nothing.
    if not tracker_tsv.strip():
        return None
    parts = tracker_tsv.strip("\r\n").split("\t")
    if len(parts) < _TRACKER_TSV_COLUMNS:
        return None
    if len(parts) > _TRACKER_TSV_COLUMNS and not _REPORT_CELL_RE.match(parts[_REPORT_IDX].strip()):
        return None
    return parts



def _restore_trailing_cells(tracker_tsv: str) -> str:
    """Pad a row back to the full column count when trailing cells were empty.

    An empty Notes cell is a trailing TAB, and every strip between the model and
    here eats it — `extract_tag` strips the tag body before we ever see the row.
    The row then has 8 fields, and each sanitizer below no-ops on its 9-column
    guard: the score is left unnormalized, merge-tracker cannot tell it from the
    status, and it refuses the row and archives it. So the row that arrives one
    cell short is exactly the row this chain exists to save.

    Exactly ONE cell, deliberately. Notes is the last column, so a lost trailing
    tab can only ever cost one. A row short by more is genuinely malformed, and
    padding it would manufacture a well-formed-looking row out of garbage —
    the sanitizers' column guards are the right answer there, and stay in force."""
    parts = tracker_tsv.strip("\r\n").split("\t")
    if len(parts) == _TRACKER_TSV_COLUMNS - 1:
        parts.append("")
    return "\t".join(parts)


def _strip_role_pipe(tracker_tsv: str) -> str:
    """Remove any '| <suffix>' the LLM appends to the role column.

    The LLM sometimes writes "Software Engineer | Remote" or
    "Platform Engineer | $35/hr Remote" as the role. That pipe is harmless in
    a tab-delimited TSV but merge-tracker.mjs copies it verbatim into a
    markdown table row where it splits the cell, shifting every subsequent
    column (score ends up as status, report link ends up in notes, etc.)."""
    # rstrip("\n") not strip(): a trailing TAB is an empty Notes cell, and
    # stripping it drops the row to 8 fields — after which the two sanitizers
    # below both no-op on their 9-column guard, leaving the unnormalized score
    # merge-tracker then refuses. The row this chain exists to save was the one
    # it skipped.
    parts = _row_parts(tracker_tsv)
    if parts is None:
        return tracker_tsv
    parts[_ROLE_IDX] = re.sub(r"\s*\|.*$", "", parts[_ROLE_IDX]).strip()
    return "\t".join(parts)


# Score cells merge-tracker.mjs accepts verbatim (tracker-parse.mjs's
# `looksLikeScoreCell`), beyond the plain `N/5` / `N.N/5` form.
_SCORE_SENTINELS = SCORE_SENTINELS
# Two ways to read a score out of a cell, tried in order.
#
# An explicit "N/5" is unambiguous wherever it sits, so decoration around it is
# harmless: "(4.5/5)", "~4.5/5", "Score: 4.5/5" all mean 4.5. Tracker rows in the
# wild carry all of these, and they are what the READ side sorts by.
#
# Failing that, the cell must OPEN with the number. Searching anywhere turns
# prose into a score, and the invented value is plausible rather than obviously
# wrong — "Top 5%" became 5/5, "not scored (4 blockers)" became 4/5. A fabricated
# top score is the exact harm the out-of-range branch below refuses to cause,
# since the handoff work-order ranks by score descending. Leading punctuation is
# allowed; a leading LETTER is not, because that is what prose looks like.
_SCORE_OVER_FIVE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*/\s*5\b")
_SCORE_LEAD_RE = re.compile(r"^[^0-9A-Za-z]*(\d+(?:[.,]\d+)?)")


def score_value(cell: str) -> float | None:
    """The numeric score in a cell, or None. One reader for both ends: the write
    side normalizes what the model emitted, the read side sorts the tracker by
    it, and they used to disagree — "4,2/5" parsed as 4.0 where the writer meant
    4.2."""
    text = (cell or "").replace("*", "")
    m = _SCORE_OVER_FIVE_RE.search(text) or _SCORE_LEAD_RE.match(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _normalize_score_cell(tracker_tsv: str) -> str:
    """Rewrite the score column (col 6) to the exact `N.N/5` shape
    merge-tracker.mjs requires, or to the `N/A` sentinel when it holds no number.

    merge-tracker resolves which of columns 5-6 is the status and which is the
    score by asking whether exactly one of them *looks like* a score cell
    (`tracker-parse.mjs:resolveScoreStatus` — `/^\\d+(?:\\.\\d+)?\\/5$/`, plus the
    N/A / DUP / dash sentinels). When neither matches it refuses the row rather
    than risk merging a column swap. That refusal is counted as `skipped`, not
    as a failure: merge-tracker still exits 0 AND still moves the TSV into
    tracker-additions/merged/. So a model that writes "4.2" instead of "4.2/5"
    loses the evaluation outright — the report is on disk, the row never
    reaches applications.md, `run_merge_tracker` sees returncode 0, and
    `has_pending_tracker_additions` goes false so no later run retries it.

    The older merge-tracker had a heuristic whose final fallback assumed
    "status then score" — this row order — so any score string merged and the
    prompt's "format X.X/5" rule was advisory. It is load-bearing now, and a
    prompt rule is not something a local Ollama/Groq/DeepSeek model reliably
    honours. This is the one place we own the value, so normalize it here
    rather than depend on model compliance."""
    parts = _row_parts(tracker_tsv)
    if parts is None:
        return tracker_tsv
    raw = parts[_SCORE_IDX].replace("*", "").strip()
    if raw.upper() in _SCORE_SENTINELS:
        parts[_SCORE_IDX] = raw.upper()
        return "\t".join(parts)
    # The model may have written the pair the other way round (status in col 6,
    # score in col 5). merge-tracker resolves that on its own — it asks which
    # ONE of the two looks like a score. Writing N/A here would make BOTH look
    # like one, so its "exactly one" test fails and it refuses a row it would
    # otherwise have merged correctly: the loss this function exists to prevent,
    # caused by this function. Leave a swapped pair alone.
    if is_score_cell(parts[_STATUS_IDX]):
        return tracker_tsv
    value = score_value(raw)
    if value is None:
        # No number at all (empty, "unknown", a stray status). N/A is a shape
        # merge-tracker recognises, so the row still merges and the evaluation
        # stays visible — a scoreless row beats a silently discarded one.
        parts[_SCORE_IDX] = "N/A"
        return "\t".join(parts)
    # Out of range means the model answered on some other scale ("8/10", "84%")
    # and we cannot know which. Clamping to 5 would invent a perfect score, and
    # the handoff work-order ranks by score descending — a fabricated 5 puts the
    # role first. N/A merges the row and leaves it unranked, which is the true
    # statement.
    parts[_SCORE_IDX] = f"{value:g}/5" if 0 <= value <= 5 else "N/A"
    return "\t".join(parts)


# Any http(s) URL, and the `req <id>` we write — the two things a notes cell is
# checked for before something is prepended to it.
_NOTES_URL_RE = re.compile(r"https?://\S+")
_NOTES_REQ_ID_RE = re.compile(r"\breq[\s:#_-]*([A-Za-z0-9_-]+)", re.I)


def _prepend_to_notes(tracker_tsv: str, text: str, already_present) -> str:
    """Put `text` at the front of the notes cell, unless it is already there.

    The notes cell has grown a grammar — `req <id> — <url> — <the model's
    sentence>` — read back by merge-tracker's `extractReqNumber`, by the UI's
    "Open posting" link and by bridge's URL dedup. One composer, so the column
    guard, the ` — ` separator and the front-of-cell rule are stated once and a
    third injector cannot invent a fourth shape.

    `already_present(notes)` is the only real difference between the callers:
    "any URL at all" defers to a model that supplied its own, while "this exact
    id" is idempotence. Both are one predicate slot, not two mechanisms.

    A row `_row_parts` declines is returned untouched — better to lose the
    annotation than to write it into the wrong cell."""
    if not text:
        return tracker_tsv
    parts = _row_parts(tracker_tsv)
    if parts is None:
        return tracker_tsv
    notes = parts[_NOTES_IDX].strip()
    if already_present(notes):
        return tracker_tsv
    parts[_NOTES_IDX] = f"{text} — {notes}" if notes else text
    return "\t".join(parts)


def _inject_url_into_notes(tracker_tsv: str, url: str) -> str:
    """Splice the job URL into the notes cell so the UI's "Open posting" link
    works.

    The LLM generates the notes cell freely — per our prompt it's a
    one-sentence summary, not the URL. The UI's report pane resolves the
    "Open posting" target by regex-matching the first http(s) URL in the notes
    cell ([app.js:extractUrl]), so without this the link has nothing to point
    at and renders as `#`. Splicing the URL here keeps the prompt unchanged
    and works for every provider / model.

    If the LLM already put a URL in notes, leave it alone."""
    return _prepend_to_notes(tracker_tsv, url,
                             lambda notes: bool(_NOTES_URL_RE.search(notes)))


# Markdown decoration, normalized before matching rather than matched around.
# JobSpy's `description_format` defaults to MARKDOWN and `scrape.py` forwards it
# untouched, so an Indeed JD reaches the cache as `**Job ID:** 88214`, and
# markdownify escapes `_` and `-` mid-word (`R\_1488728`, `JR\-00124259`). All
# three defeat a pattern written for plain prose. LinkedIn's descriptions are
# backfilled as plain text by screen.py, so this hit one board and not the other
# — which is worse than hitting both, because the extractor then looks like it
# works.
#
# The two characters get DIFFERENT treatment, and the difference was measured
# rather than guessed. A backslash is an escape INSIDE an id, so it is deleted:
# spacing it out would turn `R\_1488728` into `R _1488728` and capture a bare
# `R`. An asterisk is emphasis AROUND words, so it becomes a space: deleting it
# glued `Remote**Requisition Number` into `RemoteRequisition`, and the label's
# leading `\b` no longer held — two real postings whose requisition number sat
# right there, three newlines below the label, read as having none.
_MARKDOWN_ESCAPE_RE = re.compile(r"\\")
_MARKDOWN_EMPHASIS_RE = re.compile(r"\*")


def _demarkdown(text: str) -> str:
    return _MARKDOWN_EMPHASIS_RE.sub(" ", _MARKDOWN_ESCAPE_RE.sub("", text))


# A requisition id as JOB-DESCRIPTION PROSE states it. Deliberately narrower
# than merge-tracker's own REQ_NUMBER_RE, which reads the tracker's Notes cell:
# that regex accepts a BARE `job`/`req`/`ref` label because a human wrote that
# cell and meant the number they put there. A JD is thousands of words we did not
# write, where "job 2 of 5" and "reference 401k" are ordinary prose — so here the
# label must carry an explicit qualifier. Recall costs nothing (no id is exactly
# today's behaviour); a wrong id SPLITS two rows that should fold.
#
# `#` is a qualifier in its own right ("Req #1311") and must NOT be followed by
# `\b`: both it and the space after it are non-word characters, so the boundary
# could only hold when a digit followed immediately — `Req # 1311` and
# `Position #: 12345` matched nothing. Upstream sidesteps this by keeping `#` in
# its separator class; here the qualifier is mandatory, so it needs both roles.
#
# `code` is deliberately absent from the qualifiers: in public-sector and
# healthcare postings — the shape this exists for — "Job Code: 4021" is a pay
# grade shared by every posting of that title, so reading it as a requisition
# would split a cross-board pair rather than two genuine reqs.
_JD_REQ_ID_RE = re.compile(
    r"\b(?:job|requisition|req|posting|position|vacancy|reference|ref)\s*"
    r"(?:(?:id|no|nr|num|number)\b|#)[\s:#=.-]*"
    r"([A-Za-z0-9][A-Za-z0-9_-]*)", re.I)

# The capture is unbounded and the length judged AFTER, because a bounded
# `{2,23}` does not decline an over-long token — it truncates one, and a
# truncation is more dangerous than a miss. `Job ID: 5340-Nurse-Practitioner-
# Days-FT` yielded `5340-NURSE-PRACTITIONER-`, and `REQ-2026-3206100…` yielded
# `REQ-2026-`, which merge-tracker reads back as `REQ-2026` — a DIFFERENT string
# from the one we wrote, and one that two genuinely different reqs sharing a
# prefix would both produce. Over-long means "we cannot tell where the id ends",
# which is a decline.
_REQ_ID_MAX_LEN = 24
_REQ_ID_MIN_LEN = 3


def extract_req_id(jd_text: str) -> str:
    """The requisition id a JD states, uppercased — or "" when it states none,
    or more than one.

    Why this is worth extracting: `company::role` is the identity the pipeline
    runs on, and merge-tracker's fuzzy tier drops tokens of three characters or
    fewer before comparing titles, so `INSURANCE SPECIALIST I` and `… II` (and
    `PACT PRN Representative I` vs `PACT Representative I`) match as one opening
    and fold into a single row — the level and the PRN are invisible to it. A req
    id in the Notes column is the signal career-ops documents for exactly this:
    merge-tracker reads it and treats two rows carrying DIFFERENT ids as distinct
    openings, overriding the title match.

    Ambiguity resolves to "": several DIFFERENT ids in one JD means we cannot
    tell which is the requisition, and guessing splits a row that should fold.
    The same id repeated (JD body and footer) is not ambiguity, so the values are
    compared, not counted. That doubles as the guard for the UI's Add-Job path,
    where `screen.extract_description` can fall back to a whole page body: a
    related-jobs panel usually carries several ids, and several is a decline. A
    page carrying exactly one foreign id is the residual risk there.

    Deliberately not the board's own job key (`jk=`, `currentJobId`): that is a
    per-POSTING identifier, so a re-post of one requisition carries a new one, and
    keying on it would split every re-post into a second tracker row. The
    employer's req id is stable across re-posts and differs between levels, which
    is the distinction being drawn here."""
    text = _demarkdown(jd_text or "")
    found = set()
    for m in _JD_REQ_ID_RE.finditer(text):
        # rstrip: a trailing separator is never part of the id, and leaving one
        # on changes what merge-tracker reads back out of what we write.
        value = m.group(1).rstrip("-_")
        if not _REQ_ID_MIN_LEN <= len(value) <= _REQ_ID_MAX_LEN:
            continue
        if any(c.isdigit() for c in value):
            found.add(value.upper())
    return found.pop() if len(found) == 1 else ""


def _inject_req_id_into_notes(tracker_tsv: str, req_id: str) -> str:
    """Prepend `req <id>` to the notes cell so merge-tracker can read it.

    FIRST in the cell, ahead of the URL, because merge-tracker's
    `extractReqNumber` takes the first match in the cell: leading with the id we
    resolved makes the extraction deterministic instead of dependent on whatever
    the LLM's free-text sentence happens to contain. (Today's posting URLs yield
    no match at all — pinned by a test — but that is a property of current URL
    shapes, not a guarantee about the next board.)

    `req <id>` because that is a form merge-tracker's own regex reads back
    unchanged; it uppercases both sides, so case never matters here either."""
    return _prepend_to_notes(
        tracker_tsv, f"req {req_id}" if req_id else "",
        lambda notes: any(m.group(1).upper() == req_id.upper()
                          for m in _NOTES_REQ_ID_RE.finditer(notes)))


def write_job_result(
    response_text: str,
    job_meta: dict,
    reports_dir: Path,
    tracker_dir: Path,
    today: str,
) -> dict:
    """Parse XML-tagged response, write report .md and tracker .tsv, return summary dict."""
    report_content = extract_tag(response_text, "report")
    tracker_tsv = extract_tag(response_text, "tracker_tsv")
    summary = parse_json_loose(extract_tag(response_text, "summary")) or {}

    job_id = job_meta["id"]
    report_num = job_meta.get("report_num") or summary.get("report_num", "000")
    company = summary.get("company") or job_meta.get("company") or "unknown"
    company_slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    report_name = f"{report_num}-{company_slug}-{today}.md"

    if report_content:
        (reports_dir / report_name).write_text(report_content, encoding="utf-8")
    if tracker_tsv:
        tracker_tsv = sanitize_addition(tracker_tsv, job_meta.get("url", ""),
                                        job_meta.get("jd_text", ""))
        (tracker_dir / f"{job_id}.tsv").write_text(tracker_tsv + "\n", encoding="utf-8")

    return {
        "report_file": report_name if report_content else None,
        "tracker_file": f"{job_id}.tsv" if tracker_tsv else None,
        "summary": summary,
    }


def build_user_message(job_meta: dict, today: str) -> str:
    return (
        f"Evaluate this job posting.\n\n"
        f"**Job ID:** {job_meta['id']}\n"
        f"**Report Number:** {job_meta.get('report_num', '000')}\n"
        f"**Tracker Number:** {job_meta.get('tracker_num', 0)}\n"
        f"**Evaluation Date:** {today}\n"
        f"**URL:** {job_meta.get('url', '')}\n"
        f"**Company (source field):** {job_meta.get('company') or 'unknown'}\n"
        f"**Role (source field):** {job_meta.get('role') or 'unknown'}\n\n"
        f"**Job Description:**\n{job_meta.get('jd_text') or '(no JD cached — infer from the URL and source fields)'}"
    )


def jd_cache_path(career_ops: Path, job_id: str) -> Path:
    """Where batch_prep caches a job's description, keyed on its queue id — the
    same id that names its tracker addition, which is what lets the merge-time
    sanitizer find the JD for a row it did not write."""
    return career_ops / "batch" / "jds" / f"{job_id}.txt"


def assign_job_numbers(
    pending: list[dict],
    state: dict,
    report_start: int,
    tracker_start: int,
    career_ops: Path,
    load_jd_text: bool = True,
) -> list[dict]:
    """Pre-assign sequential report and tracker numbers to pending jobs.

    Pre-assigns sequential report and tracker numbers to pending jobs so
    parallel workers don't conflict on numbering. Mutates `state["jobs"]` to
    record each job's metadata (excluding jd_text to keep state small)."""
    report_counter = report_start
    tracker_counter = tracker_start
    if "jobs" not in state:
        state["jobs"] = {}

    jobs: list[dict] = []
    for row in pending:
        jid = str(row["id"]).strip()
        report_counter += 1
        tracker_counter += 1
        meta = {
            "id": jid,
            "url": (row.get("url") or "").strip(),
            "company": (row.get("source") or "").strip(),
            "role": (row.get("notes") or "").strip(),
            "report_num": f"{report_counter:03d}",
            "tracker_num": tracker_counter,
            "status": "pending",
        }
        if load_jd_text:
            meta["jd_text"] = read_text(jd_cache_path(career_ops, jid))
        jobs.append(meta)

        state_entry = {k: v for k, v in meta.items() if k != "jd_text"}
        state["jobs"][jid] = state_entry

    return jobs


def load_pending(
    batch_input: Path,
    state: dict,
    done_statuses: frozenset = frozenset({"completed"}),
) -> list[dict]:
    """Return rows from batch-input.tsv whose IDs are not in done_statuses."""
    already_done = {
        jid for jid, job in state.get("jobs", {}).items()
        if job.get("status") in done_statuses
    }
    return [row for row in read_batch_input(batch_input)
            if (jid := str(row.get("id", "")).strip()) and jid not in already_done]


# Canonical applications.md header (matches career-ops onboarding). Seeded
# before merge so merge-tracker has a file to merge INTO.
APPLICATIONS_HEADER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
)


def ensure_applications_md(career_ops: Path) -> Path:
    """Make sure career-ops/data/applications.md exists with the canonical
    header, returning its path.

    merge-tracker.mjs only merges INTO an existing applications.md — if none
    exists it prints "No applications.md found. Nothing to merge into." and
    exits 0 (a false success). Our pipeline never runs career-ops's
    interactive onboarding that would otherwise seed the file, so on a fresh
    run (especially in CI, where nothing's cached yet) the merge silently
    no-ops and applications.md never gets created. Seeding the header here
    fixes that for both local and cloud runs. The `data/` path matches the
    layout merge-tracker prefers and the workflow uploads."""
    apps_md = career_ops / "data" / "applications.md"
    if not apps_md.exists():
        apps_md.parent.mkdir(parents=True, exist_ok=True)
        apps_md.write_text(APPLICATIONS_HEADER, encoding="utf-8")
        print(f"[batch] seeded data/applications.md (was missing)")
    return apps_md


def sanitize_addition(tracker_tsv: str, url: str = "", jd_text: str = "") -> str:
    """Turn a model's tracker row into one merge-tracker can read, and read
    correctly. The whole chain, in the one order that works.

    Each step is documented at its own definition; the ORDER is the part that
    only makes sense here. Trailing cells are restored first because every guard
    below counts columns; the role's pipe suffix goes before anything reads the
    role; the score is normalized before merge-tracker has to tell it from the
    status; and the req id is prepended AFTER the URL so it ends up ahead of it
    in the cell, where `extractReqNumber`'s first-match rule finds it.

    **Idempotent**, which is what lets it run at two points without a second
    mechanism: padding a nine-column row does nothing, the pipe strip has nothing
    left to strip, a normalized score re-normalizes to itself, and both injectors
    bail when their content is already present."""
    row = _restore_trailing_cells(tracker_tsv)
    row = _strip_role_pipe(row)
    row = _normalize_score_cell(row)
    # The URL the pipeline QUEUED first, the row's own trailing field second.
    # Everything downstream routes by the notes URL — handoff picks the site
    # session from it — so it should be the board URL the run searched from, not
    # a careers-page URL an agent resolved and wrote as the 10th field. The
    # trailing field is what a `{num}-{slug}.tsv` row has when nothing else does.
    row = _inject_url_into_notes(row, url or _trailing_url(row))
    return _inject_req_id_into_notes(row, extract_req_id(jd_text))


def read_batch_input(batch_input: Path) -> list[dict]:
    """Every row of batch-input.tsv, as dicts. Missing file → [].

    One reader because the FORMAT is one fact — tab-delimited, header row, utf-8
    — even though the three callers ask different questions of it (rows not yet
    done; max id and seen urls; id → url). It deliberately RAISES on an
    unreadable file rather than choosing a posture for them: `batch_prep`
    swallowing an IO error would restart ids at 1 and re-queue everything, while
    the merge swallowing one costs a URL lookup and nothing else."""
    if not batch_input.exists():
        return []
    # utf-8-sig, as rowio reads: an Excel round-trip leaves a BOM that renames the
    # first header to `\ufeffid`, after which every `row.get("id")` is None —
    # batch_prep restarts ids at 1 and the merge finds no URLs, both in silence.
    with open(batch_input, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _batch_input_urls(batch_input: Path) -> dict[str, str]:
    """{job id: posting URL} from batch-input.tsv — the URL for a row that was
    queued by this pipeline. An unreadable queue costs the URL repair only, so
    it degrades to {} rather than taking the merge down with it. All three ways
    the read can fail, not just OSError: a hand-edited file saved as cp1252
    raises UnicodeDecodeError, a NUL byte raises csv.Error, and the UI's Add-Job
    handler above this has already written the report and the TSV."""
    try:
        rows = read_batch_input(batch_input)
    except (OSError, UnicodeDecodeError, csv.Error):
        return {}
    urls: dict[str, str] = {}
    for row in rows:
        jid, url = (row.get("id") or "").strip(), (row.get("url") or "").strip()
        if jid and url:
            urls[jid] = url
    return urls


def _trailing_url(tracker_tsv: str) -> str:
    """The posting URL a row carries in its own optional trailing fields.

    career-ops' batch worker is told to write "9 columns plus an optional
    trailing `url`", so a foreign row often carries its URL with it. Detected by
    shape as merge-tracker's `parseTsvExtras` does, so it stays order-independent
    with the optional location and `via=` extras — and with the SAME pattern the
    notes injector uses to ask "already present", or a `HTTPS://` cell would be
    detected here, prepended, and not recognised on the next pass: the chain
    grew a URL per merge."""
    parts = _row_parts(tracker_tsv)
    for cell in (parts or [])[_TRACKER_TSV_COLUMNS:]:
        if _NOTES_URL_RE.match(cell.strip()):
            return cell.strip()
    return ""


def _sanitize_pending_additions(career_ops: Path, tracker_dir: Path) -> None:
    """Apply `sanitize_addition` to every un-merged addition on disk, whoever
    wrote it, and say so when something was repaired.

    `write_job_result` covers the rows this pipeline's Python produces, but it is
    not the only writer: career-ops' own evaluators (`batch-runner.sh` on the
    `--batch` path, `gemini-eval.mjs`, the web runner) write straight into
    `tracker-additions/`, never entering Python. Those rows reach a merge with
    none of the chain applied — no score normalization, so an unreadable score
    cell gets the row REFUSED and archived (exit 0, never retried); no URL in
    notes, so the UI's "Open posting" link renders as `#`; and no req id, so two
    levels of one title fold.

    **This covers the merges WE run** — `--evaluate-batch`, the UI, and the heal
    sweep that picks up whatever is still pending. It does NOT cover a `--batch`
    run end to end: `batch-runner.sh` calls `node merge-tracker.mjs` itself as its
    last step, with no Python in the process tree, so those rows are merged before
    this can see them. Recovering those means going after `tracker-additions/
    merged/` once the runner has finished; see #156.

    Recovery of what a foreign row is missing prefers the row's OWN trailing URL
    (career-ops' documented 10th field) and falls back to `batch-input.tsv` keyed
    on the addition's filename — which is a job id for our writers and for
    career-ops' batch prompt, but not for its other writers, whose
    `{num}-{slug}.tsv` names simply find nothing and keep the score repair.

    A pipe-delimited row is left alone. merge-tracker parses that shape natively,
    while every step here splits on tabs and no-ops on it — rewriting it would
    change which of upstream's two parsers reads the row, for no gain."""
    urls = _batch_input_urls(career_ops / "batch" / "batch-input.tsv")
    changed = 0
    for f in sorted(tracker_dir.glob("*.tsv")):
        try:
            # Line endings only — NOT read_text's strip(), which eats the trailing
            # tab of an empty Notes cell. Read that way, an 8-cell row is padded
            # back to 9, differs from what was read, and is rewritten and
            # counted as repaired on every merge, forever.
            raw = f.read_text(encoding="utf-8").strip("\r\n")
        except (OSError, UnicodeDecodeError):
            continue        # one unreadable row must not hold up the other nine
        fixed = sanitize_addition(raw, urls.get(f.stem, ""),
                                  read_text(jd_cache_path(career_ops, f.stem)))
        if fixed == raw:
            continue
        try:
            atomic_write_text(f, fixed + "\n")
        except OSError:
            continue        # a row we cannot repair still merges as it stands
        changed += 1
    if changed:
        print(f"[batch] sanitized {changed} addition(s) written outside the "
              "Python path (score/URL/req-id)")


def run_merge_tracker(career_ops: Path) -> bool:
    merge_script = career_ops / "merge-tracker.mjs"
    if not merge_script.exists():
        return False
    # Seed the tracker header first — otherwise merge-tracker no-ops on a
    # fresh run (see ensure_applications_md).
    ensure_applications_md(career_ops)
    tracker_dir = career_ops / "batch" / "tracker-additions"
    # Recovery first, so a restored row is sanitized and snapshotted with the
    # rest; sanitizing before the snapshot, since it can change the role cell (a
    # pipe suffix), which is half the identity the loss guard matches on.
    _recover_refused_additions(career_ops, tracker_dir)
    _sanitize_pending_additions(career_ops, tracker_dir)
    before = _pending_additions(tracker_dir)
    print("[batch] running merge-tracker.mjs...")
    r = subprocess.run(["node", "merge-tracker.mjs"], cwd=career_ops, capture_output=True, text=True, encoding="utf-8")
    if r.returncode == 0:
        print("[batch] tracker merged")
        # BOTH streams: merge-tracker refuses a row with console.warn, which is
        # stderr. Passing stdout alone left the reasons block empty on every
        # genuine loss — and printed the one refusal it does log to stdout (the
        # benign unscoreable re-eval) as the explanation for some other
        # addition's disappearance.
        _warn_on_lost_additions(before, career_ops, tracker_dir,
                                f"{r.stdout}\n{r.stderr}")
        return True
    print(f"[batch] merge-tracker failed:\n{r.stderr.strip()}")
    _hint_missing_node_modules(r.stderr)
    return False


def _addition_key(company: str, role: str) -> str:
    """The company::role identity, normalized the way merge-tracker's own dedup
    normalizes it. Not stable across a merge on its own — see `_report_key`."""
    return f"{normalize_company(company)}::{normalize_company(role)}"


def _report_key(company: str, num: str) -> str:
    """The company + report-number identity, which a merge DOES preserve.

    merge-tracker matches an addition against the tracker on four tiers, and only
    two of them are provable identity: an exact posting URL, and an exact report
    number. On the GUESSED tiers — entry number, fuzzy title — it updates the row
    but deliberately keeps THE ROW'S OWN TITLE, so that a fuzzy false positive
    cannot also destroy the evidence that two reqs were distinct
    (`role: (reportNumMatched || dupReason === 'url') ? addition.role :
    duplicate.role`). Its `🔄 Update` log line prints the ADDITION's role, so the
    substitution is invisible there too.

    The report link, the score and the company are written through on every one
    of those tiers, so they are what survives. Company is part of the key because
    merge-tracker's own report-number tier requires it: report-file numbering and
    tracker-row numbering drift, so a bare number can name an unrelated row.

    Not a replacement for `_addition_key` — a second reading of the same row. An
    unscoreable re-eval is skipped with the existing score, report and PDF left
    standing, so there the row keeps its company::role and never receives the new
    report number. Each key covers the other's blind spot."""
    return f"{normalize_company(company)}::{num}"


def _addition_cells(text: str) -> list[str]:
    """The cells of one addition row, in ADDITION_COLUMNS order.

    Both shapes merge-tracker accepts, because a row it can read is a row it can
    also REFUSE and archive, and a shape this function cannot parse is an
    evaluation that vanishes without even being counted — the one outcome the
    guard exists to prevent. A model that ignores the prompt's tab rule and emits
    a markdown table row is the case `_strip_role_pipe` already tells us happens;
    every sanitizer no-ops on it (they split on tabs and find one cell), so it
    reaches merge-tracker exactly as the model wrote it.

    The two shapes disagree about columns 5-6 — the pipe form is score-then-status
    — but agree on every cell read here (company, role, report, notes), and
    merge-tracker resolves that pair by content rather than position anyway. Do
    not read `row["score"]` or `row["status"]` off this without fixing that.

    maxsplit on the tab form, as `app/data.py:parse_tracker_additions` reads the
    same file: a tab inside the free-text notes must not become a tenth cell."""
    text = text.strip()
    if text.startswith("|"):
        cells = [c.strip() for c in text.split("|")]
        if cells and not cells[0]:
            cells.pop(0)
        if cells and not cells[-1]:
            cells.pop()
        return cells
    return [c.strip() for c in text.split("\t", len(ADDITION_COLUMNS) - 1)]


def _pending_additions(tracker_dir: Path) -> list[dict]:
    """One record per un-merged addition TSV: the file it came from, and the
    company, role and report number it carries — the raw cells, with both
    identities derived at the one place they are compared.

    One record per FILE, not per key: a re-evaluation of a role already queued
    puts two TSVs under one company::role, and collapsing them would under-report
    a loss and send the operator hunting for one report while another stays
    buried. The filename is also what "still pending" is decided on, since
    merge-tracker moves a processed TSV into merged/ under its own name — so the
    file answers "was this addition processed at all" exactly, where a shared key
    answers it for whichever TSV moved first.

    Short of `role` the row is unreadable and skipped; short of `notes` it is
    not, since a lost trailing tab is routine (see `_restore_trailing_cells`)."""
    additions: list[dict] = []
    for f in sorted(tracker_dir.glob("*.tsv")):
        row = dict(zip(ADDITION_COLUMNS, _addition_cells(read_text(f))))
        if "role" not in row:
            continue
        additions.append({
            "name": f.name,
            "company": row["company"],
            "role": row["role"],
            "report": row_report_num(row.get("report", ""), row.get("notes", "")),
        })
    return additions


def _classify_landing(records: list[dict], career_ops: Path) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(lost, retitled) for every addition record: which of the two readings of
    "did it land" found it in the tracker, or neither.

    Shared by the loss guard, which reports the answer, and the recovery pass,
    which acts on it — one definition, so the row the guard calls lost is the
    row recovery goes after, never two slightly different sets."""
    landed, role_by_report = _tracker_identities(career_ops / "data" / "applications.md")
    lost: list[dict] = []
    retitled: list[tuple[dict, str]] = []
    for add in records:
        # The two readings, side by side. Either one finding the row means the
        # evaluation is in the tracker.
        if _addition_key(add["company"], add["role"]) in landed:
            continue
        key = _report_key(add["company"], add["report"]) if add["report"] else None
        # `in`, not truthiness of the title: a tracker row with a blank Role cell
        # (hand-added, or half-migrated) matched here would otherwise be read as
        # no match at all, and an intact evaluation would get the loud loss
        # warning — the #152 cry-wolf back again by a different route.
        if key in role_by_report:
            retitled.append((add, role_by_report[key]))
        else:
            lost.append(add)
    return lost, retitled


def _recover_refused_additions(career_ops: Path, tracker_dir: Path) -> None:
    """Put back into the queue the rows a previous merge refused for a reason
    the sanitizer can fix.

    This is the half of #156 the merge-time sanitizer cannot reach: `--batch`
    runs career-ops' `batch-runner.sh`, whose last step is its OWN
    `node merge-tracker.mjs`, so a row the agent CLI wrote with a bare `4.2` is
    refused and archived into `merged/` before any Python runs. From there
    nothing retries it — the report is on disk, the tracker has no row, and the
    evaluation is simply gone. `run.sh`/`run.ps1` run `pipeline.merge_additions`
    after the runner returns, which lands here.

    Two rules decide what comes back, and both are load-bearing:

    **It did not land, by either reading.** `_classify_landing` is the loss
    guard's own test, so the set recovered is exactly the set that guard would
    have reported. A row merge-tracker folded into an existing one under a
    different title (#152) is NOT lost and is not touched.

    **The score fix would change it.** Of the whole chain, an unreadable score
    cell is the only defect that gets a row REFUSED; the others corrupt or
    impoverish a row that still merges. So a lost row whose score is already
    readable was refused for a reason we cannot cure — a report number marked
    `failed` in batch-state.tsv, which upstream refuses on purpose — and pulling
    it back would only have it refused again, run after run. The same rule is
    what stops a row the user DELETED from the tracker coming back: it merged
    with a readable score, so it never qualifies. (A row that is both repairable
    and doomed gets one retry: after it, the archived copy is the repaired one,
    and the score no longer changes.)

    A row already back in the queue is left to the merge in progress."""
    merged = tracker_dir / "merged"
    lost, _ = _classify_landing(_pending_additions(merged), career_ops)
    if not lost:
        return
    urls = _batch_input_urls(career_ops / "batch" / "batch-input.tsv")
    restored = 0
    for add in lost:
        src, dst = merged / add["name"], tracker_dir / add["name"]
        if dst.exists():
            continue
        try:
            raw = src.read_text(encoding="utf-8").strip("\r\n")
        except (OSError, UnicodeDecodeError):
            continue
        if _normalize_score_cell(_restore_trailing_cells(raw)) == _restore_trailing_cells(raw):
            continue                    # refused for a reason we cannot fix
        fixed = sanitize_addition(raw, urls.get(Path(add["name"]).stem, ""),
                                  read_text(jd_cache_path(career_ops, Path(add["name"]).stem)))
        try:
            atomic_write_text(dst, fixed + "\n")
        except OSError:
            continue
        restored += 1
    if restored:
        print(f"[batch] recovered {restored} evaluation(s) a previous merge refused "
              "for an unreadable score — re-queued with the score repaired")


def _warn_on_lost_additions(before: list[dict], career_ops: Path,
                            tracker_dir: Path, merge_output: str = "") -> None:
    """Report what became of each evaluation that left tracker-additions/.

    merge-tracker declines a row it can't read confidently — an unreadable score
    cell, a report number marked `failed` in batch-state.tsv — and archives the
    TSV into merged/ anyway, with a warning on stdout and exit 0. So the row is
    gone from the queue, absent from applications.md, and
    `has_pending_tracker_additions` is false, meaning nothing ever retries it.

    Asking the filesystem and the tracker, rather than grepping merge-tracker's
    prose for "Skipping", is what makes this survive an upstream release: those
    messages come in half a dozen phrasings and at least one of them is BENIGN
    (a re-eval that produced no score deliberately keeps the row's existing
    score — a case `_normalize_score_cell`'s N/A output makes more likely, not
    less). "Did the row land?" has one answer and upstream cannot reword it.

    But it has TWO readings, and asking only for company::role made this cry wolf
    on three intact evaluations in a real run (#152): each had been merged into an
    existing row on a heuristic tier, which keeps that row's title, so the key the
    addition carried was not the key it landed under. A guard that fires on intact
    rows camouflages the loss it exists to catch, so an addition found under
    EITHER identity counts as landed. The substitution gets its own line, because
    it is real information nothing else surfaces: the fuzzy tier can fold two
    genuinely distinct reqs (a leveled title and its sibling) into one row, and
    from then on only one of them is visible to dedup, the handoff and the UI."""
    if not before:
        return
    # The directory entry is the whole question here — a TSV merge-tracker
    # processed is one it moved into merged/ under the same name — so this is a
    # listing, not a re-read of every file `before` already parsed.
    still_pending = {f.name for f in tracker_dir.glob("*.tsv")}
    lost, retitled = _classify_landing(
        [add for add in before if add["name"] not in still_pending], career_ops)

    if lost:
        print(f"[batch] WARNING: {len(lost)} evaluation(s) left "
              "batch/tracker-additions/ without reaching applications.md — their "
              "reports are on disk but the roles are invisible to the UI and to the "
              "handoff, and no later run retries them. The TSVs are in "
              "batch/tracker-additions/merged/:")
        for add in lost:
            print(f"[batch]   {add['name']} ({add['company']} — {add['role']})")
        # merge-tracker's output is captured, so the one line saying WHY it
        # refused each row would otherwise be unreachable — in a cloud run the
        # log is all the operator has. `merge_output` must therefore carry
        # stderr: every refusal but one is a console.warn.
        reasons = [l.strip() for l in merge_output.splitlines() if "Skipping" in l]
        if reasons:
            print("[batch] merge-tracker's reasons:")
            for line in reasons:
                print(f"[batch]   {line}")

    if retitled:
        print(f"[batch] note: {len(retitled)} evaluation(s) merged into an existing "
              "tracker row that kept its own role title — merge-tracker matched them "
              "on a guessed tier (entry number, or fuzzy title), where it will not "
              "let an addition rewrite a title. Nothing was lost: the report link "
              "and score wrote through. Worth a look, because a fuzzy match can fold "
              "two genuinely distinct reqs into one row:")
        for add, row_role in retitled:
            print(f"[batch]   {add['name']}: {add['company']} — "
                  f'"{add["role"]}" merged into "{row_role}" (report {add["report"]})')


def _tracker_identities(applications_md: Path) -> tuple[set[str], dict[str, str]]:
    """The two identities every tracker data row can be found by, in one walk:
    the set of company::role keys, and {company::report_num: that row's role}.

    The role title comes back with the report key rather than a bare `True`
    because it is what makes the retitle line above readable — the operator needs
    to see which existing row swallowed the addition to judge whether the pairing
    was right."""
    keys: set[str] = set()
    role_by_report: dict[str, str] = {}
    if not applications_md.exists():
        return keys, role_by_report
    for columns, cells in data_rows(read_text(applications_md)):
        # By name, as every tracker reader here does: the optional Via column
        # shifts Role right by one, and read positionally the agency lands where
        # the role belongs. Zipping is also the width guard — a row too short to
        # reach Role has no Role key, and Report/Notes are optional columns.
        row = dict(zip(columns, cells))
        if "role" not in row:
            continue
        company, role = row["company"], row["role"]
        keys.add(_addition_key(company, role))
        num = row_report_num(row.get("report", ""), row.get("notes", ""))
        if num:
            role_by_report.setdefault(_report_key(company, num), role.strip())
    return keys, role_by_report


def _hint_missing_node_modules(stderr: str) -> None:
    """merge-tracker.mjs used to import only Node builtins; it now reaches
    js-yaml through tracker-utils.mjs. A career-ops checkout that was updated
    without a fresh `npm install` fails to resolve it, and the raw
    ERR_MODULE_NOT_FOUND stack doesn't say what to do about it."""
    if "ERR_MODULE_NOT_FOUND" not in stderr and "Cannot find package" not in stderr:
        return
    print("[batch] hint: career-ops now has npm dependencies that merge-tracker "
          "needs (js-yaml). Run `npm install --ignore-scripts` in your "
          "career-ops checkout — a `git pull`/rebase there does not install them.")


def build_system_prompt(cv: str, profile_yml: str, profile_md: str = "", article_digest: str = "", *, profile_master: str = "") -> str:
    # PROFILE.md (Commit 4), when present, is the candidate's living master and
    # supersedes the 4 seed fragments: it already folds in the CV, profile,
    # positioning, and proof points, and the browser agent grows it as it learns.
    # Only the candidate-profile *content* swaps; the evaluation framework below is
    # unchanged. A blank/whitespace master → the original seed-fragment assembly.
    if (profile_master or "").strip():
        candidate_profile = (
            "### Candidate Profile (PROFILE.md — the living master)\n"
            "_Folds in the CV/experience, structured profile, positioning, and "
            'proof points. Where the framework below says "CV" or "_profile.md", '
            "read this profile._\n\n"
            f"{profile_master}\n"
        )
    else:
        extra = ""
        if profile_md:
            extra += f"\n### User Customizations (_profile.md)\n{profile_md}\n"
        if article_digest:
            extra += f"\n### Proof Points (article-digest.md)\n{article_digest}\n"
        candidate_profile = (
            f"### CV (read-only)\n{cv}\n\n"
            f"### Profile (profile.yml)\n{profile_yml}\n{extra}"
        )

    return f"""You are a job evaluation expert running in headless batch mode. \
Evaluate job postings for the candidate described below. \
You have no file system access, cannot generate PDFs, and cannot run real-time web searches.

## CANDIDATE PROFILE

{candidate_profile}
## EVALUATION FRAMEWORK

Work through blocks A-G in order. Rules in _profile.md override system defaults for scoring.

**Archetype Detection** - classify the role as one of:
AI Platform/LLMOps Engineer | Agentic Workflows/Automation | Technical AI Product Manager |
AI Solutions Architect | AI Forward Deployed Engineer | AI Transformation Lead

**Block A - Role Summary**
Table: Archetype, Domain, Function, Seniority, Remote policy, Team size, TL;DR.

**Block B - CV Match**
Map each JD requirement to exact CV lines or note a gap.
Gaps section: hard blocker vs nice-to-have, adjacent experience, mitigation plan.
Adapt framing to the detected archetype.

**Block C - Level & Strategy**
1. JD level vs candidate's natural level.
2. "Sell senior without lying" plan with specific phrases.
3. "If downleveled" acceptance criteria.

**Block D - Comp & Demand**
Use training-knowledge salary benchmarks (Glassdoor/Levels.fyi estimates - label as such).
Score comp 1-5: 5=top quartile, 1=well below market.

**Block E - Personalization Plan**
Table of top 5 CV changes + top 5 LinkedIn changes.

**Block F - Interview Plan**
6-10 STAR stories mapped to JD requirements. 1 case study recommendation. Red-flag questions.

**Block G - Posting Legitimacy**
Analyze JD quality signals. Mark posting freshness "unverified (batch mode)".
Assess: High Confidence | Proceed with Caution | Suspicious.

**Global Score** table: CV Match, North Star Alignment, Comp, Cultural Signals, Red Flags penalty, Global (all X/5).

**Machine Summary** YAML block:
```yaml
company: "name"
role: "title"
score: X.X
legitimacy_tier: "High Confidence | Proceed with Caution | Suspicious"
archetype: "detected"
final_decision: "Apply | Consider | Research first | Skip"
hard_stops: []
soft_gaps: []
top_strengths: []
risk_level: "Low | Medium | High"
confidence: "Low | Medium | High"
next_action: "one concrete next step"
```

## OUTPUT FORMAT

Respond with ONLY this XML. No prose outside the tags. Use real tab characters in tracker_tsv.

<evaluation>
<report>
# Evaluacion: COMPANY - ROLE

**Fecha:** DATE
**Arquetipo:** ARCHETYPE
**Score:** SCORE/5
**Legitimacy:** LEGITIMACY_TIER
**URL:** URL
**PDF:** null (batch mode)
**Batch ID:** JOB_ID

---

## Machine Summary

```yaml
MACHINE_SUMMARY_YAML
```

## A) Resumen del Rol
CONTENT

## B) Match con CV
CONTENT

## C) Nivel y Estrategia
CONTENT

## D) Comp y Demanda
CONTENT

## E) Plan de Personalizacion
CONTENT

## F) Plan de Entrevistas
CONTENT

## G) Posting Legitimacy
CONTENT

---

## Keywords extraidas
15-20 ATS keywords from the JD
</report>
<tracker_tsv>
TRACKER_NUM	DATE	COMPANY	ROLE	STATUS	SCORE/5	PDF	[REPORT_NUM](reports/REPORT_NUM-company-slug-DATE.md)	ONE_SENTENCE_NOTES
</tracker_tsv>
<summary>
{{"status": "completed", "id": "JOB_ID", "report_num": "REPORT_NUM", "company": "COMPANY", "role": "ROLE", "score": SCORE_FLOAT, "legitimacy": "LEGITIMACY_TIER", "pdf": null, "report": "reports/REPORT_NUM-company-slug-DATE.md", "error": null}}
</summary>
</evaluation>

Tracker TSV rules (9 tab-separated columns, no header):
- Col 1 TRACKER_NUM: use the Tracker Number from the user message
- Col 5 STATUS: "Evaluada" normally; "NO APLICAR" if score < 3.0
- Col 6 SCORE: format X.X/5
- Col 7 PDF: null (batch mode)
- Col 8 report link: [REPORT_NUM](reports/REPORT_NUM-company-slug-DATE.md)
- company-slug: lowercase, spaces to hyphens
- Col 9 notes: one sentence starting with APPLY / CONSIDER / SKIP + key reason

Summary JSON: id and report_num must be the exact values from the user message. score is a float.

On catastrophic failure output only:
<evaluation><summary>{{"status": "failed", "id": "JOB_ID", "report_num": "REPORT_NUM", "company": "unknown", "role": "unknown", "score": null, "legitimacy": null, "pdf": null, "report": null, "error": "brief description"}}</summary></evaluation>
"""


def eval_system_prompt(career_ops: Path) -> str:
    """The evaluation system prompt for a career-ops install — the single place
    the candidate-profile source is resolved, so `--evaluate-batch` and the UI
    add-job eval can never disagree on it. Uses the living PROFILE.md when present
    (authoritative), else the cv.md / profile.yml / _profile.md / article-digest.md
    seeds."""
    # Lazy import: handoff imports this module at load, so a top-level import of
    # it here would cycle.
    from pipeline.handoff import resolve_profile_md
    career_ops = Path(career_ops)
    return build_system_prompt(
        read_text(career_ops / "cv.md"),
        read_text(career_ops / "config" / "profile.yml"),
        read_text(career_ops / "modes" / "_profile.md"),
        read_text(career_ops / "article-digest.md"),
        profile_master=resolve_profile_md(career_ops),
    )
