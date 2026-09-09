"""Tests for pipeline/daily_digest.py — what a run found worth attention.

The digest is the one thing a cloud user sees without opening the Actions tab,
so the cases here are the ones where a cheaper reading would look right and be
wrong: a report that was REWRITTEN this run (same path, new stat), a Skip that
scored above the bar, a zero-padded report number against an unpadded one, a
manifest of the wrong scope, a Discord 500 — and the rule that whatever
happens, the exit code is 0 and a failed run is announced.
"""

import email
import email.policy
import io
import json
import re
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import daily_digest as dd
from pipeline import run_artifact

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 9, 12, 45, tzinfo=timezone.utc)

HEADER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
)


def _report(num, company, role, *, decision="Apply", strengths=("Eight years of Java",),
            hard_stops=(), url="https://www.indeed.com/viewjob?jk=abc123",
            score="4.6") -> str:
    """A report in the shape the evaluation prompt in _batch_common asks for:
    the header block, then `## Machine Summary` with its fenced YAML."""
    def yaml_list(items):
        return "[" + ", ".join(json.dumps(i) for i in items) + "]"
    return (
        f"# Evaluacion: {company} - {role}\n\n"
        f"**Fecha:** 2026-09-09\n**Arquetipo:** Backend\n**Score:** {score}/5\n"
        f"**Legitimacy:** High Confidence\n**URL:** {url}\n**PDF:** null (batch mode)\n"
        f"**Batch ID:** {num}-acme\n\n---\n\n"
        "## Machine Summary\n\n"
        "```yaml\n"
        f'company: "{company}"\nrole: "{role}"\nscore: {score}\n'
        'legitimacy_tier: "High Confidence"\narchetype: "Backend"\n'
        f'final_decision: "{decision}"\nhard_stops: {yaml_list(hard_stops)}\n'
        'soft_gaps: ["No Kubernetes"]\n'
        f"top_strengths: {yaml_list(strengths)}\n"
        'risk_level: "Low"\nconfidence: "High"\nnext_action: "Apply today"\n'
        "```\n\n"
        "## A) Resumen del Rol\nA fine role.\n\n## B) Match con CV\nGood.\n"
    )


def _row(num, company, role, score, status, report_num, notes) -> str:
    rep = f"[{report_num}](reports/{report_num}-{company.lower()}-2026-09-09.md)" if report_num else ""
    return f"| {num} | 2026-09-09 | {company} | {role} | {score} | {status} | ❌ | {rep} | {notes} |\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _snapshot(root: Path, manifest: Path) -> Path:
    """The pre-run manifest exactly as the workflow's snapshot step writes it."""
    run_artifact._main(["snapshot", "--root", str(root), "--manifest", str(manifest),
                        "--delta", dd.DELTA])
    return manifest


@pytest.fixture
def world(tmp_path):
    """A career-ops tree after a run: one report restored from the cache (in
    the manifest), three minted by this run, and a tracker naming them all."""
    root = tmp_path / "career-ops"
    _write(root / "reports" / "001-oldco-2026-09-01.md",
           _report("001", "Oldco", "Old Role"))
    manifest = _snapshot(root, tmp_path / "manifest.json")
    _write(root / "reports" / "002-acme-2026-09-09.md",
           _report("002", "Acme", "Backend Engineer", score="4.6"))
    _write(root / "reports" / "003-globex-2026-09-09.md",
           _report("003", "Globex", "Platform Engineer", decision="Consider",
                   strengths=("Strong Python",), hard_stops=("On-site in Ohio",),
                   url="https://www.linkedin.com/jobs/view/999", score="4.2"))
    _write(root / "reports" / "004-initech-2026-09-09.md",
           _report("004", "Initech", "Staff Engineer", decision="Skip", score="4.9"))
    _write(root / "data" / "applications.md", HEADER
           + _row(1, "Oldco", "Old Role", "4.8/5", "Evaluated", "001", "APPLY old https://x.example/old")
           + _row(2, "Acme", "Backend Engineer", "4.6/5", "Evaluated", "002",
                  "APPLY — strong Java match https://www.indeed.com/viewjob?jk=abc123")
           + _row(3, "Globex", "Platform Engineer", "4.2/5", "Evaluated", "003",
                  "CONSIDER: on-site https://www.linkedin.com/jobs/view/999")
           + _row(4, "Initech", "Staff Engineer", "4.9/5", "Evaluated", "004", "SKIP despite score")
           + _row(5, "Umbrella", "Engineer", "3.1/5", "Evaluated", "", "no report"))
    return root, manifest


def _build(root, manifest, **kw):
    kw.setdefault("min_score", 4.0)
    kw.setdefault("limit", 10)
    kw.setdefault("now", NOW)
    return dd.build_digest(root, manifest, **kw)


# ── Import shape ─────────────────────────────────────────────────────────────

def test_importable_without_ui_deps():
    """Not a leaf, but it must import wherever pipeline.app.data does — the
    UI-free venv the daily runs in has no fastapi and no markdown. A fresh
    interpreter, because conftest has already imported pipeline.app.data,
    server and onboard into THIS one: blocking the two names here and
    re-importing only the digest would let a fastapi import added to data.py,
    or a `from pipeline.app import onboard` (→ batch_evaluate → provider
    SDKs), pass while the cloud step (requirements.txt only) broke."""
    probe = ("import sys; sys.modules['fastapi'] = None; sys.modules['markdown'] = None; "
             "import pipeline.daily_digest as m; "
             "assert m.SECRET_VARS and m.SETTING_VARS and m.DELTA == 'reports'")
    res = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_dotenv_is_loaded_only_by_the_main_block():
    """`.env.example` says a local `python -m pipeline.daily_digest` reads the
    names from `.env`, so the __main__ block loads it — and ONLY there: main()
    is what the tests call after conftest cleared every DIGEST_* name, and
    load_dotenv (override=False) would put a developer's real webhook back
    for `test_no_secrets_is_a_no_op_that_exits_zero` to post to."""
    src = (ROOT / "pipeline" / "daily_digest.py").read_text(encoding="utf-8")
    head, _, tail = src.partition('if __name__ == "__main__":')
    assert tail, "no __main__ block"
    assert "load_dotenv" not in head
    assert re.search(r"load_dotenv\(.*\.env", tail), tail


def test_no_third_party_imports_of_its_own():
    src = (ROOT / "pipeline" / "daily_digest.py").read_text(encoding="utf-8")
    top = [l for l in src.splitlines() if re.match(r"^(import|from) ", l)]
    third = [l for l in top if not re.match(
        r"^(import|from) (argparse|json|os|re|smtplib|sys|traceback|urllib|uuid|"
        r"dataclasses|datetime|email|pathlib|__future__|pipeline)\b", l)]
    assert not third, third
    assert "import yaml" in src and "yaml" not in " ".join(top)


# ── Selection ────────────────────────────────────────────────────────────────

class TestSelection:
    def test_only_this_runs_reports_qualify(self, world):
        root, manifest = world
        d = _build(root, manifest)
        # Oldco (001) scored 4.8 but was in the manifest; Initech is a Skip.
        assert [i.company for i in d.items] == ["Acme", "Globex"]
        assert d.evaluated == 3          # 002, 003, 004 minted this run
        assert d.qualifying == 2
        assert d.open == 5

    def test_rewritten_report_counts_as_new(self, world):
        """A retried job rewrites its file under the same name; a path-set
        diff would call it old. Same rule as run_artifact.stage."""
        root, manifest = world
        f = root / "reports" / "001-oldco-2026-09-01.md"
        f.write_text(f.read_text(encoding="utf-8") + "\n\nRe-evaluated.\n", encoding="utf-8")
        d = _build(root, manifest)
        assert [i.company for i in d.items] == ["Oldco", "Acme", "Globex"]

    def test_min_score_and_evaluated_only(self, world):
        root, manifest = world
        assert [i.company for i in _build(root, manifest, min_score=4.5).items] == ["Acme"]
        apps = root / "data" / "applications.md"
        apps.write_text(apps.read_text(encoding="utf-8").replace(
            "| Acme | Backend Engineer | 4.6/5 | Evaluated |",
            "| Acme | Backend Engineer | 4.6/5 | Applied |"), encoding="utf-8")
        assert [i.company for i in _build(root, manifest).items] == ["Globex"]

    def test_skip_decision_is_excluded_above_min_score(self, world):
        root, manifest = world
        assert "Initech" not in [i.company for i in _build(root, manifest).items]

    def test_unscoreable_row_does_not_qualify(self, world):
        root, manifest = world
        apps = root / "data" / "applications.md"
        apps.write_text(apps.read_text(encoding="utf-8").replace("4.6/5", "N/A"), encoding="utf-8")
        assert [i.company for i in _build(root, manifest).items] == ["Globex"]

    def test_limit_drops_the_tail_and_logs_it(self, world, capsys):
        root, manifest = world
        d = _build(root, manifest, limit=1)
        assert [i.company for i in d.items] == ["Acme"]
        assert [i.company for i in d.dropped] == ["Globex"]
        assert d.qualifying == 2
        out = capsys.readouterr().out
        assert "1 more at ≥ 4.0 not shown (limit 1): Globex — Platform Engineer (4.2)" in out

    def test_sorted_by_score_then_company(self, world):
        root, manifest = world
        _write(root / "reports" / "005-zeta-2026-09-09.md", _report("005", "Zeta", "Eng"))
        _write(root / "reports" / "006-beta-2026-09-09.md", _report("006", "Beta", "Eng"))
        apps = root / "data" / "applications.md"
        apps.write_text(apps.read_text(encoding="utf-8")
                        + _row(6, "Zeta", "Eng", "4.6/5", "Evaluated", "005", "APPLY")
                        + _row(7, "Beta", "Eng", "4.6/5", "Evaluated", "006", "APPLY"),
                        encoding="utf-8")
        assert [i.company for i in _build(root, manifest).items] == ["Acme", "Beta", "Zeta", "Globex"]

    def test_zero_padded_file_matches_unpadded_row(self, world):
        root, manifest = world
        apps = root / "data" / "applications.md"
        apps.write_text(apps.read_text(encoding="utf-8").replace(
            "[002](reports/002-acme-2026-09-09.md)", "[2](reports/2-acme.md)"), encoding="utf-8")
        assert "Acme" in [i.company for i in _build(root, manifest).items]

    def test_reservation_locks_are_not_reports(self, world):
        root, manifest = world
        _write(root / "reports" / "007-RESERVED.md", '{"pid": 1}')
        d = _build(root, manifest)
        assert d.evaluated == 3
        assert 7 not in dd.report_number_map(dd.new_report_files(root, run_artifact._read_manifest(
            manifest, root, [dd.DELTA])))

    def test_item_carries_verdict_url_and_summary(self, world):
        root, manifest = world
        acme, globex = _build(root, manifest).items
        assert acme.url == "https://www.indeed.com/viewjob?jk=abc123"
        assert acme.decision == "Apply"
        assert acme.strength == "Eight years of Java"
        assert acme.hard_stop == ""
        assert acme.verdict == "APPLY — strong Java match"
        assert acme.title == "4.6/5 · Apply · Acme — Backend Engineer"
        assert globex.hard_stop == "On-site in Ohio"
        assert globex.description == "+ Strong Python\n! On-site in Ohio\nCONSIDER: on-site"
        assert globex.report_file == "reports/003-globex-2026-09-09.md"


class TestManifest:
    def test_missing_manifest_means_no_new_reports_not_a_crash(self, world, capsys):
        root, _ = world
        d = _build(root, root / "nope.json")
        assert d.items == [] and d.evaluated == 0 and d.evaluated_known is False
        assert d.health.startswith("? (manifest unreadable) evaluated this run")
        assert "[digest] run_artifact: no manifest" in capsys.readouterr().out
        assert d.should_send      # the heartbeat still goes out

    def test_wrong_scope_is_refused_the_same_way(self, world, tmp_path, capsys):
        root, _ = world
        other = tmp_path / "other.json"
        run_artifact._main(["snapshot", "--root", str(root), "--manifest", str(other),
                            "--delta", "batch"])
        d = _build(root, other)
        assert d.evaluated == 0 and d.evaluated_known is False
        assert "same --root and --delta" in capsys.readouterr().out

    def test_a_truncated_manifest_is_no_new_reports_not_a_crash(self, world, capsys):
        """`_read_manifest` raises SystemExit for the cases it recognises and
        lets `json.loads` raise for the rest; both must reach the senders."""
        root, manifest = world
        manifest.write_text(manifest.read_text(encoding="utf-8")[:40], encoding="utf-8")
        d = _build(root, manifest)
        assert d.items == [] and d.evaluated_known is False and d.should_send
        assert "[digest] manifest unreadable" in capsys.readouterr().out


# ── Verdict cleaning + Machine Summary ───────────────────────────────────────

class TestVerdict:
    @pytest.mark.parametrize("notes, want", [
        ("APPLY — strong fit https://x.example/a/b, tailor the résumé", "APPLY — strong fit, tailor the résumé"),
        ("https://indeed.com/x — CONSIDER: comp unknown", "CONSIDER: comp unknown"),
        ("APPLY https://a/b Re-eval 2026-08-01 (3.9→4.4): still good https://a/c", "APPLY (3.9→4.4): still good"),
        ("CONSIDER x Closed 2026-08-02 (liveness re-check: body: (?:closed filled))", "CONSIDER x"),
        ("APPLY q. Closed 2026-08-02 (liveness re-check: 404)", "APPLY q."),
        ("APPLY y Reopened 2026-08-03 (re-posted and re-evaluated)", "APPLY y"),
        ("APPLY z Set Applied 2026-08-04 (by hand)", "APPLY z"),
        ("", ""),
    ])
    def test_marks_and_urls_are_stripped(self, notes, want):
        assert dd.clean_verdict(notes) == want

    def test_capped_at_300(self):
        assert len(dd.clean_verdict("x" * 400)) == 300

    def test_machine_summary_parses_the_real_shape(self):
        summary = dd.parse_machine_summary(_report("009", "Acme", "Eng", decision="Research first",
                                                   strengths=("A", "B"), hard_stops=("H",)))
        assert summary["final_decision"] == "Research first"
        assert summary["top_strengths"] == ["A", "B"]
        assert summary["hard_stops"] == ["H"]

    def test_machine_summary_regex_fallback_without_yaml(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yaml", None)
        summary = dd.parse_machine_summary(_report("009", "Acme", "Eng", decision="Skip"))
        assert summary == {"final_decision": "Skip"}

    def test_unparseable_block_falls_back_to_the_decision_line(self):
        text = "## Machine Summary\n\n```yaml\nfoo: [unclosed\nfinal_decision: Consider\n```\n"
        assert dd.parse_machine_summary(text)["final_decision"] == "Consider"

    def test_no_block_is_empty(self):
        assert dd.parse_machine_summary("# nothing here") == {}

    def test_prose_between_heading_and_fence_is_skipped(self):
        """A model that adds a sentence under the heading is drift, not a
        different shape; reading it as "no summary" would fail OPEN — a Skip
        above min_score would be digested."""
        text = ("## Machine Summary\n\nHere is the structured summary.\n\n"
                "```yaml\nfinal_decision: Skip\nhard_stops: [\"On-site\"]\n```\n")
        assert dd.parse_machine_summary(text) == {"final_decision": "Skip",
                                                  "hard_stops": ["On-site"]}
        # But a later fence is not this block's: nothing between them is YAML.
        assert dd.parse_machine_summary("## Machine Summary\n\nno fence at all\n") == {}


# ── Content + health ─────────────────────────────────────────────────────────

class TestContent:
    def test_health_line(self, world):
        root, manifest = world
        d = _build(root, manifest, run_url="https://gh/run/1")
        assert d.health == "3 evaluated this run · 2 at ≥ 4.0 · 5 open in your queue"
        assert "https://gh/run/1" in d.content()
        assert dd.DEFAULT_NEXT_STEP in d.content()

    def test_duration_and_monthly_projection(self, world):
        root, manifest = world
        started = str(int(NOW.timestamp()) - 40 * 60)
        d = _build(root, manifest, run_started_at=started)
        assert d.duration_min == 40
        assert d.monthly_estimate == 1200
        assert "run took 40 min ≈ 1,200 min/month of 2,000 at this pace" in d.health
        assert "WARNING" not in d.health

    def test_warns_above_the_budget_line(self, world):
        root, manifest = world
        d = _build(root, manifest, run_started_at=str(int(NOW.timestamp()) - 61 * 60))
        assert d.monthly_estimate == 1830
        assert "WARNING: above 1,800" in d.health

    def test_unparseable_start_is_no_duration(self, world):
        root, manifest = world
        assert _build(root, manifest, run_started_at="soon").duration_min is None

    def test_failed_run_is_announced_first(self, world):
        root, manifest = world
        d = _build(root, manifest, run_outcome="failure", run_url="https://gh/run/2", always=False)
        assert d.failed and d.should_send
        assert d.content().splitlines()[0] == "Today's run failed — open https://gh/run/2"
        assert d.failure_line == "Today's run failed — open https://gh/run/2"

    def test_heartbeat_when_nothing_qualifies(self, world):
        root, manifest = world
        d = _build(root, manifest, min_score=5.0)
        assert d.items == []
        assert d.should_send                  # DIGEST_ALWAYS defaults on
        assert d.content().startswith("nothing at ≥ 5.0 today; 3 evaluated")
        assert not _build(root, manifest, min_score=5.0, always=False).should_send

    def test_next_step_override_and_mention_scrub(self, world):
        root, manifest = world
        d = _build(root, manifest, next_step="@everyone go apply")
        assert "@everyone" not in d.content()
        assert "everyone go apply" in d.content()

    def test_attachment_concatenates_the_shown_reports(self, world):
        root, manifest = world
        d = _build(root, manifest)
        assert d.attachment_name == "digest-2026-09-09.md"
        assert d.attachment_text.index("Evaluacion: Acme") < d.attachment_text.index("Evaluacion: Globex")
        assert "Evaluacion: Initech" not in d.attachment_text
        assert _build(root, manifest, attach=False).attachment_text == ""


# ── Discord ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(calls, status=204):
    def _open(req, timeout=None):
        calls.append((req, timeout))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "boom", {}, io.BytesIO(b"rate limited"))
        return _Resp(status)
    return _open


def _payload(req) -> tuple[dict, list]:
    """(payload_json, attachments) out of a recorded request, either encoding."""
    ctype = req.get_header("Content-type")
    if ctype == "application/json":
        return json.loads(req.data), []
    msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req.data,
                                   policy=email.policy.default)
    payload, files = None, []
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "payload_json":
            payload = json.loads(part.get_content())
        else:
            files.append((part.get_filename(), part.get_content()))
    return payload, files


def _item(n, score=4.6, url="https://x/%d", title_len=20, desc_len=100):
    return dd.DigestItem(company="C" * title_len, role=f"R{n}", score=score, url=url % n if url else "",
                         decision="Apply", strength="s" * desc_len, hard_stop="", verdict="",
                         report_num=str(n), report_file="")


def _digest(items, **kw):
    d = dd.Digest(date="2026-09-09", min_score=4.0, limit=10, evaluated=len(items),
                  evaluated_known=True, qualifying=len(items), open=3, run_url="https://gh/run/1", run_outcome="success",
                  failed=False, duration_min=None, monthly_estimate=None, items=items)
    for k, v in kw.items():
        setattr(d, k, v)
    return d


class TestDiscord:
    def test_payload_shape(self):
        calls = []
        d = _digest([_item(1), _item(2, url="")])
        assert dd.send_discord(d, "https://discord.test/hook", urlopen=_fake_urlopen(calls)) == 1
        req, timeout = calls[0]
        assert timeout == dd.HTTP_TIMEOUT and req.get_method() == "POST"
        payload, files = _payload(req)
        assert payload["content"] == d.content()
        assert payload["allowed_mentions"] == {"parse": []}
        assert files == []
        e1, e2 = payload["embeds"]
        assert e1 == {"title": "4.6/5 · Apply · " + "C" * 20 + " — R1",
                      "description": "+ " + "s" * 100, "color": dd.embed_color(4.6),
                      "url": "https://x/1"}
        assert "url" not in e2          # empty url omitted, not sent as ""

    def test_chunks_on_ten_embeds(self):
        calls = []
        d = _digest([_item(i) for i in range(23)])
        assert dd.send_discord(d, "https://discord.test/hook", urlopen=_fake_urlopen(calls)) == 3
        sizes = [len(_payload(req)[0]["embeds"]) for req, _ in calls]
        assert sizes == [10, 10, 3]
        assert _payload(calls[0][0])[0]["content"] == d.content()
        assert _payload(calls[1][0])[0]["content"] == ""

    def test_chunks_on_six_thousand_chars(self):
        """With the title and description caps, ten embeds are at most 5,560
        characters, so the char rule is reached only if the caps move — which
        is why it is driven directly here rather than through discord_embed."""
        embeds = [{"title": f"t{i}", "description": "d" * 1000} for i in range(8)]
        chunks = dd.chunk_embeds(embeds)
        assert [len(c) for c in chunks] == [5, 3]
        for chunk in chunks:
            assert sum(len(e["title"]) + len(e["description"]) for e in chunk) <= 6000
        assert [len(c) for c in dd.chunk_embeds([{"title": "", "description": ""}] * 23)] == [10, 10, 3]
        assert dd.chunk_embeds([]) == []

    def test_title_and_description_are_capped(self):
        e = dd.discord_embed(_item(1, title_len=400, desc_len=900))
        assert len(e["title"]) <= 256 and len(e["description"]) <= 300

    def test_content_is_capped(self):
        d = _digest([], next_step="n" * 3000)
        assert len(d.content()) <= 2000

    def test_attachment_goes_multipart_on_the_first_request(self):
        calls = []
        d = _digest([_item(i) for i in range(12)], attachment_name="digest-2026-09-09.md",
                    attachment_text="# Evaluacion: A\n")
        dd.send_discord(d, "https://discord.test/hook", urlopen=_fake_urlopen(calls))
        first, second = calls[0][0], calls[1][0]
        assert first.get_header("Content-type").startswith("multipart/form-data; boundary=")
        payload, files = _payload(first)
        assert len(payload["embeds"]) == 10 and payload["content"] == d.content()
        assert files == [("digest-2026-09-09.md", "# Evaluacion: A\n")]
        assert second.get_header("Content-type") == "application/json"

    def test_a_500_is_logged_not_raised(self, capsys):
        calls = []
        assert dd.send_discord(_digest([_item(1)]), "https://discord.test/hook",
                               urlopen=_fake_urlopen(calls, status=500)) == 0
        assert "Discord returned 500: rate limited" in capsys.readouterr().out

    def test_unreachable_is_logged_not_raised(self, capsys):
        def boom(req, timeout=None):
            raise urllib.error.URLError("no route")
        assert dd.send_discord(_digest([_item(1)]), "https://discord.test/hook", urlopen=boom) == 0
        assert "Discord unreachable" in capsys.readouterr().out

    def test_no_webhook_is_a_no_op(self, capsys):
        def never(req, timeout=None):
            raise AssertionError("must not be called")
        assert dd.send_discord(_digest([_item(1)]), "", urlopen=never) == 0
        assert "DIGEST_DISCORD_WEBHOOK unset" in capsys.readouterr().out

    def test_heartbeat_alone_is_one_request_with_no_embeds(self):
        calls = []
        dd.send_discord(_digest([]), "https://discord.test/hook", urlopen=_fake_urlopen(calls))
        payload, _ = _payload(calls[0][0])
        assert payload["embeds"] == [] and payload["content"].startswith("nothing at ≥ 4.0 today")


# ── Email ────────────────────────────────────────────────────────────────────

class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls, self.msg = [], None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append("send")
        self.msg = msg


class TestEmail:
    def setup_method(self):
        FakeSMTP.instances.clear()

    def test_sends_multipart_alternative_over_starttls(self):
        d = _digest([_item(1)], attachment_name="digest-2026-09-09.md", attachment_text="# R\n")
        settings = {"DIGEST_EMAIL_TO": "me@example.com", "DIGEST_SMTP_HOST": "smtp.example.com",
                    "DIGEST_SMTP_USER": "me@gmail.com", "DIGEST_SMTP_PASS": "app-pass"}
        assert dd.send_email(d, settings, smtp_cls=FakeSMTP)
        (s,) = FakeSMTP.instances
        assert (s.host, s.port) == ("smtp.example.com", 587)
        assert s.calls == ["ehlo", "starttls", "ehlo", ("login", "me@gmail.com", "app-pass"), "send"]
        msg = s.msg
        assert msg["To"] == "me@example.com" and msg["From"] == "me@gmail.com"
        assert msg["Subject"] == "Job digest 2026-09-09: 1 role at ≥ 4.0"
        types = [p.get_content_type() for p in msg.walk()]
        assert "multipart/alternative" in types and "text/plain" in types and "text/html" in types
        attachments = [p for p in msg.walk() if p.get_filename()]
        assert [a.get_filename() for a in attachments] == ["digest-2026-09-09.md"]
        body = msg.get_body(("html",)).get_content()
        assert 'href="https://x/1"' in body and "R1" in body

    def test_from_and_port_defaults(self):
        settings = {"DIGEST_EMAIL_TO": "me@example.com", "DIGEST_SMTP_HOST": "h",
                    "DIGEST_SMTP_PORT": "2525", "DIGEST_EMAIL_FROM": "digest@example.com"}
        dd.send_email(_digest([]), settings, smtp_cls=FakeSMTP)
        (s,) = FakeSMTP.instances
        assert s.port == 2525 and s.msg["From"] == "digest@example.com"
        assert ("login" in str(s.calls)) is False        # no user → no login

    def test_no_recipient_or_host_is_a_no_op(self, capsys):
        assert not dd.send_email(_digest([]), {"DIGEST_EMAIL_TO": "me@example.com"}, smtp_cls=FakeSMTP)
        assert FakeSMTP.instances == []
        assert "email skipped" in capsys.readouterr().out

    def test_transport_failure_is_logged_not_raised(self, capsys):
        class Broken(FakeSMTP):
            def starttls(self):
                raise OSError("tls down")
        settings = {"DIGEST_EMAIL_TO": "me@example.com", "DIGEST_SMTP_HOST": "h"}
        assert not dd.send_email(_digest([]), settings, smtp_cls=Broken)
        assert "failed (OSError('tls down')); not sent" in capsys.readouterr().out

    def test_failed_run_subject(self):
        settings = {"DIGEST_EMAIL_TO": "me@example.com", "DIGEST_SMTP_HOST": "h"}
        dd.send_email(_digest([], failed=True), settings, smtp_cls=FakeSMTP)
        assert FakeSMTP.instances[0].msg["Subject"] == "Run failed — job digest 2026-09-09"


# ── Entry point ──────────────────────────────────────────────────────────────

class TestMain:
    def test_no_secrets_is_a_no_op_that_exits_zero(self, world, capsys):
        root, manifest = world
        assert dd.main(["--root", str(root), "--manifest", str(manifest)]) == 0
        out = capsys.readouterr().out
        assert "DIGEST_DISCORD_WEBHOOK unset" in out and "email skipped" in out

    def test_env_settings_reach_the_digest_and_dump(self, world, tmp_path, monkeypatch, capsys):
        root, manifest = world
        dump = tmp_path / "out" / "digest.json"
        monkeypatch.setenv("DIGEST_MIN_SCORE", "4.5")
        monkeypatch.setenv("DIGEST_LIMIT", "5")
        monkeypatch.setenv("DIGEST_ALWAYS", "false")
        monkeypatch.setenv("RUN_URL", "https://gh/run/7")
        monkeypatch.setenv("RUN_OUTCOME", "success")
        monkeypatch.setenv("DIGEST_NEXT_STEP", "Open the board")
        assert dd.main(["--root", str(root), "--manifest", str(manifest), "--dump", str(dump)]) == 0
        doc = json.loads(dump.read_text(encoding="utf-8"))
        assert doc["min_score"] == 4.5 and doc["limit"] == 5 and doc["always"] is False
        assert doc["run_url"] == "https://gh/run/7" and doc["failed"] is False
        assert [i["company"] for i in doc["items"]] == ["Acme"]
        assert doc["evaluated"] == 3 and doc["open"] == 5 and doc["qualifying"] == 1
        assert doc["content"].endswith("Open the board")
        assert doc["should_send"] is True and doc["attachment_chars"] > 0
        assert "attachment_text" not in doc
        assert "dumped to" in capsys.readouterr().out

    def test_cli_flags_override_env(self, world, tmp_path, monkeypatch):
        root, manifest = world
        dump = tmp_path / "d.json"
        monkeypatch.setenv("DIGEST_MIN_SCORE", "4.5")
        monkeypatch.setenv("DIGEST_ALWAYS", "false")
        dd.main(["--root", str(root), "--manifest", str(manifest), "--dump", str(dump),
                 "--min-score", "4.0", "--limit", "1", "--always", "--no-attach",
                 "--run-url", "https://gh/run/8"])
        doc = json.loads(dump.read_text(encoding="utf-8"))
        assert doc["min_score"] == 4.0 and doc["limit"] == 1 and doc["always"] is True
        assert doc["attachment_chars"] == 0 and doc["run_url"] == "https://gh/run/8"

    def test_nothing_sent_when_always_off_and_nothing_qualifies(self, world, monkeypatch, capsys):
        root, manifest = world
        monkeypatch.setenv("DIGEST_ALWAYS", "false")
        monkeypatch.setenv("DIGEST_MIN_SCORE", "5")
        monkeypatch.setenv("DIGEST_DISCORD_WEBHOOK", "https://discord.test/hook")
        monkeypatch.setattr(dd.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not post")))
        assert dd.main(["--root", str(root), "--manifest", str(manifest)]) == 0
        assert "nothing sent (DIGEST_ALWAYS is off)" in capsys.readouterr().out

    def test_failed_run_is_sent_even_with_always_off(self, world, monkeypatch, capsys):
        root, manifest = world
        monkeypatch.setenv("DIGEST_ALWAYS", "false")
        monkeypatch.setenv("DIGEST_MIN_SCORE", "5")
        monkeypatch.setenv("RUN_OUTCOME", "failure")
        monkeypatch.setenv("DIGEST_DISCORD_WEBHOOK", "https://discord.test/hook")
        calls = []
        monkeypatch.setattr(dd.urllib.request, "urlopen", _fake_urlopen(calls))
        assert dd.main(["--root", str(root), "--manifest", str(manifest)]) == 0
        payload, _ = _payload(calls[0][0])
        assert payload["content"].startswith("Today's run failed — open")

    def test_exit_zero_on_a_corrupt_tracker(self, world, monkeypatch, capsys):
        """A tracker the run left half-written is the case where the run
        failed and the notice matters most — exiting 0 is not enough, the
        failure line must still go out."""
        root, manifest = world
        (root / "data" / "applications.md").write_bytes(b"\xff\xfe| garbage |\n| more |")
        monkeypatch.setenv("RUN_OUTCOME", "failure")
        monkeypatch.setenv("DIGEST_DISCORD_WEBHOOK", "https://discord.test/hook")
        calls = []
        monkeypatch.setattr(dd.urllib.request, "urlopen", _fake_urlopen(calls))
        assert dd.main(["--root", str(root), "--manifest", str(manifest)]) == 0
        out = capsys.readouterr().out
        assert "[digest] tracker unreadable" in out
        assert len(calls) == 1
        payload, _ = _payload(calls[0][0])
        assert payload["content"].startswith("Today's run failed — open")

    def test_exit_zero_on_a_corrupt_manifest_and_the_notice_still_goes(self, world, monkeypatch):
        root, manifest = world
        manifest.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("RUN_OUTCOME", "failure")
        monkeypatch.setenv("DIGEST_DISCORD_WEBHOOK", "https://discord.test/hook")
        calls = []
        monkeypatch.setattr(dd.urllib.request, "urlopen", _fake_urlopen(calls))
        assert dd.main(["--root", str(root), "--manifest", str(manifest)]) == 0
        payload, _ = _payload(calls[0][0])
        assert payload["content"].startswith("Today's run failed — open")
        assert "? (manifest unreadable) evaluated this run" in payload["content"]

    def test_exit_zero_on_a_missing_manifest(self, world):
        root, _ = world
        assert dd.main(["--root", str(root), "--manifest", str(root / "missing.json")]) == 0

    def test_exit_zero_on_an_unexpected_error(self, world, monkeypatch, capsys):
        root, manifest = world
        monkeypatch.setattr(dd, "build_digest", lambda *a, **k: 1 / 0)
        assert dd.main(["--root", str(root), "--manifest", str(manifest)]) == 0
        assert "never fails the daily" in capsys.readouterr().out

    def test_exit_zero_on_bad_arguments(self, capsys):
        assert dd.main(["--min-score", "high"]) == 0
        assert "bad arguments" in capsys.readouterr().out


# ── Mirrors ──────────────────────────────────────────────────────────────────

class TestEnvExampleMirror:
    """`.env.example` restates the digest's env names for a human; the module's
    constants are the source. Parsed in a fixed shape: commented secrets as
    `# NAME=…` and settings as `#   NAME=<default>`, between the block's
    heading and the next one (or the end of the file)."""

    def _block(self) -> str:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        start = text.index("# ── Daily digest")
        rest = text[start + 1:]
        end = rest.find("\n# ── ")
        return text[start:] if end < 0 else rest[:end]

    def test_every_secret_is_shown(self):
        names = set(re.findall(r"^# (DIGEST_\w+)=", self._block(), re.M))
        assert names == set(dd.SECRET_VARS)

    def test_every_setting_carries_the_code_default(self):
        found = dict(re.findall(r"^#\s{3}(DIGEST_\w+)=(.*)$", self._block(), re.M))
        assert found == dd.SETTING_VARS

    def test_no_secret_in_the_settings_shape_and_vice_versa(self):
        block = self._block()
        assert not set(re.findall(r"^#\s{3}(DIGEST_\w+)=", block, re.M)) & set(dd.SECRET_VARS)
        assert not set(re.findall(r"^# (DIGEST_\w+)=", block, re.M)) & set(dd.SETTING_VARS)


def test_onboard_shares_the_secret_names():
    """The wizard's status check reads the same names the sender does."""
    from pipeline.app import onboard
    assert onboard.DIGEST_SECRET_NAMES == dd.SECRET_VARS


def test_conftest_isolates_every_name_the_module_reads():
    """Any os.environ read here must be cleared before every test — a webhook
    in a developer's .env would otherwise post to a real channel."""
    src = (ROOT / "pipeline" / "daily_digest.py").read_text(encoding="utf-8")
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', src))
    read |= set(re.findall(r'env_(?:float|int|bool)\("([A-Z_]+)"', src))
    read |= set(re.findall(r'_env_bool\("([A-Z_]+)"', src))
    known = set(dd.SECRET_VARS) | set(dd.SETTING_VARS) | set(dd.RUN_VARS)
    assert read <= known, read - known
    # The three shapes above are the only ones the scan sees, so they are the
    # only ones allowed: an `os.environ["X"]` or `os.getenv("X")` would be
    # invisible to it, and a guard with a blind spot is not a guard.
    assert not re.search(r"os\.getenv\(|os\.environ\[", src)
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    for const in ("SECRET_VARS", "SETTING_VARS", "RUN_VARS"):
        assert f"daily_digest.{const}" in conftest
