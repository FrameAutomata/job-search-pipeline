"""Tests for pipeline/gemini_limits.py — hardcoded Gemini free-tier limits + the
viability check that warns when a batch run will exceed the free daily cap.

Limits are from Google AI Studio's "Rate limits by model" (free tier); there's
no API to fetch them, so they're hardcoded. Only the text models usable for
resume evaluation/tailoring are included.
"""

from pipeline import gemini_limits as gl


class TestTable:
    def test_known_models_and_values(self):
        t = gl.GEMINI_FREE_TIER_LIMITS
        assert t["gemini-2.5-flash"] == {"rpm": 5, "tpm": 250_000, "rpd": 20}
        assert t["gemini-2.5-flash-lite"]["rpm"] == 10
        assert t["gemini-3-flash-preview"]["rpd"] == 20
        assert t["gemini-3.5-flash"]["rpd"] == 20
        assert t["gemini-3.1-flash-lite"] == {"rpm": 15, "tpm": 250_000, "rpd": 500}
        # Gemma: unlimited TPM (None), the high-RPD option for batch runs.
        assert t["gemma-4-26b-a4b-it"] == {"rpm": 15, "tpm": None, "rpd": 1500}
        assert t["gemma-4-31b-it"]["rpd"] == 1500


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
        v = gl.free_tier_viability("gemma-4-26b-a4b-it", 2000)
        assert v["exceeds"] is True
        assert "suggestion" not in v          # already the best free option

    def test_gemma_within_cap(self):
        assert gl.free_tier_viability("gemma-4-26b-a4b-it", 1000)["exceeds"] is False

    def test_unknown_model_returns_none(self):
        assert gl.free_tier_viability("claude-sonnet-4-6", 50) is None
        assert gl.free_tier_viability("gemma-4-26b-it", 50) is None   # the wrong ID isn't in the table


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
