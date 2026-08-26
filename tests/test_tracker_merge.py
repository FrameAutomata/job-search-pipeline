"""Tests for data.reconcile_trackers — the offline-first tracker merge.

Refresh pulls the cloud tracker and merges it into the durable local tracker:
cloud wins for shared roles, local-only rows (offline `Run local` results) are
preserved and renumbered to avoid colliding with cloud row/report numbers.

New report numbers are assigned as max(used)+1 so they can't collide with cloud
reports or with each other. "Cloud reports" means every number the cloud tracker
names, not just the ones whose files rode down in today's artifact — the daily
artifact carries only that run's reports (issue #129).
"""

from pipeline.app import data

HEADER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
)


def _row(num, company, role, *, status="Evaluated", score="3.0/5", report=None, notes="x"):
    report = num if report is None else report
    return (f"| {num} | 2026-06-20 | {company} | {role} | {score} | {status} "
            f"| null | [{report}](reports/{report}-x.md) | {notes} |\n")


def _tracker(*rows):
    return HEADER + "".join(rows)


def _by_company(md):
    return {r["company"]: r for r in data.parse_applications_text(md)}


class TestReconcileTrackers:
    def test_empty_local_returns_cloud_unchanged(self):
        cloud = _tracker(_row(1, "Acme", "Eng"), _row(2, "Globex", "Dev"))
        merged, renames = data.reconcile_trackers(cloud, "", {"1", "2"})
        assert renames == []
        rows = data.parse_applications_text(merged)
        assert [r["company"] for r in rows] == ["Acme", "Globex"]
        assert [r["num"] for r in rows] == ["1", "2"]

    def test_shared_role_keeps_cloud_row(self):
        cloud = _tracker(_row(1, "Acme", "Eng", status="Evaluated", score="4.0/5"))
        local = _tracker(_row(9, "Acme", "Eng", status="Applied", score="2.0/5"))
        merged, renames = data.reconcile_trackers(cloud, local, {"1"})
        rows = data.parse_applications_text(merged)
        assert len(rows) == 1
        assert rows[0]["status"] == "Evaluated"   # cloud wins
        assert rows[0]["num"] == "1"
        assert rows[0]["score"] == "4.0/5"
        assert renames == []

    def test_local_only_row_preserved_and_renumbered(self):
        cloud = _tracker(_row(1, "Acme", "Eng"), _row(2, "Beta", "Dev"))
        local = _tracker(_row(1, "Acme", "Eng"), _row(5, "Zeta", "Ops", report="99"))
        merged, renames = data.reconcile_trackers(cloud, local, {"1", "2"})
        by = _by_company(merged)
        assert set(by) == {"Acme", "Beta", "Zeta"}
        assert by["Zeta"]["num"] == "3"          # cloud max row # (2) + 1
        assert by["Zeta"]["report_num"] == "99"  # 99 not in cloud reports -> kept
        assert renames == []

    def test_identity_match_is_case_and_punct_insensitive(self):
        cloud = _tracker(_row(1, "Acme Inc.", "Senior  Engineer"))
        local = _tracker(_row(7, " acme  inc ", "senior engineer"))
        merged, renames = data.reconcile_trackers(cloud, local, {"1"})
        rows = data.parse_applications_text(merged)
        assert len(rows) == 1            # local deduped against cloud
        assert rows[0]["num"] == "1"

    def test_local_only_report_collision_renumbered(self):
        cloud = _tracker(_row(1, "Acme", "Eng", report="10"),
                         _row(2, "Beta", "Dev", report="20"))
        local = _tracker(_row(1, "Acme", "Eng", report="10"),
                         _row(3, "Zeta", "Ops", report="10"))  # report 10 collides
        merged, renames = data.reconcile_trackers(cloud, local, {"10", "20"})
        assert ("10", "21") in renames               # max(10,20)+1
        by = _by_company(merged)
        assert by["Zeta"]["report_num"] == "21"
        assert by["Zeta"]["report_path"].startswith("reports/21-")
        assert by["Zeta"]["num"] == "3"              # cloud max row # (2) + 1

    def test_cloud_row_reserves_its_number_without_the_file(self):
        """The daily artifact is a per-run delta (issue #129), so a cloud report
        from a Refresh the user missed — artifacts expire after a week — arrives
        as a tracker row with no file beside it. Its number is still spoken for:
        leaving it to a local-only row would point the cloud row's report link at
        the local report."""
        cloud = _tracker(_row(1, "Acme", "Eng", report="10"),
                         _row(2, "Beta", "Dev", report="11"))
        local = _tracker(_row(3, "Zeta", "Ops", report="11"))
        # Only report 10 rode along in today's artifact; 11 is older.
        merged, renames = data.reconcile_trackers(cloud, local, {"10"})
        assert ("11", "12") in renames
        assert _by_company(merged)["Zeta"]["report_num"] == "12"

    def test_local_only_report_no_collision_kept(self):
        cloud = _tracker(_row(1, "Acme", "Eng", report="10"))
        local = _tracker(_row(2, "Zeta", "Ops", report="55"))
        merged, renames = data.reconcile_trackers(cloud, local, {"10"})
        assert renames == []
        assert _by_company(merged)["Zeta"]["report_num"] == "55"

    def test_multiple_local_only_sequential_and_collision_free(self):
        cloud = _tracker(_row(1, "Acme", "Eng", report="10"))
        local = _tracker(_row(8, "Zeta", "Ops", report="10"),    # collides with cloud 10
                         _row(9, "Yota", "Sec", report="50"))     # no collision -> kept
        merged, renames = data.reconcile_trackers(cloud, local, {"10"})
        by = _by_company(merged)
        # rows renumbered after cloud max row # (1), stable order
        assert by["Zeta"]["num"] == "2"
        assert by["Yota"]["num"] == "3"
        # Yota keeps 50; Zeta's collision -> max(used incl. kept 50)+1 = 51
        assert by["Yota"]["report_num"] == "50"
        assert by["Zeta"]["report_num"] == "51"
        assert ("10", "51") in renames


class TestSyncPulledTracker:
    """sync_pulled_tracker merges a downloaded artifact into the durable local
    career-ops: merged applications.md, cloud reports + pipeline.md copied in,
    local-only reports renamed before the copy so a collision can't clobber them."""

    def _setup(self, tmp_path, cloud_apps, local_apps, cloud_reports, local_reports,
               cloud_pipeline="cloud pipeline"):
        art = tmp_path / "artifact"
        (art / "data").mkdir(parents=True)
        (art / "reports").mkdir()
        (art / "data" / "applications.md").write_text(cloud_apps, encoding="utf-8")
        (art / "data" / "pipeline.md").write_text(cloud_pipeline, encoding="utf-8")
        for n, content in cloud_reports.items():
            (art / "reports" / f"{n}-x.md").write_text(content, encoding="utf-8")
        loc = tmp_path / "career-ops"
        (loc / "data").mkdir(parents=True)
        (loc / "reports").mkdir()
        (loc / "data" / "applications.md").write_text(local_apps, encoding="utf-8")
        for n, content in local_reports.items():
            (loc / "reports" / f"{n}-x.md").write_text(content, encoding="utf-8")
        return art, loc

    def test_merges_and_preserves_local_only_with_report_rename(self, tmp_path):
        cloud = _tracker(_row(1, "Acme", "Eng", report="1"), _row(2, "Beta", "Dev", report="2"))
        local = _tracker(_row(1, "Acme", "Eng", report="1"),
                         _row(7, "Zeta", "Ops", report="2"))  # local-only, report 2 collides
        art, loc = self._setup(
            tmp_path, cloud, local,
            cloud_reports={"1": "cloudA", "2": "cloudB"},
            local_reports={"1": "locA", "2": "ZETA-REPORT"},
        )
        data.sync_pulled_tracker(art, loc)

        by = {r["company"]: r for r in data.parse_applications(loc / "data" / "applications.md")}
        assert set(by) == {"Acme", "Beta", "Zeta"}
        # Zeta's report renamed (2 -> 3) and its content preserved (not clobbered by cloud's 2).
        assert by["Zeta"]["report_num"] == "3"
        assert (loc / "reports" / "3-x.md").read_text(encoding="utf-8") == "ZETA-REPORT"
        # Cloud reports synced into local (canonical for shared numbers).
        assert (loc / "reports" / "1-x.md").read_text(encoding="utf-8") == "cloudA"
        assert (loc / "reports" / "2-x.md").read_text(encoding="utf-8") == "cloudB"
        # pipeline.md synced from cloud.
        assert (loc / "data" / "pipeline.md").read_text(encoding="utf-8") == "cloud pipeline"

    def test_rename_moves_only_the_local_only_row_s_report(self, tmp_path):
        """A report number can name two files in a local reports dir: the cloud
        report a past Refresh copied in, and a local-only one that happens to
        share the number. Renaming by number prefix alone moves both.

        That was invisible while the artifact carried every cloud report — step 3
        copied the cloud one straight back. The artifact is now a per-run delta
        (issue #129), so an older cloud report is not re-copied, and moving it
        would leave its tracker row pointing at nothing. Slugs differ here
        because that is what makes the two files distinguishable at all."""
        cloud = _tracker(_row(1, "Acme", "Eng", report="1"),
                         _row(2, "Globex", "Dev", report="2"))
        # Spelled out rather than via _row(), which hardcodes the `-x` slug: the
        # whole point is that Zeta's file is a DIFFERENT file from cloud report 2.
        zeta = ("| 7 | 2026-06-20 | Zeta | Ops | 3.0/5 | Evaluated | null "
                "| [2](reports/2-zeta.md) | x |\n")
        local = _tracker(_row(1, "Acme", "Eng", report="1"), zeta)
        art = tmp_path / "artifact"
        (art / "data").mkdir(parents=True)
        (art / "reports").mkdir()
        (art / "data" / "applications.md").write_text(cloud, encoding="utf-8")
        # Today's delta carries no report at all — both cloud reports are older.
        loc = tmp_path / "career-ops"
        (loc / "data").mkdir(parents=True)
        (loc / "reports").mkdir()
        (loc / "data" / "applications.md").write_text(local, encoding="utf-8")
        for name, body in (("1-x.md", "cloudA"), ("2-x.md", "GLOBEX-CLOUD"),
                           ("2-zeta.md", "ZETA-LOCAL")):
            (loc / "reports" / name).write_text(body, encoding="utf-8")

        data.sync_pulled_tracker(art, loc)

        by = {r["company"]: r for r in data.parse_applications(loc / "data" / "applications.md")}
        # The local-only row moved; the cloud row's file stayed where its link says.
        assert by["Zeta"]["report_num"] == "3"
        assert (loc / "reports" / "3-zeta.md").read_text(encoding="utf-8") == "ZETA-LOCAL"
        assert by["Globex"]["report_path"] == "reports/2-x.md"
        assert (loc / "reports" / "2-x.md").read_text(encoding="utf-8") == "GLOBEX-CLOUD"
        # Every row the merged tracker holds resolves to a file that exists.
        for row in by.values():
            assert (loc / row["report_path"]).exists(), row

    def test_first_pull_into_empty_local(self, tmp_path):
        cloud = _tracker(_row(1, "Acme", "Eng", report="1"))
        art, loc = self._setup(tmp_path, cloud, "", cloud_reports={"1": "cloudA"}, local_reports={})
        data.sync_pulled_tracker(art, loc)
        rows = data.parse_applications(loc / "data" / "applications.md")
        assert [r["company"] for r in rows] == ["Acme"]
        assert (loc / "reports" / "1-x.md").read_text(encoding="utf-8") == "cloudA"

    def test_empty_cloud_tracker_leaves_local_intact(self, tmp_path):
        # A successful artifact can be reports-only (no data/applications.md). That
        # must NOT be treated as "cloud has zero rows" — merging would renumber
        # every local row and write a header-less file over the durable tracker.
        local = _tracker(_row(1, "Acme", "Eng", report="1"))
        art = tmp_path / "artifact"
        (art / "reports").mkdir(parents=True)        # NOTE: no data/applications.md
        (art / "reports" / "9-x.md").write_text("cloud orphan", encoding="utf-8")
        loc = tmp_path / "career-ops"
        (loc / "data").mkdir(parents=True)
        (loc / "reports").mkdir()
        (loc / "data" / "applications.md").write_text(local, encoding="utf-8")
        (loc / "reports" / "1-x.md").write_text("locA", encoding="utf-8")
        before = (loc / "data" / "applications.md").read_text(encoding="utf-8")

        data.sync_pulled_tracker(art, loc)

        assert (loc / "data" / "applications.md").read_text(encoding="utf-8") == before

    def test_renumber_avoids_existing_local_report_file(self, tmp_path):
        # Acme is shared but its LOCAL report file is number 2 (orphaned after the
        # merge picks cloud's Acme). Zeta is local-only with report 1, which
        # collides with cloud's report 1. The renumber must avoid BOTH cloud's
        # reports AND Acme's existing local file 2 — else the rename clobbers it.
        cloud = _tracker(_row(1, "Acme", "Eng", report="1"))
        local = _tracker(_row(1, "Acme", "Eng", report="2"),
                         _row(5, "Zeta", "Ops", report="1"))
        art, loc = self._setup(
            tmp_path, cloud, local,
            cloud_reports={"1": "cloudA"},
            local_reports={"2": "acme-local", "1": "ZETA"},
        )
        data.sync_pulled_tracker(art, loc)

        by = {r["company"]: r for r in data.parse_applications(loc / "data" / "applications.md")}
        assert by["Zeta"]["report_num"] == "3"                       # avoids cloud 1 AND local 2
        assert (loc / "reports" / "3-x.md").read_text(encoding="utf-8") == "ZETA"
        assert (loc / "reports" / "2-x.md").read_text(encoding="utf-8") == "acme-local"  # not clobbered
        assert (loc / "reports" / "1-x.md").read_text(encoding="utf-8") == "cloudA"      # cloud copied in
