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
        assert gl.free_tier_viability("gemma-4-26b-a4b-it", 10_000)["exceeds"] is False

    def test_unknown_model_returns_none(self):
        assert gl.free_tier_viability("claude-sonnet-4-6", 50) is None
        assert gl.free_tier_viability("gemma-4-26b-it", 50) is None   # the wrong ID isn't in the table

    def test_sums_failover_chain_rpd(self):
        # A chain fails over member-to-member, so its daily capacity is the SUM.
        v = gl.free_tier_viability("gemini-2.5-flash,gemma-4-26b-a4b-it", 20_000)
        assert v["rpd"] == 14_420                     # 20 + 14,400
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
        clock = [0.0]
        sleeps = []

        def fsleep(s):
            sleeps.append(s)
            clock[0] += s
        rl = gl.RateLimiter(60, monotonic=lambda: clock[0], sleep=fsleep)  # 1 req/sec
        for _ in range(4):
            rl.acquire()
        assert sleeps == [1.0, 1.0, 1.0]   # first immediate, then ~1s apart

    def test_first_acquire_does_not_sleep(self):
        sleeps = []
        rl = gl.RateLimiter(5, monotonic=lambda: 0.0, sleep=sleeps.append)
        rl.acquire()
        assert sleeps == []


class TestPacedCaller:
    def test_acquires_then_calls_when_conforming(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        events = []

        class FakeRL:
            def __init__(self, rpm):
                events.append(("init", rpm))

            def acquire(self):
                events.append(("acquire",))
        monkeypatch.setattr(gl, "RateLimiter", FakeRL)

        def base(*a, **k):
            events.append(("call",))
            return "R"
        wrapped = gl.paced_caller(base, "gemini-2.5-flash")
        assert wrapped() == "R"
        assert events == [("init", 5), ("acquire",), ("call",)]

    def test_noop_when_conforming_off(self, monkeypatch):
        monkeypatch.delenv("GEMINI_FREE_TIER", raising=False)
        base = lambda: "R"
        assert gl.paced_caller(base, "gemini-2.5-flash") is base

    def test_noop_for_non_free_tier_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        base = lambda: "R"
        assert gl.paced_caller(base, "gpt-4o-mini") is base


class TestUserOverrides:
    """The user's AI Studio numbers beat the baked table, per model.

    The table is a snapshot of a page Google changes and is free-tier-only, so
    it is wrong for any paid project by construction. These tests pin the
    precedence and — more importantly — that a bad file degrades to the table
    instead of taking a run down.
    """

    @staticmethod
    def _point_at(tmp_path, monkeypatch, text=None):
        f = tmp_path / "gemini-limits.json"
        if text is not None:
            f.write_text(text, encoding="utf-8")
        monkeypatch.setenv("GEMINI_LIMITS_FILE", str(f))
        gl._override_cache.clear()
        return f

    def test_absent_file_is_just_the_baked_table(self, tmp_path, monkeypatch):
        self._point_at(tmp_path, monkeypatch)
        assert gl.effective_limits() == gl.GEMINI_FREE_TIER_LIMITS
        assert gl.user_limits() == {}

    def test_override_wins_and_drives_the_cap(self, tmp_path, monkeypatch):
        self._point_at(tmp_path, monkeypatch,
                       '{"gemini-2.5-flash": {"rpm": 10, "tpm": 250000, "rpd": 250}}')
        assert gl.effective_limits()["gemini-2.5-flash"]["rpd"] == 250
        # The whole point: 50 pending no longer "exceeds" once the real number is known.
        assert gl.free_tier_viability("gemini-2.5-flash", 50)["exceeds"] is False
        assert gl.format_free_tier_warning("gemini-2.5-flash", 50) is None

    def test_a_model_google_added_becomes_conformable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_FREE_TIER", "true")
        self._point_at(tmp_path, monkeypatch)
        # Unknown: conforming can't act, and says so rather than going quiet.
        assert gl.paced_caller(lambda: 1, "gemini-9-flash").__name__ == "<lambda>"
        assert gl.cap_to_rpd(list(range(50)), "gemini-9-flash") == (list(range(50)), 0)
        assert "will NOT be paced" in gl.format_unconformable_warning("gemini-9-flash")

        self._point_at(tmp_path, monkeypatch,
                       '{"gemini-9-flash": {"rpm": 15, "tpm": null, "rpd": 30}}')
        assert gl.format_unconformable_warning("gemini-9-flash") is None
        assert gl.paced_caller(lambda: 1, "gemini-9-flash").__name__ == "wrapped"
        kept, deferred = gl.cap_to_rpd(list(range(50)), "gemini-9-flash")
        assert (len(kept), deferred) == (30, 20)

    def test_partial_override_keeps_the_rest_of_the_row(self, tmp_path, monkeypatch):
        self._point_at(tmp_path, monkeypatch, '{"gemini-2.5-flash": {"rpd": 999}}')
        row = gl.effective_limits()["gemini-2.5-flash"]
        assert row == {"rpm": 5, "tpm": 250_000, "rpd": 999}

    def test_other_models_survive_one_override(self, tmp_path, monkeypatch):
        self._point_at(tmp_path, monkeypatch, '{"gemini-2.5-flash": {"rpd": 999}}')
        assert gl.effective_limits()["gemma-4-26b-a4b-it"]["rpd"] == 14_400

    def test_unreadable_file_degrades_to_the_table(self, tmp_path, monkeypatch):
        for junk in ("{not json", "[]", '"a string"', "null"):
            self._point_at(tmp_path, monkeypatch, junk)
            assert gl.effective_limits() == gl.GEMINI_FREE_TIER_LIMITS

    def test_bad_rows_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        # One good row, several unusable ones. The good row must survive: losing
        # it to a neighbour's typo is how a user ends up back on wrong numbers.
        self._point_at(tmp_path, monkeypatch, """{
            "good":       {"rpm": 5, "rpd": 100},
            "zero-rpd":   {"rpm": 5, "rpd": 0},
            "bool-rpd":   {"rpm": 5, "rpd": true},
            "str-rpm":    {"rpm": "5", "rpd": 100},
            "no-rpd":     {"rpm": 5},
            "not-a-dict": 7
        }""")
        assert gl.user_limits() == {"good": {"rpm": 5, "rpd": 100, "tpm": None}}

    def test_cache_follows_mtime(self, tmp_path, monkeypatch):
        f = self._point_at(tmp_path, monkeypatch, '{"m": {"rpm": 1, "rpd": 10}}')
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
    @staticmethod
    def _point_at(tmp_path, monkeypatch):
        f = tmp_path / "nested" / "gemini-limits.json"
        monkeypatch.setenv("GEMINI_LIMITS_FILE", str(f))
        gl._override_cache.clear()
        return f

    def test_round_trips_and_creates_the_dir(self, tmp_path, monkeypatch):
        f = self._point_at(tmp_path, monkeypatch)
        gl.save_user_limits({"m": {"rpm": 15, "tpm": None, "rpd": 1000}})
        assert f.exists()
        assert gl.user_limits() == {"m": {"rpm": 15, "tpm": None, "rpd": 1000}}

    def test_clearing_removes_the_file(self, tmp_path, monkeypatch):
        f = self._point_at(tmp_path, monkeypatch)
        gl.save_user_limits({"m": {"rpm": 15, "tpm": None, "rpd": 1000}})
        gl.save_user_limits({"m": None})
        # Removed, not left as "{}" — "clear my overrides" must be
        # indistinguishable from never having set any.
        assert not f.exists()
        assert gl.effective_limits() == gl.GEMINI_FREE_TIER_LIMITS

    def test_invalid_row_raises_instead_of_writing(self, tmp_path, monkeypatch):
        f = self._point_at(tmp_path, monkeypatch)
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
        f = tmp_path / "l.json"
        f.write_text('{"gemini-99-new": {"rpm": 1, "rpd": 5}}', encoding="utf-8")
        monkeypatch.setenv("GEMINI_LIMITS_FILE", str(f))
        gl._override_cache.clear()
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
