"""Tests for pipeline/app/data.py — applications.md parsing + report lookup."""

from pathlib import Path

import pytest

from pipeline.app import data


SAMPLE_APPLICATIONS = """# Applications Tracker

| # | Date | Company | Role | Score | Status | PDF | Report | Notes |
|---|------|---------|------|-------|--------|-----|--------|-------|
| 1 | 2026-05-27 | Acme | Backend Engineer | 4.2/5 | Evaluated | ❌ | [001](reports/001-acme-2026-05-27.md) | APPLY strong match |
| 2 | 2026-05-27 | Globex | Frontend Engineer | 2.8/5 | SKIP | ❌ | [002](reports/002-globex-2026-05-27.md) | SKIP weak fit |
| 3 | 2026-05-27 | Initech | Full Stack Engineer | 3.5/5 | Applied | ✅ | [003](reports/003-initech-2026-05-27.md) | CONSIDER ok |
"""


class TestParseApplications:
    def test_missing_file_returns_empty(self, tmp_path):
        assert data.parse_applications(tmp_path / "nope.md") == []

    def test_header_only_returns_empty(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "# Applications Tracker\n\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n",
            encoding="utf-8",
        )
        assert data.parse_applications(f) == []

    def test_parses_rows(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert len(rows) == 3
        first = rows[0]
        assert first["company"] == "Acme"
        assert first["role"] == "Backend Engineer"
        assert first["status"] == "Evaluated"
        assert first["num"] == "1"

    def test_extracts_report_num_and_path(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert rows[0]["report_num"] == "001"
        assert rows[0]["report_path"] == "reports/001-acme-2026-05-27.md"

    def test_parses_score_value(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert rows[0]["score_value"] == 4.2
        assert rows[1]["score_value"] == 2.8
        assert rows[2]["score_value"] == 3.5

    def test_score_value_none_when_unparseable(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "# Applications Tracker\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-05-27 | Acme | Eng | n/a | Evaluated | ❌ | [001](reports/001-x.md) | note |\n",
            encoding="utf-8",
        )
        rows = data.parse_applications(f)
        assert rows[0]["score_value"] is None

    def test_skips_separator_and_header(self, tmp_path):
        # No data row should ever be the header or separator.
        f = tmp_path / "applications.md"
        f.write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        rows = data.parse_applications(f)
        assert all(r["num"] not in ("#", "num") for r in rows)
        assert all("---" not in r["date"] for r in rows)

    def test_row_missing_columns_skipped(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-05-27 | Acme |\n"  # too few columns
            "| 2 | 2026-05-27 | Globex | Eng | 3.0/5 | Evaluated | ❌ | [002](reports/002-x.md) | ok |\n",
            encoding="utf-8",
        )
        rows = data.parse_applications(f)
        assert len(rows) == 1
        assert rows[0]["company"] == "Globex"


class TestFindReportFile:
    def _make_reports(self, tmp_path):
        d = tmp_path / "reports"
        d.mkdir()
        (d / "001-acme-2026-05-27.md").write_text("# Acme report", encoding="utf-8")
        (d / "042-globex-2026-05-27.md").write_text("# Globex report", encoding="utf-8")
        return d

    def test_finds_by_padded_number(self, tmp_path):
        d = self._make_reports(tmp_path)
        f = data.find_report_file(d, "001")
        assert f is not None and f.name == "001-acme-2026-05-27.md"

    def test_finds_with_padding_mismatch(self, tmp_path):
        # Tracker may store "42" while the file is "042-...".
        d = self._make_reports(tmp_path)
        f = data.find_report_file(d, "42")
        assert f is not None and f.name == "042-globex-2026-05-27.md"

    def test_missing_number_returns_none(self, tmp_path):
        d = self._make_reports(tmp_path)
        assert data.find_report_file(d, "999") is None

    def test_non_numeric_returns_none(self, tmp_path):
        d = self._make_reports(tmp_path)
        assert data.find_report_file(d, "abc") is None

    def test_missing_dir_returns_none(self, tmp_path):
        assert data.find_report_file(tmp_path / "nope", "001") is None


SAMPLE_TSV = (
    "2920\t2026-05-27\tTential Solutions\tFullstack Developer\tEvaluada\t4.0/5\tnull\t"
    "[2920](reports/2920-tential-solutions-2026-05-27.md)\tCONSIDER: strong match\n"
)


class TestParseTrackerAdditions:
    def _make(self, tmp_path, files: dict[str, str]) -> Path:
        d = tmp_path / "batch" / "tracker-additions"
        d.mkdir(parents=True)
        for name, content in files.items():
            (d / name).write_text(content, encoding="utf-8")
        return d

    def test_missing_dir_returns_empty(self, tmp_path):
        assert data.parse_tracker_additions(tmp_path / "nope") == []

    def test_parses_tsv_row(self, tmp_path):
        d = self._make(tmp_path, {"562.tsv": SAMPLE_TSV})
        rows = data.parse_tracker_additions(d)
        assert len(rows) == 1
        r = rows[0]
        assert r["num"] == "2920"
        assert r["company"] == "Tential Solutions"
        assert r["role"] == "Fullstack Developer"
        assert r["status"] == "Evaluada"      # status before score in TSV order
        assert r["score_value"] == 4.0
        assert r["report_num"] == "2920"
        assert r["report_path"] == "reports/2920-tential-solutions-2026-05-27.md"
        assert "CONSIDER" in r["notes"]

    def test_sorted_by_tracker_number(self, tmp_path):
        d = self._make(tmp_path, {
            "a.tsv": SAMPLE_TSV.replace("2920", "30"),
            "b.tsv": SAMPLE_TSV.replace("2920", "5"),
            "c.tsv": SAMPLE_TSV.replace("2920", "100"),
        })
        rows = data.parse_tracker_additions(d)
        assert [r["num"] for r in rows] == ["5", "30", "100"]

    def test_notes_with_tab_not_oversplit(self, tmp_path):
        # A stray tab in notes must not break column alignment (maxsplit guard).
        tsv = SAMPLE_TSV.rstrip("\n").rsplit("\t", 1)[0] + "\tnote\twith\ttabs\n"
        d = self._make(tmp_path, {"x.tsv": tsv})
        rows = data.parse_tracker_additions(d)
        assert len(rows) == 1
        assert rows[0]["notes"] == "note\twith\ttabs"

    def test_short_row_skipped(self, tmp_path):
        d = self._make(tmp_path, {"bad.tsv": "1\t2026-05-27\tAcme\n"})
        assert data.parse_tracker_additions(d) == []


class TestLoadJobs:
    def _career_ops(self, tmp_path) -> Path:
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        (co / "batch" / "tracker-additions").mkdir(parents=True)
        return co

    def test_prefers_applications_md(self, tmp_path):
        co = self._career_ops(tmp_path)
        (co / "data" / "applications.md").write_text(SAMPLE_APPLICATIONS, encoding="utf-8")
        (co / "batch" / "tracker-additions" / "x.tsv").write_text(SAMPLE_TSV, encoding="utf-8")
        result = data.load_jobs(co)
        assert result["source"] == "applications"
        assert len(result["rows"]) == 3  # from applications.md, not the tsv

    def test_falls_back_to_tracker_additions(self, tmp_path):
        # No applications.md → use raw tracker-additions. This is exactly the
        # production case where merge-tracker didn't run.
        co = self._career_ops(tmp_path)
        (co / "batch" / "tracker-additions" / "562.tsv").write_text(SAMPLE_TSV, encoding="utf-8")
        result = data.load_jobs(co)
        assert result["source"] == "tracker-additions"
        assert len(result["rows"]) == 1
        assert result["rows"][0]["company"] == "Tential Solutions"

    def test_empty_applications_md_falls_back(self, tmp_path):
        co = self._career_ops(tmp_path)
        (co / "data" / "applications.md").write_text(
            "# Applications Tracker\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n",
            encoding="utf-8",
        )
        (co / "batch" / "tracker-additions" / "562.tsv").write_text(SAMPLE_TSV, encoding="utf-8")
        result = data.load_jobs(co)
        assert result["source"] == "tracker-additions"

    def test_nothing_anywhere(self, tmp_path):
        co = self._career_ops(tmp_path)
        result = data.load_jobs(co)
        assert result == {"rows": [], "source": "none"}


class TestCanonicalStatus:
    def test_canonical_passthrough(self):
        assert data.canonical_status("Applied") == "Applied"
        assert data.canonical_status("evaluated") == "Evaluated"

    def test_spanish_aliases(self):
        assert data.canonical_status("Evaluada") == "Evaluated"
        assert data.canonical_status("Aplicada") == "Applied"
        assert data.canonical_status("Rechazado") == "Rejected"
        assert data.canonical_status("Descartada") == "Discarded"

    def test_strips_markdown_bold(self):
        assert data.canonical_status("**Applied**") == "Applied"

    def test_unknown_passes_through(self):
        assert data.canonical_status("Negotiating") == "Negotiating"

    def test_status_canonical_field_on_rows(self, tmp_path):
        f = tmp_path / "applications.md"
        f.write_text(
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-05-27 | Acme | Eng | 4.0/5 | Evaluada | ❌ | [001](reports/001-x.md) | n |\n",
            encoding="utf-8",
        )
        rows = data.parse_applications(f)
        assert rows[0]["status"] == "Evaluada"
        assert rows[0]["status_canonical"] == "Evaluated"


class TestSetStatusInText:
    APPS = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 1 | 2026-05-27 | Acme | Eng | 4.0/5 | Evaluated | ❌ | [001](reports/001-x.md) | apply now |\n"
        "| 2 | 2026-05-27 | Globex | Dev | 3.0/5 | Evaluated | ❌ | [002](reports/002-y.md) | maybe |\n"
    )

    @staticmethod
    def _status_of(text, num):
        for line in text.splitlines():
            if line.lstrip().startswith(f"| {num} "):
                return [c.strip() for c in line.split("|")][6]
        return None

    def test_changes_only_target_status(self):
        out = data.set_status_in_text(self.APPS, "2", "Applied")
        assert self._status_of(out, "2") == "Applied"
        assert self._status_of(out, "1") == "Evaluated"

    def test_preserves_other_cells_verbatim(self):
        out = data.set_status_in_text(self.APPS, "1", "Interview")
        assert "[001](reports/001-x.md)" in out
        assert "apply now" in out
        assert self._status_of(out, "1") == "Interview"

    def test_unknown_num_unchanged(self):
        assert data.set_status_in_text(self.APPS, "999", "Applied") == self.APPS

    def test_header_not_editable(self):
        # Passing "#" must not rewrite the header row's Status cell.
        assert data.set_status_in_text(self.APPS, "#", "Applied") == self.APPS


class TestRecordStatusOverride:
    """The shared pending-status channel (kanban drags + apply auto-submits)."""

    def test_creates_and_merges(self, tmp_path):
        import json
        p = tmp_path / "overrides.json"
        data.record_status_override("7", "Applied", p)
        data.record_status_override("9", "Rejected", p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"7": "Applied", "9": "Rejected"}

    def test_overwrites_same_num(self, tmp_path):
        import json
        p = tmp_path / "overrides.json"
        data.record_status_override("7", "Evaluated", p)
        data.record_status_override("7", "Applied", p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"7": "Applied"}

    def test_corrupt_file_tolerated(self, tmp_path):
        import json
        p = tmp_path / "overrides.json"
        p.write_text("not json{", encoding="utf-8")
        data.record_status_override("7", "Applied", p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"7": "Applied"}

    def test_non_dict_top_level_tolerated(self, tmp_path):
        # A JSON array (or any non-object) must not poison index access — the
        # torn-read-wipe path the consolidation guards against.
        import json
        p = tmp_path / "overrides.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert data.load_status_overrides(p) == {}
        data.record_status_override("7", "Applied", p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"7": "Applied"}

    def test_identity_anchored_value(self, tmp_path):
        import json
        p = tmp_path / "overrides.json"
        data.record_status_override("7", "Applied", p, company="Acme", role="Eng")
        v = json.loads(p.read_text(encoding="utf-8"))["7"]
        assert v == {"status": "Applied", "company": "Acme", "role": "Eng"}
        assert data.override_status(v) == "Applied"
        assert data.override_identity(v) == ("Acme", "Eng")

    def test_plain_value_has_no_identity(self):
        assert data.override_status("Applied") == "Applied"
        assert data.override_identity("Applied") is None

    def test_clear_only_named_keys(self, tmp_path):
        # Selective clear keeps an entry written between a push's snapshot and now.
        import json
        p = tmp_path / "overrides.json"
        data.record_status_override("1", "Applied", p)
        data.record_status_override("2", "Rejected", p)
        data.clear_status_overrides(["1"], p)
        assert json.loads(p.read_text(encoding="utf-8")) == {"2": "Rejected"}


class TestOverrideMatchesRow:
    def test_identity_matches_by_company_and_role(self):
        row = {"company": "Acme Inc.", "role": "Senior Engineer", "num": "9"}
        v = {"status": "Applied", "company": "acme inc", "role": "senior engineer"}
        assert data.override_matches_row(v, row) is True

    def test_company_only_anchor_matches_any_role(self):
        row = {"company": "Acme", "role": "Whatever", "num": "9"}
        assert data.override_matches_row({"status": "Applied", "company": "Acme", "role": ""}, row) is True

    def test_wrong_company_does_not_match(self):
        row = {"company": "Globex", "role": "Eng", "num": "9"}
        assert data.override_matches_row({"status": "Applied", "company": "Acme", "role": "Eng"}, row) is False

    def test_plain_value_never_matches(self):
        assert data.override_matches_row("Applied", {"company": "Acme", "role": "Eng"}) is False


class TestResolveNumByIdentity:
    APPS = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 11 | 2026-06-01 | Acme | Eng | 4.0/5 | Evaluated | ❌ | [011](reports/011.md) | x |\n"
        "| 12 | 2026-06-01 | Globex | Dev | 4.5/5 | Evaluated | ❌ | [012](reports/012.md) | y |\n"
    )

    def test_resolves_to_correct_num(self):
        assert data.resolve_num_by_identity(self.APPS, "Globex", "Dev") == "12"

    def test_company_only(self):
        assert data.resolve_num_by_identity(self.APPS, "Acme", "") == "11"

    def test_no_match_returns_none(self):
        assert data.resolve_num_by_identity(self.APPS, "Initech", "QA") is None


class TestResolveOverridesForPush:
    """#1: building the push payload. An identity-anchored override that DOESN'T
    resolve in the base tracker must NOT fall back to its (foreign) num and mark
    a different company — and must be reported unresolved so the caller doesn't
    clear it (losing the real pending Applied forever)."""

    # num 5 is Globex here — an Acme identity override keyed by '5' must not
    # touch this row.
    APPS = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 3 | 2026-06-01 | Acme | Engineer | 4.2/5 | Evaluated | ❌ | [003](reports/003.md) | a |\n"
        "| 5 | 2026-06-01 | Globex | Dev | 4.5/5 | Evaluated | ❌ | [005](reports/005.md) | b |\n"
    )

    def test_unresolved_identity_is_not_applied_or_dispatched(self):
        base_no_acme = (
            "# Applications Tracker\n\n"
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 5 | 2026-06-01 | Globex | Dev | 4.5/5 | Evaluated | ❌ | [005](reports/005.md) | b |\n"
        )
        overrides = {"5": {"status": "Applied", "company": "Acme", "role": "Engineer"}}
        new_text, cloud_payload, unresolved = data.resolve_overrides_for_push(base_no_acme, overrides)
        assert cloud_payload == {}            # nothing dispatched to the cloud
        assert unresolved == ["5"]            # flagged so the caller won't clear it
        assert new_text == base_no_acme       # Globex (num 5) left untouched

    def test_resolved_identity_marks_correct_row(self):
        overrides = {"99": {"status": "Applied", "company": "Acme", "role": "Engineer"}}
        new_text, cloud_payload, unresolved = data.resolve_overrides_for_push(self.APPS, overrides)
        assert cloud_payload == {"3": "Applied"}     # resolved to Acme's real num
        assert unresolved == []
        assert "| Applied |" in new_text and "Acme" in new_text

    def test_plain_override_applied_by_num(self):
        overrides = {"5": "SKIP"}
        new_text, cloud_payload, unresolved = data.resolve_overrides_for_push(self.APPS, overrides)
        assert cloud_payload == {"5": "SKIP"}
        assert unresolved == []

    def test_build_text_false_keeps_payload_but_skips_rebuild(self):
        # The refreshed-artifact push only needs cloud_payload (it doesn't persist
        # the merged text), so build_text=False leaves the base unchanged while
        # still resolving + dispatching the same payload.
        overrides = {"99": {"status": "Applied", "company": "Acme", "role": "Engineer"}}
        new_text, cloud_payload, unresolved = data.resolve_overrides_for_push(
            self.APPS, overrides, build_text=False)
        assert cloud_payload == {"3": "Applied"}   # resolution unaffected
        assert unresolved == []
        assert new_text == self.APPS               # base text untouched


class TestRenderReportHtml:
    def test_renders_markdown_or_falls_back(self, tmp_path):
        f = tmp_path / "r.md"
        f.write_text("# Title\n\nSome **bold** text.", encoding="utf-8")
        html = data.render_report_html(f)
        # Either real markdown (markdown installed) or a <pre> fallback — both
        # must contain the source text.
        assert "Title" in html
        assert "bold" in html

    def test_markdown_renders_headings_when_available(self, tmp_path):
        pytest.importorskip("markdown")
        f = tmp_path / "r.md"
        f.write_text("# Heading One\n\nbody", encoding="utf-8")
        html = data.render_report_html(f)
        assert "<h1>" in html.lower()
