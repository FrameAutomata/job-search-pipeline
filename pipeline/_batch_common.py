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


# Tracker schema (9 cols): # · Date · Company · Role · Status · Score · PDF ·
# Report · Notes. Mirrors what the LLM is told to emit in _profile.md and what
# merge-tracker.mjs writes into applications.md.
_TRACKER_TSV_COLUMNS = 9


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
    parts = tracker_tsv.strip("\r\n").split("\t")
    if len(parts) >= 4:
        parts[3] = re.sub(r"\s*\|.*$", "", parts[3]).strip()
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
    parts = tracker_tsv.strip("\r\n").split("\t")
    if len(parts) != _TRACKER_TSV_COLUMNS:
        return tracker_tsv
    raw = parts[5].replace("*", "").strip()
    if raw.upper() in _SCORE_SENTINELS:
        parts[5] = raw.upper()
        return "\t".join(parts)
    # The model may have written the pair the other way round (status in col 6,
    # score in col 5). merge-tracker resolves that on its own — it asks which
    # ONE of the two looks like a score. Writing N/A here would make BOTH look
    # like one, so its "exactly one" test fails and it refuses a row it would
    # otherwise have merged correctly: the loss this function exists to prevent,
    # caused by this function. Leave a swapped pair alone.
    if is_score_cell(parts[4]):
        return tracker_tsv
    value = score_value(raw)
    if value is None:
        # No number at all (empty, "unknown", a stray status). N/A is a shape
        # merge-tracker recognises, so the row still merges and the evaluation
        # stays visible — a scoreless row beats a silently discarded one.
        parts[5] = "N/A"
        return "\t".join(parts)
    # Out of range means the model answered on some other scale ("8/10", "84%")
    # and we cannot know which. Clamping to 5 would invent a perfect score, and
    # the handoff work-order ranks by score descending — a fabricated 5 puts the
    # role first. N/A merges the row and leaves it unranked, which is the true
    # statement.
    parts[5] = f"{value:g}/5" if 0 <= value <= 5 else "N/A"
    return "\t".join(parts)


def _inject_url_into_notes(tracker_tsv: str, url: str) -> str:
    """Splice the job URL into the notes (last) column of the tracker row so
    the UI's "Open posting" link works.

    The LLM generates the notes cell freely — per our prompt it's a
    one-sentence summary, not the URL. The UI's report pane resolves the
    "Open posting" target by regex-matching the first http(s) URL in the notes
    cell ([app.js:extractUrl]), so without this the link has nothing to point
    at and renders as `#`. Splicing the URL here keeps the prompt unchanged
    and works for every provider / model.

    If the LLM already put a URL in notes, or returned a row with an
    unexpected number of columns, leave it alone — better to lose the link
    than corrupt the row."""
    if not url:
        return tracker_tsv
    line = tracker_tsv.strip("\r\n")
    if not line.strip():
        return tracker_tsv
    parts = line.split("\t")
    if len(parts) != _TRACKER_TSV_COLUMNS:
        return tracker_tsv
    notes = parts[-1].strip()
    if re.search(r"https?://\S+", notes):
        return tracker_tsv
    parts[-1] = f"{url} — {notes}" if notes else url
    return "\t".join(parts)


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
        tracker_tsv = _restore_trailing_cells(tracker_tsv)
        tracker_tsv = _strip_role_pipe(tracker_tsv)
        tracker_tsv = _normalize_score_cell(tracker_tsv)
        tracker_tsv = _inject_url_into_notes(tracker_tsv, job_meta.get("url", ""))
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
            meta["jd_text"] = read_text(career_ops / "batch" / "jds" / f"{jid}.txt")
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
    if not batch_input.exists():
        return []
    already_done = {
        jid for jid, job in state.get("jobs", {}).items()
        if job.get("status") in done_statuses
    }
    pending: list[dict] = []
    with open(batch_input, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            jid = str(row.get("id", "")).strip()
            if jid and jid not in already_done:
                pending.append(dict(row))
    return pending


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


def run_merge_tracker(career_ops: Path) -> bool:
    merge_script = career_ops / "merge-tracker.mjs"
    if not merge_script.exists():
        return False
    # Seed the tracker header first — otherwise merge-tracker no-ops on a
    # fresh run (see ensure_applications_md).
    ensure_applications_md(career_ops)
    tracker_dir = career_ops / "batch" / "tracker-additions"
    before = _pending_addition_keys(tracker_dir)
    print("[batch] running merge-tracker.mjs...")
    r = subprocess.run(["node", "merge-tracker.mjs"], cwd=career_ops, capture_output=True, text=True, encoding="utf-8")
    if r.returncode == 0:
        print("[batch] tracker merged")
        _warn_on_lost_additions(before, career_ops, tracker_dir, r.stdout)
        return True
    print(f"[batch] merge-tracker failed:\n{r.stderr.strip()}")
    _hint_missing_node_modules(r.stderr)
    return False


def _addition_key(company: str, role: str) -> str:
    """The identity merge-tracker itself dedups on. Keying on the addition's `#`
    instead would flag every re-evaluation as lost: merge-tracker folds a re-eval
    into the EXISTING row, keeping that row's num, so the addition's own num
    never appears in the tracker even though the evaluation landed."""
    return f"{normalize_company(company)}::{normalize_company(role)}"


def _pending_addition_keys(tracker_dir: Path) -> dict[str, list[str]]:
    """{company::role: [filename, ...]} for the un-merged addition TSVs (cols 3
    and 4). A list, not a single name: a re-evaluation of a role already queued
    puts two TSVs under one key, and collapsing them would under-report the loss
    and send the operator hunting for one report while another stays buried."""
    keys: dict[str, list[str]] = {}
    for f in sorted(tracker_dir.glob("*.tsv")) if tracker_dir.exists() else []:
        cells = read_text(f).split("\t")
        if len(cells) >= 4:
            keys.setdefault(_addition_key(cells[2], cells[3]), []).append(f.name)
    return keys


def _warn_on_lost_additions(before: dict[str, list[str]], career_ops: Path,
                            tracker_dir: Path, merge_output: str = "") -> None:
    """Report evaluations that left tracker-additions/ without reaching the
    tracker.

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
    less). "Did the row land?" has one answer and upstream cannot reword it."""
    if not before:
        return
    still_pending = set(_pending_addition_keys(tracker_dir))
    landed = _tracker_keys(career_ops / "data" / "applications.md")
    lost = {key: names for key, names in before.items()
            if key not in still_pending and key not in landed}
    if not lost:
        return
    print(f"[batch] WARNING: {sum(len(n) for n in lost.values())} evaluation(s) left "
          "batch/tracker-additions/ without reaching applications.md — their "
          "reports are on disk but the roles are invisible to the UI and to the "
          "handoff, and no later run retries them. The TSVs are in "
          "batch/tracker-additions/merged/:")
    for key, names in sorted(lost.items()):
        print(f"[batch]   {', '.join(names)} ({key.replace('::', ' / ')})")
    # merge-tracker's stdout is captured, so the one line saying WHY it refused
    # each row would otherwise be unreachable — in a cloud run the log is all
    # the operator has.
    reasons = [l.strip() for l in merge_output.splitlines() if "Skipping" in l]
    if reasons:
        print("[batch] merge-tracker's reasons:")
        for line in reasons:
            print(f"[batch]   {line}")


def _tracker_keys(applications_md: Path) -> set[str]:
    """The company::role identity of every data row in the tracker."""
    if not applications_md.exists():
        return set()
    keys = set()
    for columns, cells in data_rows(read_text(applications_md)):
        company_idx, role_idx = columns.index("company"), columns.index("role")
        if len(cells) > role_idx:
            keys.add(_addition_key(cells[company_idx], cells[role_idx]))
    return keys


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
