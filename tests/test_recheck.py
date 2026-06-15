"""Tests for the tracker liveness re-check stage (pipeline/recheck.py).

The pipeline already checks liveness at SCRAPE time (screen.py) for brand-new
postings. Nothing re-checks a role once it's sitting in the tracker as
`Evaluated`, so a posting that closes days after evaluation lingers in the
active queue until someone tries to apply. This stage closes that gap: it
re-fetches every Evaluated role and marks the ones that are demonstrably gone
`Discarded`.

The HTTP layer (screen.fetch_and_classify) is monkeypatched — the same
convention test_screen.py uses; we exercise the selection + classify->mark
logic, not real network calls.

Design under test:
- select_evaluated(applications_md) -> [RecheckJob]: every row whose canonical
  status is Evaluated AND that has a URL in notes. NOT score-gated and NOT
  LinkedIn-only (liveness applies to every site, unlike apply's queue.select).
- run(career_ops, *, applications_md=None, timeout=8, concurrency=8,
      dry_run=False, progress=None) -> dict: re-check each role; mark ONLY
  `expired` ones Discarded (cell edit + identity-anchored override). `active`
  and `uncertain` are left Evaluated — an ambiguous/transient fetch must never
  discard a live role. Returns {checked, discarded, dead, skipped, errors};
  calls progress(checked, total, discarded) once per role.
"""

import datetime as dt
import json

import pytest

from pipeline import recheck, screen
from pipeline.app import data as app_data


# A tracker covering every selection branch:
#   1 Evaluated + LinkedIn /jobs/view URL      -> rechecked (via guest endpoint)
#   2 Evaluated + Indeed URL                    -> rechecked (original URL)
#   3 Applied  + LinkedIn URL                   -> NOT rechecked (acted on)
#   4 Evaluated + no URL in notes               -> skipped (nothing to fetch)
#   5 Rejected                                  -> NOT rechecked
#   6 Evaluated + Glassdoor URL, score 1.0/5    -> rechecked (score-agnostic)
#   7 Discarded                                 -> NOT rechecked
_TRACKER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-06-01 | Acme | Engineer | 4.5/5 | Evaluated | ❌ | [001](reports/001.md) | "
    "https://www.linkedin.com/jobs/view/111 — strong fit |\n"
    "| 2 | 2026-06-01 | Globex | Backend Dev | 4.2/5 | Evaluated | ❌ | [002](reports/002.md) | "
    "https://www.indeed.com/viewjob?jk=222 — solid |\n"
    "| 3 | 2026-06-01 | Initech | TPS Eng | 4.0/5 | Applied | ❌ | [003](reports/003.md) | "
    "https://www.linkedin.com/jobs/view/333 — already applied |\n"
    "| 4 | 2026-06-01 | Umbrella | Dev | 4.1/5 | Evaluated | ❌ | [004](reports/004.md) | "
    "no link captured for this one |\n"
    "| 5 | 2026-06-01 | Soylent | SRE | 3.0/5 | Rejected | ❌ | [005](reports/005.md) | "
    "https://www.indeed.com/viewjob?jk=555 — passed |\n"
    "| 6 | 2026-06-01 | Vandelay | Importer | 1.0/5 | Evaluated | ❌ | [006](reports/006.md) | "
    "https://www.glassdoor.com/job-listing/666 — low fit |\n"
    "| 7 | 2026-06-01 | Hooli | Eng | 4.4/5 | Discarded | ❌ | [007](reports/007.md) | "
    "https://www.linkedin.com/jobs/view/777 — gone |\n"
)


@pytest.fixture
def tracker(tmp_path):
    """A career-ops dir holding the sample applications.md; returns (career_ops, apps_md)."""
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    apps = co / "data" / "applications.md"
    apps.write_text(_TRACKER, encoding="utf-8")
    return co, apps


@pytest.fixture
def fake_fetch(monkeypatch):
    """Stub screen.fetch_and_classify with a per-URL result map and a call log.

    `results` maps a substring of the fetched URL -> (liveness, reason); the
    default is ('active', ...). `urls` records every URL actually fetched so a
    test can assert LinkedIn went through the guest endpoint."""
    state = {"results": {}, "urls": []}

    def _fetch(url, timeout=8):
        state["urls"].append(url)
        for needle, outcome in state["results"].items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                liveness, reason = outcome
                return liveness, reason, "<html/>"
        return "active", "apply control visible", "<html/>"

    monkeypatch.setattr(screen, "fetch_and_classify", _fetch)
    monkeypatch.setattr(screen.time, "sleep", lambda *_: None)  # don't sleep on throttle retries
    return state


def _status_cell(apps_md, num):
    """The Status cell text for tracker row `num` (e.g. 'Evaluated')."""
    for row in app_data.parse_applications(apps_md):
        if row.get("num") == str(num):
            return row.get("status_canonical")
    return None


def _overrides():
    f = app_data.STATUS_OVERRIDES_FILE
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


# ── select_evaluated ─────────────────────────────────────────────────────────

class TestSelectEvaluated:
    def test_selects_only_evaluated_rows_with_urls(self, tracker):
        _, apps = tracker
        nums = {j.num for j in recheck.select_evaluated(apps)}
        # 1, 2, 6 are Evaluated + have a URL. 3/5/7 wrong status; 4 has no URL.
        assert nums == {"1", "2", "6"}

    def test_is_not_score_gated(self, tracker):
        """Row 6 scores 1.0/5 — apply's queue would skip it, recheck must not
        (a low-scoring role can still go stale and should leave the queue)."""
        _, apps = tracker
        assert "6" in {j.num for j in recheck.select_evaluated(apps)}

    def test_includes_non_linkedin_sites(self, tracker):
        """Indeed (2) and Glassdoor (6) are in scope — liveness isn't
        LinkedIn-only the way the Easy Apply fast-path is."""
        _, apps = tracker
        jobs = {j.num: j for j in recheck.select_evaluated(apps)}
        assert "indeed.com" in jobs["2"].url
        assert "glassdoor.com" in jobs["6"].url

    def test_carries_identity(self, tracker):
        _, apps = tracker
        j = {x.num: x for x in recheck.select_evaluated(apps)}["1"]
        assert (j.company, j.role) == ("Acme", "Engineer")
        assert j.url == "https://www.linkedin.com/jobs/view/111"


# ── run: classification -> marking ───────────────────────────────────────────

class TestRecheckMarking:
    def test_expired_role_marked_discarded(self, tracker, fake_fetch):
        co, apps = tracker
        fake_fetch["results"] = {"111": ("expired", "HTTP 404")}
        summary = recheck.run(co, applications_md=apps)
        assert _status_cell(apps, "1") == "Discarded"
        assert summary["discarded"] == 1
        assert [d["num"] for d in summary["dead"]] == ["1"]

    def test_active_role_left_evaluated(self, tracker, fake_fetch):
        co, apps = tracker
        # all default to 'active'
        summary = recheck.run(co, applications_md=apps)
        assert _status_cell(apps, "1") == "Evaluated"
        assert _status_cell(apps, "2") == "Evaluated"
        assert summary["discarded"] == 0
        assert _overrides() == {}

    def test_uncertain_does_not_discard(self, tracker, fake_fetch):
        """The critical safety invariant: an 'uncertain' result (network blip,
        login wall, ambiguous body) must NOT discard a role we can't confirm
        dead — only a definitive 'expired' does."""
        co, apps = tracker
        fake_fetch["results"] = {"111": ("uncertain", "content present, no apply control")}
        summary = recheck.run(co, applications_md=apps)
        assert _status_cell(apps, "1") == "Evaluated"
        assert summary["discarded"] == 0
        assert summary["unconfirmed"] == 1   # row 1; rows 2 & 6 stay active
        assert "1" not in _overrides()

    def test_marks_via_identity_override(self, tracker, fake_fetch):
        """Discard writes the same identity-anchored override apply uses, so a
        later cloud Push targets the right company/role, not just a num that the
        cloud tracker may have assigned to a different row."""
        co, apps = tracker
        fake_fetch["results"] = {"666": ("expired", "body: applications? closed")}
        recheck.run(co, applications_md=apps)
        assert _overrides()["6"] == {
            "status": "Discarded", "company": "Vandelay", "role": "Importer",
        }

    def test_only_expired_among_mixed_results(self, tracker, fake_fetch):
        co, apps = tracker
        fake_fetch["results"] = {
            "111": ("expired", "HTTP 410"),     # row 1 -> Discarded
            "222": ("active", "apply visible"), # row 2 -> stays
            "666": ("uncertain", "blip"),       # row 6 -> stays
        }
        summary = recheck.run(co, applications_md=apps)
        assert _status_cell(apps, "1") == "Discarded"
        assert _status_cell(apps, "2") == "Evaluated"
        assert _status_cell(apps, "6") == "Evaluated"
        assert summary["checked"] == 3 and summary["discarded"] == 1
        assert summary["skipped"] == 1   # row 4 (Evaluated, no URL)


# ── run: fetch routing ───────────────────────────────────────────────────────

class TestFetchRouting:
    def test_linkedin_uses_guest_endpoint(self, tracker, fake_fetch):
        """LinkedIn /jobs/view/ is login-walled from datacenter IPs; recheck must
        fetch the public guest job-posting endpoint like screen.py does."""
        co, apps = tracker
        recheck.run(co, applications_md=apps)
        guest = screen.linkedin_guest_jd_url("https://www.linkedin.com/jobs/view/111")
        assert guest in fake_fetch["urls"]
        assert "https://www.linkedin.com/jobs/view/111" not in fake_fetch["urls"]

    def test_non_linkedin_fetches_original_url(self, tracker, fake_fetch):
        co, apps = tracker
        recheck.run(co, applications_md=apps)
        assert "https://www.indeed.com/viewjob?jk=222" in fake_fetch["urls"]


# ── run: summary, progress, dry-run, resilience ──────────────────────────────

class TestRunReporting:
    def test_summary_counts(self, tracker, fake_fetch):
        co, apps = tracker
        fake_fetch["results"] = {"111": ("expired", "gone"), "666": ("expired", "gone")}
        summary = recheck.run(co, applications_md=apps)
        assert summary["checked"] == 3      # rows 1, 2, 6
        assert summary["skipped"] == 1      # row 4
        assert summary["discarded"] == 2    # rows 1, 6
        assert summary["unconfirmed"] == 0
        assert {d["num"] for d in summary["dead"]} == {"1", "6"}

    def test_progress_callback_invoked_per_role(self, tracker, fake_fetch):
        co, apps = tracker
        seen = []
        recheck.run(co, applications_md=apps, progress=lambda c, t, d: seen.append((c, t, d)))
        # One call per checked role; checked count climbs 1..N; total is constant.
        assert len(seen) == 3
        assert {c for c, _, _ in seen} == {1, 2, 3}
        assert {t for _, t, _ in seen} == {3}

    def test_dry_run_detects_but_does_not_mutate(self, tracker, fake_fetch):
        co, apps = tracker
        before = apps.read_text(encoding="utf-8")
        fake_fetch["results"] = {"111": ("expired", "HTTP 404")}
        summary = recheck.run(co, applications_md=apps, dry_run=True)
        # The dead role is reported, but nothing is written.
        assert [d["num"] for d in summary["dead"]] == ["1"]
        assert summary["discarded"] == 0
        assert apps.read_text(encoding="utf-8") == before
        assert _overrides() == {}

    def test_one_failing_fetch_does_not_abort_sweep(self, tracker, fake_fetch):
        """A raised error on one role is caught and treated as uncertain (left
        Evaluated, counted unconfirmed) and must not stop the others."""
        co, apps = tracker
        fake_fetch["results"] = {
            "111": RuntimeError("boom"),     # row 1 raises
            "666": ("expired", "gone"),      # row 6 still gets discarded
        }
        summary = recheck.run(co, applications_md=apps)
        assert _status_cell(apps, "1") == "Evaluated"   # not discarded on error
        assert _status_cell(apps, "6") == "Discarded"
        assert summary["unconfirmed"] == 1
        assert summary["discarded"] == 1

    def test_no_evaluated_rows_is_noop(self, tmp_path, fake_fetch):
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        apps = co / "data" / "applications.md"
        apps.write_text(
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
            "| 1 | 2026-06-01 | Acme | Eng | 4.5/5 | Applied | ❌ | [1](reports/1.md) | "
            "https://www.linkedin.com/jobs/view/1 — done |\n",
            encoding="utf-8",
        )
        summary = recheck.run(co, applications_md=apps)
        assert summary == {"checked": 0, "discarded": 0, "dead": [], "skipped": 0,
                           "unconfirmed": 0, "throttled": 0, "deferred": 0}
        assert fake_fetch["urls"] == []


class TestRecheckState:
    """Per-role last-checked state + per-run budget: the recheck re-fetches only
    a budget of the least-recently-checked roles, and only a CONCLUSIVE
    (non-throttled) result stamps the timestamp — so a throttled/closed posting
    stays stale and is retried next run instead of being marked done. This caps
    the per-run request burst that trips LinkedIn's rate limiter."""

    def _now(self):
        return dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.timezone.utc)

    def test_skips_recently_checked_within_min_age(self, tracker):
        _, apps = tracker
        now = self._now()
        state = {
            "https://www.linkedin.com/jobs/view/111": now - dt.timedelta(hours=1),   # row 1, fresh
            "https://www.indeed.com/viewjob?jk=222": now - dt.timedelta(hours=10),    # row 2, stale
        }
        nums = {j.num for j in recheck.select_for_recheck(
            apps, state=state, now=now, min_age_hours=6, budget=100)}
        assert "1" not in nums                 # checked 1h ago (< 6h) → skipped
        assert {"2", "6"} <= nums              # 10h ago + unseen → included

    def test_budget_caps_and_prefers_stalest(self, tracker):
        _, apps = tracker
        now = self._now()
        state = {
            "https://www.linkedin.com/jobs/view/111": now - dt.timedelta(hours=20),  # row 1
            "https://www.indeed.com/viewjob?jk=222": now - dt.timedelta(hours=30),    # row 2 (stalest seen)
            # row 6 unseen → treated as oldest
        }
        nums = [j.num for j in recheck.select_for_recheck(
            apps, state=state, now=now, min_age_hours=6, budget=2)]
        assert len(nums) == 2
        assert set(nums) == {"6", "2"}         # unseen(6) + stalest(2); row 1 dropped by budget

    def test_state_round_trip(self, tmp_path):
        now = self._now()
        p = tmp_path / "recheck-state.tsv"
        recheck._save_state(p, {"https://x/1": now})
        loaded = recheck._load_state(p)
        assert "https://x/1" in loaded
        assert abs((loaded["https://x/1"] - now).total_seconds()) < 1

    def test_run_stamps_conclusive_not_throttled(self, tracker, fake_fetch, tmp_path):
        co, apps = tracker
        state_path = tmp_path / "recheck-state.tsv"
        fake_fetch["results"] = {
            "111": ("active", "ok"),            # conclusive → stamp
            "222": ("throttled", "HTTP 403"),   # NOT conclusive → no stamp, retry next run
            "666": ("expired", "gone"),         # conclusive → stamp
        }
        recheck.run(co, applications_md=apps, state_path=state_path)
        state = recheck._load_state(state_path)
        assert "https://www.linkedin.com/jobs/view/111" in state
        assert "https://www.glassdoor.com/job-listing/666" in state
        assert "https://www.indeed.com/viewjob?jk=222" not in state   # throttled → unstamped

    def test_run_reports_throttled_count(self, tracker, fake_fetch):
        co, apps = tracker
        fake_fetch["results"] = {"111": ("throttled", "HTTP 429")}
        summary = recheck.run(co, applications_md=apps)
        assert summary["throttled"] == 1
        assert summary["discarded"] == 0          # a throttle is never a discard

    def test_save_prunes_urls_no_longer_evaluated(self, tracker, fake_fetch, tmp_path):
        """State entries for URLs that are no longer Evaluated roles (an old
        discard, or a role since marked Applied) are pruned on save instead of
        accumulating in the file forever."""
        co, apps = tracker
        state_path = tmp_path / "recheck-state.tsv"
        recheck._save_state(state_path, {"https://gone.example/stale": self._now()})
        recheck.run(co, applications_md=apps, state_path=state_path)
        state = recheck._load_state(state_path)
        assert "https://gone.example/stale" not in state            # pruned
        assert "https://www.linkedin.com/jobs/view/111" in state    # live role kept

    def test_empty_tracker_does_not_wipe_state(self, tmp_path, fake_fetch):
        """Pruning must be safe: a transiently empty/unreadable tracker (zero
        Evaluated rows) must NOT wipe the accumulated cadence state."""
        co = tmp_path / "career-ops"
        (co / "data").mkdir(parents=True)
        apps = co / "data" / "applications.md"
        apps.write_text(
            "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
            "|---|------|---------|------|-------|--------|-----|--------|-------|\n",
            encoding="utf-8",
        )
        state_path = tmp_path / "recheck-state.tsv"
        recheck._save_state(state_path, {"https://www.linkedin.com/jobs/view/keep": self._now()})
        recheck.run(co, applications_md=apps, state_path=state_path)
        assert "https://www.linkedin.com/jobs/view/keep" in recheck._load_state(state_path)

    def test_select_for_recheck_tolerates_naive_now(self, tracker):
        """The exported seam coerces a naive `now` to UTC rather than raising
        TypeError against the always-tz-aware stored timestamps."""
        _, apps = tracker
        aware = self._now()
        state = {"https://www.linkedin.com/jobs/view/111": aware - dt.timedelta(hours=1)}
        naive_now = dt.datetime(2026, 6, 15, 12, 0)   # no tzinfo
        nums = {j.num for j in recheck.select_for_recheck(
            apps, state=state, now=naive_now, min_age_hours=6, budget=100)}
        assert "1" not in nums           # checked 1h ago → still skipped
        assert {"2", "6"} <= nums        # unseen included (no crash)
