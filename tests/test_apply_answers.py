"""Contract for the PROFILE.md → apply-answers compiler (Phase 4c).

Turns the living PROFILE.md into the canonical answers dict the deterministic
fill tier consumes. The fixture is the real shape of Thomas's PROFILE.md: an
"Identity & contact" section and a "Standing answers" bullet block.
"""

import pytest

from pipeline.apply_answers import below_comp_floor, compile_answers, salary_answer

PROFILE = """\
# Thomas Thirlwall — candidate profile

## Identity & contact
- **Name:** Thomas Thirlwall
- **Contact:** thomas.thirlwall.dev@gmail.com · +1 (956) 525-3015 · Dallas, TX · linkedin.com/in/thomas-thirlwall · github.com/FrameAutomata

## Positioning
#### Deal-breakers
- **Compensation floor:** $80,000.

## Standing answers
What applications keep asking — answer once here, reuse everywhere:

- **Work authorization:** US Citizen — no sponsorship required
- **Compensation target:** $80K-$200K (minimum $80K)
- **Location:** Dallas, Texas (CST) — Remote (US), or on-site/hybrid within 60 miles of Dallas, TX
- **Gender:** Male
- **Race / ethnicity:** Hispanic
- **Veteran status:** I am not a protected veteran
- **Disability status:** Yes, I have a disability (or previously had one)

## Tailoring rules
- one page.
"""


@pytest.fixture
def answers():
    return compile_answers(PROFILE, resume_path=r"C:\resumes\Thomas_Standard.pdf")


# ── identity ─────────────────────────────────────────────────────────────────

def test_name_splits_first_and_last(answers):
    assert answers["first_name"] == "Thomas"
    assert answers["last_name"] == "Thirlwall"


def test_email_and_phone_from_contact_line(answers):
    assert answers["email"] == "thomas.thirlwall.dev@gmail.com"
    assert answers["phone"] == "+1 (956) 525-3015"


# ── standing answers ─────────────────────────────────────────────────────────

def test_work_authorization_yes(answers):
    # "US Citizen — no sponsorship required" → has the legal right to work
    assert answers["work_authorization"] == "Yes"


def test_sponsorship_no(answers):
    assert answers["sponsorship"] == "No"


def test_location_city_is_city_state(answers):
    # the location typeahead wants "Dallas, Texas", not the whole policy sentence
    assert answers["location_city"] == "Dallas, Texas"


def test_eeo_answers(answers):
    assert answers["gender"] == "Male"
    assert answers["race"] == "Hispanic"
    assert answers["veteran_status"] == "I am not a protected veteran"
    assert answers["disability_status"].startswith("Yes")


# ── runtime passthrough ──────────────────────────────────────────────────────

def test_referral_and_resume(answers):
    assert answers["referral_source"] == "LinkedIn"
    assert answers["resume"] == r"C:\resumes\Thomas_Standard.pdf"


def test_resume_absent_when_not_given():
    assert "resume" not in compile_answers(PROFILE)


# ── salary: posting-band midpoint, else "Negotiable" ─────────────────────────

def test_salary_defaults_to_negotiable_without_a_posting_range(answers):
    # no JD range → never guess a number → "Negotiable" (free-text fields take
    # it; a numeric-only field rejects it, goes [invalid], and escalates)
    assert answers["salary_expectation"] == "Negotiable"


def test_salary_uses_midpoint_of_posting_band():
    # the company's own disclosed band is the best "what they pay for this
    # role" data — answer its midpoint
    a = compile_answers(PROFILE, jd_salary_range=(150000, 200000))
    assert a["salary_expectation"] == "175000"


def test_salary_rule_edges():
    assert salary_answer(80000, None) == "Negotiable"
    assert salary_answer(80000, (150000, 200000)) == "175000"  # midpoint
    assert salary_answer(80000, (70000, 100000)) == "85000"    # midpoint ≥ floor
    assert salary_answer(80000, (75000, 84000)) == "80000"     # clamp up to floor


def test_below_floor_band_is_a_role_skip_not_an_answer():
    # the comp floor set at setup is a deal-breaker: the driver must skip the
    # role entirely (skip:below-comp-floor), never apply to it
    assert below_comp_floor(80000, (60000, 70000)) is True
    assert below_comp_floor(80000, (75000, 84000)) is False  # band reaches the floor
    assert below_comp_floor(80000, None) is False            # unknown band ≠ skip
    # defense-in-depth: if a below-floor role is answered anyway, the field
    # stays unanswered (escalates) rather than committing an under-floor number
    assert salary_answer(80000, (60000, 70000)) is None


# ── robustness ───────────────────────────────────────────────────────────────

def test_missing_standing_answers_section_yields_identity_only():
    minimal = "## Identity & contact\n- **Name:** Ada Lovelace\n- **Contact:** ada@x.io · +1 (555) 000-1111\n"
    a = compile_answers(minimal)
    assert a["first_name"] == "Ada" and a["last_name"] == "Lovelace"
    assert a["email"] == "ada@x.io"
    assert "work_authorization" not in a  # absent, not guessed


def test_only_canonical_string_values():
    # every value the planner might type must be a plain string
    a = compile_answers(PROFILE, resume_path="r.pdf", jd_salary_range=(150000, 200000))
    assert all(isinstance(v, str) for v in a.values())
