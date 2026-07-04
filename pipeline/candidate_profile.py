"""Candidate profile loader.

Loads the contact fields of `career-ops/config/profile.yml` into a small typed
struct for resume tailoring and cover letters (the only remaining consumers).
profile.yml itself stays rich — onboarding writes the full work-auth/EEO/
compensation model there for career-ops and the user's browser agent to read
directly; this loader deliberately mirrors only what pipeline code consumes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ApplyProfile:
    full_name: str = ""
    email: str = ""
    city: str = ""
    country: str = ""

    @classmethod
    def load(cls, career_ops: Path) -> "ApplyProfile":
        """Read profile.yml under the given career-ops directory.

        Missing file or missing sections degrade gracefully to empty defaults —
        a half-configured profile shouldn't crash tailoring; callers fall back
        to generic phrasing for anything the profile can't supply."""
        path = Path(career_ops) / "config" / "profile.yml"
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        cand = data.get("candidate", {}) or {}
        loc = data.get("location", {}) or {}

        return cls(
            full_name=str(cand.get("full_name", "")).strip(),
            email=str(cand.get("email", "")).strip(),
            city=str(cand.get("city") or loc.get("city") or "").strip(),
            country=str(loc.get("country", "")).strip(),
        )
