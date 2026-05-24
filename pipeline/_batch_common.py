"""Shared utilities for batch job evaluation (submit, evaluate, retrieve)."""

import csv
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

MAX_TOKENS = 8192


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

    Used by both batch_evaluate and batch_submit so number assignment stays
    consistent across the two code paths. Mutates `state["jobs"]` to record
    each job's metadata (excluding jd_text to keep state small)."""
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


def run_merge_tracker(career_ops: Path) -> bool:
    merge_script = career_ops / "merge-tracker.mjs"
    if not merge_script.exists():
        return False
    print("[batch] running merge-tracker.mjs...")
    r = subprocess.run(["node", "merge-tracker.mjs"], cwd=career_ops, capture_output=True, text=True)
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
