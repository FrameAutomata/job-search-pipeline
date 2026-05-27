"""career-ops skill launchpad: capability detection + the resume-tailoring skill.

A triaged role can run a career-ops skill two ways, chosen by the user per
action (unless they set SKILL_PATH_DEFAULT):

  - **API path** — a synchronous provider call that reuses
    ``pipeline.batch_evaluate``'s provider plumbing. Bounded, non-interactive,
    needs only an API key (no agent CLI installed).
  - **CLI path** — we build a ready-to-run command for the user's agent CLI and
    hand it off. We never spawn a tool-wielding agent from this web server.

Resume tailoring is the first skill. It reads ``cv.md`` + ``config/profile.yml``
from the **local** career-ops install (the source of truth — these are not in a
downloaded artifact) and uses the role's evaluation **report** as the JD signal.
"""

import os
import re
import shutil
from datetime import date
from pathlib import Path

from pipeline import batch_evaluate as be


class SkillError(RuntimeError):
    """A skill couldn't run for a reason worth surfacing to the user."""


# ── Capability detection ─────────────────────────────────────────────────────

def cli_name() -> str:
    """The agent CLI the CLI path would hand off to (BATCH_CLI, default claude)."""
    return (os.environ.get("BATCH_CLI") or "").strip() or "claude"


def cli_available() -> bool:
    return shutil.which(cli_name()) is not None


def detect_provider() -> str | None:
    """The provider the API path would use, or None if no usable key is set.

    Reuses batch_evaluate's detection + validation so the UI and
    ``--evaluate-batch`` always agree on which provider is active."""
    provider = be._detect_provider()
    if not provider:
        return None
    # BATCH_PROVIDER may name a provider whose key isn't actually present.
    return None if be._check_provider(provider) else provider


def default_path() -> str:
    """Preferred path when both are available: 'ask' (let the user pick),
    'cli', or 'api'. Anything unrecognized falls back to 'ask'."""
    v = (os.environ.get("SKILL_PATH_DEFAULT") or "ask").strip().lower()
    return v if v in ("ask", "cli", "api") else "ask"


def capabilities() -> dict:
    provider = detect_provider()
    return {
        "cli": {"available": cli_available(), "name": cli_name()},
        "api": {"available": provider is not None, "provider": provider},
        "default_path": default_path(),
    }


# ── Resume tailoring ─────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").lower())
    return re.sub(r"[\s_]+", "-", s).strip("-") or "x"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _candidate_slug(local: Path) -> str:
    text = _read(local / "config" / "profile.yml")
    m = re.search(r"(?m)^\s*name:\s*[\"']?(.+?)[\"']?\s*$", text)
    return _slug(m.group(1)) if m else "candidate"


# Used only if the local career-ops modes/ aren't available (e.g. the UI was
# pointed at a bare artifact dir). The mode file is the authoritative source.
_FALLBACK_RULES = (
    "Generate a JD-tailored resume as markdown that mirrors the structure of the "
    "candidate's CV: same section headings, same order. Reorder and reword the "
    "candidate's REAL experience to surface JD-relevant content and inject JD "
    "keywords naturally. NEVER invent skills or experience beyond the CV. Output "
    "pure markdown — no tables, no HTML, no code fences."
)


def build_tailor_messages(local: Path, role_context: str) -> tuple[str, str]:
    """Build (system, user) messages for the API tailoring call. The mode rules
    come from the local ``modes/text.md`` so we don't duplicate them here."""
    cv = _read(local / "cv.md")
    if not cv.strip():
        raise SkillError(
            "cv.md not found in your local career-ops — run setup/onboarding "
            "first, or launch the UI from your career-ops clone."
        )
    profile = _read(local / "config" / "profile.yml")
    rules = _read(local / "modes" / "text.md").strip() or _FALLBACK_RULES
    system = (
        "You tailor resumes. Follow these mode rules exactly:\n\n"
        f"{rules}\n\n"
        "=== CANDIDATE CV (source of truth — never claim anything beyond it) ===\n"
        f"{cv}\n\n"
        "=== CANDIDATE PROFILE (identity / contact) ===\n"
        f"{profile}\n"
    )
    user = (
        "Tailor the resume for the role below. The text is the evaluation report "
        "for that role; use its Requirements Map and role summary as the JD "
        "signal.\n\n"
        f"{role_context}\n\n"
        "Output ONLY the tailored resume as markdown — no preamble, no code fences."
    )
    return system, user


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def tailor_resume_markdown(
    local: Path, role_context: str, company: str, provider: str, model: str | None,
) -> Path:
    """Run the API tailoring path. Writes the tailored markdown to the local
    career-ops ``output/`` dir (matching modes/text.md's naming) and returns it."""
    system, user = build_tailor_messages(local, role_context)
    model = model or os.environ.get("BATCH_MODEL") or be.PROVIDER_DEFAULTS[provider]
    caller = be._build_caller(provider, model)
    content = _strip_fences(be._call_with_retry(caller, system, user))
    if not content:
        raise SkillError("The provider returned an empty resume.")
    out = (local / "output" /
           f"cv-{_candidate_slug(local)}-{_slug(company)}-{date.today().isoformat()}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content + "\n", encoding="utf-8")
    return out


def tailor_resume_command(report_rel: str, company: str, role: str) -> str:
    """Build the hand-off command for the CLI path. We never spawn it — the user
    runs it in their own terminal where the agent's tools/cost are visible."""
    prompt = (f"use text mode to tailor my resume for {company} / {role} "
              f"(evaluation report: {report_rel})")
    return f'cd career-ops && {cli_name()} "{prompt}"'
