"""Tests for pipeline/gemini_limits.py — hardcoded Gemini free-tier limits + the
viability check that warns when a batch run will exceed the free daily cap.

Limits are from Google AI Studio's "Rate limits by model" (free tier); there's
no API to fetch them, so they're hardcoded. Only the text models usable for
resume evaluation/tailoring are included.
"""

import os

import pytest

from pipeline import gemini_limits as gl


@pytest.fixture(autouse=True)
def _no_stray_override_file(tmp_path, monkeypatch):
    """Point every test at a path that doesn't exist unless it says otherwise.

    effective_limits() reads config/gemini-limits.json, which a developer running
    this suite may genuinely have — and it would silently change the numbers the
    viability assertions below are written against. Tests that want an override
    set GEMINI_LIMITS_FILE themselves; this just guarantees the default isn't a
    real machine's file."""
    monkeypatch.setenv("GEMINI_LIMITS_FILE", str(tmp_path / "absent.json"))
    gl._override_cache.clear()
    yield
    gl._override_cache.clear()


def point_at(tmp_path, monkeypatch, text=None):
    """Point GEMINI_LIMITS_FILE at a file with `text` in it, or at one that
    doesn't exist. Every test that touches overrides needs the same three steps,
    and the cache clear is the one that is silent when forgotten."""
    f = tmp_path / "gemini-limits.json"
    if text is not None:
        f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("GEMINI_LIMITS_FILE", str(f))
    gl._override_cache.clear()
    return f


def fake_clock(advance=True):
    """A (state, monotonic, sleep) triple for the pacers.

    `advance=True` moves the clock forward by each sleep, which is what a real
    run does — use it when the assertion is about when calls actually land.
    `advance=False` leaves the clock still, so the sleeps recorded are the
    schedule the pacer computed rather than one the test drove into it."""
    state = {"t": 0.0, "sleeps": []}

    def sleep(s):
        state["sleeps"].append(s)
        if advance:
            state["t"] += s
    return state, (lambda: state["t"]), sleep


class TestTable:
    def test_known_models_and_values(self):
        t = gl.GEMINI_FREE_TIER_LIMITS
        assert t["gemini-2.5-flash"] == {"rpm": 5, "tpm": 250_000, "rpd": 20}
        assert t["gemini-2.5-flash-lite"]["rpm"] == 10
        assert t["gemini-3.5-flash"]["rpd"] == 20
        assert t["gemini-3.1-flash-lite"] == {"rpm": 15, "tpm": 250_000, "rpd": 500}
        # The Flash-Lite tier is the throughput one: 25x the RPD of plain Flash.
        assert t["gemini-3.5-flash-lite"] == {"rpm": 15, "tpm": 250_000, "rpd": 500}
        # Gemma: the high-RPD option, but a LOW 16K TPM — not the "unlimited"
        # this table claimed before the 2026-08-27 refresh.
        assert t["gemma-4-26b-a4b-it"] == {"rpm": 30, "tpm": 16_000, "rpd": 14_400}
        assert t["gemma-4-31b-it"]["rpd"] == 14_400
        # Models the free tier grants no quota at all are absent, not zero-valued.
        assert "gemini-2.5-pro" not in t


class TestViability:
    def test_flash_exceeded_suggests_gemma(self):
        v = gl.free_tier_viability("gemini-2.5-flash", 50)
        assert v["rpd"] == 20
        assert v["exceeds"] is True
        assert v["suggestion"] == "gemma-4-26b-a4b-it"

    def test_flash_within_cap(self):
        v = gl.free_tier_viability("gemini-2.5-flash", 10)
        assert v["exceeds"] is False
        assert "suggestion" not in v

    def test_gemma_exceeded_has_no_better_free_suggestion(self):
        v = gl.free_tier_viability("gemma-4-26b-a4b-it", 20_000)
        assert v["exceeds"] is True
        assert "suggestion" not in v          # already the best free option

    def test_gemma_within_cap(self):
        assert gl.free_tier_viability("gemma-4-26b-a4b-it", 2_000)["exceeds"] is False

    def test_tpm_bound_capacity_is_what_exceeds_compares_against(self):
        """#143: 10,000 jobs "fit" gemma's 14,400 RPD and do not fit the day.

        16,000 TPM against a nominal 8,000-token prompt plus its response is
        ~1.7 calls/minute, so a day of continuous running is ~2,425 evaluations.
        Answering with the RPD told the user the queue was fine and then 429'd
        them on a quota nothing had warned about."""
        v = gl.free_tier_viability("gemma-4-26b-a4b-it", 10_000)
        assert v["rpd"] == 14_400             # the published quota, unchanged
        assert v["capacity"] == 2_425         # what the token budget can pace
        assert v["exceeds"] is True

    def test_a_slow_request_rate_caps_the_day_too(self, tmp_path, monkeypatch):
        """Capacity is the lowest of all three limits, not RPD-versus-TPM.

        The baked table hides this — every row's RPM buys thousands a day — so it
        bites exactly the case the override file exists for: a user's own numbers
        reach combinations the table never had, and 1 RPM is 1,440 evaluations a
        day however large the quota printed next to it."""
        point_at(tmp_path, monkeypatch,
                 '{"slowpoke": {"rpm": 1, "tpm": null, "rpd": 100000}}')
        v = gl.free_tier_viability("slowpoke", 5_000)
        assert v["rpd"] == 100_000 and v["capacity"] == 1_440
        assert v["exceeds"] is True
        # ...and the message says which limit did it, rather than assuming TPM.
        assert "RPM-bound" in gl.format_free_tier_warning("slowpoke", 5_000)
        # The headline number must not be one it cannot deliver.
        assert gl.batch_recommendation() == "gemma-4-26b-a4b-it"

    def test_capacity_equals_rpd_when_tpm_is_slack(self):
        # Every Flash row: 250K TPM is ~37,000 evaluations/day, so RPD binds and
        # nothing about these models changes.
        v = gl.free_tier_viability("gemini-3.1-flash-lite", 10)
        assert v["capacity"] == v["rpd"] == 500
        assert v["exceeds"] is False

    def test_no_suggestion_for_an_identical_twin(self):
        # The two Gemma rows carry the same numbers, so "switch to the other
        # one" is advice that quotes the same capacity it just called too small.
        v = gl.free_tier_viability("gemma-4-31b-it", 10_000)
        assert v["exceeds"] is True
        assert "suggestion" not in v

    def test_unknown_model_returns_none(self):
        assert gl.free_tier_viability("claude-sonnet-4-6", 50) is None
        assert gl.free_tier_viability("gemma-4-26b-it", 50) is None   # the wrong ID isn't in the table

    def test_sums_failover_chain_rpd(self):
        # A chain fails over member-to-member, so its daily capacity is the SUM —
        # of the honest per-member capacities as much as of the quotas.
        v = gl.free_tier_viability("gemini-2.5-flash,gemma-4-26b-a4b-it", 20_000)
        assert v["rpd"] == 14_420                     # 20 + 14,400
        assert v["capacity"] == 2_445                 # 20 + 2,425
        assert v["exceeds"] is True
        assert "suggestion" not in v                  # the recommended model is already in the chain

    def test_chain_suggests_when_recommendation_absent(self):
        v = gl.free_tier_viability("gemini-2.5-flash,gemini-3.5-flash", 50)
        assert v["rpd"] == 40                          # 20 + 20
        assert v["exceeds"] is True
        assert v["suggestion"] == "gemma-4-26b-a4b-it"


class TestWarning:
    def test_warning_when_exceeded_names_model_rpd_and_suggestion(self):
        msg = gl.format_free_tier_warning("gemini-2.5-flash", 50)
        assert msg is not None
        assert "gemini-2.5-flash" in msg
        assert "20" in msg
        assert "gemma-4-26b-a4b-it" in msg

    def test_no_warning_within_cap(self):
        assert gl.format_free_tier_warning("gemini-2.5-flash", 10) is None

    def test_no_warning_for_unknown_model(self):
        assert gl.format_free_tier_warning("gpt-4o-mini", 9999) is None

    def test_tpm_bound_warning_quotes_capacity_and_says_why(self):
        msg = gl.format_free_tier_warning("gemma-4-26b-a4b-it", 10_000)
        assert "2,425" in msg                 # what it gets through
        assert "TPM-bound" in msg             # ...and which limit did that
        # The whole complaint in #143: the message must not advertise a number
        # the model cannot deliver. 14,400 may appear only as the thing being
        # corrected, never as the answer.
        assert "~14,400 evaluations/day" not in msg

    def test_suggestion_quotes_the_capacity_it_ranked_by(self):
        # Not the RPD: recommending gemma "(14,400/day)" is how a user ends up
        # with a queue five times the size of what arrives.
        msg = gl.format_free_tier_warning("gemini-2.5-flash", 50)
        assert "BATCH_MODEL=gemma-4-26b-a4b-it (~2,425/day)" in msg


class TestConforming:
    def test_conforming_enabled_reads_env(self, monkeypatch):
        for v in ("true", "1", "yes", "on", "TRUE"):
            monkeypatch.setenv("GEMINI_FREE_TIER", v)
            assert gl.conforming_enabled() is True
        for v in ("", "false", "0", "no"):
            monkeypatch.setenv("GEMINI_FREE_TIER", v)
            assert gl.conforming_enabled() is False
        monkeypatch.delenv("GEMINI_FREE_TIER", raising=False)
        assert gl.conforming_enabled() is False

    def test_rpd_cap_only_when_conforming_and_free_tier(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        assert gl.rpd_cap("gemini-2.5-flash") == 20
        assert gl.rpd_cap("gemma-4-26b-a4b-it") == 14_400
        assert gl.rpd_cap("gpt-4o-mini") is None          # not a free-tier model
        monkeypatch.setenv("GEMINI_FREE_TIER", "false")
        assert gl.rpd_cap("gemini-2.5-flash") is None     # conforming off

    def test_cap_to_rpd_slices_and_counts(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        items = list(range(50))
        kept, deferred = gl.cap_to_rpd(items, "gemini-2.5-flash")
        assert kept == list(range(20)) and deferred == 30
        # under the cap → unchanged
        assert gl.cap_to_rpd(list(range(5)), "gemini-2.5-flash") == (list(range(5)), 0)
        # conforming off → never caps
        monkeypatch.setenv("GEMINI_FREE_TIER", "false")
        assert gl.cap_to_rpd(items, "gemini-2.5-flash") == (items, 0)

    def test_cap_slices_on_the_quota_not_the_capacity(self, monkeypatch):
        """The deliberate divergence: the warning talks about capacity, the cap
        acts on RPD.

        10,000 jobs is four days of gemma's token budget, so the warning fires —
        but every one of them is inside the daily quota, and deferring work this
        run would have finished (slowly) to a next run that has no more tokens
        per minute than this one buys the user nothing."""
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        items = list(range(10_000))
        assert gl.cap_to_rpd(items, "gemma-4-26b-a4b-it") == (items, 0)
        assert gl.free_tier_viability("gemma-4-26b-a4b-it", 10_000)["exceeds"] is True

    def test_chain_aware_cap(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        # summed cap; whitespace tolerated; unknown members ignored
        assert gl.rpd_cap("gemini-2.5-flash,gemma-4-26b-a4b-it") == 14_420
        assert gl.rpd_cap("gemini-2.5-flash , gemini-3.5-flash") == 40
        assert gl.rpd_cap("gemini-2.5-flash,unknown-x") == 20
        assert gl.rpd_cap("unknown-a,unknown-b") is None
        items = list(range(50))
        assert gl.cap_to_rpd(items, "gemini-2.5-flash,gemini-3.5-flash") == (list(range(40)), 10)
        assert gl.cap_to_rpd(items, "gemini-2.5-flash,gemma-4-26b-a4b-it") == (items, 0)


class TestRateLimiter:
    def test_paces_to_rpm(self):
        st, mono, sleep = fake_clock()
        rl = gl.RateLimiter(60, monotonic=mono, sleep=sleep)   # 1 req/sec
        for _ in range(4):
            rl.acquire()
        assert st["sleeps"] == [1.0, 1.0, 1.0]   # first immediate, then ~1s apart

    def test_first_acquire_does_not_sleep(self):
        sleeps = []
        rl = gl.RateLimiter(5, monotonic=lambda: 0.0, sleep=sleeps.append)
        rl.acquire()
        assert sleeps == []


class TestEstimateTokens:
    def test_rounds_up_from_characters(self):
        assert gl.estimate_tokens("") == 0
        assert gl.estimate_tokens("a") == 1          # rounds up, never to zero
        assert gl.estimate_tokens("x" * 4) == 1
        assert gl.estimate_tokens("x" * 4001) == 1001


class TestTokenBudget:
    """The TPM half of conforming. RPM is a min-interval between starts; TPM is a
    budget a single call can eat half of, so this is a rolling window rather than
    a second RateLimiter."""

    def test_spends_freely_inside_the_window(self):
        st, mono, sleep = fake_clock(advance=False)
        b = gl.TokenBudget(10_000, monotonic=mono, sleep=sleep)
        b.acquire(4_000)
        b.acquire(4_000)
        assert st["sleeps"] == []                     # 8,000 of 10,000 — no wait

    def test_waits_for_the_oldest_charge_to_age_out(self):
        st, mono, sleep = fake_clock(advance=False)
        b = gl.TokenBudget(10_000, monotonic=mono, sleep=sleep)
        b.acquire(4_000)
        b.acquire(4_000)
        b.acquire(4_000)
        # 12,000 > 10,000, so the third waits out the first charge's window —
        # not a fixed interval, which is what makes this different from RPM.
        assert st["sleeps"] == [60.0]

    def test_unlimited_tpm_is_a_no_op(self):
        st, mono, sleep = fake_clock(advance=False)
        for tpm in (None, 0):
            b = gl.TokenBudget(tpm, monotonic=mono, sleep=sleep)
            for _ in range(5):
                b.acquire(10_000_000)
        assert st["sleeps"] == []

    def test_a_call_bigger_than_the_whole_budget_still_runs(self):
        st, mono, sleep = fake_clock(advance=False)
        b = gl.TokenBudget(1_000, monotonic=mono, sleep=sleep)
        b.acquire(5_000)
        # No amount of waiting makes room for it, so it goes alone rather than
        # hanging the run; the NEXT call waits out the window it overspent.
        assert st["sleeps"] == []
        b.acquire(100)
        assert st["sleeps"] == [60.0]

    def test_schedule_is_fifo(self):
        st, mono, sleep = fake_clock(advance=False)
        b = gl.TokenBudget(1_000, monotonic=mono, sleep=sleep)
        b.acquire(900)                                # t=0
        b.acquire(900)                                # scheduled at t=60
        b.acquire(10)                                 # cheap — must NOT jump ahead
        assert st["sleeps"] == [60.0, 60.0]

    def test_charge_records_without_waiting(self):
        st, mono, sleep = fake_clock(advance=False)
        b = gl.TokenBudget(1_000, monotonic=mono, sleep=sleep)
        b.charge(900)                                 # the response reconciliation
        assert st["sleeps"] == []
        b.acquire(900)                                # ...but it counts against the window
        assert st["sleeps"] == [60.0]

    def test_expired_charges_stop_counting(self):
        st, mono, sleep = fake_clock(advance=False)
        b = gl.TokenBudget(1_000, monotonic=mono, sleep=sleep)
        b.acquire(900)
        st["t"] = 61.0                                # a minute later
        b.acquire(900)
        assert st["sleeps"] == []


class TestPacedCaller:
    @staticmethod
    def _real_clock_injected(monkeypatch):
        """Swap in the REAL limiter and budget bound to a clock that advances on
        sleep, so these assert the pacing that a run would get rather than a
        stand-in's bookkeeping."""
        st, mono, sleep = fake_clock()
        # Bind the real classes before patching: the replacements construct them,
        # and reading gl.RateLimiter from inside the lambda would find the
        # replacement itself.
        real_rl, real_tb = gl.RateLimiter, gl.TokenBudget
        monkeypatch.setattr(gl, "RateLimiter",
                            lambda rpm: real_rl(rpm, monotonic=mono, sleep=sleep))
        monkeypatch.setattr(gl, "TokenBudget",
                            lambda tpm: real_tb(tpm, monotonic=mono, sleep=sleep))
        return st

    def test_acquires_then_calls_when_conforming(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        events = []

        class FakeRL:
            def __init__(self, rpm):
                events.append(("rpm-init", rpm))

            def acquire(self):
                events.append(("rpm-acquire",))

        class FakeTB:
            def __init__(self, tpm):
                events.append(("tpm-init", tpm))

            def acquire(self, tokens):
                events.append(("tpm-acquire", tokens))

            def charge(self, tokens):
                events.append(("tpm-charge", tokens))
        monkeypatch.setattr(gl, "RateLimiter", FakeRL)
        monkeypatch.setattr(gl, "TokenBudget", FakeTB)

        def base(*a, **k):
            events.append(("call",))
            return "R"
        wrapped = gl.paced_caller(base, "gemini-2.5-flash")
        assert wrapped("sys!", "u" * 400) == "R"
        assert events == [
            ("rpm-init", 5),
            ("tpm-init", 250_000),
            # The token wait is taken FIRST, so the RPM spacing that follows is
            # measured between calls that happen rather than slots sat on. The
            # charge is every string argument — system (4 chars) and user (400) —
            # plus the response reserve.
            ("tpm-acquire", 1 + 100 + gl._RESERVED_OUTPUT_TOKENS),
            ("rpm-acquire",),
            ("call",),
        ]

    def test_tpm_paces_where_rpm_would_not(self, monkeypatch):
        """#143 in one assertion: gemma grants 30 RPM (a call every 2s) and
        16,000 TPM. At ~8K tokens a call, the second call waits out a window."""
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        st = self._real_clock_injected(monkeypatch)
        prompt = "x" * 32_000                          # 8,000 tokens
        wrapped = gl.paced_caller(lambda s, u: "ok", "gemma-4-26b-a4b-it")
        wrapped("", prompt)
        wrapped("", prompt)
        assert st["sleeps"] == [60.0]                  # not the 2.0s RPM allows

    def test_slack_tpm_leaves_rpm_in_charge(self, monkeypatch):
        # The Flash rows: 250K TPM against 5 RPM is slack no prompt can use up,
        # so pacing is exactly what it was before TPM entered the picture.
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        st = self._real_clock_injected(monkeypatch)
        wrapped = gl.paced_caller(lambda s, u: "ok", "gemini-2.5-flash")
        for _ in range(3):
            wrapped("", "x" * 32_000)
        assert st["sleeps"] == [12.0, 12.0]            # 60/5 RPM, no token waits

    def test_separately_built_callers_share_one_pace(self, monkeypatch):
        """The pacers are per model, not per caller instance.

        The UI builds a fresh caller per Add-Job request thread and per résumé
        tailoring call, so per-caller state would hand each of them the model's
        whole 16,000 TPM — two clicks, double the budget, the 429 the pacing
        exists to prevent."""
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        st = self._real_clock_injected(monkeypatch)
        prompt = "x" * 32_000
        a = gl.paced_caller(lambda s, u: "ok", "gemma-4-26b-a4b-it")
        b = gl.paced_caller(lambda s, u: "ok", "gemma-4-26b-a4b-it")
        a("", prompt)
        b("", prompt)
        assert st["sleeps"] == [60.0]                  # b waits on a's tokens

    def test_edited_limits_are_not_paced_by_the_old_numbers(self, tmp_path, monkeypatch):
        # Keyed on the limits as well as the model, because the UI can rewrite
        # the override file in a process that has already built a pacer.
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        point_at(tmp_path, monkeypatch, '{"m": {"rpm": 5, "tpm": 1000, "rpd": 100}}')
        gl.paced_caller(lambda s, u: "ok", "m")
        point_at(tmp_path, monkeypatch, '{"m": {"rpm": 5, "tpm": 250000, "rpd": 100}}')
        gl.paced_caller(lambda s, u: "ok", "m")
        assert sorted(k[2] for k in gl._pacers) == [1_000, 250_000]

    def test_response_tokens_are_reconciled_into_the_window(self, monkeypatch):
        """The reserve is an estimate of the response, so an evaluation that
        returns far more than it must not go uncounted — otherwise a run of long
        answers paces to a budget it is quietly three times over."""
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        st = self._real_clock_injected(monkeypatch)
        big = "y" * 40_000                             # 10,000 tokens back
        wrapped = gl.paced_caller(lambda s, u: big, "gemma-4-26b-a4b-it")
        for _ in range(3):
            wrapped("", "")
        # Empty prompts, so only the responses can account for a wait — and on
        # the reserve alone (1,500 x 3 against 16,000) there would be none. The
        # third call lands a full window after the first; RPM alone would have
        # put it at t=4.0.
        assert st["t"] == 60.0

    def test_noop_when_conforming_off(self, monkeypatch):
        monkeypatch.delenv("GEMINI_FREE_TIER", raising=False)
        base = lambda: "R"
        assert gl.paced_caller(base, "gemini-2.5-flash") is base

    def test_noop_for_non_free_tier_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        base = lambda: "R"
        assert gl.paced_caller(base, "gpt-4o-mini") is base


class TestBatchRecommendation:
    """What we steer batch users toward. Ranked by honest capacity, because the
    constant this replaced ranked by RPD and therefore recommended a model on the
    strength of a number its TPM makes unreachable (#143)."""

    def test_default_table_recommends_gemma(self):
        assert gl.batch_recommendation() == "gemma-4-26b-a4b-it"

    def test_a_huge_rpd_does_not_win_on_its_own(self, tmp_path, monkeypatch):
        # 100,000 RPD is seven times gemma's — and 1,000 TPM pays for ~150
        # evaluations a day, which is less than a Flash-Lite row.
        point_at(tmp_path, monkeypatch,
                 '{"paper-tiger": {"rpm": 30, "tpm": 1000, "rpd": 100000}}')
        assert gl.effective_limits()["paper-tiger"]["rpd"] == 100_000
        assert gl.batch_recommendation() == "gemma-4-26b-a4b-it"

    def test_a_huge_rpd_behind_a_slow_rate_does_not_win_either(self, tmp_path, monkeypatch):
        # The RPM twin of the paper tiger above: unlimited tokens, one request a
        # minute, a six-figure quota. 1,440/day, so gemma still wins.
        point_at(tmp_path, monkeypatch,
                 '{"slowpoke": {"rpm": 1, "tpm": null, "rpd": 100000}}')
        assert gl.batch_recommendation() == "gemma-4-26b-a4b-it"

    def test_a_users_own_numbers_change_the_advice(self, tmp_path, monkeypatch):
        # The point of computing it: on a paid project the free-tier ranking is
        # not the user's ranking.
        point_at(tmp_path, monkeypatch,
                 '{"gemini-2.5-flash": {"rpm": 1000, "tpm": null, "rpd": 50000}}')
        assert gl.batch_recommendation() == "gemini-2.5-flash"

    def test_ties_break_deterministically(self, monkeypatch):
        # gemma-4-26b-a4b-it and gemma-4-31b-it carry identical rows, so the
        # advice must not depend on which one the table lists first. Asserting
        # against the table as shipped proves nothing — min() is stable, so the
        # expected model wins on insertion order whether or not the model id is
        # in the sort key. Reverse the table and the tiebreak has to do the work.
        reversed_table = dict(reversed(list(gl.GEMINI_FREE_TIER_LIMITS.items())))
        monkeypatch.setattr(gl, "GEMINI_FREE_TIER_LIMITS", reversed_table)
        assert gl.batch_recommendation() == "gemma-4-26b-a4b-it"


class TestUserOverrides:
    """The user's AI Studio numbers beat the baked table, per model.

    The table is a snapshot of a page Google changes and is free-tier-only, so
    it is wrong for any paid project by construction. These tests pin the
    precedence and — more importantly — that a bad file degrades to the table
    instead of taking a run down.
    """

    def test_absent_file_is_just_the_baked_table(self, tmp_path, monkeypatch):
        point_at(tmp_path, monkeypatch)
        assert gl.effective_limits() == gl.GEMINI_FREE_TIER_LIMITS
        assert gl.user_limits() == {}

    def test_override_wins_and_drives_the_cap(self, tmp_path, monkeypatch):
        point_at(tmp_path, monkeypatch,
                       '{"gemini-2.5-flash": {"rpm": 10, "tpm": 250000, "rpd": 250}}')
        assert gl.effective_limits()["gemini-2.5-flash"]["rpd"] == 250
        # The whole point: 50 pending no longer "exceeds" once the real number is known.
        assert gl.free_tier_viability("gemini-2.5-flash", 50)["exceeds"] is False
        assert gl.format_free_tier_warning("gemini-2.5-flash", 50) is None

    def test_a_model_google_added_becomes_conformable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        point_at(tmp_path, monkeypatch)
        # Unknown: conforming can't act, and says so rather than going quiet.
        assert gl.paced_caller(lambda: 1, "gemini-9-flash").__name__ == "<lambda>"
        assert gl.cap_to_rpd(list(range(50)), "gemini-9-flash") == (list(range(50)), 0)
        assert "will NOT be paced" in gl.format_unconformable_warning("gemini-9-flash")

        point_at(tmp_path, monkeypatch,
                       '{"gemini-9-flash": {"rpm": 15, "tpm": null, "rpd": 30}}')
        assert gl.format_unconformable_warning("gemini-9-flash") is None
        assert gl.paced_caller(lambda: 1, "gemini-9-flash").__name__ == "wrapped"
        kept, deferred = gl.cap_to_rpd(list(range(50)), "gemini-9-flash")
        assert (len(kept), deferred) == (30, 20)

    def test_partial_override_keeps_the_rest_of_the_row(self, tmp_path, monkeypatch):
        point_at(tmp_path, monkeypatch, '{"gemini-2.5-flash": {"rpd": 999}}')
        row = gl.effective_limits()["gemini-2.5-flash"]
        assert row == {"rpm": 5, "tpm": 250_000, "rpd": 999}

    def test_an_omitted_tpm_keeps_the_baked_one_but_null_clears_it(self, tmp_path, monkeypatch):
        """The distinction the Setup wizard's blank TPM box relies on.

        It posts the row without a `tpm` key rather than with `tpm: null`,
        because null is "unlimited" and would merge OVER the built-in 16,000 —
        switching the token pacing off for a user who simply didn't copy that
        number, through the only UI this feature has."""
        point_at(tmp_path, monkeypatch,
                 '{"gemma-4-26b-a4b-it": {"rpm": 30, "rpd": 14400}}')
        assert gl.effective_limits()["gemma-4-26b-a4b-it"]["tpm"] == 16_000
        point_at(tmp_path, monkeypatch,
                 '{"gemma-4-26b-a4b-it": {"rpm": 30, "tpm": null, "rpd": 14400}}')
        assert gl.effective_limits()["gemma-4-26b-a4b-it"]["tpm"] is None

    def test_other_models_survive_one_override(self, tmp_path, monkeypatch):
        point_at(tmp_path, monkeypatch, '{"gemini-2.5-flash": {"rpd": 999}}')
        assert gl.effective_limits()["gemma-4-26b-a4b-it"]["rpd"] == 14_400

    def test_unreadable_file_degrades_to_the_table(self, tmp_path, monkeypatch):
        for junk in ("{not json", "[]", '"a string"', "null"):
            point_at(tmp_path, monkeypatch, junk)
            assert gl.effective_limits() == gl.GEMINI_FREE_TIER_LIMITS

    def test_bad_rows_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        # One good row, several unusable ones. The good row must survive: losing
        # it to a neighbour's typo is how a user ends up back on wrong numbers.
        point_at(tmp_path, monkeypatch, """{
            "good":       {"rpm": 5, "rpd": 100},
            "zero-rpd":   {"rpm": 5, "rpd": 0},
            "bool-rpd":   {"rpm": 5, "rpd": true},
            "str-rpm":    {"rpm": "5", "rpd": 100},
            "no-rpd":     {"rpm": 5},
            "not-a-dict": 7
        }""")
        assert gl.user_limits() == {"good": {"rpm": 5, "rpd": 100, "tpm": None}}

    def test_cache_follows_mtime(self, tmp_path, monkeypatch):
        f = point_at(tmp_path, monkeypatch, '{"m": {"rpm": 1, "rpd": 10}}')
        assert gl.effective_limits()["m"]["rpd"] == 10
        f.write_text('{"m": {"rpm": 1, "rpd": 20}}', encoding="utf-8")
        # Stamp AFTER the write, not before. The write sets mtime to "now", and
        # Windows' system-clock granularity (~15.6ms) is coarse enough that
        # "now" can equal the mtime the first read already cached — so the
        # second read hits a stale entry and this asserts 10 == 20. Stamping
        # afterwards makes the invalidation deterministic instead of a race
        # against the clock that only Linux happens to win.
        os.utime(f, (1, 1))
        assert gl.effective_limits()["m"]["rpd"] == 20


class TestSaveUserLimits:
    def test_round_trips_and_creates_the_dir(self, tmp_path, monkeypatch):
        f = point_at(tmp_path / "nested", monkeypatch)
        gl.save_user_limits({"m": {"rpm": 15, "tpm": None, "rpd": 1000}})
        assert f.exists()
        assert gl.user_limits() == {"m": {"rpm": 15, "tpm": None, "rpd": 1000}}

    def test_saving_one_model_keeps_the_rest(self, tmp_path, monkeypatch):
        """A save merges. The wizard edits one model and posts one row, and it
        tells chain users to add the other members to this file by hand — so a
        wholesale rewrite would delete the rows it had just asked for, and drop
        those models back onto baked numbers that are wrong for a paid project
        by construction."""
        point_at(tmp_path / "nested", monkeypatch)
        gl.save_user_limits({"a": {"rpm": 5, "tpm": 1000, "rpd": 100}})
        gl.save_user_limits({"b": {"rpm": 9, "tpm": None, "rpd": 200}})
        assert sorted(gl.user_limits()) == ["a", "b"]
        # Deleting stays available — it just has to be asked for by name.
        gl.save_user_limits({"b": None})
        assert sorted(gl.user_limits()) == ["a"]

    def test_clearing_removes_the_file(self, tmp_path, monkeypatch):
        f = point_at(tmp_path / "nested", monkeypatch)
        gl.save_user_limits({"m": {"rpm": 15, "tpm": None, "rpd": 1000}})
        gl.save_user_limits({"m": None})
        # Removed, not left as "{}" — "clear my overrides" must be
        # indistinguishable from never having set any.
        assert not f.exists()
        assert gl.effective_limits() == gl.GEMINI_FREE_TIER_LIMITS

    def test_invalid_row_raises_instead_of_writing(self, tmp_path, monkeypatch):
        f = point_at(tmp_path / "nested", monkeypatch)
        with pytest.raises(ValueError, match="rpm and rpd"):
            gl.save_user_limits({"m": {"rpm": 0, "rpd": 10}})
        assert not f.exists()


class TestDiffModels:
    def test_retired_and_unlisted(self):
        live = ["gemini-2.5-flash", "gemini-99-new"]
        d = gl.diff_models(live)
        # Everything in the table but not live is a row to fix...
        assert "gemma-4-26b-a4b-it" in d["retired"]
        assert "gemini-2.5-flash" not in d["retired"]
        # ...and a live model we don't track is a candidate, not an error.
        assert d["unlisted"] == ["gemini-99-new"]

    def test_user_added_models_count_as_known(self, tmp_path, monkeypatch):
        point_at(tmp_path, monkeypatch, '{"gemini-99-new": {"rpm": 1, "rpd": 5}}')
        assert gl.diff_models(["gemini-99-new"])["unlisted"] == []


class TestFetchModelIds:
    """The shared Gemini catalog fetch — verify_models._fetch_gemini delegates
    here, so the guarantees its own tests pinned now live at this seam."""

    def test_key_goes_in_the_header_not_the_url(self):
        seen = {}

        def fake(url, *, headers, params):
            seen["url"], seen["headers"] = url, headers
            return {"models": [{"name": "models/gemini-x",
                                "supportedGenerationMethods": ["generateContent"]}]}
        gl.fetch_model_ids("SECRET", get=fake)
        assert seen["headers"]["x-goog-api-key"] == "SECRET"
        # The whole point: a key in the query string leaks through any logged URL.
        assert "SECRET" not in seen["url"]

    def test_follows_pagination(self):
        pages = [
            {"models": [{"name": "models/a", "supportedGenerationMethods": ["generateContent"]}],
             "nextPageToken": "t1"},
            {"models": [{"name": "models/b", "supportedGenerationMethods": ["generateContent"]}]},
        ]
        calls = []

        def fake(url, *, headers, params):
            calls.append(params.get("pageToken"))
            return pages[len(calls) - 1]
        # A single page would report every model on page 2 as retired.
        assert gl.fetch_model_ids("k", get=fake) == ["a", "b"]
        assert calls == [None, "t1"]

    def test_generate_content_filter_is_optional(self):
        payload = {"models": [
            {"name": "models/chat", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
        ]}
        fake = lambda url, *, headers, params: payload
        assert gl.fetch_model_ids("k", get=fake) == ["chat"]
        # verify_models checks IDs configured for any purpose, so it opts out.
        assert gl.fetch_model_ids("k", get=fake, generate_content_only=False) == ["chat", "embed"]
