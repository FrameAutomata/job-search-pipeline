"""Candidate profile for the apply engine.

Loads `career-ops/config/profile.yml` (the Phase 1 work-authorization model
plus contact/compensation) into a typed struct the answer engine uses to fill
forms and answer screening questions. All personal data lives in the profile —
nothing here is hardcoded to a specific candidate or country."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _parse_salary(value: str | int | None) -> int | None:
    """Parse "$75K", "75,000", "$130K-170K" (takes the first number) → int.
    Returns None when there's no parseable number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.search(r"(\d[\d,\.]*)\s*([kKmM]?)", str(value))
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    suffix = m.group(2).lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


def _parse_salary_target(value: str | int | None) -> int | None:
    """A figure to STATE as the expectation — the midpoint of a target range
    ("$130K-$170K" → 150000), or the single value if there's only one. Distinct
    from the walk-away minimum, which we never reveal on an application."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    nums: list[int] = []
    for n, suf in re.findall(r"(\d[\d,\.]*)\s*([kKmM]?)", str(value)):
        x = float(n.replace(",", ""))
        if suf.lower() == "k":
            x *= 1_000
        elif suf.lower() == "m":
            x *= 1_000_000
        nums.append(int(x))
    if not nums:
        return None
    return (nums[0] + nums[1]) // 2 if len(nums) >= 2 else nums[0]


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value:
        return [str(value).strip()]
    return []


def _as_bool(value, default: bool) -> bool:
    """Parse a yaml/string boolean; an unset (None) value takes the default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ApplyProfile:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    linkedin: str = ""
    github: str = ""
    # Work authorization (Phase 1 model)
    citizenship: str = ""
    authorized_regions: list[str] = field(default_factory=list)
    requires_sponsorship: bool = False
    work_permit_type: str = ""
    eligible_countries: list[str] = field(default_factory=list)
    # Compensation. Only the DISCLOSABLE figure lives on the profile: salary_target
    # is what we put in a numeric salary field. The walk-away minimum is
    # deliberately NOT loaded here — keeping it off the answer-facing model means
    # no form-filling path can ever state it (structural "never reveal the floor").
    salary_target: int | None = None
    salary_currency: str = "USD"
    # Voluntary EEO / self-identification. Captured once at setup so the apply
    # engine answers these deterministically (no review-hold). Empty = decline
    # ("prefer not to say"), the default — set a value only to self-identify.
    eeo_gender: str = ""
    eeo_race: str = ""
    eeo_veteran: str = ""
    eeo_disability: str = ""
    # Voluntary self-ID (EEO) consent prefs — platform-agnostic. The data-
    # processing consent some apply forms require to submit defaults on (we
    # provide no demographic data either way); saving/sharing answers default off.
    eeo_data_consent: bool = True
    eeo_save_answers: bool = False
    eeo_share_answers: bool = False

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name else ""

    @property
    def last_name(self) -> str:
        parts = self.full_name.split()
        return parts[-1] if len(parts) > 1 else ""

    @property
    def phone_digits(self) -> str:
        return "".join(c for c in self.phone if c.isdigit())

    @classmethod
    def load(cls, career_ops: Path) -> "ApplyProfile":
        """Read profile.yml under the given career-ops directory.

        Missing file or missing sections degrade gracefully to empty defaults —
        a half-configured profile shouldn't crash the apply stage; the answer
        engine will fall back to the LLM for anything the profile can't supply."""
        path = Path(career_ops) / "config" / "profile.yml"
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        cand = data.get("candidate", {}) or {}
        loc = data.get("location", {}) or {}
        wa = data.get("work_authorization", {}) or {}
        comp = data.get("compensation", {}) or {}
        eeo = data.get("voluntary_disclosures", {}) or {}

        authorized = _as_list(wa.get("legally_authorized_to_work_in"))
        eligible = _as_list(wa.get("eligible_countries")) or authorized

        return cls(
            full_name=str(cand.get("full_name", "")).strip(),
            email=str(cand.get("email", "")).strip(),
            phone=str(cand.get("phone", "")).strip(),
            city=str(cand.get("city") or loc.get("city") or "").strip(),
            country=str(loc.get("country", "")).strip(),
            linkedin=str(cand.get("linkedin", "")).strip(),
            github=str(cand.get("github", "")).strip(),
            citizenship=str(wa.get("citizenship", "")).strip(),
            authorized_regions=authorized,
            requires_sponsorship=bool(wa.get("requires_sponsorship", False)),
            work_permit_type=str(wa.get("work_permit_type", "")).strip(),
            eligible_countries=eligible,
            salary_target=_parse_salary_target(comp.get("target_range") or comp.get("minimum")),
            salary_currency=str(comp.get("currency", "USD")).strip() or "USD",
            eeo_gender=str(eeo.get("gender", "")).strip(),
            eeo_race=str(eeo.get("race_ethnicity", "")).strip(),
            eeo_veteran=str(eeo.get("veteran_status", "")).strip(),
            eeo_disability=str(eeo.get("disability_status", "")).strip(),
            eeo_data_consent=_as_bool(eeo.get("data_processing_consent"), True),
            eeo_save_answers=_as_bool(eeo.get("save_answers"), False),
            eeo_share_answers=_as_bool(eeo.get("share_answers"), False),
        )

    def summary_lines(self) -> list[str]:
        """Human-readable profile block for the LLM answerer's context."""
        lines = [
            f"Name: {self.full_name}",
            f"Email: {self.email}",
            f"Phone: {self.phone}",
            f"Location: {self.city}, {self.country}".strip(", "),
        ]
        if self.linkedin:
            lines.append(f"LinkedIn: {self.linkedin}")
        if self.github:
            lines.append(f"GitHub: {self.github}")
        lines.append(f"Citizenship: {self.citizenship or 'not specified'}")
        lines.append(
            "Authorized to work without sponsorship in: "
            + (", ".join(self.authorized_regions) or "not specified")
        )
        lines.append(f"Requires visa sponsorship: {'yes' if self.requires_sponsorship else 'no'}")
        if self.work_permit_type:
            lines.append(f"Work permit / status: {self.work_permit_type}")
        # Deliberately NOT exposing salary floor/target — applications and cover
        # letters should never reveal the walk-away minimum, and salary fields are
        # handled deterministically ("Negotiable" / target), not via this context.
        return lines
