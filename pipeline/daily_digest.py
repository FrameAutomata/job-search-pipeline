"""After a daily run, tell the candidate what is worth their attention.

The cloud daily evaluates 160–190 roles a day into a private repository's
Actions cache, and nobody sees a result unless they open the Actions tab,
download an artifact and unpack it. This is the delivery half: one Discord
message (and/or one email) per run naming the handful of roles that scored at
or above `DIGEST_MIN_SCORE`, with the evaluator's verdict, the posting link and
the reports themselves attached — so the person's next step is "open the UI
and hand off", not "go and look whether anything happened".

What counts as "this run" is the same question `pipeline/run_artifact.py`
already answers for the artifact, so it is answered the same way rather than a
second one: the workflow snapshots `reports/` right after the cache restore,
and a report file is new when it is absent from that manifest or differs from
it in `(size, mtime_ns)`. The manifest carries its own scope and
`run_artifact._read_manifest` refuses a mismatch with `SystemExit`; that
refusal IS the scope check wanted here, so it is caught and read as "no new
reports" rather than re-implemented.

Which rows those files belong to comes from the tracker, read through the UI's
own parser (`pipeline.app.data.parse_applications`): it is the one reader that
knows both tracker layouts, the score cell's shapes and the status vocabulary
career-ops ships as data. The verdict comes from the report's own
`## Machine Summary` block — `final_decision`, `top_strengths`, `hard_stops` —
which the evaluation prompt (`_batch_common.py`) asks for in exactly that
shape; the Notes sentence is the fallback and the tail.

Two rules are load-bearing:

  - **Exit code is always 0.** The digest runs `if: always()` after the
    pipeline step and must never be the reason a day's run shows red; a sender
    that cannot reach its endpoint logs a line and moves on, and `main` swallows
    everything else the same way.
  - **When the run failed, say so.** `RUN_OUTCOME` (the pipeline step's
    `outcome`) other than `success` always sends a one-line failure notice with
    the run URL, whatever else qualified — a daily that silently stops running
    is the failure the whole feature exists to make visible. `DIGEST_ALWAYS`
    (default true) is the heartbeat for the success case with nothing to show.

Not a leaf: no third-party imports of its own (urllib, smtplib, email, json,
re), but it reads the tracker through `pipeline.app.data` and shares
`env_int`/`env_float`, `LIVENESS_CLOSED_RE` and `RESERVED_REPORT_SUFFIX` with
`pipeline._batch_common` rather than carrying copies. PyYAML — a pipeline
dependency — is imported lazily for the Machine Summary and a regex stands in
when it is missing, so the module imports wherever `pipeline.app.data` does
(`tests/test_daily_digest.py::test_importable_without_ui_deps`).

Every env name this module reads is in `SECRET_VARS`, `SETTING_VARS` or
`RUN_VARS`; the workflow test derives the daily's digest step from those
constants, `.env.example`'s block is guarded against them, and
`tests/conftest.py` clears them before every test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from pipeline import run_artifact
from pipeline._batch_common import (
    LIVENESS_CLOSED_RE,
    RESERVED_REPORT_SUFFIX,
    _report_int,
    env_float,
    env_int,
)
from pipeline.app import data
from pipeline.stdio import line_buffer_stdout

# ── Env contract ─────────────────────────────────────────────────────────────
# Repository SECRETS: the two delivery channels. Each sender is a no-op with a
# log line when its secret is unset, so a copy with neither configured runs the
# step for free.
SECRET_VARS = (
    "DIGEST_DISCORD_WEBHOOK",
    "DIGEST_EMAIL_TO",
    "DIGEST_SMTP_HOST",
    "DIGEST_SMTP_PORT",
    "DIGEST_SMTP_USER",
    "DIGEST_SMTP_PASS",
    "DIGEST_EMAIL_FROM",
)
# Repository VARIABLES with their defaults, as the workflow wires them:
# `${{ vars.NAME || '<default>' }}`. The default lives HERE and the workflow
# test reads it from here, so the two cannot disagree.
SETTING_VARS = {
    "DIGEST_MIN_SCORE": "4.0",
    "DIGEST_LIMIT": "10",
    "DIGEST_ALWAYS": "true",
    "DIGEST_ATTACH_REPORTS": "true",
    "DIGEST_NEXT_STEP": "",
}
# What the workflow tells the digest about the run it follows.
RUN_VARS = ("RUN_URL", "RUN_OUTCOME", "RUN_STARTED_AT")
# The reports directory, relative to `--root`, as the snapshot step's --delta
# names it. The workflow test asserts it is in the snapshot's list.
DELTA = "reports"

DEFAULT_SMTP_PORT = 587
DEFAULT_NEXT_STEP = ("Next: open your triage UI (./run-ui.sh or run-ui.ps1), "
                     "click ↻ Refresh, then Hand off")

# GitHub Actions Free: 2,000 Linux minutes per month per ACCOUNT for private
# repositories. The health line projects one run's duration over a month of
# dailies against it and warns inside the last 10%.
MONTHLY_MINUTES = 2000
MINUTES_WARN_AT = 1800
RUNS_PER_MONTH = 30

# Discord's documented limits for a webhook execute; each is enforced below.
DISCORD_MAX_EMBEDS = 10
DISCORD_MAX_EMBED_CHARS = 6000
DISCORD_MAX_TITLE = 256
DISCORD_MAX_DESCRIPTION = 300   # ours, not Discord's 4096: a digest is a glance
DISCORD_MAX_CONTENT = 2000
HTTP_TIMEOUT = 20

VERDICT_MAX = 300
DECISION_SKIP = "skip"

# The marks the pipeline appends to a Notes cell (#163) and the URL the bridge
# stores there. The digest wants the evaluator's one-line verdict, so all of
# them come out. The URL and Re-eval patterns are `data`'s own, the Closed one
# is `_batch_common`'s; the two below are spelled here because their sources
# (`reopened_mark`, `by_hand_mark`) are writers with no matching reader regex
# that keeps the date.
_REOPENED_RE = re.compile(r"\bReopened \d{4}-\d{2}-\d{2}(?: \([^)]*\))?", re.I)
_BY_HAND_RE = re.compile(r"\bSet \w+ \d{4}-\d{2}-\d{2} \(by hand\)", re.I)
# LIVENESS_CLOSED_RE stops at the open paren so `closed_by_recheck` can match a
# prefix; the mark itself runs to the closing paren — one level of nesting
# allowed, since the re-check's reason can quote a pattern like `(?:closed
# filled)` (see `_note_safe`, which strips `|` and URLs but keeps parens).
_CLOSED_FULL_RE = re.compile(
    LIVENESS_CLOSED_RE.pattern + r"(?:[^()]|\([^()]*\))*\)?", re.I)
_MENTION_RE = re.compile(r"@(everyone|here)\b")

_MACHINE_SUMMARY_RE = re.compile(
    r"##\s*Machine Summary\s*\n+\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```", re.S)
_DECISION_RE = re.compile(r"^\s*final_decision:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", re.M)


def _log(msg: str) -> None:
    print(f"[digest] {msg}")


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ── Selection ────────────────────────────────────────────────────────────────

@dataclass
class DigestItem:
    company: str
    role: str
    score: float
    url: str
    decision: str
    strength: str
    hard_stop: str
    verdict: str
    report_num: str
    report_file: str        # path relative to --root, "" when unknown

    @property
    def title(self) -> str:
        head = f"{self.score:.1f}/5"
        if self.decision:
            head += f" · {self.decision}"
        return f"{head} · {self.company} — {self.role}"

    @property
    def description(self) -> str:
        parts = []
        if self.strength:
            parts.append(f"+ {self.strength}")
        if self.hard_stop:
            parts.append(f"! {self.hard_stop}")
        if self.verdict:
            parts.append(self.verdict)
        return _clip("\n".join(parts), DISCORD_MAX_DESCRIPTION)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def load_manifest(manifest: Path, root: Path) -> dict | None:
    """The pre-run manifest's files, or None when it cannot be trusted.

    `run_artifact._read_manifest` refuses (SystemExit) a missing manifest and a
    manifest taken of a different --root/--delta. Both mean the diff below would
    call every restored report new, so "no new reports" is the honest reading —
    the failure notice / heartbeat still goes out."""
    try:
        return run_artifact._read_manifest(Path(manifest), Path(root), [DELTA])
    except SystemExit as exc:
        _log(str(exc))
        return None


def new_report_files(root: Path, manifest_files: dict | None) -> list[str]:
    """Report paths (relative to `root`) written or rewritten since the
    manifest — the same `(size, mtime_ns)` rule `run_artifact.stage` applies.
    Reservation locks are skipped: they carry the report-number prefix and no
    evaluation."""
    if manifest_files is None:
        return []
    out = []
    for key, st in sorted(run_artifact.scan(Path(root), [DELTA]).items()):
        if Path(key).name.endswith(RESERVED_REPORT_SUFFIX):
            continue
        if manifest_files.get(key) != st:
            out.append(key)
    return out


def report_number_map(files: list[str]) -> dict[int, str]:
    """{report number: relative path} for report-shaped basenames. Numbers as
    ints because the pipeline mints them zero-padded and the tracker may not
    (`data._report_ints`); the first file wins a duplicate number, in path
    order."""
    out: dict[int, str] = {}
    for key in files:
        m = data._REPORT_FILE_RE.match(Path(key).name)
        if not m:
            continue
        n = _report_int(m.group(1))
        if n is not None and n not in out:
            out[n] = key
    return out


def parse_machine_summary(report_text: str) -> dict:
    """The `## Machine Summary` YAML block of a report as a dict, `{}` when
    there is none. PyYAML when it is importable and the block parses; else a
    regex that recovers `final_decision` alone, which is the field that
    changes what the digest does (a `Skip` is excluded)."""
    m = _MACHINE_SUMMARY_RE.search(report_text or "")
    if not m:
        return {}
    block = m.group(1)
    try:
        import yaml  # a pipeline dependency; optional here on purpose
        doc = yaml.safe_load(block)
        if isinstance(doc, dict):
            return doc
    except Exception:
        pass
    d = _DECISION_RE.search(block)
    return {"final_decision": d.group(1).strip()} if d else {}


def _first(value) -> str:
    """The first entry of a YAML list, or a scalar, as one line."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if value is None:
        return ""
    return " ".join(str(value).split())


def clean_verdict(notes: str) -> str:
    """The Notes cell as the one-line verdict career-ops writes there
    ("APPLY/CONSIDER/SKIP + reason"): URLs and the pipeline's own marks
    stripped, separators tidied, capped at 300 characters."""
    text = notes or ""
    # The URL pattern is greedy to the next space, so the sentence punctuation
    # after a link belongs to the sentence — `extract_url` trims the same set.
    text = data._NOTES_URL_RE.sub(
        lambda m: m.group(0)[len(m.group(0).rstrip(".,);]")):], text)
    for pattern in (data._REEVAL_MARK_RE, _CLOSED_FULL_RE, _REOPENED_RE, _BY_HAND_RE):
        text = pattern.sub(" ", text)
    text = " ".join(text.split())
    text = re.sub(r"\s+([,;:.)\]])", r"\1", text)      # no space before punctuation
    text = re.sub(r"\(\s*\)", "", text)                # parens the strip emptied
    # A leading or trailing separator is scaffolding left where a URL or a mark
    # was (`https://… — CONSIDER: …`, `APPLY —`).
    text = re.sub(r"^[\s—:;|,.-]+|[\s—:;|,-]+$", "", text)
    return _clip(" ".join(text.split()), VERDICT_MAX)


def select_items(rows: list[dict], new_files: dict[int, str], root: Path, *,
                 min_score: float, limit: int) -> tuple[list[DigestItem], list[DigestItem]]:
    """(shown, dropped): the tracker rows whose report is new this run, at
    `min_score` or above, still Evaluated, and not a Machine-Summary `Skip` —
    sorted by score desc then company, cut at `limit`. `dropped` is the tail
    the limit removed, for the log."""
    items: list[DigestItem] = []
    for row in rows:
        n = _report_int(row.get("report_num"))
        if n is None or n not in new_files:
            continue
        score = row.get("score_value")
        if score is None or score < min_score:
            continue
        if row.get("status_canonical") != "Evaluated":
            continue
        rel = new_files[n]
        try:
            summary = parse_machine_summary((Path(root) / rel).read_text(encoding="utf-8"))
        except OSError:
            summary = {}
        decision = _first(summary.get("final_decision"))
        if decision.lower().startswith(DECISION_SKIP):
            continue
        items.append(DigestItem(
            company=(row.get("company") or "").strip(),
            role=(row.get("role") or "").strip(),
            score=float(score),
            url=data.extract_url(row.get("notes", "")),
            decision=decision,
            strength=_first(summary.get("top_strengths")),
            hard_stop=_first(summary.get("hard_stops")),
            verdict=clean_verdict(row.get("notes", "")),
            report_num=str(row.get("report_num") or ""),
            report_file=rel,
        ))
    items.sort(key=lambda i: (-i.score, i.company.lower(), i.role.lower()))
    limit = max(0, int(limit))
    return items[:limit], items[limit:]


# ── The digest itself ────────────────────────────────────────────────────────

@dataclass
class Digest:
    date: str
    min_score: float
    limit: int
    evaluated: int
    qualifying: int
    open: int
    run_url: str
    run_outcome: str
    failed: bool
    duration_min: float | None
    monthly_estimate: int | None
    items: list[DigestItem] = field(default_factory=list)
    dropped: list[DigestItem] = field(default_factory=list)
    always: bool = True
    next_step: str = DEFAULT_NEXT_STEP
    attachment_name: str = ""
    attachment_text: str = ""

    @property
    def health(self) -> str:
        line = (f"{self.evaluated} evaluated this run · {self.qualifying} at "
                f"≥ {self.min_score:g} · {self.open} open in your queue")
        if self.duration_min is not None and self.monthly_estimate is not None:
            line += (f" · run took {self.duration_min:.0f} min ≈ "
                     f"{self.monthly_estimate:,} min/month of {MONTHLY_MINUTES:,} at this pace")
            if self.monthly_estimate > MINUTES_WARN_AT:
                line += (f" — WARNING: above {MINUTES_WARN_AT:,}, the free Actions "
                         "budget is per ACCOUNT (one copy per account)")
        return line

    @property
    def heartbeat(self) -> str:
        return f"nothing at ≥ {self.min_score:g} today; {self.evaluated} evaluated"

    @property
    def should_send(self) -> bool:
        return bool(self.items) or self.failed or self.always

    @property
    def failure_line(self) -> str:
        where = self.run_url or f"the Actions tab (outcome: {self.run_outcome or '?'})"
        return f"Today's run failed — open {where}"

    def content(self) -> str:
        """The message body above the embeds / the email's lead."""
        lines = []
        if self.failed:
            lines.append(self.failure_line)
        if self.items:
            lines.append(f"Job digest {self.date}: {len(self.items)} role"
                         f"{'s' if len(self.items) != 1 else ''} worth a look")
        else:
            lines.append(self.heartbeat)
        lines.append(self.health)
        if self.run_url and not self.failed:
            lines.append(self.run_url)
        if self.next_step:
            lines.append(self.next_step)
        # A zero-width space after the @ keeps the words and kills the ping.
        text = _MENTION_RE.sub(lambda m: "@\u200b" + m.group(1), "\n".join(lines))
        return _clip(text, DISCORD_MAX_CONTENT)

    def to_json(self) -> dict:
        d = asdict(self)
        d["health"] = self.health
        d["content"] = self.content()
        d["should_send"] = self.should_send
        d["attachment_chars"] = len(self.attachment_text)
        del d["attachment_text"]
        return d


def _duration(run_started_at: str, now: datetime) -> float | None:
    raw = (run_started_at or "").strip()
    if not raw:
        return None
    try:
        started = float(raw)
    except ValueError:
        return None
    return max(0.0, (now.timestamp() - started) / 60)


def build_attachment(items: list[DigestItem], root: Path, date: str) -> tuple[str, str]:
    """`digest-<date>.md`: the qualifying reports concatenated in digest order,
    so the person reads the evaluations where the notification landed."""
    parts = []
    for item in items:
        try:
            body = (Path(root) / item.report_file).read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(body.strip() + "\n")
    return f"digest-{date}.md", "\n---\n\n".join(parts)


def build_digest(root: Path, manifest: Path, *, min_score: float, limit: int,
                 run_url: str = "", run_outcome: str = "", run_started_at: str = "",
                 always: bool = True, attach: bool = True, next_step: str = "",
                 now: datetime | None = None) -> Digest:
    """Compute the digest — no I/O beyond reading the manifest, the reports
    directory and the tracker. Every env-resolved setting is a kwarg so tests
    pass values instead of setting the environment."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")

    new_files = report_number_map(new_report_files(root, load_manifest(manifest, root)))
    rows = data.parse_applications(root / "data" / "applications.md")
    shown, dropped = select_items(rows, new_files, root, min_score=min_score, limit=limit)
    open_count = sum(1 for r in rows if r.get("status_canonical") == "Evaluated")

    duration = _duration(run_started_at, now)
    monthly = round(duration * RUNS_PER_MONTH) if duration is not None else None
    outcome = (run_outcome or "").strip().lower()
    digest = Digest(
        date=date, min_score=float(min_score), limit=int(limit),
        evaluated=len(new_files), qualifying=len(shown) + len(dropped),
        open=open_count, run_url=run_url or "", run_outcome=outcome,
        failed=bool(outcome) and outcome != "success",
        duration_min=duration, monthly_estimate=monthly,
        items=shown, dropped=dropped, always=always,
        next_step=(next_step or "").strip() or DEFAULT_NEXT_STEP,
    )
    if attach and shown:
        digest.attachment_name, digest.attachment_text = build_attachment(shown, root, date)

    _log(digest.health)
    for item in shown:
        _log(f"  {item.title}")
    if dropped:
        _log(f"{len(dropped)} more at ≥ {min_score:g} not shown (limit {limit}): "
             + "; ".join(f"{d.company} — {d.role} ({d.score:.1f})" for d in dropped))
    return digest


# ── Discord ──────────────────────────────────────────────────────────────────

def embed_color(score: float) -> int:
    """A colour per score band: the strongest roles stand out in the channel."""
    if score >= 4.5:
        return 0x2ECC71     # green
    if score >= 4.0:
        return 0x3498DB     # blue
    return 0x95A5A6         # grey


def discord_embed(item: DigestItem) -> dict:
    embed = {
        "title": _clip(item.title, DISCORD_MAX_TITLE),
        "description": item.description,
        "color": embed_color(item.score),
    }
    # Discord rejects an empty `url`; omit it rather than send "".
    if item.url:
        embed["url"] = item.url
    return embed


def _embed_chars(embed: dict) -> int:
    return len(embed.get("title", "")) + len(embed.get("description", ""))


def chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Split embeds into requests Discord accepts: at most 10 per request AND
    at most 6,000 characters of title+description per request."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for embed in embeds:
        size = _embed_chars(embed)
        if current and (len(current) >= DISCORD_MAX_EMBEDS
                        or chars + size > DISCORD_MAX_EMBED_CHARS):
            chunks.append(current)
            current, chars = [], 0
        current.append(embed)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def _multipart(payload: dict, filename: str, content: bytes) -> tuple[bytes, str]:
    """A hand-built multipart/form-data body: Discord's file upload wants the
    JSON under `payload_json` and the file under `files[0]`."""
    boundary = f"----digest-{uuid.uuid4().hex}"
    body = b"".join([
        (f"--{boundary}\r\n"
         'Content-Disposition: form-data; name="payload_json"\r\n'
         "Content-Type: application/json\r\n\r\n").encode("utf-8"),
        json.dumps(payload).encode("utf-8"), b"\r\n",
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
         "Content-Type: text/markdown; charset=utf-8\r\n\r\n").encode("utf-8"),
        content, b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def _post(webhook: str, payload: dict, attachment: tuple[str, str] | None,
          urlopen) -> bool:
    """One webhook execute. True on 2xx; anything else is logged, never raised."""
    if attachment:
        body, ctype = _multipart(payload, attachment[0], attachment[1].encode("utf-8"))
    else:
        body, ctype = json.dumps(payload).encode("utf-8"), "application/json"
    req = urllib.request.Request(
        webhook, data=body, method="POST",
        headers={"Content-Type": ctype, "User-Agent": "job-search-pipeline digest"})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            if 200 <= int(status) < 300:
                return True
            _log(f"Discord returned {status}; not sent")
            return False
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        _log(f"Discord returned {exc.code}: {detail or exc.reason}; not sent")
    except Exception as exc:     # URLError, timeout, a broken socket
        _log(f"Discord unreachable ({exc!r}); not sent")
    return False


def send_discord(digest: Digest, webhook: str, *, urlopen=None) -> int:
    """POST the digest to a Discord webhook. Returns the number of requests
    that succeeded (0 when the webhook is unset). The first request carries the
    content line(s) and the report attachment; later ones only embeds."""
    if not (webhook or "").strip():
        _log("DIGEST_DISCORD_WEBHOOK unset; Discord skipped")
        return 0
    urlopen = urlopen or urllib.request.urlopen
    chunks = chunk_embeds([discord_embed(i) for i in digest.items]) or [[]]
    attachment = ((digest.attachment_name, digest.attachment_text)
                  if digest.attachment_text else None)
    sent = 0
    for i, chunk in enumerate(chunks):
        payload = {
            "content": digest.content() if i == 0 else "",
            "embeds": chunk,
            # Never ping anyone: a webhook cannot be trusted with @everyone.
            "allowed_mentions": {"parse": []},
        }
        if _post(webhook.strip(), payload, attachment if i == 0 else None, urlopen):
            sent += 1
    _log(f"Discord: {sent}/{len(chunks)} request(s) delivered, "
         f"{len(digest.items)} embed(s)")
    return sent


# ── Email ────────────────────────────────────────────────────────────────────

def _html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_email_text(digest: Digest) -> str:
    lines = [digest.content(), ""]
    for item in digest.items:
        lines.append(f"* {item.title}")
        if item.url:
            lines.append(f"  {item.url}")
        for part in item.description.splitlines():
            lines.append(f"  {part}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_email_html(digest: Digest) -> str:
    head = "<br>".join(_html_escape(l) for l in digest.content().splitlines())
    rows = []
    for item in digest.items:
        name = _html_escape(f"{item.company} — {item.role}")
        link = (f'<a href="{_html_escape(item.url)}">{name}</a>' if item.url else name)
        desc = "<br>".join(_html_escape(p) for p in item.description.splitlines())
        rows.append(f"<tr><td>{item.score:.1f}/5</td><td>{_html_escape(item.decision)}</td>"
                    f"<td>{link}</td><td>{desc}</td></tr>")
    table = ("<table border=\"1\" cellpadding=\"4\"><tr><th>Score</th><th>Verdict</th>"
             "<th>Role</th><th>Why</th></tr>" + "".join(rows) + "</table>") if rows else ""
    return f"<html><body><p>{head}</p>{table}</body></html>"


def send_email(digest: Digest, settings: dict, *, smtp_cls=None) -> bool:
    """Send the digest as multipart/alternative (text + HTML) over STARTTLS,
    with the report attachment when there is one. `settings` is the env view
    (`SECRET_VARS` names). `smtp_cls` is injected for tests. A missing
    recipient or host is a no-op with a log line; a transport failure is
    logged, never raised."""
    to = (settings.get("DIGEST_EMAIL_TO") or "").strip()
    host = (settings.get("DIGEST_SMTP_HOST") or "").strip()
    if not to or not host:
        _log("DIGEST_EMAIL_TO / DIGEST_SMTP_HOST unset; email skipped")
        return False
    user = (settings.get("DIGEST_SMTP_USER") or "").strip()
    password = settings.get("DIGEST_SMTP_PASS") or ""
    sender = (settings.get("DIGEST_EMAIL_FROM") or "").strip() or user or to
    try:
        port = int((settings.get("DIGEST_SMTP_PORT") or "").strip() or DEFAULT_SMTP_PORT)
    except ValueError:
        port = DEFAULT_SMTP_PORT

    msg = EmailMessage()
    n = len(digest.items)
    subject = (f"Run failed — job digest {digest.date}" if digest.failed
               else f"Job digest {digest.date}: {n} role{'s' if n != 1 else ''} at ≥ {digest.min_score:g}")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(render_email_text(digest))
    msg.add_alternative(render_email_html(digest), subtype="html")
    if digest.attachment_text:
        msg.add_attachment(digest.attachment_text.encode("utf-8"), maintype="text",
                           subtype="markdown", filename=digest.attachment_name)

    smtp_cls = smtp_cls or smtplib.SMTP
    try:
        with smtp_cls(host, port, timeout=HTTP_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:
        _log(f"email to {to} via {host}:{port} failed ({exc!r}); not sent")
        return False
    _log(f"email sent to {to}")
    return True


# ── Entry point ──────────────────────────────────────────────────────────────

def _settings_from_env() -> dict:
    return {name: os.environ.get(name, "") for name in SECRET_VARS}


def run(root: Path, manifest: Path, *, min_score: float, limit: int, run_url: str,
        run_outcome: str, run_started_at: str, always: bool, attach: bool,
        next_step: str, dump: Path | None = None, settings: dict | None = None,
        urlopen=None, smtp_cls=None, now: datetime | None = None) -> Digest:
    """Build, optionally dump, and deliver. Returns the digest."""
    digest = build_digest(root, manifest, min_score=min_score, limit=limit,
                          run_url=run_url, run_outcome=run_outcome,
                          run_started_at=run_started_at, always=always,
                          attach=attach, next_step=next_step, now=now)
    if dump:
        Path(dump).parent.mkdir(parents=True, exist_ok=True)
        Path(dump).write_text(json.dumps(digest.to_json(), indent=2, ensure_ascii=False),
                              encoding="utf-8")
        _log(f"dumped to {dump}")
    if not digest.should_send:
        _log(f"{digest.heartbeat} — nothing sent (DIGEST_ALWAYS is off)")
        return digest
    settings = _settings_from_env() if settings is None else settings
    send_discord(digest, settings.get("DIGEST_DISCORD_WEBHOOK", ""), urlopen=urlopen)
    send_email(digest, settings, smtp_cls=smtp_cls)
    return digest


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.daily_digest",
        description="Tell the candidate what this run found worth their attention.")
    ap.add_argument("--root", type=Path, default=Path("career-ops"),
                    help="the career-ops checkout (default: career-ops)")
    ap.add_argument("--manifest", type=Path, required=True,
                    help="the pre-run reports manifest `run_artifact snapshot` wrote")
    ap.add_argument("--min-score", type=float, default=None,
                    help=f"lowest score to include (env DIGEST_MIN_SCORE, default "
                         f"{SETTING_VARS['DIGEST_MIN_SCORE']})")
    ap.add_argument("--limit", type=int, default=None,
                    help=f"roles per digest (env DIGEST_LIMIT, default {SETTING_VARS['DIGEST_LIMIT']})")
    ap.add_argument("--run-url", default=None, help="link to the run (env RUN_URL)")
    ap.add_argument("--dump", type=Path, default=None,
                    help="write the computed digest as JSON here")
    ap.add_argument("--always", action="store_true", default=None,
                    help="send even when nothing qualifies (env DIGEST_ALWAYS, default on)")
    ap.add_argument("--no-attach", action="store_true",
                    help="do not attach the qualifying reports (env DIGEST_ATTACH_REPORTS)")
    return ap.parse_args(argv)


def _main(argv: list[str]) -> int:
    args = _parse_args(argv)
    min_score = (env_float("DIGEST_MIN_SCORE", float(SETTING_VARS["DIGEST_MIN_SCORE"]))
                 if args.min_score is None else args.min_score)
    limit = (env_int("DIGEST_LIMIT", int(SETTING_VARS["DIGEST_LIMIT"]))
             if args.limit is None else args.limit)
    always = _env_bool("DIGEST_ALWAYS", True) if args.always is None else True
    attach = False if args.no_attach else _env_bool("DIGEST_ATTACH_REPORTS", True)
    run(args.root, args.manifest, min_score=min_score, limit=limit,
        run_url=os.environ.get("RUN_URL", "") if args.run_url is None else args.run_url,
        run_outcome=os.environ.get("RUN_OUTCOME", ""),
        run_started_at=os.environ.get("RUN_STARTED_AT", ""),
        always=always, attach=attach,
        next_step=os.environ.get("DIGEST_NEXT_STEP", ""), dump=args.dump)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Always 0: the digest must never be why the daily shows red."""
    line_buffer_stdout()
    try:
        return _main(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:      # argparse — a usage error is still not the run's
        if exc.code not in (None, 0):
            _log(f"bad arguments ({exc.code}); nothing sent")
        return 0
    except Exception:
        _log("failed; the digest never fails the daily. Traceback:")
        traceback.print_exc()
        return 0


if __name__ == "__main__":
    line_buffer_stdout()

    raise SystemExit(main())
