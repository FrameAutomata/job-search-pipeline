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

from pipeline.batch_evaluate import _PROVIDER_KEYS
# Borrowed from handoff, which reads the same career-ops/config/profile.yml for
# the browser agent's standing answers: how to tolerate a half-written file
# (_load_yaml_or_empty), how to read a sub-section (_dsect), and what a yes/no in
# THIS file means (_as_bool — a quoted "false" is a truthy string). Private names,
# but two readers of one file must not carry two policies for it.
from pipeline.handoff import _as_bool, _dsect, _load_yaml_or_empty
from pipeline.sites import SUPPORTED_SITES, keep_supported, normalize_pass, resolve_sites

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

# What makes a Locations-field chunk a remote pass. One constant because both
# directions of the round-trip test it — build_onboarding_json writing the pass,
# and _location_chunk reading it back — and widening one copy alone would show a
# location the wizard then writes back as non-remote.
_REMOTE_RE = re.compile(r"\bremote\b", re.I)

# provider name (as the UI sends it) -> the secret the workflows read. Derived
# from the eval stage's own key table so a provider added there automatically
# reaches the wizard (this used to be a hand-copy that had to be edited in
# lockstep — the DeepSeek addition proved the drift risk).
PROVIDER_SECRETS = dict(_PROVIDER_KEYS)

# Generated file -> secret name. The first four are required by the workflow;
# PROFILE_MD_B64 is optional but always generated, so we include it.
# ARTICLE_DIGEST_B64 is optional AND only produced when the (best-effort,
# LLM-grounded) article-digest generation succeeded — collect_secret_blobs
# includes it only when the file exists.
SECRET_FILES = {
    "SEARCH_CONFIG_B64": "config/search.yml",
    "RESUME_TXT_B64": "resumes/resume.txt",
    "CV_MD_B64": "career-ops/cv.md",
    "PROFILE_YML_B64": "career-ops/config/profile.yml",
    "PROFILE_MD_B64": "career-ops/modes/_profile.md",
    "ARTICLE_DIGEST_B64": "career-ops/article-digest.md",
}
REQUIRED_SECRETS = ["SEARCH_CONFIG_B64", "RESUME_TXT_B64", "CV_MD_B64", "PROFILE_YML_B64"]

# GitHub caps a repository secret at 48 KB. PROFILE.md is the one append-only,
# agent-grown secret source, so we bound the base64 blob and skip it (the cloud
# degrades to the seed profile) rather than fail the whole onboard when it
# outgrows the cap — which would also strand the provider-key write that follows.
PROFILE_MASTER_MAX_B64 = 45_000

# Sidecar of the last submitted onboarding payload (minus the API key), so a
# second visit to the wizard prefills every field instead of forcing the user
# to re-fill the whole form just to tweak one knob like `results_wanted`.
# Lives under .ui-cache/ — already gitignored. Excludes the API key so the
# file is safe to keep on disk; provider key stays in GitHub Secrets.
#
# It answers "did someone submit THIS WIZARD before", which is not the same
# question as "is this copy configured" — and only the second one may gate
# prefill or the resume requirement. Gating on the sidecar made every copy set
# up another way (`node setup-profile.mjs`, or writing the files by hand) look
# first-time forever: no prefill, and a resume upload nothing could satisfy,
# which walled off every later step including the only UI path to writing a
# local provider key (#145). See derive_form / is_configured below.
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


# ── what this copy is actually configured with ─────────────────────────────
#
# Prefill reads the files a RUN uses — career-ops/config/profile.yml and
# config/search.yml — with the sidecar supplying only what has no home in them.
# Two consequences of that split, both deliberate:
#
#   * The files WIN on every field both carry. They are what the pipeline acts
#     on, so they are what the wizard must show before it offers to overwrite
#     them, and a hand edit made since the last submit is the newer answer.
#   * A field missing or BLANK in the files is not an answer — it is a field the
#     setup route never filled — so it is omitted here and the sidecar keeps it.
#     Emitting "" would win the merge and then render as an empty box, which is
#     the symptom this exists to fix.
#
# The Narrative step (exit story, deal-breakers, flexibility, portfolio) is the
# sidecar's remaining job: setup-profile.mjs renders it into
# career-ops/modes/_profile.md as prose, under headings the file's own banner
# invites the user to rewrite, so there is nothing there to read back reliably.
#
# config/search.yml deliberately, not search.local.yml: the wizard writes and
# ships the shared/cloud config (setup-profile.mjs writes that path, and
# SEARCH_CONFIG_B64 is made from it), and the local override has its own editor.


def _text(value) -> str:
    """A scalar as a trimmed string. Lists join as the CSV the form fields use —
    through _split_csv, which is the same rule read the other way, so the two
    halves of a round-trip can't disagree about dropping blanks."""
    if isinstance(value, (list, tuple)):
        return ", ".join(_split_csv(value))
    if value is None or isinstance(value, (dict, bool)):
        return ""
    return str(value).strip()


def _put(out: dict, key: str, value) -> None:
    """Record a derived field, dropping the blanks. See the note above on why an
    empty file value must not win the merge."""
    text = _text(value)
    if text:
        out[key] = text


# form field -> profile.yml key, per section. A table rather than 22 near-identical
# _put lines so this can be read in one screen against build_onboarding_json above,
# which is the same mapping in the other direction. Only the pairs whose value is
# free text live here; target_roles and the booleans need their own handling below.
_PROFILE_TEXT_FIELDS = {
    "candidate": {"name": "full_name", "email": "email", "phone": "phone",
                  "location": "location", "linkedin": "linkedin",
                  "github": "github", "website": "portfolio_url"},
    "location": {"street": "street", "state": "state", "postal_code": "postal_code"},
    "tailoring": {"tailoring_instructions": "instructions"},
    "compensation": {"comp_target": "target_range", "comp_min": "minimum",
                     "location_flexibility": "location_flexibility"},
    "work_authorization": {"citizenship": "citizenship",
                           "work_permit_type": "work_permit_type",
                           "work_auth_regions": "legally_authorized_to_work_in",
                           "eligible_countries": "eligible_countries"},
    "voluntary_disclosures": {"eeo_gender": "gender", "eeo_race": "race_ethnicity",
                              "eeo_veteran": "veteran_status",
                              "eeo_disability": "disability_status"},
}

# form field -> (section, profile.yml key) for the yes/no answers.
_PROFILE_BOOL_FIELDS = {
    "requires_sponsorship": ("work_authorization", "requires_sponsorship"),
    "data_processing_consent": ("voluntary_disclosures", "data_processing_consent"),
    "save_answers": ("voluntary_disclosures", "save_answers"),
    "share_answers": ("voluntary_disclosures", "share_answers"),
}


def derive_from_profile(profile: dict) -> dict:
    """Wizard fields read back out of career-ops/config/profile.yml."""
    out: dict = {}
    for section, fields in _PROFILE_TEXT_FIELDS.items():
        src = _dsect(profile, section)
        for form_key, yaml_key in fields.items():
            _put(out, form_key, src.get(yaml_key))

    # target_roles.archetypes holds every role the user named, in order and
    # verbatim (`primary` is the same list truncated to two), so it round-trips
    # the form field where `primary` would silently drop roles 3+ on the next save.
    roles = _dsect(profile, "target_roles")
    archetypes = [
        a.get("name") for a in (roles.get("archetypes") or [])
        if isinstance(a, dict) and a.get("name")
    ]
    _put(out, "target_roles", archetypes or roles.get("primary"))

    # Booleans, where BOTH values are answers — so they're keyed on the key being
    # present rather than on the value being non-blank, and serialized the way the
    # form does ("yes"/"no" selects and consent checkboxes). Read through handoff's
    # _as_bool, which already owns what a yes/no in THIS file means: a quoted
    # `requires_sponsorship: "false"` is a truthy Python string, so plain
    # truthiness would prefill "yes" and the next Save would write that inversion
    # back. A value it can't read at all is left to the sidecar rather than guessed.
    for field, (section, key) in _PROFILE_BOOL_FIELDS.items():
        src = _dsect(profile, section)
        if key in src:
            answer = _as_bool(src[key])
            if answer is not None:
                out[field] = "yes" if answer else "no"
    return out


def search_entries(cfg: dict) -> list:
    """The raw per-search mappings in a search config — a `searches:` list, or the
    legacy single `search:` mapping, exactly as pipeline.scrape.load_searches reads
    them. Entries are returned verbatim, including malformed ones; each caller
    applies its own policy (the wizard's prefill skips non-mappings, the UI's save
    endpoint refuses the config outright).

    Here rather than in server.py because `pipeline.scrape` is unimportable from
    the UI venv (jobspy), so ONE app-side mirror of the shape rule is unavoidable
    — two was not. Retiring the legacy `search:` key in one copy and not the other
    would have the save endpoint 400 a config the wizard happily prefills from."""
    if not isinstance(cfg, dict):
        return []
    if "searches" in cfg:
        entries = cfg["searches"]
        return entries if isinstance(entries, list) else []
    single = cfg.get("search")
    return [single] if single is not None else []


def _search_passes(cfg: dict) -> list[dict]:
    """The search passes in `cfg`, normalized so an option counts as set here
    exactly when JobSpy would act on it (`easy_apply: "true"` is a live filter;
    `is_remote: "false"` is not)."""
    return [normalize_pass(e) for e in search_entries(cfg) if isinstance(e, dict)]


def _location_chunk(p: dict) -> str:
    """One pass as the Locations-field chunk that produced it.

    Prefer the pass NAME: setup-profile.mjs writes `name: loc.raw`, the text the
    user typed, so when it still round-trips it is the answer rather than a
    restatement of it — the wizard promises the fields show what's in effect, and
    rewriting someone's "US Remote" to "Remote United States" breaks that promise
    on a config the wizard itself wrote.

    Otherwise rebuild from what the pass searches. A remote pass is spelled
    "Remote <where>" rather than the parenthetical "Remote (<where>)": both
    round-trip a wizard-written config, whose remote passes always carry a
    comma-free country (infer_remote_location), but only the prefix survives a
    hand-written `location: "Dallas, TX"` — parse_locations rejoins "City, ST"
    pairs and "TX)" is not a state code.
    """
    where = _text(p.get("location"))
    name = _text(p.get("name"))
    if name and _rebuild_location(name) == (where, bool(p.get("is_remote"))):
        return name
    if not where:
        return ""
    return where if _REMOTE_RE.search(where) else (
        f"Remote {where}" if p.get("is_remote") else where)


def _rebuild_location(chunk: str) -> tuple[str, bool]:
    """The (location, is_remote) a Locations-field chunk scrapes as — the same
    derivation build_onboarding_json runs, so `_location_chunk` can ask whether a
    pass name still describes the pass rather than assuming it does."""
    if len(parse_locations(chunk)) != 1:
        return ("", False)      # splits into several chunks; not one pass's name
    is_remote = bool(_REMOTE_RE.search(chunk))
    country = infer_country(chunk)
    return (infer_remote_location(country) if is_remote else chunk, is_remote)


def derive_from_search(cfg: dict) -> dict:
    """Wizard fields read back out of config/search.yml."""
    passes = _search_passes(cfg)
    out: dict = {}
    _put(out, "negative_roles", _dsect(cfg, "filter").get("negative_titles"))
    if not passes:
        return out

    chunks: list[str] = []
    for p in passes:
        if p.get("easy_apply"):
            continue        # not a location of its own — it re-uses one below
        chunk = _location_chunk(p)
        if chunk and chunk not in chunks:
            chunks.append(chunk)
    _put(out, "locations", chunks)

    for key in ("results_wanted", "hours_old", "distance"):
        _put(out, key, next((p[key] for p in passes if p.get(key)), None))

    sites = {s.lower() for p in passes for s in resolve_sites(p)[0]}
    if sites:
        out["sites"] = [s for s in SUPPORTED_SITES if s in sites]
    # Both values are answers, and there is a pass list to judge it against.
    out["include_easy_apply"] = any(p.get("easy_apply") for p in passes)
    return out


# Every per-pass key setup-profile.mjs's buildPass writes. A Save rewrites
# `searches:` wholesale from the wizard's own fields, so anything a pass carries
# that is NOT in here is dropped outright.
_PASS_KEYS_WRITTEN = {
    "name", "search_terms", "sites", "results_wanted", "location",
    "country_indeed", "linkedin_fetch_description",
    "easy_apply", "is_remote", "hours_old", "distance",
}


def search_detail_at_risk(path: Path) -> list[str]:
    """What a Save would silently take away from THIS search config.

    Saving has always rewritten `searches:` wholesale from six form fields — and
    that cost nothing while only wizard-written configs ever reached the wizard,
    because rewriting one reproduces it. Inviting hand-written and CLI-written
    configs in (#145) makes the flattening reachable and invisible: someone who
    opens Setup to change `results_wanted` can lose a `job_type` filter, a
    `linkedin_company_ids` list, or a country the wizard re-infers differently.
    So say it, on the Search and Review steps, rather than let Save be the way
    they find out.

    Deliberately precise rather than exhaustive. `search_terms` is rewritten too,
    from Target roles via the generator's own expansion — but predicting whether
    that reproduces the current list means mirroring `expandSearchTerms` from
    setup-profile.mjs in Python, and a warning that fires on every config
    (including every wizard-written one) is wallpaper, not information.
    """
    cfg = _load_yaml_or_empty(path)
    dropped: set[str] = set()
    notes: list[str] = []
    for p in search_entries(cfg):
        if not isinstance(p, dict):
            continue
        dropped |= set(p) - _PASS_KEYS_WRITTEN
        p = normalize_pass(p)
        where = _text(p.get("location"))
        if not where:
            continue
        chunk = _location_chunk(p)
        # The Locations field can't hold every location: parse_locations rejoins
        # only "City, ST" pairs, so "Berlin, Germany" comes back as two passes
        # rather than one, each with its country re-inferred.
        rebuilt, remote = _rebuild_location(chunk)
        if (rebuilt, remote) != (where, bool(p.get("is_remote"))):
            notes.append(f'the location "{where}" (the Locations field would '
                         f'split or re-target it)')
        elif _text(p.get("country_indeed")) not in ("", infer_country(chunk)):
            notes.append(f'country_indeed "{_text(p.get("country_indeed"))}" for '
                         f'"{where}" (it is re-inferred from the location text)')
    return sorted(dropped) + sorted(set(notes))


def derive_form(root: Path, career_ops: Path) -> dict:
    """The wizard form as the files this copy actually runs on describe it.

    Reads through handoff's `_load_yaml_or_empty`, which already tolerates a
    missing / unreadable / malformed profile.yml for the same reason and on the
    same file — one tolerance policy, not two. Prefill is a nicety: a half-written
    config must degrade to "nothing to prefill", never a 500 that costs the user
    the wizard entirely. The wizard says so out loud when that happens, rather
    than showing blank fields under a banner claiming they are current."""
    derived = derive_from_profile(_load_yaml_or_empty(career_ops / "config" / "profile.yml"))
    derived.update(derive_from_search(_load_yaml_or_empty(root / "config" / "search.yml")))
    return derived


def prefill_form(root: Path, career_ops: Path) -> dict:
    """Everything the wizard should render, sidecar under real files."""
    return {**(load_sidecar(root) or {}), **derive_form(root, career_ops)}


def is_configured(root: Path, career_ops: Path) -> bool:
    """Has this copy been set up — by ANY route?

    The question edit mode is really asking. Any of the artifacts a completed
    setup produces answers it, whichever tool produced them.

    config/search.yml is deliberately NOT evidence: setup.sh copies the example
    to it before the user has answered anything, so its existence says only that
    setup ran. It is still read for prefill — a file that exists but says
    nothing about this user is a fine prefill source and a bad gate."""
    return any(
        p.exists() for p in (
            career_ops / "config" / "profile.yml",
            career_ops / "cv.md",
            root / _SIDECAR_NAME,
        )
    )


def resume_on_file(root: Path) -> bool:
    """Is there a resume the wizard can submit without a fresh upload?

    Asked in the order onboard_submit falls back through, so the step-0 gate and
    the submit that follows it can't disagree: the resume.txt a previous submit
    extracted, else an explicit RESUME_PATH or a probed
    resumes/resume.{pdf,docx,odt} (which submit extracts for itself)."""
    from pipeline.resume_text import resolve_resume_path

    return (
        (root / "resumes" / "resume.txt").exists()
        or resolve_resume_path(os.environ.get("RESUME_PATH", ""), root).exists()
    )


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


def portfolio_urls(form: dict) -> list[str]:
    """Portfolio/proof-point URLs from the onboarding form: the `portfolio`
    field (CSV string or list) plus the GitHub URL. Used to fetch README context
    for article-digest generation — kept here so form-field mapping stays in one
    place rather than re-parsed in the HTTP layer."""
    urls = _split_csv(form.get("portfolio"))
    gh_url = (form.get("github") or "").strip()
    if gh_url:
        urls.append(gh_url)
    return urls


# ── form -> --from-json payload ────────────────────────────────────────────

def build_onboarding_json(form: dict, resume_text: str) -> dict:
    """Map the flat web form into the structure setup-profile.mjs --from-json
    expects. Empty fields are left for the node side to default."""
    distance = int(form.get("distance") or 50)
    entries = []
    for raw in parse_locations(form.get("locations", "")):
        is_remote = bool(_REMOTE_RE.search(raw))
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
    # Home country for work-auth defaults — derived from the candidate's own
    # locations rather than assuming the US, so a non-US user gets sane defaults.
    home_country = infer_remote_location(entries[0]["country"])
    return {
        "resumeText": resume_text,
        "info": {
            "name": form.get("name") or "Your Name",
            "email": form.get("email") or "",
            "phone": form.get("phone") or "",
            "location": form.get("location") or "",
            # Full mailing address (optional) for apply forms that require a street
            # (Workday/iCIMS); setup-profile.mjs writes these into location.*.
            "street": form.get("street") or "",
            "state": form.get("state") or "",
            "postalCode": form.get("postal_code") or "",
            # Free-text resume-tailoring guidance -> profile.yml tailoring.instructions.
            "tailoringInstructions": form.get("tailoring_instructions") or "",
            "linkedin": form.get("linkedin") or "",
            "github": form.get("github") or "",
            "portfolio_url": form.get("website") or "",
            "country": home_country,
            # Work authorization (country-neutral — drives the eligibility
            # pre-filter and apply-time screening answers). Blank fields fall
            # back to home-country defaults on the node side.
            "citizenship": form.get("citizenship") or "",
            "workAuthRegions": _split_csv(form.get("work_auth_regions")),
            "requiresSponsorship": str(form.get("requires_sponsorship") or "").lower() == "yes",
            "workPermitType": form.get("work_permit_type") or "",
            "eligibleCountries": _split_csv(form.get("eligible_countries")),
            # Voluntary EEO self-identification — blank = decline on every form.
            "eeoGender": form.get("eeo_gender") or "",
            "eeoRace": form.get("eeo_race") or "",
            "eeoVeteran": form.get("eeo_veteran") or "",
            "eeoDisability": form.get("eeo_disability") or "",
            # Voluntary self-ID (EEO) consent prefs — platform-agnostic. The
            # data-processing consent some forms require to submit defaults agree;
            # saving/sharing answers default off.
            "eeoConsent": str(form.get("data_processing_consent", "yes")).strip().lower() != "no",
            "eeoSaveAnswers": str(form.get("save_answers", "")).strip().lower() == "yes",
            "eeoShareAnswers": str(form.get("share_answers", "")).strip().lower() == "yes",
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
            # Filter, don't just default: a stale saved wizard state (or
            # hand-crafted POST) may still carry retired boards.
            "sites": keep_supported(_split_csv(form.get("sites"))) or list(SUPPORTED_SITES),
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


def extract_resume_text(data: bytes, filename: str) -> str:
    """Extract text from an uploaded resume, dispatching on the upload's filename
    suffix (PDF/DOCX/ODT). Delegates to pipeline.resume_text so the UI and the
    keyword-scoring filter share one extraction implementation. Raises ValueError
    for an unsupported format."""
    from pipeline import resume_text

    return resume_text.extract_resume_bytes(data, filename)


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

    # PROFILE_MASTER_B64: the browser agent's living PROFILE.md, so the cloud
    # evaluator scores against the same master as local eval. Optional and
    # special-cased — it lives at HANDOFF_OUT_DIR (resolve_profile_md), not a
    # repo-relative path, and a user who hasn't grown one yet just ships the seeds.
    from pipeline.handoff import resolve_profile_md
    master = resolve_profile_md()
    if master:
        blob = base64.b64encode(master.encode("utf-8")).decode("ascii")
        if len(blob) <= PROFILE_MASTER_MAX_B64:
            blobs["PROFILE_MASTER_B64"] = blob
        else:
            print(f"[onboard] PROFILE.md too large to sync as a secret "
                  f"({len(blob) // 1024} KB base64 > {PROFILE_MASTER_MAX_B64 // 1024} KB cap) — "
                  f"cloud eval will use the seed profile; trim PROFILE.md to sync it.")
    return blobs
