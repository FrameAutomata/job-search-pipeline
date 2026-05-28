"""Onboarding orchestration for the UI.

Turns a web-form payload + an uploaded resume into the four artifacts the cloud
pipeline needs (search.yml, profile.yml, cv.md, _profile.md) and the base64
blobs that become GitHub secrets. Generation itself is delegated to
`setup-profile.mjs --from-json` so there's a single source of truth for how a
profile is built; this module only maps the form, runs node, and reads back the
results. Kept off server.py so the HTTP layer stays declarative.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

# US states (50 + DC) and Canadian provinces — mirror of the sets in
# setup-profile.mjs, used to keep "City, ST" pairs together and infer country.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "ON", "PE", "QC", "SK", "NT", "NU", "YT",
}

# provider name (as the UI sends it) -> the secret the workflows read.
PROVIDER_SECRETS = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Generated file -> secret name. The first four are required by the workflow;
# PROFILE_MD_B64 is optional but always generated, so we include it.
SECRET_FILES = {
    "SEARCH_CONFIG_B64": "config/search.yml",
    "RESUME_TXT_B64": "resumes/resume.txt",
    "CV_MD_B64": "career-ops/cv.md",
    "PROFILE_YML_B64": "career-ops/config/profile.yml",
    "PROFILE_MD_B64": "career-ops/modes/_profile.md",
}
REQUIRED_SECRETS = ["SEARCH_CONFIG_B64", "RESUME_TXT_B64", "CV_MD_B64", "PROFILE_YML_B64"]

# Sidecar of the last submitted onboarding payload (minus the API key), so a
# second visit to the wizard prefills every field instead of forcing the user
# to re-fill the whole form just to tweak one knob like `results_wanted`.
# Lives under .ui-cache/ — already gitignored. Excludes the API key so the
# file is safe to keep on disk; provider key stays in GitHub Secrets.
_SIDECAR_NAME = Path(".ui-cache") / "onboarding.json"


# The generator script lives at the repo root regardless of which working dir
# we run it in (production: the repo; tests: an isolated tmp dir).
_SCRIPT = Path(__file__).resolve().parent.parent.parent / "setup-profile.mjs"


def save_sidecar(root: Path, payload: dict) -> None:
    """Persist the last-submitted onboarding form (minus api_key) so the
    wizard can prefill on its next visit. Never raises — sidecar persistence
    is a UX nicety; failures shouldn't fail the onboarding submission."""
    sidecar = root / _SIDECAR_NAME
    try:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {k: v for k, v in payload.items() if k != "api_key"}
        sidecar.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_sidecar(root: Path) -> dict | None:
    """Return the saved onboarding payload, or None if there isn't one yet
    or the file is unreadable. Read-only — the wizard uses it for prefill."""
    sidecar = root / _SIDECAR_NAME
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


class OnboardError(RuntimeError):
    """Onboarding failed in a way worth surfacing to the user."""


# ── location parsing (ported from setup-profile.mjs) ───────────────────────

def parse_locations(text: str) -> list[str]:
    """Split a comma-separated location string, keeping "City, ST" pairs whole.

    "US Remote, Dallas, TX, Fort Worth, TX" -> ["US Remote", "Dallas, TX", "Fort Worth, TX"]
    """
    if not text:
        return []
    parts = [p.strip() for p in text.split(",") if p.strip()]
    out: list[str] = []
    i = 0
    while i < len(parts):
        cur = parts[i]
        nxt = parts[i + 1] if i + 1 < len(parts) else None
        if nxt and re.fullmatch(r"[A-Z]{2}", nxt):
            out.append(f"{cur}, {nxt}")
            i += 2
        else:
            out.append(cur)
            i += 1
    return out


def infer_country(location: str) -> str:
    """Best-effort JobSpy `country_indeed` from a free-text location."""
    m = re.search(r",\s*([A-Z]{2})\s*$", location)
    if m:
        if m.group(1) in _US_STATES:
            return "USA"
        if m.group(1) in _CA_PROVINCES:
            return "Canada"
    if re.search(r"\b(us|usa|united states|america)\b", location, re.I):
        return "USA"
    if re.search(r"\bcanad(a|ian)\b", location, re.I):
        return "Canada"
    if re.search(r"\b(uk|united kingdom|britain|england|scotland|wales)\b", location, re.I):
        return "UK"
    if re.search(r"\baustralia\b", location, re.I):
        return "Australia"
    return "USA"


def infer_remote_location(country: str) -> str:
    return {
        "Canada": "Canada",
        "UK": "United Kingdom",
        "Australia": "Australia",
    }.get(country, "United States")


def _split_csv(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not value:
        return []
    return [p.strip() for p in str(value).split(",") if p.strip()]


# ── form -> --from-json payload ────────────────────────────────────────────

def build_onboarding_json(form: dict, resume_text: str) -> dict:
    """Map the flat web form into the structure setup-profile.mjs --from-json
    expects. Empty fields are left for the node side to default."""
    distance = int(form.get("distance") or 50)
    entries = []
    for raw in parse_locations(form.get("locations", "")):
        is_remote = bool(re.search(r"\bremote\b", raw, re.I))
        country = infer_country(raw)
        entry = {
            "raw": raw,
            "isRemote": is_remote,
            "country": country,
            "location": infer_remote_location(country) if is_remote else raw,
        }
        if not is_remote:
            entry["distance"] = distance
        entries.append(entry)
    if not entries:
        entries = [{"raw": "United States", "isRemote": True,
                    "location": "United States", "country": "USA"}]

    flexibility_pref = form.get("location_flexibility") or "Remote preferred"
    return {
        "resumeText": resume_text,
        "info": {
            "name": form.get("name") or "Your Name",
            "email": form.get("email") or "",
            "phone": form.get("phone") or "",
            "location": form.get("location") or "",
            "linkedin": form.get("linkedin") or "",
            "github": form.get("github") or "",
            "portfolio_url": form.get("website") or "",
        },
        "criteria": {
            "targetRoles": _split_csv(form.get("target_roles")),
            "negativeRoles": _split_csv(form.get("negative_roles")),
            "compensationTarget": form.get("comp_target") or "$130K-170K",
            "compensationMin": form.get("comp_min") or "$110K",
            "locationFlexibility": flexibility_pref,
        },
        "searchSettings": {
            "locations": entries,
            "hoursOld": int(form.get("hours_old") or 24),
            "resultsWanted": int(form.get("results_wanted") or 100),
            "sites": _split_csv(form.get("sites")) or ["indeed", "linkedin", "glassdoor"],
            "includeEasyApply": bool(form.get("include_easy_apply")),
        },
        "narrative": {
            "exitStory": form.get("exit_story") or "",
            "dealBreakers": _split_csv(form.get("deal_breakers")),
            "locationPolicy": {
                "preferred": flexibility_pref,
                "flexibility": form.get("flexibility") or "Flexible for right opportunity",
            },
            "portfolio": _split_csv(form.get("portfolio")),
        },
    }


# ── resume text extraction ─────────────────────────────────────────────────

def parse_resume_info(text: str) -> dict:
    """Best-effort extraction of contact details from resume text, to autofill
    the onboarding 'About' step. Mirrors setup-profile.mjs's parseResumeInfo.
    Every field may be None; the user reviews/edits before submitting."""
    info = {"name": None, "email": None, "phone": None, "location": None,
            "linkedin": None, "github": None, "website": None}

    m = re.search(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+\.[a-zA-Z0-9_-]+", text)
    if m:
        info["email"] = m.group(0)

    m = re.search(r"\+?1?\s*\(?(\d{3})\)?\s*[-.\s]?(\d{3})[-.\s]?(\d{4})", text)
    if m:
        info["phone"] = f"+1 ({m.group(1)}) {m.group(2)}-{m.group(3)}"

    m = re.search(r"linkedin\.com/in/([a-zA-Z0-9-]+)", text, re.I)
    if m:
        info["linkedin"] = f"linkedin.com/in/{m.group(1)}"

    m = re.search(r"github\.com/([a-zA-Z0-9-]+)", text, re.I)
    if m:
        info["github"] = f"github.com/{m.group(1)}"

    # City, ST (e.g. "Dallas, TX") — the first such pair.
    m = re.search(r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})\b", text)
    if m:
        info["location"] = m.group(1)

    # Website / portfolio: first http(s) URL that isn't linkedin/github.
    for um in re.finditer(r"https?://[^\s|)\]]+", text):
        url = um.group(0).rstrip(".,);")
        if "linkedin.com" not in url.lower() and "github.com" not in url.lower():
            info["website"] = url
            break

    # Name: the first non-empty line, if it looks like a name (short, no @).
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if first_line and len(first_line) < 100 and "@" not in first_line:
        info["name"] = first_line

    return info


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from an uploaded resume PDF using pdfplumber (the same lib
    filter.py uses for keyword scoring)."""
    import pdfplumber

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp = f.name
    try:
        with pdfplumber.open(tmp) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    finally:
        os.unlink(tmp)


# ── generation + collection ────────────────────────────────────────────────

def run_generation(root: Path, payload: dict, timeout: int = 120) -> dict:
    """Write the payload to a temp file and run setup-profile.mjs --from-json.
    Returns the parsed result line ({ok, profileFile, ...})."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            r = subprocess.run(
                ["node", str(_SCRIPT), "--from-json", tmp],
                cwd=str(root), capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError:
            raise OnboardError(
                "node not found. Install Node.js (https://nodejs.org) to generate "
                "your profile."
            )
        except subprocess.TimeoutExpired:
            raise OnboardError(f"profile generation timed out after {timeout}s")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if r.returncode != 0:
        raise OnboardError(
            "profile generation failed: " + ((r.stderr or r.stdout or "").strip() or "unknown error")
        )
    # The script prints a final JSON result line; tolerate extra log lines above it.
    for line in reversed(r.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"ok": True}


def collect_secret_blobs(root: Path) -> dict[str, str]:
    """Read the generated artifacts and return {SECRET_NAME: base64}. Raises if a
    required artifact is missing (generation didn't produce it)."""
    blobs: dict[str, str] = {}
    for secret, rel in SECRET_FILES.items():
        p = root / rel
        if p.exists():
            blobs[secret] = base64.b64encode(p.read_bytes()).decode("ascii")
        elif secret in REQUIRED_SECRETS:
            raise OnboardError(f"expected generated file {rel} was not produced")
    return blobs
