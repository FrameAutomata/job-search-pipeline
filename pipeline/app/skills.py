"""career-ops skill launchpad: capability detection + a registry of skills a
triaged role can launch.

Each skill runs one of two ways, chosen by the user per action (unless they set
SKILL_PATH_DEFAULT):

  - **API path** — a synchronous provider call that reuses
    ``pipeline.batch_evaluate``'s provider plumbing. Bounded, non-interactive,
    needs only an API key (no agent CLI installed).
  - **CLI path** — we build a ready-to-run command for the user's agent CLI and
    hand it off. We never spawn a tool-wielding agent from this web server.

Only skills whose work fits a single bounded text call expose the API path.
Résumé-markdown tailoring does; **PDF** (needs Playwright to render),
**interview-prep** (needs live WebSearch) and **apply** (needs a live browser)
are CLI-only — the agent does what an API call can't.

Skills read ``cv.md`` + ``config/profile.yml`` from the **local** career-ops
install (the source of truth — these are not in a downloaded artifact) and use
the role's evaluation **report** as the JD signal.
"""

import os
import re
import shutil
import subprocess
import tempfile
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


# ── "Run in terminal" (one-click launch of the CLI hand-off command) ─────────
# This is *not* the agent running inside the web server — we spawn a new
# visible console window from which the user's agent runs. They see what's
# executing, can read its output, and can kill it; we never have a
# tool-wielding agent as a child of the FastAPI process.

def terminal_available() -> bool:
    """Whether we can launch a visible terminal on this OS. Windows uses cmd
    (always present); mac/Linux launchers can be added later."""
    return os.name == "nt"


def launch_in_terminal(command: str, cwd: str) -> dict:
    """Open a new console window and run `command` from `cwd`. The window stays
    open after the command exits so the user can read agent output (it's a
    new top-level window — they close it when done).

    Returns a small dict describing what was launched, for the UI to show."""
    if os.name != "nt":
        raise SkillError(
            "Run-in-terminal is only wired up for Windows so far. Use Copy "
            "command and run it in your own terminal."
        )
    # A tiny .cmd wrapper handles cmd's quoting for us (the verbatim command
    # string already has the right shape for cmd) and keeps the window open via
    # `pause` so prompts and final messages don't disappear.
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"   # UTF-8 console so accented characters render
        f"{command}\r\n"
        "echo.\r\n"
        "echo (You can close this window when done.)\r\n"
        "pause >nul\r\n"
    )
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".cmd", delete=False, encoding="utf-8",
    )
    tf.write(script)
    tf.close()
    # CREATE_NEW_CONSOLE = 0x10. We deliberately don't unlink the script —
    # the child needs it alive to run, and Windows cleans %TEMP% over time.
    subprocess.Popen(
        [tf.name], cwd=cwd, creationflags=0x10, close_fds=True,
    )
    return {"launcher": "cmd", "script": tf.name}


# ── Skill registry ───────────────────────────────────────────────────────────
# Each skill maps to a career-ops mode. `api=True` means the work fits a single
# bounded provider call (we implement a runner below); otherwise it's CLI-only
# because it needs agent tools (browser, live web). `verb` fills the hand-off
# prompt: "use {mode} mode to {verb} for {company} / {role}".
SKILLS: dict[str, dict] = {
    "tailor-resume": {
        "label": "Tailor résumé (Markdown)",
        "mode": "text",
        "verb": "tailor my résumé (markdown output)",
        "api": True,
    },
    "tailor-resume-pdf": {
        "label": "Tailor résumé (PDF)",
        "mode": "pdf",
        "verb": "generate a tailored ATS-optimized PDF résumé",
        "api": False,  # PDF rendering needs the agent (Playwright)
    },
    "interview-prep": {
        "label": "Interview prep",
        "mode": "interview-prep",
        "verb": "prep me for an interview",
        "api": False,  # needs live WebSearch research
    },
    "apply": {
        "label": "Apply assistant",
        "mode": "apply",
        "verb": "help me fill out the application",
        "api": False,  # needs a live browser
    },
}


def capabilities() -> dict:
    provider = detect_provider()
    return {
        "cli": {"available": cli_available(), "name": cli_name()},
        "api": {"available": provider is not None, "provider": provider},
        "terminal": {"available": terminal_available()},
        "default_path": default_path(),
        "skills": [
            {"id": sid, "label": s["label"], "api": s["api"]}
            for sid, s in SKILLS.items()
        ],
    }


def skill_command(skill_id: str, report_path: Path | None,
                  company: str, role: str) -> str:
    """Build the CLI hand-off command for any skill. We never spawn it — the
    user runs it in their terminal where the agent's tools/cost are visible.

    The command `cd`s into career-ops (the UI is launched from the repo root, so
    the relative path is fine) and points at the role's report by **absolute**
    path — the report may live in a downloaded-artifact cache (after a Refresh),
    not under career-ops/reports/, so a career-ops-relative path would miss it.
    Forward slashes work across PowerShell, cmd, and bash."""
    s = SKILLS[skill_id]
    prompt = f"use {s['mode']} mode to {s['verb']} for {company} / {role}"
    if report_path is not None:
        prompt += f" (evaluation report: {report_path.resolve().as_posix()})"
    return f'cd career-ops && {cli_name()} "{prompt}"'


# ── Résumé tailoring (the one API-path skill) ────────────────────────────────

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
