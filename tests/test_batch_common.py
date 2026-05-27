"""Tests for pipeline/_batch_common.py"""

import csv
import json
from pathlib import Path

import pytest

from pipeline._batch_common import (
    build_system_prompt,
    build_user_message,
    extract_tag,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    parse_json_loose,
    read_text,
    run_merge_tracker,
    write_job_result,
)


class TestReadText:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello world", encoding="utf-8")
        assert read_text(f) == "hello world"

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("  trimmed  \n", encoding="utf-8")
        assert read_text(f) == "trimmed"

    def test_returns_default_on_missing(self, tmp_path):
        assert read_text(tmp_path / "missing.txt") == ""
        assert read_text(tmp_path / "missing.txt", default="fallback") == "fallback"


class TestLoadState:
    def test_returns_empty_jobs_on_missing_file(self, tmp_path):
        result = load_state(tmp_path / "state.json")
        assert result == {"jobs": {}}

    def test_loads_valid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"batch_id": "abc", "jobs": {"1": {"status": "completed"}}}), encoding="utf-8")
        result = load_state(state_file)
        assert result["batch_id"] == "abc"
        assert result["jobs"]["1"]["status"] == "completed"

    def test_returns_empty_jobs_on_invalid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not valid json {{{", encoding="utf-8")
        result = load_state(state_file)
        assert result == {"jobs": {}}


class TestMaxReportNum:
    def test_returns_zero_for_missing_dir(self, tmp_path):
        state = {"jobs": {}}
        assert max_report_num(tmp_path / "reports", state) == 0

    def test_reads_highest_from_filenames(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "001-acme-2026-01-01.md").write_text("x")
        (reports / "005-globex-2026-01-02.md").write_text("x")
        (reports / "003-initech-2026-01-03.md").write_text("x")
        assert max_report_num(reports, {"jobs": {}}) == 5

    def test_reads_from_state_when_higher(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "003-acme-2026-01-01.md").write_text("x")
        state = {"jobs": {"j1": {"report_num": "010"}}}
        assert max_report_num(reports, state) == 10

    def test_ignores_non_numeric_prefixes(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "no-number.md").write_text("x")
        (reports / "abc-company-2026.md").write_text("x")
        assert max_report_num(reports, {"jobs": {}}) == 0


class TestMaxTrackerNum:
    def test_returns_zero_for_missing_file(self, tmp_path):
        state = {"jobs": {}}
        assert max_tracker_num(tmp_path / "missing.md", state) == 0

    def test_reads_highest_from_table(self, tmp_path):
        apps = tmp_path / "applications.md"
        apps.write_text("| 1 | stuff |\n| 5 | stuff |\n| 3 | stuff |\n", encoding="utf-8")
        assert max_tracker_num(apps, {"jobs": {}}) == 5

    def test_reads_from_state_when_higher(self, tmp_path):
        apps = tmp_path / "applications.md"
        apps.write_text("| 1 | stuff |\n", encoding="utf-8")
        state = {"jobs": {"j1": {"tracker_num": 20}}}
        assert max_tracker_num(apps, state) == 20

    def test_ignores_non_numeric_table_values(self, tmp_path):
        apps = tmp_path / "applications.md"
        apps.write_text("| # | Company | Role |\n| --- | --- | --- |\n| 7 | Acme | Eng |\n", encoding="utf-8")
        assert max_tracker_num(apps, {"jobs": {}}) == 7


class TestExtractTag:
    def test_extracts_simple_tag(self):
        assert extract_tag("<foo>bar</foo>", "foo") == "bar"

    def test_extracts_multiline_content(self):
        text = "<report>\nline1\nline2\n</report>"
        assert extract_tag(text, "report") == "line1\nline2"

    def test_returns_empty_when_tag_missing(self):
        assert extract_tag("no tags here", "missing") == ""

    def test_extracts_first_match(self):
        text = "<tag>first</tag> ... <tag>second</tag>"
        assert extract_tag(text, "tag") == "first"


class TestParseJsonLoose:
    def test_parses_clean_json(self):
        result = parse_json_loose('{"key": "value", "num": 3.5}')
        assert result == {"key": "value", "num": 3.5}

    def test_extracts_json_from_surrounding_text(self):
        text = 'some text before {"score": 4.2} and after'
        result = parse_json_loose(text)
        assert result["score"] == 4.2

    def test_returns_none_on_no_json(self):
        assert parse_json_loose("no json here at all") is None

    def test_returns_none_on_empty_string(self):
        assert parse_json_loose("") is None


class TestBuildUserMessage:
    def test_includes_all_fields(self):
        meta = {
            "id": "42",
            "report_num": "007",
            "tracker_num": 99,
            "url": "https://example.com/job",
            "company": "Acme Corp",
            "role": "Software Engineer",
            "jd_text": "Must know Python.",
        }
        msg = build_user_message(meta, "2026-05-24")
        assert "42" in msg
        assert "007" in msg
        assert "99" in msg
        assert "https://example.com/job" in msg
        assert "Acme Corp" in msg
        assert "Software Engineer" in msg
        assert "Must know Python." in msg
        assert "2026-05-24" in msg

    def test_uses_fallback_for_missing_jd_text(self):
        meta = {"id": "1", "report_num": "001", "tracker_num": 1, "url": "", "company": "", "role": "", "jd_text": ""}
        msg = build_user_message(meta, "2026-05-24")
        assert "no JD cached" in msg

    def test_uses_fallback_for_missing_company(self):
        meta = {"id": "1", "report_num": "001", "tracker_num": 1, "url": "", "company": None, "role": "", "jd_text": "some text"}
        msg = build_user_message(meta, "2026-05-24")
        assert "unknown" in msg


class TestLoadPending:
    def _write_tsv(self, path: Path, rows: list[dict]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "url", "source", "notes"], delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

    def test_returns_empty_for_missing_file(self, tmp_path):
        result = load_pending(tmp_path / "missing.tsv", {"jobs": {}})
        assert result == []

    def test_returns_all_when_none_done(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        self._write_tsv(tsv, [
            {"id": "1", "url": "https://a.com", "source": "Acme", "notes": "Eng"},
            {"id": "2", "url": "https://b.com", "source": "Globex", "notes": "Dev"},
        ])
        result = load_pending(tsv, {"jobs": {}})
        assert len(result) == 2

    def test_skips_completed_by_default(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        self._write_tsv(tsv, [
            {"id": "1", "url": "https://a.com", "source": "Acme", "notes": "Eng"},
            {"id": "2", "url": "https://b.com", "source": "Globex", "notes": "Dev"},
        ])
        state = {"jobs": {"1": {"status": "completed"}}}
        result = load_pending(tsv, state)
        assert len(result) == 1
        assert result[0]["id"] == "2"

    def test_custom_done_statuses_skips_pending(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        self._write_tsv(tsv, [
            {"id": "1", "url": "https://a.com", "source": "Acme", "notes": "Eng"},
            {"id": "2", "url": "https://b.com", "source": "Globex", "notes": "Dev"},
        ])
        state = {"jobs": {"1": {"status": "pending"}}}
        result = load_pending(tsv, state, done_statuses=frozenset({"pending", "completed"}))
        assert len(result) == 1
        assert result[0]["id"] == "2"

    def test_includes_failed_by_default(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        self._write_tsv(tsv, [{"id": "1", "url": "https://a.com", "source": "Acme", "notes": "Eng"}])
        state = {"jobs": {"1": {"status": "failed"}}}
        result = load_pending(tsv, state)
        assert len(result) == 1

    def test_skips_blank_ids(self, tmp_path):
        tsv = tmp_path / "batch-input.tsv"
        with open(tsv, "w", newline="", encoding="utf-8") as f:
            f.write("id\turl\tsource\tnotes\n")
            f.write("\thttps://a.com\tAcme\tEng\n")
            f.write("2\thttps://b.com\tGlobex\tDev\n")
        result = load_pending(tsv, {"jobs": {}})
        assert len(result) == 1
        assert result[0]["id"] == "2"


class TestWriteJobResult:
    def _make_response(self, report="report content", tracker="1\t2026-01-01\tAcme\tEng\tEvaluada\t4.0/5\tnull\t[001](reports/001-acme-2026-01-01.md)\tAPPLY", score=4.0):
        summary = json.dumps({"status": "completed", "id": "42", "report_num": "001", "company": "Acme", "role": "Eng", "score": score, "legitimacy": "High Confidence", "pdf": None, "report": "reports/001-acme-2026-01-01.md", "error": None})
        return f"<evaluation><report>{report}</report><tracker_tsv>{tracker}</tracker_tsv><summary>{summary}</summary></evaluation>"

    def test_writes_report_file(self, tmp_path):
        reports = tmp_path / "reports"
        tracker = tmp_path / "tracker"
        reports.mkdir()
        tracker.mkdir()
        meta = {"id": "42", "report_num": "001", "company": "Acme"}
        out = write_job_result(self._make_response(), meta, reports, tracker, "2026-01-01")
        assert out["report_file"] == "001-acme-2026-01-01.md"
        assert (reports / "001-acme-2026-01-01.md").exists()

    def test_writes_tracker_file(self, tmp_path):
        reports = tmp_path / "reports"
        tracker = tmp_path / "tracker"
        reports.mkdir()
        tracker.mkdir()
        meta = {"id": "42", "report_num": "001", "company": "Acme"}
        out = write_job_result(self._make_response(), meta, reports, tracker, "2026-01-01")
        assert out["tracker_file"] == "42.tsv"
        assert (tracker / "42.tsv").exists()

    def test_returns_score_from_summary(self, tmp_path):
        reports = tmp_path / "reports"
        tracker = tmp_path / "tracker"
        reports.mkdir()
        tracker.mkdir()
        meta = {"id": "42", "report_num": "001", "company": "Acme"}
        out = write_job_result(self._make_response(score=3.7), meta, reports, tracker, "2026-01-01")
        assert out["summary"]["score"] == 3.7

    def test_handles_missing_tags(self, tmp_path):
        reports = tmp_path / "reports"
        tracker = tmp_path / "tracker"
        reports.mkdir()
        tracker.mkdir()
        meta = {"id": "99", "report_num": "002", "company": "Globex"}
        out = write_job_result("<evaluation><summary>{}</summary></evaluation>", meta, reports, tracker, "2026-01-01")
        assert out["report_file"] is None
        assert out["tracker_file"] is None

    def test_slugifies_company_name(self, tmp_path):
        reports = tmp_path / "reports"
        tracker = tmp_path / "tracker"
        reports.mkdir()
        tracker.mkdir()
        summary = json.dumps({"company": "Acme Corp LLC", "score": 4.0})
        response = f"<evaluation><report>content</report><tracker_tsv>row</tracker_tsv><summary>{summary}</summary></evaluation>"
        meta = {"id": "1", "report_num": "001"}
        out = write_job_result(response, meta, reports, tracker, "2026-01-01")
        assert "acme-corp-llc" in out["report_file"]


class TestRunMergeTracker:
    def test_returns_false_when_script_missing(self, tmp_path):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        assert run_merge_tracker(career_ops) is False

    def test_returns_true_on_success(self, tmp_path, mocker):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        (career_ops / "merge-tracker.mjs").write_text("// noop", encoding="utf-8")
        mock_run = mocker.patch("pipeline._batch_common.subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0)
        assert run_merge_tracker(career_ops) is True

    def test_returns_false_on_failure(self, tmp_path, mocker):
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        (career_ops / "merge-tracker.mjs").write_text("// noop", encoding="utf-8")
        mock_run = mocker.patch("pipeline._batch_common.subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=1, stderr="error msg")
        assert run_merge_tracker(career_ops) is False

    def test_seeds_applications_md_before_merge(self, tmp_path, mocker):
        # Regression: merge-tracker no-ops if applications.md doesn't exist.
        # run_merge_tracker must seed the header first so the merge lands.
        career_ops = tmp_path / "career-ops"
        career_ops.mkdir()
        (career_ops / "merge-tracker.mjs").write_text("// noop", encoding="utf-8")
        mocker.patch("pipeline._batch_common.subprocess.run",
                     return_value=mocker.MagicMock(returncode=0))
        apps_md = career_ops / "data" / "applications.md"
        assert not apps_md.exists()
        run_merge_tracker(career_ops)
        assert apps_md.exists()
        assert "Applications Tracker" in apps_md.read_text(encoding="utf-8")

    def test_does_not_clobber_existing_applications_md(self, tmp_path, mocker):
        # If applications.md already exists (e.g. restored from cache with the
        # user's status edits), seeding must NOT overwrite it.
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)
        (career_ops / "merge-tracker.mjs").write_text("// noop", encoding="utf-8")
        apps_md = career_ops / "data" / "applications.md"
        apps_md.write_text("# Applications Tracker\n\nEXISTING USER DATA\n", encoding="utf-8")
        mocker.patch("pipeline._batch_common.subprocess.run",
                     return_value=mocker.MagicMock(returncode=0))
        run_merge_tracker(career_ops)
        assert "EXISTING USER DATA" in apps_md.read_text(encoding="utf-8")


class TestEnsureApplicationsMd:
    def test_creates_header_when_missing(self, tmp_path):
        from pipeline._batch_common import ensure_applications_md
        career_ops = tmp_path / "career-ops"
        p = ensure_applications_md(career_ops)
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert text.startswith("# Applications Tracker")
        assert "| # | Date | Company | Role |" in text

    def test_idempotent_preserves_content(self, tmp_path):
        from pipeline._batch_common import ensure_applications_md
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)
        (career_ops / "data" / "applications.md").write_text("custom", encoding="utf-8")
        ensure_applications_md(career_ops)
        assert (career_ops / "data" / "applications.md").read_text(encoding="utf-8") == "custom"


class TestBuildSystemPrompt:
    def test_includes_cv(self):
        result = build_system_prompt("MY CV CONTENT", "profile: yaml")
        assert "MY CV CONTENT" in result

    def test_includes_profile_yml(self):
        result = build_system_prompt("cv", "profile: yaml content")
        assert "profile: yaml content" in result

    def test_includes_profile_md_when_provided(self):
        result = build_system_prompt("cv", "profile", profile_md="custom instructions")
        assert "custom instructions" in result

    def test_profile_md_omitted_when_empty(self):
        result = build_system_prompt("cv", "profile", profile_md="")
        assert "User Customizations" not in result

    def test_includes_article_digest_when_provided(self):
        result = build_system_prompt("cv", "profile", article_digest="proof points here")
        assert "proof points here" in result

    def test_article_digest_omitted_when_empty(self):
        result = build_system_prompt("cv", "profile", article_digest="")
        assert "Proof Points" not in result
