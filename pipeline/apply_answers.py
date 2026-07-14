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
    if auth := standing.get("work authorization"):
        if re.search(r"citizen|authorized|green card|permanent resident", auth, re.I):
            answers["work_authorization"] = "Yes"
        answers["sponsorship"] = "No" if re.search(r"no sponsorship", auth, re.I) else "Yes"
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
