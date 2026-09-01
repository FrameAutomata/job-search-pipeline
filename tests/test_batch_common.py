"""Tests for pipeline/_batch_common.py"""

import csv
import json
from pathlib import Path

import pytest

from pipeline._batch_common import (
    ADDITION_COLUMNS,
    _sanitize_pending_additions,
    sanitize_addition,
    _inject_req_id_into_notes,
    _normalize_score_cell,
    _pending_additions,
    _warn_on_lost_additions,
    build_system_prompt,
    build_user_message,
    eval_system_prompt,
    extract_req_id,
    extract_tag,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    parse_json_loose,
    read_text,
    run_merge_tracker,
    tail_text,
    write_job_result,
)


class TestTailText:
    def test_returns_last_n_lines(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
        assert tail_text(f, 3) == "line 7\nline 8\nline 9"

    def test_missing_file_returns_empty(self, tmp_path):
        assert tail_text(tmp_path / "nope.txt", 5) == ""

    def test_zero_lines_returns_empty_not_whole_window(self, tmp_path):
        # [-0:] would slice the WHOLE file; the guard must return "" for 0.
        f = tmp_path / "log.txt"
        f.write_text("a\nb\nc", encoding="utf-8")
        assert tail_text(f, 0) == ""

    def test_reads_only_tail_bytes(self, tmp_path):
        # A line beyond the byte window is excluded even if the line count allows
        # it — the read is bounded to the last max_bytes of the file.
        f = tmp_path / "log.txt"
        f.write_text("OLD-LINE\n" + "x" * 100 + "\nNEW-LINE", encoding="utf-8")
        out = tail_text(f, 100, max_bytes=16)
        assert "OLD-LINE" not in out
        assert "NEW-LINE" in out


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

    def test_url_spliced_into_notes_cell(self, tmp_path):
        # The UI's "Open posting" link reads the URL out of the notes cell.
        # Per our prompt, the LLM writes a freeform one-sentence summary there
        # — typically no URL — so without this splice the link points to `#`
        # for every row. Confirm we write the URL into the notes column.
        reports = tmp_path / "reports"; reports.mkdir()
        tracker = tmp_path / "tracker"; tracker.mkdir()
        meta = {"id": "42", "report_num": "001", "company": "Acme",
                "url": "https://www.linkedin.com/jobs/view/12345"}
        write_job_result(self._make_response(), meta, reports, tracker, "2026-01-01")
        written = (tracker / "42.tsv").read_text(encoding="utf-8").rstrip("\n")
        cells = written.split("\t")
        assert len(cells) == 9
        assert "https://www.linkedin.com/jobs/view/12345" in cells[-1]
        # Original LLM notes are preserved alongside the URL.
        assert "APPLY" in cells[-1]

    def test_url_injection_skipped_when_notes_already_have_a_url(self, tmp_path):
        # Don't double up if the LLM already included a URL.
        reports = tmp_path / "reports"; reports.mkdir()
        tracker = tmp_path / "tracker"; tracker.mkdir()
        existing = "https://other.example/posting"
        tracker_row = f"1\t2026-01-01\tAcme\tEng\tEvaluada\t4.0/5\tnull\t[001](reports/001-acme-2026-01-01.md)\t{existing}"
        meta = {"id": "42", "report_num": "001", "company": "Acme",
                "url": "https://canonical.example/job"}
        write_job_result(self._make_response(tracker=tracker_row), meta, reports, tracker, "2026-01-01")
        written = (tracker / "42.tsv").read_text(encoding="utf-8").rstrip("\n")
        notes = written.split("\t")[-1]
        # Existing URL is preserved; we don't append the canonical one.
        assert notes == existing

    def test_url_injection_no_op_when_meta_lacks_url(self, tmp_path):
        # Older callers / legacy state may pass meta without a url field.
        # Leave the row untouched rather than write the literal "—  ".
        reports = tmp_path / "reports"; reports.mkdir()
        tracker = tmp_path / "tracker"; tracker.mkdir()
        meta = {"id": "42", "report_num": "001", "company": "Acme"}  # no url
        write_job_result(self._make_response(), meta, reports, tracker, "2026-01-01")
        written = (tracker / "42.tsv").read_text(encoding="utf-8").rstrip("\n")
        assert written.split("\t")[-1] == "APPLY"

    def test_url_injection_no_op_on_malformed_row(self, tmp_path):
        # If the LLM returned a row with the wrong number of columns, leave it
        # alone rather than corrupt it by splitting/rejoining at the wrong
        # boundary.
        reports = tmp_path / "reports"; reports.mkdir()
        tracker = tmp_path / "tracker"; tracker.mkdir()
        malformed = "1\t2026-01-01\tAcme"  # only 3 cols
        meta = {"id": "42", "report_num": "001", "company": "Acme",
                "url": "https://example.com/job"}
        write_job_result(self._make_response(tracker=malformed), meta, reports, tracker, "2026-01-01")
        written = (tracker / "42.tsv").read_text(encoding="utf-8").rstrip("\n")
        assert written == malformed
        assert "example.com" not in written


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

    def test_refusal_reasons_are_read_off_stderr(self, tmp_path, mocker, capsys):
        """merge-tracker refuses a row with console.warn — stderr. Reading only
        stdout left the reasons block empty on every genuine loss, and printed
        the one refusal it does log to stdout (the benign unscoreable re-eval)
        as the explanation for a different addition's disappearance.
        `tests/test_merge_tracker_contract.py` pins the stream itself."""
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)
        (career_ops / "merge-tracker.mjs").write_text("// noop", encoding="utf-8")
        additions = career_ops / "batch" / "tracker-additions"
        (additions / "merged").mkdir(parents=True)
        (additions / "7.tsv").write_text(
            "11\t2026-09-01\tAcme Corp\tPlatform Engineer\tEvaluated\t4.7/5\tnull\t"
            "[229](reports/229-x.md)\tnote", encoding="utf-8")

        def archive_like_merge_tracker(*a, **kw):
            """merge-tracker archives a row it refused, and still exits 0."""
            for f in additions.glob("*.tsv"):
                f.rename(additions / "merged" / f.name)
            return mocker.MagicMock(
                returncode=0, stdout="📊 Existing: 1 entries\n",
                stderr='⚠️  Skipping 7.tsv: report #229 is marked "failed"\n')

        mocker.patch("pipeline._batch_common.subprocess.run",
                     side_effect=archive_like_merge_tracker)
        run_merge_tracker(career_ops)
        out = capsys.readouterr().out
        assert "WARNING: 1 evaluation(s)" in out
        assert 'Skipping 7.tsv: report #229 is marked "failed"' in out

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

    # --- Commit 4: PROFILE.md as the authoritative candidate profile ----------
    # When a living PROFILE.md is resolved, it supersedes the 4 seed fragments
    # (cv.md / profile.yml / _profile.md / article-digest.md) as the candidate
    # profile — one master drives evaluation. Only the profile *content* swaps;
    # the evaluation framework is unchanged.

    def test_profile_master_supersedes_seed_fragments(self):
        result = build_system_prompt(
            "MYCV_XYZ", "PROFILEYAML_XYZ",
            profile_md="CUSTOM_XYZ", article_digest="PROOF_XYZ",
            profile_master="LIVING MASTER PROFILE",
        )
        assert "LIVING MASTER PROFILE" in result
        # the raw seed fragments are NOT separately dumped when a master is present
        for frag in ("MYCV_XYZ", "PROFILEYAML_XYZ", "CUSTOM_XYZ", "PROOF_XYZ"):
            assert frag not in result

    def test_profile_master_keeps_evaluation_framework(self):
        result = build_system_prompt("cv", "profile", profile_master="MASTER")
        assert "EVALUATION FRAMEWORK" in result
        assert "Machine Summary" in result

    def test_empty_profile_master_is_current_behavior(self):
        # regression: no master → the 4 seed fragments render exactly as before
        result = build_system_prompt(
            "MYCV_XYZ", "PROFILEYAML_XYZ",
            profile_md="CUSTOM_XYZ", article_digest="PROOF_XYZ",
        )
        for frag in ("MYCV_XYZ", "PROFILEYAML_XYZ", "CUSTOM_XYZ", "PROOF_XYZ"):
            assert frag in result
        assert "EVALUATION FRAMEWORK" in result

    def test_whitespace_profile_master_falls_back_to_fragments(self):
        # a blank/whitespace master must not blank out the candidate profile
        result = build_system_prompt("MYCV_XYZ", "profile", profile_master="   \n  ")
        assert "MYCV_XYZ" in result

    def test_none_profile_master_falls_back_to_fragments(self):
        # defensive: a None master degrades like the sibling params, never crashes
        result = build_system_prompt("MYCV_XYZ", "profile", profile_master=None)
        assert "MYCV_XYZ" in result


class TestEvalSystemPrompt:
    """eval_system_prompt(career_ops) — the single candidate-profile resolution
    point shared by the batch (`--evaluate-batch`) and UI add-job eval paths, so
    they can never disagree: PROFILE.md wins when present, else the seed files."""

    def _career_ops(self, tmp_path):
        co = tmp_path / "career-ops"
        (co / "config").mkdir(parents=True)
        (co / "cv.md").write_text("MYCV_XYZ", encoding="utf-8")
        (co / "config" / "profile.yml").write_text("PROFILEYAML_XYZ", encoding="utf-8")
        return co

    def test_uses_profile_master_when_present(self, tmp_path, monkeypatch):
        co = self._career_ops(tmp_path)
        handoff_dir = tmp_path / "handoff"
        handoff_dir.mkdir()
        (handoff_dir / "PROFILE.md").write_text("LIVING MASTER PROFILE", encoding="utf-8")
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(handoff_dir))
        result = eval_system_prompt(co)
        assert "LIVING MASTER PROFILE" in result
        assert "MYCV_XYZ" not in result            # master supersedes the seeds
        assert "PROFILEYAML_XYZ" not in result

    def test_falls_back_to_seeds_when_absent(self, tmp_path, monkeypatch):
        co = self._career_ops(tmp_path)
        handoff_dir = tmp_path / "handoff"
        handoff_dir.mkdir()                          # no PROFILE.md anywhere
        monkeypatch.setenv("HANDOFF_OUT_DIR", str(handoff_dir))
        result = eval_system_prompt(co)
        assert "MYCV_XYZ" in result
        assert "PROFILEYAML_XYZ" in result
        assert "EVALUATION FRAMEWORK" in result


def _tracker_row(score="4.2/5", status="Evaluated", notes="note", role="SRE"):
    """One nine-field tracker-additions row, the shape write_job_result emits."""
    return "\t".join(["3", "2026-08-25", "Initech", role, status,
                      score, "null", "[003](reports/003-x.md)", notes])


class TestNormalizeScoreCell:
    """merge-tracker.mjs decides which of columns 5-6 is the score by asking
    whether exactly one of them matches `N/5` (or an N/A / DUP / dash sentinel).
    When neither does it skips the addition — counted as `skipped`, so it still
    exits 0 AND still archives the TSV to merged/. The evaluation is then lost
    with no error anywhere. The older merge-tracker's fallback assumed this row
    order, so any score string merged and the prompt's "format X.X/5" rule was
    advisory; it is load-bearing now."""

    def _score_of(self, raw):
        return _normalize_score_cell(_tracker_row(raw)).split("\t")[5]

    @pytest.mark.parametrize("raw,expected", [
        ("4.2/5", "4.2/5"),      # already correct — untouched
        ("4.2", "4.2/5"),        # the common model drift
        ("4.2/5.0", "4.2/5"),
        ("4.2 / 5", "4.2/5"),
        ("4,2/5", "4.2/5"),      # comma decimal
        ("**4.5/5**", "4.5/5"),  # bold
        ("5/5", "5/5"),
        ("0/5", "0/5"),
    ])
    def test_coerced_to_mergeable_shape(self, raw, expected):
        assert self._score_of(raw) == expected

    @pytest.mark.parametrize("raw", ["", "unknown", "TBD"])
    def test_unscorable_becomes_the_na_sentinel(self, raw):
        """N/A is a shape merge-tracker recognises, so the row still merges and
        the evaluation stays visible. A scoreless row beats a discarded one."""
        assert self._score_of(raw) == "N/A"

    @pytest.mark.parametrize("raw", ["84%", "8/10", "9.5"])
    def test_other_scales_are_not_clamped_to_a_top_score(self, raw):
        """Out of range means some other scale, and we cannot know which.
        Clamping to 5 would invent a perfect score — and the handoff work-order
        ranks by score descending, so a fabricated 5 puts the role first."""
        assert self._score_of(raw) == "N/A"

    @pytest.mark.parametrize("raw", ["N/A", "DUP"])
    def test_sentinels_pass_through(self, raw):
        assert self._score_of(raw) == raw

    def test_row_with_unexpected_column_count_is_left_alone(self):
        assert _normalize_score_cell("a\tb\tc") == "a\tb\tc"

    def test_applied_by_write_job_result(self, tmp_path):
        """The normalization has to be on the write path, not just available."""
        reports, tracker = tmp_path / "reports", tmp_path / "tsv"
        reports.mkdir(); tracker.mkdir()
        response = TestWriteJobResult()._make_response(tracker=_tracker_row("4.2"))
        write_job_result(response, {"id": 3, "url": "https://x/j/3"},
                         reports, tracker, "2026-08-25")
        written = (tracker / "3.tsv").read_text(encoding="utf-8")
        assert written.split("\t")[5] == "4.2/5"


class TestScoreNormalizationDoesNotInvent:
    """The normalizer must never manufacture a score, and never turn a row
    merge-tracker would accept into one it refuses."""

    @pytest.mark.parametrize("raw", [
        "Top 5%",                 # -> 5/5: a fabricated TOP score, ranked first
        "not scored (4 blockers)",
        "see report 003",
        "N/A - 3 red flags",
    ])
    def test_prose_does_not_become_a_score(self, raw):
        """Searching anywhere in the cell turned prose into a plausible score.
        The handoff work-order ranks by score descending, so an invented 5 puts
        that role at the top of the queue."""
        assert _normalize_score_cell(_tracker_row(raw)).split("\t")[5] == "N/A"

    def test_swapped_status_and_score_are_left_alone(self):
        """When the model writes the pair the other way round, merge-tracker
        resolves it — it asks which ONE of the two looks like a score. Writing
        N/A into col 6 makes BOTH look like one, so it refuses a row it would
        have merged: the loss this function exists to prevent, caused by it."""
        swapped = _tracker_row("Evaluated", status="4.2/5")
        assert _normalize_score_cell(swapped) == swapped


class TestEmptyTrailingCell:
    """A row whose Notes cell is empty arrives one field short — `extract_tag`
    strips the tag body, taking the trailing tab with it. Every sanitizer then
    no-ops on its 9-column guard, so the score is never normalized and
    merge-tracker refuses the row: the row that arrives short is exactly the row
    the chain exists to save."""

    def test_short_row_is_restored_and_normalized(self, tmp_path):
        reports, tracker = tmp_path / "reports", tmp_path / "tsv"
        reports.mkdir(); tracker.mkdir()
        response = (
            "<report>body</report><tracker_tsv>"
            "3\t2026-08-25\tInitech\tSRE | Remote\tEvaluada\t4.2\tnull\t[003](reports/003-x.md)\t"
            "</tracker_tsv><summary>{\"company\": \"Initech\"}</summary>"
        )
        write_job_result(response, {"id": 3, "url": "https://x/j/3"},
                         reports, tracker, "2026-08-25")
        cells = (tracker / "3.tsv").read_text(encoding="utf-8").rstrip("\n").split("\t")
        assert len(cells) == 9
        assert cells[3] == "SRE"          # role pipe stripped
        assert cells[5] == "4.2/5"        # score normalized to the mergeable shape
        assert cells[8].startswith("https://x/j/3")   # url spliced into notes


class TestMaxReportNumCountsLocks:
    """`max_report_num` must COUNT a `NNN-RESERVED.md` lock, which is the exact
    opposite of `data.find_report_file`'s rule — see the docstrings on both.
    Guarded because the tempting refactor is to make them agree: a reader who
    finds the skip in one and propagates it here would break nothing visible in
    the suite, while `server.py`'s Add-Job and `batch_evaluate` would start
    handing out a number career-ops has already reserved, so two writers race
    for the same `NNN-` and one evaluation overwrites the other."""

    def test_reserved_lock_claims_its_number(self, tmp_path):
        from pipeline._batch_common import max_report_num
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "004-acme-2026-01-01.md").write_text("# real", encoding="utf-8")
        (reports / "011-RESERVED.md").write_text('{"pid":1,"token":"x"}', encoding="utf-8")
        assert max_report_num(reports, {}) == 11, "a lock must reserve its number"


class TestLostAdditionGuard:
    """`run_merge_tracker`'s loss guard asks the filesystem "did this evaluation
    reach applications.md", because merge-tracker archives a row it refused and
    still exits 0. The question has TWO readings, and asking only for
    company::role reported three intact evaluations as lost on a real run (#152):
    merge-tracker's guessed match tiers (entry number, fuzzy title) update the row
    but keep THAT row's title, so the key the addition carried is not the key it
    landed under. Verified against the real merge-tracker.mjs — the score and
    report link write through while the title does not."""

    HEADER = ("# Applications Tracker\n\n"
              "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
              "|---|------|---------|------|-------|--------|-----|--------|-------|\n")

    @staticmethod
    def _addition(tracker_dir, name, company, role, report="229"):
        (tracker_dir / name).write_text(
            "\t".join(["11", "2026-09-01", company, role, "Evaluated", "4.7/5", "null",
                       f"[{report}](reports/{report}-x-2026-09-01.md)", "APPLY — note"]),
            encoding="utf-8")

    def _setup(self, tmp_path, rows="", header=None):
        career_ops = tmp_path / "career-ops"
        (career_ops / "data").mkdir(parents=True)
        (career_ops / "data" / "applications.md").write_text(
            (self.HEADER if header is None else header) + rows, encoding="utf-8")
        tracker_dir = career_ops / "batch" / "tracker-additions"
        (tracker_dir / "merged").mkdir(parents=True)
        return career_ops, tracker_dir

    @staticmethod
    def _row(company, role, report="229", score="4.7/5"):
        return (f"| 10 | 2026-09-01 | {company} | {role} | {score} | Evaluated | ❌ "
                f"| [{report}](../reports/{report}-x-2026-09-01.md) | note |\n")

    def _run(self, career_ops, tracker_dir, capsys, merge_output=""):
        """Snapshot the pending additions, archive them the way merge-tracker
        does, then report — the real sequence around the subprocess call."""
        before = _pending_additions(tracker_dir)
        for f in tracker_dir.glob("*.tsv"):
            f.rename(tracker_dir / "merged" / f.name)
        _warn_on_lost_additions(before, career_ops, tracker_dir, merge_output)
        return capsys.readouterr().out

    def test_retitled_row_is_not_reported_lost(self, tmp_path, capsys):
        """The #152 case: merged into an existing row on a fuzzy tier, which
        keeps the row's own title. Report and company survive; the key doesn't."""
        career_ops, tracker_dir = self._setup(
            tmp_path, self._row("UT Southwestern Medical Center", "INSURANCE SPECIALIST II"))
        self._addition(tracker_dir, "123.tsv", "UT Southwestern Medical Center",
                       "INSURANCE SPECIALIST I")
        out = self._run(career_ops, tracker_dir, capsys)
        assert "WARNING" not in out
        assert "123.tsv" in out
        assert "INSURANCE SPECIALIST I" in out and "INSURANCE SPECIALIST II" in out

    def test_landed_under_its_own_identity_is_silent(self, tmp_path, capsys):
        career_ops, tracker_dir = self._setup(tmp_path, self._row("Acme Corp", "Platform Engineer"))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer")
        assert self._run(career_ops, tracker_dir, capsys) == ""

    def test_unscoreable_reeval_keeps_the_rows_old_report_and_stays_silent(self, tmp_path, capsys):
        """merge-tracker deliberately keeps the row's score, report and PDF when a
        re-eval produces no score. The new report number never lands, so only the
        company::role reading can see this one — which is why both are kept."""
        career_ops, tracker_dir = self._setup(
            tmp_path, self._row("Acme Corp", "Platform Engineer", report="200", score="4.0/5"))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer", report="229")
        assert self._run(career_ops, tracker_dir, capsys) == ""

    def test_genuinely_lost_addition_still_warns(self, tmp_path, capsys):
        """Neither identity in the tracker, and the TSV is gone from the queue —
        merge-tracker refused it (a `failed` report number, an unreadable score)
        and archived it anyway, so nothing retries it."""
        career_ops, tracker_dir = self._setup(tmp_path, self._row("Initech", "SRE", report="200"))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer")
        out = self._run(career_ops, tracker_dir, capsys,
                        merge_output='⚠️  Skipping 7.tsv: report #229 is marked "failed"')
        assert "WARNING: 1 evaluation(s)" in out
        assert "7.tsv (Acme Corp — Platform Engineer)" in out
        assert 'Skipping 7.tsv' in out          # merge-tracker's own reason, captured

    def test_report_number_alone_does_not_count_as_landed(self, tmp_path, capsys):
        """Report-file and tracker-row sequences drift, so a bare number can name
        an unrelated row — merge-tracker's own report tier requires the company
        too (#912 upstream), and so must this."""
        career_ops, tracker_dir = self._setup(tmp_path, self._row("Initech", "SRE", report="229"))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer", report="229")
        assert "WARNING: 1 evaluation(s)" in self._run(career_ops, tracker_dir, capsys)

    def test_still_pending_addition_is_not_reported(self, tmp_path, capsys):
        """merge-tracker leaves a TSV it could not apply in place, so a later run
        retries it. That is not a loss."""
        career_ops, tracker_dir = self._setup(tmp_path)
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer")
        before = _pending_additions(tracker_dir)      # nothing archived
        _warn_on_lost_additions(before, career_ops, tracker_dir)
        assert capsys.readouterr().out == ""

    def test_two_additions_sharing_one_key_are_reported_separately(self, tmp_path, capsys):
        """A re-evaluation of a queued role puts two TSVs under one company::role.
        Collapsing them sends the operator after one report while the other stays
        buried."""
        career_ops, tracker_dir = self._setup(tmp_path)
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer", report="229")
        self._addition(tracker_dir, "8.tsv", "Acme Corp", "Platform Engineer", report="230")
        out = self._run(career_ops, tracker_dir, capsys)
        assert "WARNING: 2 evaluation(s)" in out
        assert "7.tsv" in out and "8.tsv" in out

    def test_blank_role_in_the_matched_row_is_not_a_loss(self, tmp_path, capsys):
        """A tracker row with an empty Role cell (hand-added, half-migrated) is
        still a row the addition merged into. Testing the matched title for
        truthiness rather than the key for membership put this back in the loud
        WARNING — the #152 cry-wolf by another route."""
        career_ops, tracker_dir = self._setup(tmp_path, self._row("Acme Corp", ""))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer")
        out = self._run(career_ops, tracker_dir, capsys)
        assert "WARNING" not in out
        assert "7.tsv" in out

    def test_pipe_delimited_addition_is_still_seen(self, tmp_path, capsys):
        """A model that ignores the prompt's tab rule and emits a markdown row
        produces a file merge-tracker reads — and can therefore refuse and
        archive. Splitting on tabs alone made that addition invisible here, so
        its loss would have been silent, uncounted and unnamed."""
        career_ops, tracker_dir = self._setup(tmp_path, self._row("Initech", "SRE", report="200"))
        (tracker_dir / "7.tsv").write_text(
            "| 11 | 2026-09-01 | Acme Corp | Platform Engineer | Evaluated | 4.7/5 "
            "| null | [229](reports/229-x.md) | note |", encoding="utf-8")
        out = self._run(career_ops, tracker_dir, capsys)
        assert "WARNING: 1 evaluation(s)" in out
        assert "7.tsv (Acme Corp — Platform Engineer)" in out

    def test_report_zero_is_not_an_identity(self, tmp_path, capsys):
        """`000` is what write_job_result writes when it has NO number, and
        merge-tracker's own `if (reportNum && …)` treats 0 as absent. Reading it
        as an identity would let two numberless additions at one company be
        taken for the same evaluation, and report a real loss as a retitle."""
        career_ops, tracker_dir = self._setup(
            tmp_path, self._row("Acme Corp", "Other Role", report="000"))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer", report="000")
        assert "WARNING: 1 evaluation(s)" in self._run(career_ops, tracker_dir, capsys)

    def test_via_layout_is_read_by_header_name(self, tmp_path, capsys):
        """The tracker's optional Via column shifts Role right by one. Read
        positionally the agency lands where the role belongs, and every addition
        looks lost."""
        career_ops, tracker_dir = self._setup(tmp_path, header=(
            "# Applications Tracker\n\n"
            "| # | Date | Company | Via | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|-----|------|-------|--------|-----|--------|-------|\n"
            "| 10 | 2026-09-01 | Acme Corp | Hays | Platform Engineer | 4.7/5 | Evaluated | ❌ "
            "| [229](../reports/229-x-2026-09-01.md) | note |\n"))
        self._addition(tracker_dir, "7.tsv", "Acme Corp", "Platform Engineer")
        assert self._run(career_ops, tracker_dir, capsys) == ""


class TestExtractReqId:
    """A req id in the Notes column is what stops merge-tracker's fuzzy title
    match folding two distinct requisitions into one row (#154) — its matcher
    drops tokens of ≤3 characters, so `SPECIALIST I` and `SPECIALIST II` compare
    equal. The extractor is deliberately strict: no id is exactly today's
    behaviour, while a WRONG id splits two rows that should fold."""

    @pytest.mark.parametrize("jd,expected", [
        ("Job ID: 88214", "88214"),
        ("Requisition Number R-2291", "R-2291"),
        ("Posting ID 5340", "5340"),
        ("Job Reference Number JR00124259 — apply today", "JR00124259"),
        ("job id 65136", "65136"),                     # case-insensitive
        ("Position Number: 00012345", "00012345"),
        ("Job ID: 88214 … and in the footer, Job ID 88214", "88214"),
        ("Req #1311", "1311"),
        ("Req # 1311", "1311"),            # `#` and the space are both non-word
        ("Position #: 12345", "12345"),
    ])
    def test_reads_a_labelled_id(self, jd, expected):
        assert extract_req_id(jd) == expected

    @pytest.mark.parametrize("jd,expected", [
        ("**Job ID:** 88214", "88214"),
        ("- **Job ID**: 65136", "65136"),
        ("Requisition ID: R\\_1488728", "R_1488728"),
        ("Job ID: JR\\-00124259", "JR-00124259"),
    ])
    def test_reads_the_markdown_its_own_cache_is_written_in(self, jd, expected):
        """JobSpy's description_format defaults to MARKDOWN and scrape.py
        forwards it, so an Indeed JD is cached as `**Job ID:** 88214` with `_`
        and `-` backslash-escaped mid-word. LinkedIn's is backfilled as plain
        text by screen.py — so a pattern written for prose worked on one board
        and silently never fired on the other."""
        assert extract_req_id(jd) == expected

    @pytest.mark.parametrize("jd", [
        "Job ID: 5340-Nurse-Practitioner-Days-FT",          # id + slugged title
        "Requisition ID REQ-2026-320610000000000000000000",  # not an id at all
    ])
    def test_an_overlong_token_is_declined_not_truncated(self, jd):
        """A bounded capture truncated instead of declining: this yielded
        `5340-NURSE-PRACTITIONER-` and `REQ-2026-`, and merge-tracker reads the
        latter back as `REQ-2026` — a different string from the one we wrote, and
        one that two different reqs sharing a prefix would both produce. Over-long
        means we cannot tell where the id ends."""
        assert extract_req_id(jd) == ""

    def test_a_pay_grade_is_not_a_requisition(self):
        """In public-sector and healthcare postings — the shape this exists for —
        "Job Code" is a classification shared by every posting of that title. One
        board's copy stating only that, against another stating a real req id,
        would split a cross-board pair."""
        assert extract_req_id("Job Code: 4021 — Insurance Specialist I") == ""

    @pytest.mark.parametrize("jd", [
        "This job 2 of 5 in the series",       # a bare label + a number is prose
        "benefits include a 401k plan",
        "Job Type: Full-time. Remote.",        # `Type` is not a qualifier
        "reference checks required",
        "",
    ])
    def test_prose_does_not_become_an_id(self, jd):
        """merge-tracker's own regex accepts a BARE `job`/`ref` label because a
        human wrote that cell deliberately. A JD is prose we did not write."""
        assert extract_req_id(jd) == ""

    @pytest.mark.parametrize("jd", ["Job ID: TBD", "Requisition Number: Pending"])
    def test_a_labelled_value_with_no_digit_is_not_an_id(self, jd):
        """The digit filter, which every other negative case reaches the regex
        too early to exercise. Without it, "reference number for our EEO policy
        is 11246" yields `FOR`."""
        assert extract_req_id(jd) == ""

    def test_two_different_ids_are_ambiguous_and_yield_nothing(self):
        """Guessing which one is the requisition splits a row that should fold;
        declining leaves today's behaviour untouched."""
        assert extract_req_id("Job ID 123 … Requisition Number ABC9") == ""

    def test_none_of_this_reads_the_boards_own_job_key(self):
        """A `jk=`/`currentJobId` key identifies the POSTING, so a re-post of one
        requisition carries a new one — keying on it would add a tracker row
        every time a listing is re-published."""
        assert extract_req_id("https://www.indeed.com/viewjob?jk=1a2b3c4d5e") == ""
        assert extract_req_id("https://www.linkedin.com/jobs/view/4123456789/") == ""


class TestInjectReqIdIntoNotes:
    def test_leads_the_cell(self):
        """Ahead of the URL: merge-tracker's extractReqNumber takes the FIRST
        match in the cell, so leading with the resolved id makes the reading
        deterministic rather than dependent on the free-text sentence."""
        out = _inject_req_id_into_notes(_tracker_row(notes="https://x/j — APPLY"), "R-2291")
        assert out.split("\t")[-1] == "req R-2291 — https://x/j — APPLY"

    def test_empty_notes(self):
        assert _inject_req_id_into_notes(_tracker_row(notes=""), "5340").split("\t")[-1] == "req 5340"

    def test_no_id_is_a_no_op(self):
        row = _tracker_row(notes="APPLY")
        assert _inject_req_id_into_notes(row, "") == row

    def test_does_not_restate_an_id_the_model_already_wrote(self):
        row = _tracker_row(notes="req 5340 — APPLY")
        assert _inject_req_id_into_notes(row, "5340") == row

    def test_malformed_row_is_left_alone(self):
        assert _inject_req_id_into_notes("a\tb\tc", "5340") == "a\tb\tc"

    def test_a_whitespace_only_row_is_left_alone(self):
        """`_restore_trailing_cells` pads a short row to nine fields, so an empty
        row can reach here WITH the right column count. Writing into it produces
        a half-populated garbage row instead of one visibly left untouched."""
        blank = "\t" * (len(ADDITION_COLUMNS) - 1)
        assert _inject_req_id_into_notes(blank, "5340") == blank

    def test_applied_by_write_job_result(self, tmp_path):
        """On the write path, not merely available — and after the URL, so the
        id ends up ahead of it."""
        reports, tracker = tmp_path / "reports", tmp_path / "tsv"
        reports.mkdir(); tracker.mkdir()
        response = TestWriteJobResult()._make_response(tracker=_tracker_row(notes="APPLY strong match"))
        write_job_result(response,
                         {"id": 3, "url": "https://x/j/3", "jd_text": "Job ID: 88214"},
                         reports, tracker, "2026-08-25")
        notes = (tracker / "3.tsv").read_text(encoding="utf-8").rstrip("\n").split("\t")[-1]
        assert notes == "req 88214 — https://x/j/3 — APPLY strong match"



class TestSanitizePendingAdditions:
    """career-ops' own evaluators write straight into tracker-additions/, never
    entering Python — so those rows reached a merge with none of the chain
    applied, and an unreadable score cell got the row REFUSED and archived
    (exit 0, never retried), which is permanent loss."""

    CLI_ROW = _tracker_row(role="Platform Engineer | Remote", score="4.2",
                           notes="APPLY strong match")

    def _tree(self, tmp_path, rows=None, queued=True):
        co = tmp_path / "career-ops"
        additions = co / "batch" / "tracker-additions"
        additions.mkdir(parents=True)
        (co / "batch" / "jds").mkdir()
        for name, row in (rows or {"7.tsv": self.CLI_ROW}).items():
            (additions / name).write_text(row + "\n", encoding="utf-8")
            (co / "batch" / "jds" / f"{Path(name).stem}.txt").write_text(
                "Job ID: 88214", encoding="utf-8")
        if queued:
            (co / "batch" / "batch-input.tsv").write_text(
                "id\turl\tsource\tnotes\n7\thttps://x/j/7\tAcme Corp\tPlatform Engineer\n",
                encoding="utf-8")
        return co, additions

    @staticmethod
    def _cells(additions, name="7.tsv"):
        return (additions / name).read_text(encoding="utf-8").rstrip("\n").split("\t")

    def test_repairs_a_row_no_python_writer_touched(self, tmp_path):
        co, additions = self._tree(tmp_path)
        _sanitize_pending_additions(co, additions)
        cells = self._cells(additions)
        assert cells[3] == "Platform Engineer"          # pipe suffix stripped
        assert cells[5] == "4.2/5"                      # merge-tracker can read it
        assert cells[8] == "req 88214 — https://x/j/7 — APPLY strong match"

    def test_says_so(self, tmp_path, capsys):
        """In a cloud run the log is all the operator has."""
        co, additions = self._tree(tmp_path)
        _sanitize_pending_additions(co, additions)
        assert "sanitized 1 addition" in capsys.readouterr().out

    def test_a_row_already_sanitized_is_left_exactly_alone(self, tmp_path, capsys):
        """The chain runs at two points and must not be two mechanisms."""
        done = _tracker_row(score="4.2/5",
                            notes="req 88214 — https://x/j/7 — APPLY strong match")
        co, additions = self._tree(tmp_path, rows={"7.tsv": done})
        _sanitize_pending_additions(co, additions)
        assert (additions / "7.tsv").read_text(encoding="utf-8") == done + "\n"
        assert "sanitized" not in capsys.readouterr().out

    def test_the_rows_own_trailing_url_is_preferred_over_a_lookup(self, tmp_path):
        """career-ops' batch worker is told to append the URL as a 10th field, so
        on the path this exists for it is already in the row — no lookup, and no
        dependence on the filename being a job id."""
        co, additions = self._tree(
            tmp_path, rows={"229-acme.tsv": self.CLI_ROW + "\thttps://own/j/9"},
            queued=False)
        _sanitize_pending_additions(co, additions)
        assert "https://own/j/9" in self._cells(additions, "229-acme.tsv")[8]

    def test_a_ten_column_row_gets_its_score_fixed(self, tmp_path):
        """Nine columns is OUR prompt's shape; career-ops tells its own worker to
        write nine PLUS a trailing url, and merge-tracker parses that natively. A
        `== 9` guard skipped the score normalization while the pipe strip still
        rewrote the row — so the repair was COUNTED and the row refused anyway."""
        co, additions = self._tree(
            tmp_path, rows={"7.tsv": self.CLI_ROW + "\thttps://own/j/9"})
        _sanitize_pending_additions(co, additions)
        cells = self._cells(additions)
        assert cells[5] == "4.2/5"
        assert cells[9] == "https://own/j/9"            # extras carried through

    def test_an_unknown_filename_still_gets_the_score_repair(self, tmp_path):
        """career-ops' other writers name additions `{num}-{slug}.tsv`, so the
        job-id lookups find nothing. The repair that does not depend on them must
        still happen."""
        co, additions = self._tree(tmp_path, rows={"229-acme.tsv": self.CLI_ROW},
                                   queued=False)
        _sanitize_pending_additions(co, additions)
        assert self._cells(additions, "229-acme.tsv")[5] == "4.2/5"

    def test_a_pipe_delimited_row_is_left_alone(self, tmp_path):
        """merge-tracker parses that shape natively; every step here splits on
        tabs. Rewriting it would change which of upstream's parsers reads it."""
        piped = "| 11 | 2026-09-01 | Acme Corp | Platform Engineer | Evaluated | 4.2 | null | [229](reports/229-acme.md) | note |"
        co, additions = self._tree(tmp_path, rows={"7.tsv": piped})
        _sanitize_pending_additions(co, additions)
        assert (additions / "7.tsv").read_text(encoding="utf-8") == piped + "\n"

    def test_run_merge_tracker_wires_it_in(self, tmp_path, mocker):
        """The one test that goes through the merge: sanitizing must happen, and
        must happen BEFORE the loss guard's snapshot, since stripping a role's
        pipe suffix changes half the identity that guard matches on."""
        co, additions = self._tree(tmp_path)
        (co / "data").mkdir(parents=True)
        (co / "merge-tracker.mjs").write_text("// noop", encoding="utf-8")
        mocker.patch("pipeline._batch_common.subprocess.run",
                     return_value=mocker.MagicMock(returncode=0, stdout="", stderr=""))
        run_merge_tracker(co)
        assert self._cells(additions)[5] == "4.2/5"


class TestSanitizeAddition:
    def test_is_idempotent(self):
        """What lets the chain run on the write path AND at the merge without
        being two mechanisms."""
        raw = ("11\t2026-09-01\tAcme\tPlatform Engineer | Remote\tEvaluated\t4.2\tnull\t"
               "[229](reports/229-a.md)\tAPPLY")
        once = sanitize_addition(raw, "https://x/j", "Job ID: 88214")
        assert sanitize_addition(once, "https://x/j", "Job ID: 88214") == once
