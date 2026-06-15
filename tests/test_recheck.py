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


# A tracker covering every selection branch. Liveness re-check is verifiable-site
# only — LinkedIn via the guest endpoint; Indeed/Glassdoor have no liveness path
# (anti-bot walls), so they're counted `unverifiable` and never fetched.
#   1 Evaluated + LinkedIn /jobs/view URL   -> rechecked (via guest endpoint)
#   2 Evaluated + LinkedIn URL              -> rechecked
#   3 Applied  + LinkedIn URL               -> NOT rechecked (acted on)
#   4 Evaluated + no URL in notes           -> skipped (nothing to fetch)
#   5 Rejected                              -> NOT rechecked
#   6 Evaluated + LinkedIn URL, score 1.0/5 -> rechecked (score-agnostic)
#   7 Discarded                             -> NOT rechecked
#   8 Evaluated + Indeed URL                -> unverifiable (skipped, counted)
#   9 Evaluated + Glassdoor URL             -> unverifiable (skipped, counted)
_TRACKER = (
    "# Applications Tracker\n\n"
    "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
    "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
    "| 1 | 2026-06-01 | Acme | Engineer | 4.5/5 | Evaluated | ❌ | [001](reports/001.md) | "
    "https://www.linkedin.com/jobs/view/111 — strong fit |\n"
    "| 2 | 2026-06-01 | Globex | Backend Dev | 4.2/5 | Evaluated | ❌ | [002](reports/002.md) | "
    "https://www.linkedin.com/jobs/view/222 — solid |\n"
    "| 3 | 2026-06-01 | Initech | TPS Eng | 4.0/5 | Applied | ❌ | [003](reports/003.md) | "
    "https://www.linkedin.com/jobs/view/333 — already applied |\n"
    "| 4 | 2026-06-01 | Umbrella | Dev | 4.1/5 | Evaluated | ❌ | [004](reports/004.md) | "
    "no link captured for this one |\n"
    "| 5 | 2026-06-01 | Soylent | SRE | 3.0/5 | Rejected | ❌ | [005](reports/005.md) | "
    "https://www.indeed.com/viewjob?jk=555 — passed |\n"
    "| 6 | 2026-06-01 | Vandelay | Importer | 1.0/5 | Evaluated | ❌ | [006](reports/006.md) | "
    "https://www.linkedin.com/jobs/view/666 — low fit |\n"
    "| 7 | 2026-06-01 | Hooli | Eng | 4.4/5 | Discarded | ❌ | [007](reports/007.md) | "
    "https://www.linkedin.com/jobs/view/777 — gone |\n"
    "| 8 | 2026-06-01 | Globo Gym | Trainer | 4.3/5 | Evaluated | ❌ | [008](reports/008.md) | "
    "https://www.indeed.com/viewjob?jk=888 — indeed |\n"
    "| 9 | 2026-06-01 | Wonka | Taster | 4.0/5 | Evaluated | ❌ | [009](reports/009.md) | "
    "https://www.glassdoor.com/job-listing/999 — glassdoor |\n"
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
        # 1, 2, 6 are Evaluated + verifiable (LinkedIn) URL. 3/5/7 wrong status;
        # 4 has no URL; 8/9 are Evaluated but unverifiable (Indeed/Glassdoor).
        assert nums == {"1", "2", "6"}

    def test_is_not_score_gated(self, tracker):
        """Row 6 scores 1.0/5 — apply's queue would skip it, recheck must not
        (a low-scoring role can still go stale and should leave the queue)."""
        _, apps = tracker
        assert "6" in {j.num for j in recheck.select_evaluated(apps)}

    def test_excludes_unverifiable_sites(self, tracker):
        """Indeed (8) and Glassdoor (9) have no liveness path (anti-bot walls),
        so they're NOT selected for re-check — run() counts them `unverifiable`
        instead of fetching a page it can't classify."""
        _, apps = tracker
        nums = {j.num for j in recheck.select_evaluated(apps)}
        assert "8" not in nums and "9" not in nums

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

    def test_unverifiable_sites_are_not_fetched(self, tracker, fake_fetch):
        """An unverifiable site (Indeed row 8, Glassdoor row 9) is never fetched —
        no HTTP request is made for it (it can't be classified, so we skip it)."""
        co, apps = tracker
        recheck.run(co, applications_md=apps)
        assert not any("indeed.com" in u or "glassdoor.com" in u for u in fake_fetch["urls"])
        assert "888" not in "".join(fake_fetch["urls"])   # row 8 not fetched
        assert "999" not in "".join(fake_fetch["urls"])   # row 9 not fetched


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
                           "unconfirmed": 0, "throttled": 0, "deferred": 0,
                           "unverifiable": 0}
        assert fake_fetch["urls"] == []

    def test_unverifiable_sites_counted_not_fetched(self, tracker, fake_fetch):
        """Evaluated roles on unverifiable sites (Indeed 8, Glassdoor 9) are
        counted in `unverifiable` and never fetched; only the 3 LinkedIn roles
        (1, 2, 6) are checked."""
        co, apps = tracker
        summary = recheck.run(co, applications_md=apps)
        assert summary["unverifiable"] == 2
        assert summary["checked"] == 3


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
            "https://www.linkedin.com/jobs/view/222": now - dt.timedelta(hours=10),   # row 2, stale
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
            "https://www.linkedin.com/jobs/view/222": now - dt.timedelta(hours=30),   # row 2 (stalest seen)
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
        assert "https://www.linkedin.com/jobs/view/666" in state
        assert "https://www.linkedin.com/jobs/view/222" not in state   # throttled → unstamped

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

    def test_deferred_counts_only_over_budget_not_minage(self, tracker, fake_fetch, tmp_path):
        """`deferred` = eligible roles skipped by BUDGET (genuinely due a later
        run), NOT roles skipped because they were recently checked. This is what
        makes a fully-drained backlog report deferred=0 (the final sweep's
        eligible pool fits the budget) instead of a bogus large count."""
        co, apps = tracker
        sp = tmp_path / "recheck-state.tsv"
        # Row 1 checked 1h ago (within the 6h window → ineligible, not "deferred").
        fresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        recheck._save_state(sp, {"https://www.linkedin.com/jobs/view/111": fresh})
        # Eligible now: rows 2 & 6 (unseen). budget 1 → check 1, defer the other 1.
        summary = recheck.run(co, applications_md=apps, state_path=sp, budget=1, min_age_hours=6)
        assert summary["checked"] == 1
        assert summary["deferred"] == 1     # the over-budget eligible only — NOT row 1


class TestRecheckDrain:
    """drain() loops budgeted run() sweeps (cooldown between them) until a sweep
    no longer fills its budget — i.e. the eligible backlog is covered — so a
    backlog larger than one budget is fully gone through without one
    rate-limit-tripping burst. Discards aggregate across cycles; the per-run
    state stamping (run's job) is what lets each cycle advance to fresh roles."""

    @staticmethod
    def _summary(checked, *, discarded=0, dead=None, throttled=0, unconfirmed=0,
                 deferred=0, unverifiable=0):
        return {"checked": checked, "discarded": discarded, "dead": dead or [],
                "unconfirmed": unconfirmed, "throttled": throttled,
                "deferred": deferred, "skipped": 0, "unverifiable": unverifiable}

    def test_loops_until_a_sweep_underfills_budget(self, monkeypatch):
        seq = iter([
            self._summary(100, discarded=2, dead=[{"num": "1"}, {"num": "2"}]),
            self._summary(100, discarded=1, dead=[{"num": "3"}]),
            self._summary(40, throttled=1, unconfirmed=2),   # < budget → backlog covered
        ])
        calls = []

        def fake_run(co, **kw):
            calls.append(kw)
            return next(seq)
        monkeypatch.setattr(recheck, "run", fake_run)
        sleeps = []
        monkeypatch.setattr(recheck.time, "sleep", lambda s: sleeps.append(s))

        agg = recheck.drain(object(), budget=100, cooldown=5, max_cycles=10)
        assert len(calls) == 3            # stopped after the 40<100 sweep
        assert sleeps == [5, 5]           # cooldown BETWEEN cycles, none after the last
        assert agg["cycles"] == 3
        assert agg["checked"] == 240      # cumulative work done
        assert agg["discarded"] == 3
        assert [d["num"] for d in agg["dead"]] == ["1", "2", "3"]
        assert agg["throttled"] == 1 and agg["unconfirmed"] == 2   # last cycle's remaining
        # run() was called with drain's budget each cycle
        assert all(kw.get("budget") == 100 for kw in calls)

    def test_single_sweep_when_backlog_fits_budget(self, monkeypatch):
        monkeypatch.setattr(recheck, "run",
                            lambda co, **kw: self._summary(30, discarded=1, dead=[{"num": "9"}]))
        sleeps = []
        monkeypatch.setattr(recheck.time, "sleep", lambda s: sleeps.append(s))
        agg = recheck.drain(object(), budget=100, cooldown=5, max_cycles=10)
        assert agg["cycles"] == 1
        assert sleeps == []               # one-shot: no cooldown
        assert agg["discarded"] == 1

    def test_respects_max_cycles_under_persistent_throttle(self, monkeypatch):
        # Every sweep fills its budget (heavy throttling never drains it) → the
        # loop is bounded by max_cycles, with no sleep after the final cycle.
        monkeypatch.setattr(recheck, "run", lambda co, **kw: self._summary(100, throttled=100))
        sleeps = []
        monkeypatch.setattr(recheck.time, "sleep", lambda s: sleeps.append(s))
        agg = recheck.drain(object(), budget=100, cooldown=5, max_cycles=4)
        assert agg["cycles"] == 4
        assert sleeps == [5, 5, 5]        # 3 cooldowns across 4 cycles
        assert agg["checked"] == 400

    def test_forwards_run_kwargs(self, monkeypatch):
        """timeout/dry_run etc. flow through to each sweep."""
        seen = []
        monkeypatch.setattr(recheck, "run",
                            lambda co, **kw: seen.append(kw) or self._summary(0))
        monkeypatch.setattr(recheck.time, "sleep", lambda *_: None)
        recheck.drain(object(), budget=50, cooldown=1, max_cycles=3, timeout=20)
        assert seen[0].get("timeout") == 20

    def test_sums_unconfirmed_across_cycles(self, monkeypatch):
        """`unconfirmed` is cumulative — a role read-but-ambiguous in any cycle is
        stamped and not re-checked, so 'couldn't be reached' must total the whole
        drain, not just the final cycle (which would undercount badly)."""
        seq = iter([self._summary(100, unconfirmed=3),
                    self._summary(40, unconfirmed=2)])   # underfill → stop
        monkeypatch.setattr(recheck, "run", lambda co, **kw: next(seq))
        monkeypatch.setattr(recheck.time, "sleep", lambda *_: None)
        agg = recheck.drain(object(), budget=100, cooldown=5, max_cycles=10)
        assert agg["unconfirmed"] == 5     # 3 + 2, not just the last cycle's 2
        assert agg["deferred"] == 0        # last cycle underfilled → none still due
