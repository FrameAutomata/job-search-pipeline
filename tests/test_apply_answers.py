"""Contract for the PROFILE.md → apply-answers compiler (Phase 4c).

Turns the living PROFILE.md into the canonical answers dict the deterministic
fill tier consumes. The fixture is the real shape of Thomas's PROFILE.md: an
"Identity & contact" section and a "Standing answers" bullet block.
"""

import pytest

from pipeline.apply_answers import (
    below_comp_floor,
    compile_answers,
    country_of,
    parse_work_auth,
    resolve_work_auth,
    salary_answer,
)

SEEKER = """\
## Standing answers
- **Work authorization:** On F-1 OPT in the US; will require H-1B sponsorship
"""

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


def test_legal_name_defaults_to_the_name_when_no_preferred(answers):
    assert answers["legal_first_name"] == "Thomas"
    assert answers["legal_last_name"] == "Thirlwall"


def test_preferred_first_name_overrides_first_name_only():
    profile = (
        "## Identity & contact\n"
        "- **Name:** Robert Smith\n"
        "- **Preferred first name:** Bob\n"
        "- **Contact:** bob@x.io · +1 (555) 000-1111\n"
    )
    a = compile_answers(profile)
    assert a["first_name"] == "Bob"          # what he goes by → "First Name" fields
    assert a["legal_first_name"] == "Robert"  # official → "Legal First Name" fields
    assert a["last_name"] == "Smith"


def test_email_and_phone_from_contact_line(answers):
    assert answers["email"] == "thomas.thirlwall.dev@gmail.com"
    assert answers["phone"] == "+1 (956) 525-3015"


def test_linkedin_and_website_from_contact_line(answers):
    assert answers["linkedin"] == "linkedin.com/in/thomas-thirlwall"
    assert answers["github"] == "github.com/FrameAutomata"
    assert answers["website"] == "github.com/FrameAutomata"  # portfolio fallback


def test_contact_urls_optional():
    minimal = "## Identity & contact\n- **Name:** Ada L\n- **Contact:** ada@x.io · +1 (555) 000-1111\n"
    a = compile_answers(minimal)
    assert "linkedin" not in a and "website" not in a


# ── standing answers ─────────────────────────────────────────────────────────

def test_work_auth_is_not_a_static_answer(answers):
    # work-auth is country-dependent, resolved per question by the driver — the
    # static answers dict must NOT carry a blanket work_authorization/sponsorship
    assert "work_authorization" not in answers
    assert "sponsorship" not in answers


# ── work-authorization policy (country-aware, non-US-centric) ────────────────

def test_parse_us_citizen_no_sponsorship():
    p = parse_work_auth(PROFILE)
    assert "US" in p.authorized
    assert p.open_to_sponsorship is False


def test_parse_sponsorship_seeker():
    p = parse_work_auth(SEEKER)
    assert p.open_to_sponsorship is True


def test_country_of_work_auth_question():
    assert country_of("Do you have a legal right to work in the US?") == "US"
    assert country_of("Do you have a legal right to work in the United States?") == "US"
    assert country_of("Are you authorized to work in Canada?") == "Canada"
    assert country_of("What is your favourite colour?") is None


def test_country_of_ignores_mid_sentence_in_clauses():
    # the live-acceptance bug: two "in" clauses; a lowercase "in the future" must
    # not be read as the country — only the trailing "in the United States" is
    label = "Will you now or in the future require immigration sponsorship in the United States?"
    assert country_of(label) == "US"


def test_resolve_authorized_country_is_yes_no_sponsorship():
    a = resolve_work_auth("Do you have a legal right to work in the US?", parse_work_auth(PROFILE))
    assert (a.legal_right, a.sponsorship, a.dealbreaker) == ("Yes", "No", False)


def test_resolve_unauthorized_country_no_sponsorship_is_a_dealbreaker():
    # US-only candidate, Canada question → cannot work there, won't seek it → skip
    a = resolve_work_auth("Legal right to work in Canada?", parse_work_auth(PROFILE))
    assert a.dealbreaker is True


def test_resolve_unauthorized_country_for_a_seeker_answers_truthfully():
    # sponsorship-seeker, a country they aren't yet authorized in → honest No/Yes,
    # NOT a deal-breaker (they want the role)
    a = resolve_work_auth("Legal right to work in Canada?", parse_work_auth(SEEKER))
    assert (a.legal_right, a.sponsorship, a.dealbreaker) == ("No", "Yes", False)


def test_resolve_seeker_in_authorized_country():
    # OPT seeker asked about the US: has current authorization, still needs sponsorship
    a = resolve_work_auth("legal right to work in the US?", parse_work_auth(SEEKER))
    assert (a.legal_right, a.sponsorship, a.dealbreaker) == ("Yes", "Yes", False)


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
