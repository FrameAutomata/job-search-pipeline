"""Tests for the auto-apply engine's pure logic (no browser).

Covers result classification, profile loading, the deterministic+cache+LLM
answer engine, and tracker candidate selection. The Playwright-driven modules
(browser.py, linkedin.py) are verified manually via `--apply-mode dry-run`."""

import json
import textwrap
from pathlib import Path

import pytest

import pipeline.apply as apply_pkg
from pipeline.app import data as app_data
from pipeline.apply import linkedin, queue, result
from pipeline.apply.answers import AnswerEngine, _match_option, _sanitize, salary_from_report
from pipeline.apply.profile import ApplyProfile, _parse_salary, _parse_salary_target


# ── result.py ────────────────────────────────────────────────────────────────

class TestResult:
    def test_failed_helper_is_not_permanent(self):
        r = result.failed("validation_error")
        assert r.code == "failed" and r.reason == "validation_error"
        assert r.permanent is False
        assert r.applied is False

    def test_known_terminal_codes_are_permanent(self):
        for code in ("not_easy_apply", "expired", "login_issue", "not_eligible"):
            assert result.ApplyResult(code=code).permanent is True

    def test_applied_submitted_flags(self):
        r = result.ApplyResult(code="applied", submitted=True)
        assert r.applied is True and r.submitted is True
        held = result.ApplyResult(code="applied", reason="not submitted (mode=review)")
        assert held.applied is True and held.submitted is False

    def test_str_includes_reason(self):
        assert str(result.failed("boom")) == "failed:boom"
        assert str(result.ApplyResult(code="expired")) == "expired"


# ── profile.py ───────────────────────────────────────────────────────────────

class TestParseSalary:
    @pytest.mark.parametrize("raw,expected", [
        ("$75K", 75000),
        ("130,000", 130000),
        ("$130K-170K", 130000),   # first number wins
        ("$1.2M", 1200000),
        ("", None),
        (None, None),
        (95000, 95000),
    ])
    def test_parse(self, raw, expected):
        assert _parse_salary(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("$130K-$170K", 150000),   # midpoint of a range
        ("$130K - 170K", 150000),
        ("$150K", 150000),         # single value
        ("", None),
        (None, None),
    ])
    def test_parse_target(self, raw, expected):
        assert _parse_salary_target(raw) == expected


class TestApplyProfile:
    def _write(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config"
        cfg.mkdir(parents=True)
        (cfg / "profile.yml").write_text(textwrap.dedent(body), encoding="utf-8")
        return tmp_path

    def test_load_full(self, tmp_path):
        co = self._write(tmp_path, """
            candidate:
              full_name: Thomas Thirlwall
              email: t@example.com
              phone: "+1 (956) 525-3015"
              linkedin: linkedin.com/in/x
            location:
              country: United States
              city: Dallas
            work_authorization:
              citizenship: US
              legally_authorized_to_work_in: [United States]
              requires_sponsorship: false
              work_permit_type: Citizen
              eligible_countries: [United States]
            compensation:
              minimum: $75K
              currency: USD
        """)
        p = ApplyProfile.load(co)
        assert p.full_name == "Thomas Thirlwall"
        assert p.first_name == "Thomas" and p.last_name == "Thirlwall"
        assert p.phone_digits == "19565253015"
        assert p.requires_sponsorship is False
        assert p.authorized_regions == ["United States"]
        assert p.salary_target == 75000   # minimum used as target when no target_range

    def test_eligible_countries_default_to_authorized(self, tmp_path):
        co = self._write(tmp_path, """
            work_authorization:
              legally_authorized_to_work_in: [Canada]
              requires_sponsorship: true
        """)
        p = ApplyProfile.load(co)
        assert p.eligible_countries == ["Canada"]
        assert p.requires_sponsorship is True

    def test_missing_file_is_empty_defaults(self, tmp_path):
        p = ApplyProfile.load(tmp_path)
        assert p.full_name == "" and p.salary_target is None

    def test_loads_voluntary_disclosures(self, tmp_path):
        co = self._write(tmp_path, """
            voluntary_disclosures:
              gender: Female
              race_ethnicity: Asian
              disability_status: "No, I do not have a disability"
        """)
        p = ApplyProfile.load(co)
        assert p.eeo_gender == "Female" and p.eeo_race == "Asian"
        assert p.eeo_disability == "No, I do not have a disability"
        assert p.eeo_veteran == ""        # unset → blank → declines at apply time

    def test_loads_indeed_consent_prefs(self, tmp_path):
        co = self._write(tmp_path, """
            voluntary_disclosures:
              data_processing_consent: false
              save_answers: true
              share_answers: true
        """)
        p = ApplyProfile.load(co)
        assert p.eeo_data_consent is False
        assert p.eeo_save_answers is True and p.eeo_share_answers is True

    def test_indeed_consent_prefs_default(self, tmp_path):
        # Missing → auto-agree the required consent, but don't save/share answers.
        p = ApplyProfile.load(tmp_path)
        assert p.eeo_data_consent is True
        assert p.eeo_save_answers is False and p.eeo_share_answers is False


# ── answers.py ───────────────────────────────────────────────────────────────

@pytest.fixture
def profile():
    return ApplyProfile(
        full_name="Thomas Thirlwall", email="t@example.com", phone="+1 (956) 525-3015",
        city="Dallas", country="United States", linkedin="linkedin.com/in/x",
        citizenship="US", authorized_regions=["United States"],
        requires_sponsorship=False, salary_target=150000,
    )


class TestAnswerEngineDeterministic:
    def _engine(self, profile, tmp_path):
        # Caller that explodes if invoked — deterministic answers must not hit it.
        def boom(system, user):
            raise AssertionError("LLM should not be called for deterministic fields")
        return AnswerEngine(profile, tmp_path / "cache.json", caller=boom)

    def test_contact_fields(self, profile, tmp_path):
        e = self._engine(profile, tmp_path)
        assert e.answer("First name", "text") == "Thomas"
        assert e.answer("Last name", "text") == "Thirlwall"
        assert e.answer("Email address", "text") == "t@example.com"
        assert e.answer("Mobile phone number", "numeric") == "19565253015"

    def test_sponsorship_and_authorization(self, profile, tmp_path):
        e = self._engine(profile, tmp_path)
        assert e.answer("Will you now or in the future require sponsorship?",
                        "select", ["Yes", "No"]) == "No"
        assert e.answer("Are you legally authorized to work in the United States?",
                        "select", ["Yes", "No"]) == "Yes"

    def test_sponsorship_required_flips_answers(self, tmp_path):
        p = ApplyProfile(requires_sponsorship=True)
        def boom(s, u):
            raise AssertionError
        e = AnswerEngine(p, tmp_path / "c.json", caller=boom)
        assert e.answer("Do you require visa sponsorship?", "select", ["Yes", "No"]) == "Yes"
        assert e.answer("Authorized to work without sponsorship?", "select", ["Yes", "No"]) == "No"

    def test_eeo_declines(self, profile, tmp_path):
        e = self._engine(profile, tmp_path)
        assert e.answer("Gender", "select", ["Male", "Female", "Prefer not to say"]) == "Prefer not to say"
        assert e.answer("Veteran status", "text") == "Prefer not to say"

    def test_eeo_without_decline_option_is_blank_not_first(self, profile, tmp_path):
        # #2: a demographic field with NO decline option must be left blank — never
        # fall back to options[0] (which would affirm "Male").
        e = self._engine(profile, tmp_path)
        assert e.answer("Gender", "select", ["Male", "Female"]) == ""
        assert e.answer("Do you identify as Hispanic/Latino?", "select", ["Yes", "No"]) == ""

    def test_eeo_self_identifies_when_profile_set(self, tmp_path):
        # Captured EEO self-ID is used (mapped to the form's options); fields left
        # blank still decline. No LLM call for any of it.
        p = ApplyProfile(full_name="X", eeo_gender="Female",
                         eeo_veteran="I am not a protected veteran")
        e = AnswerEngine(p, tmp_path / "c.json",
                         caller=lambda s, u: (_ for _ in ()).throw(AssertionError("no LLM")))
        assert e.answer("Gender", "select", ["Male", "Female", "Prefer not to say"]) == "Female"
        assert e.answer("Are you a protected veteran?", "select",
                        ["I am a protected veteran", "I am not a protected veteran"]) == \
            "I am not a protected veteran"
        # race not set → still declines (blank-with-no-decline-option → "")
        assert e.answer("Race/Ethnicity", "select", ["Asian", "White"]) == ""

    def test_eeo_quoted_value_and_polarity_never_picks_opposite(self, tmp_path):
        # Setup wrote quoted values with slightly different wording than the form.
        # Must align by polarity (not/yes), never fall back to the opposite option.
        p = ApplyProfile(full_name="X",
                         eeo_veteran='"I am not a veteran"',          # quoted, no "protected"
                         eeo_disability='"Yes, I have a disability"')  # quoted
        e = AnswerEngine(p, tmp_path / "c.json",
                         caller=lambda s, u: (_ for _ in ()).throw(AssertionError("no LLM")))
        vet = e.answer("Veteran status", "select", [
            "I identify as one or more of the classifications of a protected veteran",
            "I am not a protected veteran", "I don't wish to answer"])
        assert vet == "I am not a protected veteran"            # NOT the affirming option
        dis = e.answer("Disability status", "select", [
            "No, I do not have a disability", "Yes, I have a disability",
            "I do not wish to answer"])
        assert dis == "Yes, I have a disability"               # NOT the "No" option

    def test_eeo_unmappable_self_id_declines_not_guesses(self, tmp_path):
        # A self-ID that can't be matched must decline, never pick options[0].
        p = ApplyProfile(full_name="X", eeo_race="Klingon")
        e = AnswerEngine(p, tmp_path / "c.json",
                         caller=lambda s, u: (_ for _ in ()).throw(AssertionError("no LLM")))
        assert e.answer("Race/Ethnicity", "select",
                        ["Asian", "White", "Prefer not to say"]) == "Prefer not to say"

    def test_eeo_ethnicity_and_latinx_route_to_race(self, tmp_path):
        # Router breadth must match the widened gate: a lone "Ethnicity" / "Latinx"
        # question uses eeo_race, not decline.
        p = ApplyProfile(full_name="X", eeo_race="Asian")
        e = AnswerEngine(p, tmp_path / "c.json",
                         caller=lambda s, u: (_ for _ in ()).throw(AssertionError("no LLM")))
        assert e.answer("Ethnicity", "select", ["Asian", "White", "Prefer not to say"]) == "Asian"
        assert e.answer("Latinx", "select", ["Asian", "White", "Prefer not to say"]) == "Asian"

    def test_eeo_veteran_with_do_not_want_to_answer_option(self, tmp_path):
        # The decline option "I do not want to answer" must NOT leak into the
        # polarity candidate pool and drop a truthful self-ID.
        p = ApplyProfile(full_name="X", eeo_veteran="I am not a veteran")
        e = AnswerEngine(p, tmp_path / "c.json",
                         caller=lambda s, u: (_ for _ in ()).throw(AssertionError("no LLM")))
        assert e.answer("Veteran status", "select", [
            "I identify as a protected veteran", "I am not a protected veteran",
            "I do not want to answer"]) == "I am not a protected veteran"

    def test_eeo_affirmative_with_incidental_negation_not_flipped(self, tmp_path):
        # An affirmative self-ID containing an incidental "no longer" must map to
        # the affirming option, never the opposite.
        p = ApplyProfile(full_name="X", eeo_veteran="I am a veteran, no longer on active duty")
        e = AnswerEngine(p, tmp_path / "c.json",
                         caller=lambda s, u: (_ for _ in ()).throw(AssertionError("no LLM")))
        assert e.answer("Veteran status", "select", [
            "I am a protected veteran", "I am not a protected veteran"]) == "I am a protected veteran"


    def test_salary_text_is_negotiable(self, profile, tmp_path):
        # Never reveal the walk-away minimum; a text salary field gets "Negotiable".
        e = self._engine(profile, tmp_path)
        assert e.answer("What are your salary expectations?", "text") == "Negotiable"

    def test_salary_numeric_uses_target_not_floor(self, profile, tmp_path):
        # A numeric salary field can't take "Negotiable" → use the target (150000),
        # NOT the floor (75000).
        e = self._engine(profile, tmp_path)
        assert e.answer("Desired salary", "numeric") == "150000"

    def test_salary_numeric_prefers_report_comp(self, profile, tmp_path):
        # The role's researched comp (from the report) beats the profile target.
        e = self._engine(profile, tmp_path)
        e.role_salary_target = 185000
        assert e.answer("Desired salary", "numeric") == "185000"


class TestAnswerEngineNumeric:
    """Numeric screening fields. LinkedIn renders most 'how many years' questions
    as plain text inputs validated as numbers only AFTER entry, and the LLM tends
    to answer in prose — so the engine must return a BARE number for numeric
    fields AND numeric-intent questions, cache the number (so a re-fill after a
    validation error doesn't re-type prose), and never fabricate one."""

    def _engine(self, profile, tmp_path, reply):
        return AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: reply)

    def test_numeric_field_extracts_number_from_prose(self, profile, tmp_path):
        e = self._engine(profile, tmp_path, "I have about 5 years of experience with Python.")
        assert e.answer("Years of experience with Python", "numeric") == "5"

    def test_numeric_intent_inferred_for_text_field(self, profile, tmp_path):
        # LinkedIn marks the input type=text; the QUESTION is what makes it numeric.
        e = self._engine(profile, tmp_path, "Around 7 years.")
        assert e.answer("How many years of experience do you have with Java?", "text") == "7"

    def test_numeric_blank_when_no_number_never_fabricates(self, profile, tmp_path):
        # A prose answer with no extractable number → blank for review, not "0".
        e = self._engine(profile, tmp_path, "It really depends on the project.")
        assert e.answer("How many years of experience with Kubernetes?", "numeric") == ""

    def test_numeric_answer_cached_as_number(self, profile, tmp_path):
        cache = tmp_path / "c.json"
        e = AnswerEngine(profile, cache, caller=lambda s, u: "I'd say 5 years")
        assert e.answer("Years of professional experience", "numeric") == "5"
        stored = json.loads(cache.read_text(encoding="utf-8"))
        assert "5" in stored.values()        # the coerced number, not the prose

    def test_decimal_preserved_for_numeric(self, profile, tmp_path):
        e = self._engine(profile, tmp_path, "3.5 years")
        assert e.answer("Years of experience with Go", "numeric") == "3.5"

    def test_non_numeric_freetext_not_coerced(self, profile, tmp_path):
        # A genuine free-text question keeps its prose — coercion is numeric-only.
        e = self._engine(profile, tmp_path, "Because I admire the team's work.")
        assert e.answer("Why do you want to work here?", "textarea") == \
            "Because I admire the team's work."

    def test_cached_numeric_prose_is_coerced_on_hit(self, profile, tmp_path):
        # A cache entry holding prose under a numeric:: key must be coerced on the
        # cache-HIT path too, not typed verbatim. The seed carries a matching CV
        # fingerprint so the entry isn't discarded as stale.
        from pipeline.apply.answers import _cv_fingerprint
        cache = tmp_path / "c.json"
        cache.write_text(
            json.dumps({"__cv__": _cv_fingerprint(""),
                        "numeric::years of professional experience": "I have about 5 years"}),
            encoding="utf-8")

        def boom(s, u):
            raise AssertionError("a cache hit must not call the LLM")
        e = AnswerEngine(profile, cache, caller=boom)
        assert e.answer("Years of professional experience", "numeric") == "5"

    def test_textarea_not_coerced_even_with_numeric_intent(self, profile, tmp_path):
        # A textarea is free-text — never coerce it to a number, even when the
        # label reads numeric ("Years of experience"). Only text/number inputs do.
        e = self._engine(profile, tmp_path, "I have five years across three teams.")
        assert e.answer("Years of experience", "textarea") == \
            "I have five years across three teams."


class TestNumericHelpers:
    @pytest.mark.parametrize("text,expected", [
        ("I have 5 years", "5"),
        ("about 7 years of experience", "7"),
        ("3.5", "3.5"),
        (".5", "0.5"),           # leading-dot decimal normalized, NOT inflated to 5
        ("12", "12"),
        ("1,200", "1200"),       # thousands separators stripped
        ("no idea", ""),
        ("", ""),
    ])
    def test_coerce_number(self, text, expected):
        from pipeline.apply.answers import _coerce_number
        assert _coerce_number(text) == expected

    def test_coerce_number_whole_floors_without_bogus_zero(self):
        from pipeline.apply.answers import _coerce_number
        assert _coerce_number("3.5 years", whole=True) == "3"
        assert _coerce_number("5", whole=True) == "5"
        # A positive value below 1 must not floor to a bogus "0" (LinkedIn's
        # "whole number larger than 0" would reject it).
        assert _coerce_number("0.9", whole=True) == "1"
        assert _coerce_number(".5", whole=True) == "1"

    @pytest.mark.parametrize("q,expected", [
        ("How many years of experience do you have with Python?", True),
        ("Years of experience", True),
        ("How many people have you managed?", True),
        ("Number of years in the industry", True),
        ("What is your name?", False),
        ("Why do you want this job?", False),
        ("Describe your experience", False),   # 'experience' alone isn't numeric
        # Free-text cues win even when a numeric phrase is present, so a prose
        # answer isn't reduced to a bare number.
        ("Describe your years of experience with distributed systems", False),
        ("How many of our products have you used and why?", False),
    ])
    def test_wants_number(self, q, expected):
        from pipeline.apply.answers import _wants_number
        assert _wants_number(q) is expected


class TestNumericValidationMessage:
    """linkedin._numeric_required detects LinkedIn's post-entry 'enter a number'
    inline error so the fill loop can re-answer that field numerically — the case
    a text input only reveals as numeric once a non-number is typed."""

    @pytest.mark.parametrize("msg,expected", [
        ("Enter a whole number larger than 0.", True),
        ("Enter a decimal number larger than 0.0.", True),
        ("Please enter a valid number.", True),
        ("Enter a number between 0 and 99.", True),
        ("This field is required.", False),
        ("Enter a valid email address.", False),
        ("", False),
    ])
    def test_numeric_required(self, msg, expected):
        from pipeline.apply.linkedin import _numeric_required
        assert _numeric_required(msg) is expected


class TestCandidateContext:
    """The LLM answerer must be given the candidate's CV, not just contact +
    work-auth — otherwise an experience question ("how many years with Spring
    Boot") is answered with no evidence the candidate ever used it (-> 0). The
    same cv.md the cover-letter generator uses, fed to single + multi answers."""

    def _capture_engine(self, profile, tmp_path, captured, reply, cv_text):
        def caller(system, user):
            captured["system"] = system
            return reply
        return AnswerEngine(profile, tmp_path / "c.json", caller=caller, cv_text=cv_text)

    def test_cv_text_in_single_answer_context(self, profile, tmp_path):
        captured = {}
        e = self._capture_engine(
            profile, tmp_path, captured, "3",
            "EXPERIENCE\nCapital One - Software Engineer\nJava, Spring Boot REST APIs (2021-2024)")
        e.answer("How many years of experience with Spring Boot?", "numeric")
        assert "Spring Boot" in captured["system"]
        assert "Capital One" in captured["system"]

    def test_cv_text_in_multi_select_context(self, profile, tmp_path):
        captured = {}
        e = self._capture_engine(profile, tmp_path, captured, "Java",
                                 "Skills: Java, Spring Boot, AWS, Terraform")
        e.answer_multi("Which of these have you used?", ["Java", "Rust", "Go"])
        assert "Java" in captured["system"] and "Spring Boot" in captured["system"]

    def test_cv_text_capped(self, profile, tmp_path):
        from pipeline.apply.answers import _CV_CONTEXT_MAX
        captured = {}
        e = self._capture_engine(profile, tmp_path, captured, "1", "Y" * (_CV_CONTEXT_MAX + 5000))
        e.answer("How many years with Y?", "numeric")
        # The résumé block is bounded so the prompt stays a sane size.
        assert len(captured["system"]) < _CV_CONTEXT_MAX + 2000

    def test_no_cv_text_back_compat(self, profile, tmp_path):
        # cv_text defaults to "" → context is the profile block only; still answers.
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "5")
        assert e.answer("Years of experience with Python", "numeric") == "5"

    def test_salary_field_stays_deterministic_with_cv(self, profile, tmp_path):
        # Even with a comp figure in the CV, a salary field is answered
        # deterministically — it never reaches the LLM, so the CV can't leak it.
        def boom(s, u):
            raise AssertionError("a salary field must not reach the LLM")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=boom,
                         cv_text="Compensation note: target $185,000")
        assert e.answer("What are your salary expectations?", "text") == "Negotiable"

    def test_llm_prompt_forbids_salary_disclosure(self, profile, tmp_path):
        # Belt-and-suspenders: any free-text answer reaching the LLM — with the CV
        # (which could carry comp) in context — is instructed never to state a
        # figure. (A question that matched the deterministic salary regex would
        # never reach the LLM in the first place, so we use a neutral one.)
        captured = {}
        def caller(system, user):
            captured["system"] = system
            return "ok"
        e = AnswerEngine(profile, tmp_path / "c.json", caller=caller,
                         cv_text="Compensation: $185,000")
        e.answer("Why are you a strong fit for this role?", "textarea")
        sys_l = captured["system"].lower()
        assert "salary" in sys_l and "negotiable" in sys_l


class TestAnswerCacheInvalidation:
    """A CV-grounded answer must not outlive the CV it was derived from: when
    cv.md changes, cached LLM answers are stale and must be re-asked — otherwise
    editing your résumé silently has no effect until the cache file is deleted
    (which we tripped over during verification)."""

    def test_cv_edit_invalidates_cached_answers(self, profile, tmp_path):
        cache = tmp_path / "c.json"
        e1 = AnswerEngine(profile, cache, caller=lambda s, u: "5", cv_text="Java since 2021")
        assert e1.answer("How many years with Java?", "numeric") == "5"
        # CV changed → the cached "5" must NOT be replayed; the re-ask yields "9".
        e2 = AnswerEngine(profile, cache, caller=lambda s, u: "9", cv_text="Java since 2017")
        assert e2.answer("How many years with Java?", "numeric") == "9"

    def test_same_cv_reuses_cache(self, profile, tmp_path):
        cache = tmp_path / "c.json"
        calls = []
        def caller(s, u):
            calls.append(1)
            return "5"
        AnswerEngine(profile, cache, caller=caller, cv_text="CV").answer("Years with Java?", "numeric")
        # Same CV → the second engine reuses the cache (no second LLM call).
        v = AnswerEngine(profile, cache, caller=caller, cv_text="CV").answer("Years with Java?", "numeric")
        assert v == "5" and len(calls) == 1


class TestEEOHelpers:
    def test_polarity_ignores_incidental_negation(self):
        from pipeline.apply.answers import _polarity_is_negative
        assert _polarity_is_negative("I am not a veteran") is True
        assert _polarity_is_negative("No") is True
        assert _polarity_is_negative("I am a veteran, no longer on active duty") is False
        assert _polarity_is_negative("Yes, I have a disability") is False

    def test_decline_regex_covers_common_phrasings_not_real_options(self):
        from pipeline.apply.answers import _DECLINE_RE
        for s in ["I prefer not to say", "I do not want to answer", "I don't wish to answer",
                  "Choose not to disclose", "I decline to self-identify", "I'd rather not say"]:
            assert _DECLINE_RE.search(s), s
        for s in ["I am not a protected veteran", "Yes, I have a disability", "Male", "Asian"]:
            assert not _DECLINE_RE.search(s), s


class TestSalaryFromReport:
    @pytest.mark.parametrize("text,expected", [
        ("D) Comp y Demanda — the range is $150-220K, competitive", 185000),
        ("posted $150K-$220K base", 185000),
        ("$150,000 to $220,000", 185000),
        ("Posted comp: $150000-220000", 185000),       # #13: comma-less 6-digit range
        ("Base salary is $180K for this role.", 180000),  # single $-figure with suffix
        # #1: a $ is REQUIRED — bare ranges (team size, percentile, years, headcount)
        # are no longer scraped as salary.
        ("5-10 years of experience required", None),
        ("a team of 50-200 people, doubling in 9-12 months", None),
        ("rated in the top 10-20% of the index", None),
        ("$35M raised in Series B", None),             # funding (>1M) is filtered
        ("no compensation info", None),
    ])
    def test_extract(self, text, expected):
        assert salary_from_report(text) == expected

    def test_skips_candidates_own_ask(self):
        # The candidate's target/floor must never be read back as the role's comp.
        text = ("Candidate target range: $75K-$500K.\n"
                "Posted role comp: $150K-$190K.")
        assert salary_from_report(text) == 170000   # the role line, not the $75K floor

    def test_mixed_full_and_k_range_not_inflated(self):
        # a 'K' suffix must not be borrowed onto an already-full number
        # ($95,000 stays $95,000, not $95,000,000 → which used to yield None).
        assert salary_from_report("comp band $95,000-120K") == 107500

    def test_k_low_full_high_not_inflated(self):
        # the OTHER direction: "$150K-$220,000" must not apply K to 220,000.
        assert salary_from_report("Base comp $150K-$220,000 per year") == 185000

    def test_role_comp_survives_candidate_parenthetical(self):
        # The candidate's ask in parens is dropped; the role comp on the same line survives.
        assert salary_from_report(
            "Compensation: $160,000-$200,000 (candidate seeking $150K+)") == 180000

    def test_single_full_number_without_suffix(self):
        assert salary_from_report("Base salary is $180,000 annually.") == 180000

    def test_prefers_comp_keyword_line_over_incidental_range(self):
        text = "Tooling budget is $50-80K for the team.\nSalary range: $150K-$170K."
        assert salary_from_report(text) == 160000   # the comp line wins, not the $50-80K


class TestAnswerEngineCache:
    def test_llm_called_once_then_cached(self, profile, tmp_path):
        calls = []
        def caller(system, user):
            calls.append(user)
            return "5"
        cache = tmp_path / "cache.json"
        e = AnswerEngine(profile, cache, caller=caller)
        q = "How many years of experience do you have with Kubernetes?"
        assert e.answer(q, "numeric") == "5"
        assert e.answer(q, "numeric") == "5"        # second call → cache
        assert len(calls) == 1                      # LLM hit exactly once
        assert e.cache_hits == 1 and e.llm_calls == 1
        assert cache.exists()                       # persisted

        # A fresh engine reuses the on-disk cache without calling the LLM.
        e2 = AnswerEngine(profile, cache, caller=caller)
        assert e2.answer(q, "numeric") == "5"
        assert len(calls) == 1

    def test_llm_answer_mapped_to_option(self, profile, tmp_path):
        def caller(system, user):
            return "Yes, definitely"
        e = AnswerEngine(profile, tmp_path / "c.json", caller=caller)
        # Novel question (not deterministic) with options → mapped onto an option.
        ans = e.answer("Are you comfortable working night shifts?", "select", ["Yes", "No"])
        assert ans == "Yes"

    def test_no_provider_raises(self, profile, tmp_path, monkeypatch):
        # caller=None forces auto-detect; with no keys it must fail loudly
        # (a missing provider is a setup error, not a per-field fallback).
        monkeypatch.setattr("pipeline.batch_evaluate._detect_provider", lambda: None)
        e = AnswerEngine(profile, tmp_path / "c.json", caller=None)
        with pytest.raises(RuntimeError, match="no LLM provider"):
            e.answer("Describe your ideal team.", "textarea")

    def test_llm_failure_falls_back_and_flags(self, profile, tmp_path):
        # A configured provider that fails the call (after retries) must NOT skip
        # the job — fall back to a best-effort placeholder and flag for review.
        def boom(system, user):
            raise RuntimeError("provider returned empty content")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=boom)
        assert e.answer("Tell us about a project you led.", "textarea") == ""   # blank free text
        assert e.answer("Years of experience with Go?", "numeric") == ""        # blank, never a fake "0"
        assert e.answer("Are you willing to relocate to Mars?",
                        "select", ["Yes", "No", "Prefer not to say"]) == "Prefer not to say"
        assert e.unanswered == [
            "Tell us about a project you led.",
            "Years of experience with Go?",
            "Are you willing to relocate to Mars?",
        ]


class TestProfessionalFilename:
    """Uploaded files are presented to recruiters under the candidate's name,
    not the company-keyed internal filename (which looks mail-merged)."""

    def test_candidate_named(self):
        from pipeline.apply.linkedin import _professional_filename
        assert _professional_filename("Thomas Thirlwall", "Cover Letter",
                                      Path("Parloa - cover.pdf")) == "Thomas Thirlwall - Cover Letter.pdf"
        assert _professional_filename("Thomas Thirlwall", "Resume",
                                      Path("resume.pdf")) == "Thomas Thirlwall - Resume.pdf"

    def test_falls_back_without_name(self):
        from pipeline.apply.linkedin import _professional_filename
        assert _professional_filename("", "Resume", Path("resume.pdf")) == "resume.pdf"

    def test_sanitizes_illegal_chars(self):
        from pipeline.apply.linkedin import _professional_filename
        assert _professional_filename("Tom/Slash", "Resume",
                                      Path("r.pdf")) == "Tom Slash - Resume.pdf"


class TestConsentSetDetection:
    """A bundled checkbox 'group' that's really independent consents must be split
    back into per-box yes/no handling, not offered to answer_multi as menu items."""

    def test_two_consents_is_a_consent_set(self):
        from pipeline.apply.linkedin import _looks_like_consent_set
        assert _looks_like_consent_set(
            ["I agree to the Terms of Service", "I consent to a background check"]) is True

    def test_option_values_are_not_a_consent_set(self):
        from pipeline.apply.linkedin import _looks_like_consent_set
        assert _looks_like_consent_set(["1 Week", "2 Weeks", "3 Months"]) is False
        assert _looks_like_consent_set(
            ["Automation workflows", "AI/LLM-powered internal tools"]) is False

    def test_long_single_choice_is_not_a_consent_set(self):
        # A single-choice question with long labels (one containing "agree to") must
        # NOT be split into independent consents — it goes to answer_multi (picks one),
        # never auto-checks the affirmative pole as a commitment never made.
        from pipeline.apply.linkedin import _looks_like_consent_set
        assert _looks_like_consent_set([
            "Yes, I agree to relocate to the Dallas-Fort Worth metroplex within 30 days of an offer",
            "No, I am not able to relocate and would require a fully remote arrangement"]) is False


class TestAnswerMulti:
    """Checkbox-group answering: one option for single-choice, several for
    multi-select, none when nothing applies — the model decides how many."""

    def test_single_choice_returns_one(self, profile, tmp_path):
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "2 Weeks")
        assert e.answer_multi("What is your notice period?",
                              ["1 Week", "2 Weeks", "3 Weeks", "2 Months"]) == ["2 Weeks"]

    def test_multi_select_returns_several(self, profile, tmp_path):
        def caller(s, u):
            return "Automation workflows\nAI/LLM-powered internal tools"
        e = AnswerEngine(profile, tmp_path / "c.json", caller=caller)
        opts = ["Internal tools", "Automation workflows", "Prototypes",
                "AI/LLM-powered internal tools", "None of the above"]
        assert e.answer_multi("Which have you built?", opts) == [
            "Automation workflows", "AI/LLM-powered internal tools"]

    def test_none_returns_empty(self, profile, tmp_path):
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "none")
        assert e.answer_multi("Which apply?", ["A", "B"]) == []

    def test_eeo_group_never_answered(self, profile, tmp_path):
        def boom(s, u):
            raise AssertionError("must not call LLM for demographics")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=boom)
        assert e.answer_multi("Gender", ["Male", "Female"]) == []

    def test_unmatched_line_adds_nothing(self, profile, tmp_path):
        # A garbage reply must NOT add a spurious checked box (strict match).
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "something off-list")
        assert e.answer_multi("Which apply?", ["A", "B"]) == []


class TestAnswerHelpers:
    def test_sanitize_normalizes(self):
        assert _sanitize("  How  many YEARS?? ") == "how many years"

    def test_match_option_fuzzy(self):
        opts = ["Yes", "No", "Prefer not to say"]
        assert _match_option("yes", opts) == "Yes"
        assert _match_option("I prefer not to say", opts) == "Prefer not to say"
        assert _match_option("totally unrelated", opts) == "Yes"  # falls back to first

    def test_match_option_punctuation_skills(self):
        # #11: options ending in non-word chars must still match (\b fails after
        # '+'/'#'). 'java' must NOT swallow 'javascript'.
        assert _match_option("I use C++ daily", ["Java", "C++"]) == "C++"
        assert _match_option("C#", ["C#", "F#"]) == "C#"
        assert _match_option("javascript", ["Java", "JavaScript"]) == "JavaScript"


# ── queue.py ─────────────────────────────────────────────────────────────────

_TRACKER = """\
# Applications Tracker

| # | Date | Company | Role | Score | Status | PDF | Report | Notes |
|---|------|---------|------|-------|--------|-----|--------|-------|
| 1 | 2026-06-01 | Acme | Engineer | 4.2/5 | Evaluated | ❌ | [001](reports/001.md) | https://www.linkedin.com/jobs/view/123 — strong fit |
| 2 | 2026-06-01 | Globex | Dev | 4.5/5 | Evaluated | ❌ | [002](reports/002.md) | https://boards.greenhouse.io/x/jobs/9 — offsite ATS |
| 3 | 2026-06-01 | Initech | SRE | 4.9/5 | Applied | ❌ | [003](reports/003.md) | https://www.linkedin.com/jobs/view/456 |
| 4 | 2026-06-01 | Umbrella | QA | 2.0/5 | Evaluated | ❌ | [004](reports/004.md) | https://www.linkedin.com/jobs/view/789 |
| 5 | 2026-06-01 | Hooli | Backend | 4.8/5 | Evaluated | ❌ | [005](reports/005.md) | https://www.linkedin.com/jobs/view/999 |
"""


class TestQueueSelect:
    def _career_ops(self, tmp_path: Path) -> Path:
        d = tmp_path / "data"
        d.mkdir(parents=True)
        (d / "applications.md").write_text(_TRACKER, encoding="utf-8")
        return tmp_path

    def test_selects_only_eligible_linkedin_evaluated(self, tmp_path):
        co = self._career_ops(tmp_path)
        jobs = queue.select(co, min_score=4.0, linkedin_only=True)
        nums = [j.num for j in jobs]
        # #1 Acme + #5 Hooli (linkedin, Evaluated, score>=4). #2 non-LI, #3 Applied,
        # #4 below score → all excluded.
        assert nums == ["5", "1"]            # sorted by score desc (4.8, 4.2)
        assert all("linkedin.com/jobs/view" in j.url for j in jobs)

    def test_limit(self, tmp_path):
        co = self._career_ops(tmp_path)
        assert len(queue.select(co, min_score=4.0, limit=1)) == 1

    def test_linkedin_only_false_includes_other_ats(self, tmp_path):
        co = self._career_ops(tmp_path)
        jobs = queue.select(co, min_score=4.0, linkedin_only=False)
        assert "2" in [j.num for j in jobs]  # Globex greenhouse now included

    def test_missing_tracker_returns_empty(self, tmp_path):
        assert queue.select(tmp_path, min_score=4.0) == []

    def test_explicit_applications_md_override(self, tmp_path):
        """applications_md points the queue at a refreshed artifact's tracker,
        not the (possibly absent/stale) local one."""
        co = tmp_path / "career-ops"
        co.mkdir()  # deliberately no data/applications.md here
        art = tmp_path / "artifact" / "data"
        art.mkdir(parents=True)
        (art / "applications.md").write_text(_TRACKER, encoding="utf-8")
        jobs = queue.select(co, min_score=4.0, applications_md=art / "applications.md")
        assert [j.num for j in jobs] == ["5", "1"]
        # Without the override, the empty local career-ops yields nothing.
        assert queue.select(co, min_score=4.0) == []


class TestQueueHelpers:
    def test_is_linkedin_job(self):
        assert queue.is_linkedin_job("https://www.linkedin.com/jobs/view/123") is True
        assert queue.is_linkedin_job("https://linkedin.com/jobs/view/9?x=1") is True
        assert queue.is_linkedin_job("https://boards.greenhouse.io/x") is False
        assert queue.is_linkedin_job("not a url") is False

    def test_extract_url_strips_trailing_punctuation(self):
        # extract_url now lives in data.py (shared by the apply queue + recheck).
        assert app_data.extract_url("see https://x.com/a/b, fits") == "https://x.com/a/b"


# ── linkedin.py pure helpers (no browser) ────────────────────────────────────

class TestOptoutCheckbox:
    def test_detects_follow_and_marketing(self):
        for lbl in ("Follow Apexon to stay up to date with their page.",
                    "Subscribe to our newsletter",
                    "Receive marketing emails",
                    "Opt-in to promotional updates"):
            assert linkedin._is_optout_checkbox(lbl) is True

    def test_normal_consent_not_optout(self):
        assert linkedin._is_optout_checkbox("I agree to the terms and conditions") is False
        assert linkedin._is_optout_checkbox("I certify the information is accurate") is False


class TestResumeResolution:
    def test_prefers_resume_path_env(self, tmp_path, monkeypatch):
        pdf = tmp_path / "my_resume.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        monkeypatch.setenv("RESUME_PATH", str(pdf))
        assert linkedin._resume_pdf() == pdf

    def test_coerces_txt_path_to_pdf(self, tmp_path, monkeypatch):
        pdf = tmp_path / "r.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        monkeypatch.setenv("RESUME_PATH", str(tmp_path / "r.txt"))  # points at .txt sibling
        assert linkedin._resume_pdf() == pdf

    def test_falls_back_when_resume_path_missing(self, monkeypatch):
        monkeypatch.setenv("RESUME_PATH", str(Path("/nonexistent/x.pdf")))
        r = linkedin._resume_pdf()
        # Either the repo's default resumes/resume.pdf, or None if absent.
        assert r is None or r.name == "resume.pdf"


class TestMarkApplied:
    """An auto-submitted role must surface as Applied in BOTH the tracker copy
    the run used and the UI's pending-status channel (which the Push button
    carries to the cloud) — the tracker copy alone is invisible when it's the
    downloaded artifact. The override carries the company/role identity so the
    Push marks the RIGHT cloud row even when local/cloud numbering diverged."""

    _TRACKER = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 7 | 2026-06-01 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [007](reports/007.md) | x |\n"
    )

    def _job(self, num, company="Acme", role="Eng"):
        return queue.ApplyJob(num=num, company=company, role=role,
                              url="https://www.linkedin.com/jobs/view/7", score=4.2)

    def test_marks_tracker_and_records_override(self, tmp_path):
        import json
        from pipeline.app import data as app_data
        apps = tmp_path / "applications.md"
        apps.write_text(self._TRACKER, encoding="utf-8")
        apply_pkg._mark_applied(apps, self._job("7"))
        assert "| Applied |" in apps.read_text(encoding="utf-8")
        ov = json.loads(app_data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
        assert ov == {"7": {"status": "Applied", "company": "Acme", "role": "Eng"}}

    def test_override_recorded_even_when_tracker_missing(self, tmp_path):
        import json
        from pipeline.app import data as app_data
        apply_pkg._mark_applied(tmp_path / "absent.md", self._job("7"))
        ov = json.loads(app_data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
        assert ov == {"7": {"status": "Applied", "company": "Acme", "role": "Eng"}}

    def test_no_num_is_noop(self, tmp_path):
        from pipeline.app import data as app_data
        apply_pkg._mark_applied(tmp_path / "absent.md", self._job(""))
        assert not app_data.STATUS_OVERRIDES_FILE.exists()


class TestMarkClosedDiscarded:
    """A posting no longer accepting applications (EXPIRED) marks the role
    Discarded in the tracker + override channel — the apply-time sibling of
    submit -> Applied. A generic failure marks nothing."""

    _TRACKER = (
        "# Applications Tracker\n\n"
        "| # | Date | Company | Role | Score | Status | PDF | Report | Notes |\n"
        "|---|------|---------|------|-------|--------|-----|--------|-------|\n"
        "| 7 | 2026-06-01 | Acme | Eng | 4.2/5 | Evaluated | ❌ | [007](reports/007.md) | x |\n"
    )

    def _job(self, num="7", company="Acme", role="Eng"):
        return queue.ApplyJob(num=num, company=company, role=role,
                              url="https://www.linkedin.com/jobs/view/7", score=4.2)

    def test_report_marks_expired_role_discarded(self, tmp_path):
        import json
        from pipeline.app import data as app_data
        apps = tmp_path / "applications.md"
        apps.write_text(self._TRACKER, encoding="utf-8")
        apply_pkg._report(self._job(), result.ApplyResult(code=result.EXPIRED, reason="closed"),
                          "review", apps, 0, 0, 0)
        assert "| Discarded |" in apps.read_text(encoding="utf-8")
        ov = json.loads(app_data.STATUS_OVERRIDES_FILE.read_text(encoding="utf-8"))
        assert ov == {"7": {"status": "Discarded", "company": "Acme", "role": "Eng"}}

    def test_generic_failure_does_not_mark(self, tmp_path):
        from pipeline.app import data as app_data
        apps = tmp_path / "applications.md"
        apps.write_text(self._TRACKER, encoding="utf-8")
        apply_pkg._report(self._job(), result.failed("modal_did_not_open"),
                          "review", apps, 0, 0, 0)
        assert "| Evaluated |" in apps.read_text(encoding="utf-8")   # unchanged
        assert not app_data.STATUS_OVERRIDES_FILE.exists()


class TestTailoredResume:
    def _job(self, company):
        return queue.ApplyJob(num="1", company=company, role="Eng",
                              url="https://www.linkedin.com/jobs/view/1", score=4.5)

    def test_finds_by_company_slug(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "CV - Apexon Inc.pdf").write_bytes(b"%PDF-1.4")
        (out / "CV - Globex.pdf").write_bytes(b"%PDF-1.4")
        found = apply_pkg._find_tailored_resume(tmp_path, self._job("Apexon"))
        assert found is not None and found.name == "CV - Apexon Inc.pdf"

    def test_none_when_no_company_match(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "CV - Globex.pdf").write_bytes(b"%PDF-1.4")
        assert apply_pkg._find_tailored_resume(tmp_path, self._job("Apexon")) is None

    def test_none_when_dir_absent(self, tmp_path):
        assert apply_pkg._find_tailored_resume(tmp_path, self._job("Apexon")) is None

    def test_skips_cover_letter_pdf(self, tmp_path):
        # A cover letter shares the company slug ("Parloa - cover.pdf") but must
        # not be uploaded as the resume.
        out = tmp_path / "output"
        out.mkdir()
        (out / "Parloa - cover.pdf").write_bytes(b"%PDF cover")
        assert apply_pkg._find_tailored_resume(tmp_path, self._job("Parloa")) is None

    def test_finds_resume_alongside_cover(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "Parloa - cover.pdf").write_bytes(b"%PDF cover")
        (out / "Parloa - resume.pdf").write_bytes(b"%PDF resume")
        found = apply_pkg._find_tailored_resume(tmp_path, self._job("Parloa"))
        assert found is not None and "resume" in found.name.lower()

    def test_company_containing_cover_substring_not_excluded(self, tmp_path):
        # #8: "Discovery"/"Recover" contain "cover" as a substring — the exclusion
        # is whole-word, so their real tailored resumes are still found.
        out = tmp_path / "output"
        out.mkdir()
        (out / "CV - Discovery.pdf").write_bytes(b"%PDF-1.4")
        found = apply_pkg._find_tailored_resume(tmp_path, self._job("Discovery"))
        assert found is not None and found.name == "CV - Discovery.pdf"


class TestCoverLetterLazy:
    """The cover letter is generated only on first request (a form that asks for
    one), then cached — so we never generate for a form that doesn't."""

    def test_generated_once_then_cached(self, profile, tmp_path):
        calls = []
        def gen():
            calls.append(1)
            return "Dear team, I build APIs.\nThomas"
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        e.cover_letter_provider = gen
        assert e.cover_letter().startswith("Dear team")
        assert e.cover_letter().startswith("Dear team")
        assert len(calls) == 1  # provider invoked exactly once

    def test_no_provider_returns_empty(self, profile, tmp_path):
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        assert e.cover_letter() == ""

    def test_provider_failure_returns_empty(self, profile, tmp_path):
        def boom():
            raise RuntimeError("provider down")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        e.cover_letter_provider = boom
        assert e.cover_letter() == ""

    def test_cover_letter_pdf_needs_letter_then_renders(self, profile, tmp_path):
        pdf = tmp_path / "cover.pdf"
        pdf.write_bytes(b"%PDF")
        e = AnswerEngine(profile, tmp_path / "c.json", caller=lambda s, u: "x")
        # No letter yet → no PDF (request-gated, never renders for a form that
        # doesn't ask).
        assert e.cover_letter_pdf() is None
        # Once the letter is available and a pdf provider is set, returns the path.
        e.cover_letter_provider = lambda: "Dear team"
        e.cover_pdf_provider = lambda: pdf
        assert e.cover_letter_pdf() == pdf
