"""Compile PROFILE.md into canonical apply answers (Phase 4c).

The deterministic fill tier fills fields from an `answers` dict keyed by the
same answer keys `greenhouse_map` uses (first_name, work_authorization, …).
This module produces that dict from the candidate's living PROFILE.md — the
"Identity & contact" section and the "Standing answers" block — so the CLI has
real values to fill with instead of a hand-written dict.

Salary policy (the low-ball lesson): never guess a number. When the posting
discloses its own band, answer the midpoint (the best "what this company pays
for this role" data there is), clamped up to the candidate's floor; with no
band, answer "Negotiable" — free-text salary fields accept it, and a
numeric-only field rejects it into [invalid], which escalates to the human.
A band entirely below the floor escalates too (the role itself is suspect).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BULLET = re.compile(r"^\s*-\s+\*\*(?P<key>[^:*]+):\*\*\s*(?P<value>.+?)\s*$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_CITY_STATE = re.compile(r"^([A-Za-z .'-]+,\s*[A-Za-z .'-]+?)(?=\s*[(—–-]|$)")
_MONEY = re.compile(r"\$\s*([\d,]+)\s*([kK]?)")


def _section(md: str, heading: str) -> str:
    """The body of a `## heading` section (up to the next ## or EOF), or ''."""
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
                  md, re.M | re.S | re.I)
    return m.group(1) if m else ""


def _bullets(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _BULLET.match(line)
        if m:
            out[m.group("key").strip().casefold()] = m.group("value").strip()
    return out


def _floor(md: str) -> int:
    """The compensation floor in dollars, from any '$NNK'/'$NN,NNN' mention on a
    floor/minimum line; 0 when the profile doesn't state one."""
    for line in md.splitlines():
        if re.search(r"compensation floor|minimum", line, re.I):
            m = _MONEY.search(line)
            if m:
                n = int(m.group(1).replace(",", ""))
                return n * 1000 if m.group(2) else n
    return 0


def below_comp_floor(target_min: int, jd_range: tuple[int, int] | None) -> bool:
    """Role-level gate: a posting whose entire disclosed band sits below the
    candidate's floor is a deal-breaker — the driver skips the role outright
    (skip:below-comp-floor), it is never applied to. An unknown band is not a
    skip."""
    return jd_range is not None and jd_range[1] < target_min


# ── country-aware work authorization ────────────────────────────────────────
#
# "Do you have a legal right to work in <country>?" has no single answer — it
# depends on the country asked about and the candidate. So work-auth is NOT a
# static answer; the driver resolves it per question against this policy.

_US = {"us", "u.s.", "u.s.a.", "usa", "united states", "america", "the us"}


def _norm_country(name: str) -> str:
    return "US" if name.strip().casefold() in _US else name.strip()


@dataclass(frozen=True)
class WorkAuthPolicy:
    authorized: frozenset[str]      # countries the candidate can already work in
    open_to_sponsorship: bool       # will they pursue roles that need sponsorship?


@dataclass(frozen=True)
class WorkAuthAnswer:
    legal_right: str    # "Yes" / "No"
    sponsorship: str    # "No" / "Yes"
    dealbreaker: bool   # can't work there and won't seek sponsorship → skip role


def parse_work_auth(profile_md: str) -> WorkAuthPolicy:
    auth = _bullets(_section(profile_md, "Standing answers")).get("work authorization", "")
    authorized: set[str] = set()
    if re.search(r"\b(u\.?s\.?a?|united states|america|citizen|opt|green card)\b", auth, re.I):
        authorized.add("US")
    for name in re.findall(r"\b(Canada|United Kingdom|UK|Ireland|Australia|Germany|France|"
                           r"Netherlands|Singapore|India|Mexico)\b", auth):
        authorized.add(_norm_country(name))
    open_spons = bool(re.search(r"(require|need|seeking|will need|open to).{0,20}sponsor", auth, re.I))
    if re.search(r"no sponsorship (required|needed)|do not (require|need) sponsor", auth, re.I):
        open_spons = False
    return WorkAuthPolicy(frozenset(authorized), open_spons)


def country_of(label: str) -> str | None:
    """The country a work-authorization question is about, or None if the label
    isn't a work-auth / sponsorship question."""
    if not re.search(r"legal right to work|authoriz\w+ to work|work authoriz|sponsor|eligible to work",
                     label, re.I):
        return None
    # The country is a capitalized proper noun in the LAST "in <country>" clause
    # at the end (a trailing "?"/"*" is tolerated). Requiring a capital keeps a
    # mid-sentence "in the future" from being read as a country — the bug that
    # made a US sponsorship question ("...in the future require...in the United
    # States?") look like a foreign-work-auth deal-breaker.
    m = re.search(r"\bin (?:the )?([A-Z][A-Za-z.]*(?: [A-Z][A-Za-z.]*)*)\s*[?*]*\s*$", label)
    return _norm_country(m.group(1)) if m else None


def resolve_work_auth(label: str, policy: WorkAuthPolicy) -> WorkAuthAnswer | None:
    """The correct answer to a work-auth question, or None if it isn't one."""
    country = country_of(label)
    if country is None:
        return None
    authorized = country in policy.authorized
    return WorkAuthAnswer(
        legal_right="Yes" if authorized else "No",
        sponsorship="No" if (authorized and not policy.open_to_sponsorship) else "Yes",
        dealbreaker=(not authorized) and (not policy.open_to_sponsorship),
    )


def salary_answer(target_min: int, jd_range: tuple[int, int] | None) -> str | None:
    """The salary to offer for a role we ARE applying to: the posting band's
    midpoint (clamped up to the floor), or "Negotiable" when no band is known.
    Below-floor bands are the driver's business (below_comp_floor → skip the
    role); if one reaches here anyway, None keeps the field unanswered so it
    escalates rather than committing a number under the floor."""
    if jd_range is None:
        return "Negotiable"
    if below_comp_floor(target_min, jd_range):
        return None
    low, high = jd_range
    return str(max((low + high) // 2, target_min))


def compile_answers(
    profile_md: str,
    *,
    resume_path: str | None = None,
    referral: str = "LinkedIn",
    jd_salary_range: tuple[int, int] | None = None,
) -> dict[str, str]:
    """Parse PROFILE.md into the canonical answers dict for the fill planner."""
    answers: dict[str, str] = {}

    identity = _bullets(_section(profile_md, "Identity & contact"))
    name = identity.get("name")
    if name:
        first, _, last = name.partition(" ")
        answers["first_name"] = first
        if last:
            answers["last_name"] = last.strip()
    contact = identity.get("contact", "")
    if m := _EMAIL.search(contact):
        answers["email"] = m.group(0)
    if m := _PHONE.search(contact):
        answers["phone"] = m.group(0).strip()

    standing = _bullets(_section(profile_md, "Standing answers"))
    # work_authorization / sponsorship are NOT emitted here — they are
    # country-dependent and resolved per question by the driver (see
    # resolve_work_auth), so a US answer can never land on a foreign question.
    if loc := standing.get("location"):
        if m := _CITY_STATE.match(loc):
            answers["location_city"] = m.group(1).strip()
    if gender := standing.get("gender"):
        answers["gender"] = gender
    if race := standing.get("race / ethnicity") or standing.get("race"):
        answers["race"] = race
    if vet := standing.get("veteran status"):
        answers["veteran_status"] = vet
    if dis := standing.get("disability status"):
        answers["disability_status"] = dis

    answers["referral_source"] = referral
    if resume_path:
        answers["resume"] = resume_path
    if (salary := salary_answer(_floor(profile_md), jd_salary_range)) is not None:
        answers["salary_expectation"] = salary
    return answers
