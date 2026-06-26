"""Shared utilities for batch job evaluation (submit, evaluate, retrieve)."""

import csv
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

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
        for line in applications_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("|"):
                cols = [c.strip() for c in line.split("|")]
                if len(cols) >= 2:
                    try:
                        max_num = max(max_num, int(cols[1]))
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


def _strip_role_pipe(tracker_tsv: str) -> str:
    """Remove any '| <suffix>' the LLM appends to the role column.

    The LLM sometimes writes "Software Engineer | Remote" or
    "Platform Engineer | $35/hr Remote" as the role. That pipe is harmless in
    a tab-delimited TSV but merge-tracker.mjs copies it verbatim into a
    markdown table row where it splits the cell, shifting every subsequent
    column (score ends up as status, report link ends up in notes, etc.)."""
    line = tracker_tsv.strip()
    parts = line.split("\t")
    if len(parts) >= 4:
        parts[3] = re.sub(r"\s*\|.*$", "", parts[3]).strip()
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
    line = tracker_tsv.strip()
    if not line:
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
        tracker_tsv = _strip_role_pipe(tracker_tsv)
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
    print("[batch] running merge-tracker.mjs...")
    r = subprocess.run(["node", "merge-tracker.mjs"], cwd=career_ops, capture_output=True, text=True, encoding="utf-8")
    if r.returncode == 0:
        print("[batch] tracker merged")
        return True
    print(f"[batch] merge-tracker failed:\n{r.stderr.strip()}")
    return False


def build_system_prompt(cv: str, profile_yml: str, profile_md: str = "", article_digest: str = "") -> str:
    extra = ""
    if profile_md:
        extra += f"\n### User Customizations (_profile.md)\n{profile_md}\n"
    if article_digest:
        extra += f"\n### Proof Points (article-digest.md)\n{article_digest}\n"

    return f"""You are a job evaluation expert running in headless batch mode. \
Evaluate job postings for the candidate described below. \
You have no file system access, cannot generate PDFs, and cannot run real-time web searches.

## CANDIDATE PROFILE

### CV (read-only)
{cv}

### Profile (profile.yml)
{profile_yml}
{extra}
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
