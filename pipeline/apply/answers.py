"""Screening-question answer engine.

Mirrors AIHawk's fast-path strategy: answer from the profile deterministically
when we can, fall back to a persistent on-disk cache (so a question answered
once is free forever), and only call the LLM for genuinely novel free-text. The
LLM caller is the same multi-provider one the batch evaluator uses, so no new
provider plumbing is introduced.

The engine is pure with respect to the browser: it takes a question label, a
field type, and (for choice fields) the options, and returns the string to
type or the option to select. linkedin.py does the DOM work and calls this."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable

from pipeline.apply.profile import ApplyProfile

# A Caller is callable(system, user) -> str — the same contract batch_evaluate
# uses. Injected in tests; lazily built from env in production.
Caller = Callable[[str, str], str]

_CACHE_NAME = "apply-answers.json"

# Cap on the CV/résumé text inlined into the answer prompt, so the context stays
# bounded (a résumé is 1-2 pages; this is generous headroom).
_CV_CONTEXT_MAX = 6000

# Reserved cache key holding a fingerprint of the CV the cached answers were
# grounded in. When cv.md changes, the fingerprint no longer matches and the
# whole cache is discarded (see _load_cache) so stale, pre-edit answers aren't
# replayed. Never collides with a real entry (those are "<field_type>::<q>").
_CV_KEY = "__cv__"


def _cv_fingerprint(cv_text: str) -> str:
    """A short content hash of the CV, stored alongside the cached answers so a
    cv.md edit invalidates everything derived from the old résumé."""
    return hashlib.sha1((cv_text or "").encode("utf-8")).hexdigest()[:16]

# EEO / demographic questions are always declined.
# \w* on prefix tokens so the trailing \b lands at the real word end — without it
# "disab" / "ethnic" never match "disability" / "ethnicity" (the \b fails between
# "disab" and "ility"), so those questions skipped the EEO branch entirely.
_EEO_RE = re.compile(
    r"\b(gender|sex|race|ethnic\w*|hispanic|latin[oax]|veterans?|disab\w*|"
    r"sexual orientation|pronouns?)\b", re.IGNORECASE,
)
_DECLINE = "Prefer not to say"
# One place that recognizes a "decline / prefer not to say" phrasing — used to
# both detect a declining VALUE and identify a declining OPTION. Kept as a single
# constant so the three call sites (decline, EEO answer, polarity match) can't
# drift apart and disagree on what counts as a decline.
_DECLINE_RE = re.compile(
    r"prefer not|decline|rather not|choose not|"
    r"(do not|don.?t|does not|would not|wish not)\s+(wish|want|care|choose)|"
    r"not\s+(to\s+)?(wish|say|answer|disclose|specify|identif)", re.IGNORECASE,
)

# Affirmative consent-style yes/no questions.
_AFFIRM_RE = re.compile(
    r"\b(18 years|over 18|at least 18|legally able|background check|drug (test|screen)|"
    r"agree to|consent to|able to commute|reliably commute)\b", re.IGNORECASE,
)


def _sanitize(text: str) -> str:
    """Normalize a question for cache lookup: lowercase, collapse whitespace,
    drop trailing punctuation. Keeps the cache resilient to minor rewording."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip().lower()


def _cache_key(question: str, field_type: str) -> str:
    return f"{field_type}::{_sanitize(question)}"


# The first number in a string: digits with optional thousands separators and an
# optional decimal part, OR a leading-dot decimal (".5"). The leading-dot branch
# matters — without it ".5" would match the bare "5" and read as 5, not 0.5.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")


def _coerce_number(text: str, whole: bool = False) -> str:
    """The first number in `text` as a bare numeric string, or "" if none.

    Strips thousands separators ("1,200" -> "1200") and normalizes a leading-dot
    decimal (".5" -> "0.5"). With whole=True it floors to an integer ("3.5" ->
    "3") for fields LinkedIn validates as whole numbers — but a positive value
    below 1 becomes "1", never a bogus "0" (which would fail "larger than 0").
    Returns "" rather than fabricating a value when no number is present."""
    m = _NUMBER_RE.search(text or "")
    if not m:
        return ""
    num = m.group(0).replace(",", "")
    if num.startswith("."):
        num = "0" + num
    if whole and "." in num:
        f = float(num)
        num = str(int(f)) if f >= 1 else ("1" if f > 0 else "0")
    return num


# Questions whose answer is a count/duration, so the field wants a NUMBER even
# when LinkedIn renders it as a plain text input (validated numeric only after
# entry). "experience" alone is NOT enough — "Describe your experience" is prose.
_WANTS_NUMBER_RE = re.compile(
    r"\bhow many\b|\bnumber of\b|\byears?\s+of\b|"
    r"\byears?\b.{0,30}\bexperience\b|\bexperience\b.{0,15}\byears?\b|"
    r"\bhow long\b.{0,30}\b(year|month)|"
    r"\brate\b.{0,20}\b\d+\s*(?:to|-|–|—|out of)\s*\d+",
    re.IGNORECASE,
)
# Free-text cues: a question carrying one of these wants prose, even if it also
# contains a numeric phrase ("Describe your years of experience…", "How many …
# and why?") — so we don't reduce a real answer to a bare number.
_FREETEXT_RE = re.compile(
    r"\b(describe|explain|why|elaborate|summari[sz]e|tell us|in your own words)\b",
    re.IGNORECASE,
)


def _wants_number(question: str) -> bool:
    """Whether a question is asking for a number (years of experience, head
    counts, durations) — so a text-typed field still gets a numeric answer. A
    free-text cue (describe/why/explain/…) vetoes it."""
    q = question or ""
    if _FREETEXT_RE.search(q):
        return False
    return bool(_WANTS_NUMBER_RE.search(q))


# A $-anchored salary figure ("$150K", "$150,000", "$150000"). The leading $ is
# REQUIRED — it's what separates real comp from the bare number ranges a report
# is full of (team sizes "50-200", percentiles "top 10-20%", years "5-10",
# headcounts) that a $-less scan wrongly grabbed as salary.
_MONEY = r"\$\s?(\d{2,3}(?:[,\s]?\d{3})*)\s*([kKmM]?)"
_SALARY_RANGE_RE = re.compile(_MONEY + r"\s*(?:[-–—]|to)\s*\$?\s?(\d{2,3}(?:[,\s]?\d{3})*)\s*([kKmM]?)")
_SALARY_SINGLE_RE = re.compile(r"\$\s?(\d{2,3}(?:[,\s]?\d{3})*)\s*([kKmM]?)")
# Lines about the CANDIDATE's own ask, not the role's posted comp — skip them so
# we never read back the walk-away/target (which can equal the floor we hide).
_CANDIDATE_LINE_RE = re.compile(
    r"\b(candidat\w*|seeking|seeks|walk.?away|your (target|minimum|floor)|"
    r"target del candidato)\b", re.IGNORECASE,
)
# A line that's actually about compensation — preferred over an incidental $-range
# elsewhere in the report.
_COMP_KEYWORD_RE = re.compile(
    r"\b(comp|compensation|salary|salaries|base pay|base salary|pay|posted|offer|"
    r"band|range|comp y demanda)\b", re.IGNORECASE,
)


def _to_dollars(digits: str, suffix: str) -> int:
    raw = re.sub(r"[,\s]", "", digits)
    x = float(raw)
    if suffix.lower() == "k":
        x *= 1_000
    elif suffix.lower() == "m":
        x *= 1_000_000
    elif len(raw) <= 3:           # a bare 2-3 digit "$150" in a money context → 150k
        x *= 1_000
    return int(x)


def _comp_from_text(text: str) -> int | None:
    """A salary figure from a single span of text: the midpoint of a $-range, else
    a single $-figure (which must carry a K/M suffix, a comma, or be 5+ digits so a
    stray "$50 fee" isn't read as comp). None if neither is present/plausible."""
    m = _SALARY_RANGE_RE.search(text)
    if m:
        lo_d, lo_s, hi_d, hi_s = m.groups()
        short = lambda d: bool(re.fullmatch(r"\d{2,3}", d))
        # A bare 2-3 digit side shares the OTHER side's K/M suffix ("$150-220K" →
        # both K); a full number ("$220,000") keeps its own scale — never inflated.
        if not lo_s and short(lo_d):
            lo_s = hi_s
        if not hi_s and short(hi_d):
            hi_s = lo_s
        lo, hi = _to_dollars(lo_d, lo_s), _to_dollars(hi_d, hi_s)
        if 30_000 <= lo <= hi <= 1_000_000:
            return (lo + hi) // 2
    ms = _SALARY_SINGLE_RE.search(text)
    if ms and (ms.group(2) or "," in ms.group(1) or len(re.sub(r"\D", "", ms.group(1))) >= 5):
        v = _to_dollars(*ms.groups())
        if 30_000 <= v <= 1_000_000:
            return v
    return None


def salary_from_report(report_text: str) -> int | None:
    """The role's posted comp as researched by career-ops in its evaluation
    report — the midpoint of a $-anchored range, else a single $-figure. Scans
    only $-bearing lines and skips the candidate's own ask, so it returns a
    'publicly known' market figure and never echoes the walk-away floor. A line
    that explicitly mentions comp wins over an incidental $-range elsewhere. None
    if no plausible comp is found."""
    fallback: int | None = None
    for line in (report_text or "").splitlines():
        if "$" not in line:
            continue
        # Drop the candidate's own ask in parentheses ("(candidate seeking $150K)")
        # so the role comp on the SAME line still survives; skip the line entirely
        # only if a candidate-ask phrase remains outside parentheses.
        scan = re.sub(r"\([^)]*\)", " ", line)
        if "$" not in scan or _CANDIDATE_LINE_RE.search(scan):
            continue
        v = _comp_from_text(scan)
        if v is None:
            continue
        if _COMP_KEYWORD_RE.search(scan):   # an explicit comp line wins immediately
            return v
        if fallback is None:
            fallback = v
    return fallback


def thinking_disabled() -> bool:
    """Reasoning/thinking is unnecessary for short application answers and cover
    letters (and slows/garbles vLLM-served reasoning models like MiMo/Qwen3), so
    disable it by default for those use cases. Set APPLY_ENABLE_THINKING=true to
    keep it on (e.g. if a provider rejects the toggle)."""
    return os.environ.get("APPLY_ENABLE_THINKING", "").strip().lower() not in ("1", "true", "yes")


def _contains_token(haystack: str, needle: str) -> bool:
    """True if `needle` appears in `haystack` as a standalone token. Uses
    non-word-char lookarounds instead of \\b so options ending/starting in
    punctuation still match — \\b fails for "C++"/"C#"/".NET" (no word boundary
    after '+'/'#'), which dropped or mis-picked those skill options."""
    if not haystack or not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _match_option(answer: str, options: list[str]) -> str:
    """Map a free-form answer onto one of the allowed options (exact →
    case-insensitive → whole-token containment either direction). Falls back to
    the first option so a select always gets a valid value rather than crashing.

    Containment is matched on token boundaries so a short option like "No" doesn't
    match the "no" inside "I prefer not to say"."""
    if not options:
        return answer
    for opt in options:
        if answer == opt:
            return opt
    al = answer.strip().lower()
    for opt in options:
        if al == opt.strip().lower():
            return opt
    for opt in options:
        ol = opt.strip().lower()
        if _contains_token(al, ol) or _contains_token(ol, al):
            return opt
    return options[0]


def _match_option_strict(answer: str, options: list[str]) -> str | None:
    """Like _match_option but returns None instead of defaulting to the first
    option — for multi-select, where an unmatched line must NOT add a spurious
    checked box."""
    if not options or not answer.strip():
        return None
    a = answer.strip()
    for opt in options:
        if a == opt:
            return opt
    al = a.lower()
    for opt in options:
        if al == opt.strip().lower():
            return opt
    for opt in options:
        ol = opt.strip().lower()
        if _contains_token(al, ol) or _contains_token(ol, al):
            return opt
    return None


def _polarity_is_negative(text: str) -> bool:
    """Whether a yes/no self-ID NEGATES the category. Only counts a negation that
    actually applies to being the category ("not a", "am not", "do not have/identify",
    a leading "no") — NOT an incidental "no longer"/"not currently"/"no active duty"
    in an otherwise affirmative value ("I am a veteran, no longer on active duty")."""
    t = text.lower().strip()
    if re.match(r"no\b(?!\s+(longer|current))", t):              # "No" / "No, ..."
        return True
    return bool(re.search(
        r"\bnot\b(?!\s+(longer|current|recent|presently))|"
        r"\b(do|does|did|am|is|are|was|were|have|has)\s+not\b|"
        r"\b(don|doesn|didn|isn|aren|wasn|weren|haven|hasn|won)'?t\b|"
        r"\bno\s+(disabilit|veteran|histor|impairment|condition)", t))


def _match_polarity(val: str, options: list[str]) -> str | None:
    """For a yes/no self-ID (veteran/disability) whose wording doesn't exactly
    match the form ("I am not a veteran" vs "I am not a protected veteran"), align
    on negation: return the SINGLE non-decline option with the same polarity as the
    stored value. None if ambiguous (more than one candidate) so the caller
    declines rather than guessing."""
    val_neg = _polarity_is_negative(val)
    cand = [o for o in options
            if not _DECLINE_RE.search(o)
            and _polarity_is_negative(o) == val_neg]
    return cand[0] if len(cand) == 1 else None


class AnswerEngine:
    def __init__(
        self,
        profile: ApplyProfile,
        cache_path: Path,
        caller: Caller | None = None,
        job_context: str = "",
        cv_text: str = "",
    ):
        self.profile = profile
        self.cache_path = Path(cache_path)
        self.job_context = job_context
        # The candidate's CV / résumé text (career-ops/cv.md). Included in the LLM
        # Q&A context so experience questions ("years with X", "have you used Y")
        # are answered from real work history — without it the model has no
        # evidence of any skill and answers 0.
        self.cv_text = cv_text or ""
        self._caller = caller          # injected (tests) or lazily built
        self._caller_built = caller is not None
        self.cache: dict[str, str] = self._load_cache()
        self.llm_calls = 0
        self.cache_hits = 0
        # Questions we couldn't answer (LLM unavailable) — surfaced for review so
        # the human completes/verifies them before submitting.
        self.unanswered: list[str] = []
        # The role's market comp midpoint from career-ops's evaluation report,
        # used for numeric salary fields ("publicly known" figure). Set per job.
        self.role_salary_target: int | None = None
        # Tailored cover letter for the current job. Generated/loaded LAZILY —
        # only when a form actually has a cover-letter field — via the provider
        # callback set per job by run(); cached here once obtained.
        self.cover_letter_text: str = ""
        self.cover_letter_provider: Callable[[], str] | None = None
        # Returns a Path to the cover letter rendered as a PDF (for upload-style
        # cover-letter fields), or None. Set per job by run().
        self.cover_pdf_provider: Callable[[], object] | None = None
        # Returns a Path to a per-job TAILORED resume (slot-edited copy of the
        # candidate's own .docx, one-page verified), or None to use the default.
        # Set per job by run() only when the job's score clears the tailor
        # threshold; called lazily when a resume-upload field actually appears.
        self.resume_provider: Callable[[], object] | None = None

    def cover_letter(self) -> str:
        """The tailored cover letter for the current job, generated on first
        request (so we never produce one for a form that doesn't ask). Returns
        "" if none is available / generation failed."""
        if not self.cover_letter_text and self.cover_letter_provider:
            try:
                self.cover_letter_text = self.cover_letter_provider() or ""
            except Exception:
                self.cover_letter_text = ""
        return self.cover_letter_text

    def cover_letter_pdf(self):
        """Path to the cover letter rendered as a PDF (for an upload field), or
        None. Generates the letter text first (request-gated), then renders it."""
        if not self.cover_letter():       # ensure the text exists (writes the .md)
            return None
        if self.cover_pdf_provider is None:
            return None
        try:
            return self.cover_pdf_provider()
        except Exception:
            return None

    # ── public API ──────────────────────────────────────────────────────────

    def answer(self, question: str, field_type: str = "text",
               options: list[str] | None = None) -> str:
        """Return the value to enter for one form field.

        field_type: "text" | "textarea" | "numeric" | "select" | "radio".
        options: the allowed choices for select/radio (else None)."""
        det = self._deterministic(question, field_type, options)
        if det is not None:
            return det

        # A field is numeric when typed as such OR when a TEXT field's question
        # asks for a count/duration — LinkedIn often renders those as plain text
        # and only validates numeric after entry. A textarea is always free-text
        # (never coerced), and choice fields (options) pick an option. Numeric
        # answers are coerced to a bare number; the cache is keyed "numeric" so a
        # text-typed-but-numeric field and a later numeric re-ask share one entry.
        numeric = not options and (
            field_type == "numeric"
            or (field_type == "text" and _wants_number(question))
        )
        eff_type = "numeric" if numeric else field_type

        key = _cache_key(question, eff_type)
        if key in self.cache:
            self.cache_hits += 1
            cached = self.cache[key]
            # Coerce on the hit path too: an entry cached under numeric:: before
            # this fix (or by another field type) may still hold prose.
            if numeric:
                return _coerce_number(cached)
            return _match_option(cached, options) if options else cached

        # A missing provider is a setup error — surface it (raises). A provider
        # that's configured but fails the call (overloaded after retries) is
        # transient: fall back to a best-effort placeholder flagged for review,
        # so the form proceeds rather than the whole job getting skipped.
        self._ensure_caller()
        try:
            raw = self._llm(question, eff_type, options)
        except Exception:
            if question not in self.unanswered:   # a step re-filled after a validation error re-asks
                self.unanswered.append(question)
            return self._fallback(eff_type, options)
        if numeric:
            value = _coerce_number(raw)
            # Don't cache a coercion miss ("") — a later retry should get another
            # shot at the LLM rather than replaying a blank.
            if value:
                self.cache[key] = value
                self._save_cache()
            return value
        value = _match_option(raw, options) if options else raw
        self.cache[key] = value
        self._save_cache()
        return value

    def answer_multi(self, question: str, options: list[str]) -> list[str]:
        """The subset of `options` to check for a checkbox GROUP — one for a
        single-choice question (notice period), several for multi-select (which
        have you built), or none. Lets the model decide how many, instead of
        treating each checkbox as an independent yes/no (which over-checks
        single-choice questions). Cached like other answers."""
        if not options:
            return []
        if _EEO_RE.search(question or ""):
            return []  # never auto-check demographic boxes
        key = _cache_key(question, "multi")
        if key in self.cache:
            self.cache_hits += 1
            return [o for o in options if o in self.cache[key].split("\n")]

        self._ensure_caller()
        system = (
            "You answer a multiple-choice application question for the candidate "
            "below. Reply with ONLY the option(s) that truthfully apply, each on "
            "its own line, copied verbatim. If it is a single-choice question "
            "(e.g. notice period, years of experience, a duration), reply with "
            "EXACTLY ONE. If none apply, reply 'none'. Never invent.\n\n"
            + self._candidate_context()
        )
        user = f"Question: {question}\n\nOptions:\n- " + "\n- ".join(options)
        if self.job_context:
            user += f"\n\nJob: {self.job_context}"
        try:
            raw = self._llm_raw(system, user)
        except Exception:
            return []
        chosen: list[str] = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-*•").strip()
            if not line or line.lower() == "none":
                continue
            m = _match_option_strict(line, options)
            if m and m not in chosen:
                chosen.append(m)
        self.cache[key] = "\n".join(chosen)
        self._save_cache()
        return chosen

    def _fallback(self, field_type: str, options: list[str] | None) -> str:
        """Best-effort value when the LLM is unavailable — never a fabricated
        credential or number. Choice fields decline (blank if there's no decline
        option); numeric and free text are left blank for the human to complete
        in review (a fabricated "0" once submitted a desired salary of $0)."""
        if options:
            return self._decline(options)
        return ""

    # ── deterministic layer ───────────────────────────────────────────────────

    def _deterministic(self, question: str, field_type: str,
                        options: list[str] | None) -> str | None:
        """Answer standard fields straight from the profile. Returns None when
        the question isn't one we recognize (caller falls through to cache/LLM)."""
        q = question.lower()
        p = self.profile

        # EEO / demographics → the candidate's voluntary self-ID from setup, else
        # decline. Captured once so these don't hit the LLM or hold for review.
        if _EEO_RE.search(q):
            return self._eeo_answer(q, options)

        # Work authorization & sponsorship — answer truthfully from the profile.
        # Check authorization BEFORE sponsorship: a question like "authorized to
        # work without sponsorship?" is about authorization, even though it
        # mentions sponsorship. "Authorized/eligible to work" → yes unless the
        # candidate needs sponsorship; a bare "require sponsorship?" → the
        # sponsorship flag directly.
        if re.search(r"\b(authorized|authorisation|authorization|eligible|legally able|"
                     r"right to work|work authorization)\b", q) and "work" in q:
            return self._yes_no("no" if p.requires_sponsorship else "yes", options)
        if "sponsor" in q:
            return self._yes_no("yes" if p.requires_sponsorship else "no", options)
        if "citizen" in q and field_type in ("text", "textarea"):
            return p.citizenship or None

        # Contact / identity fields.
        if "first name" in q or "given name" in q:
            return p.first_name or None
        if "last name" in q or "surname" in q or "family name" in q:
            return p.last_name or None
        if "full name" in q or (q.strip() in ("name", "your name")):
            return p.full_name or None
        if "email" in q:
            return p.email or None
        if re.search(r"\b(phone|mobile|cell|telephone)\b", q):
            return (p.phone_digits if field_type == "numeric" else p.phone) or None
        if "linkedin" in q:
            return p.linkedin or None
        if "github" in q:
            return p.github or None
        if q.strip() in ("city", "current city"):
            return p.city or None
        if q.strip() == "country":
            return p.country or None
        if "location" in q and field_type in ("text", "textarea"):
            return ", ".join(x for x in (p.city, p.country) if x) or None

        # Compensation. NEVER state the walk-away minimum — that hands the
        # employer your floor. Text fields → "Negotiable"; numeric fields (which
        # can't take "Negotiable") → the target figure, not the floor.
        if re.search(r"\b(salary|compensation|expected pay|desired pay|pay expectation|"
                     r"expected compensation|comp expectation)\b", q):
            if field_type == "numeric":
                # The role's researched market comp (from the report) beats a
                # generic profile target; never the walk-away floor.
                target = self.role_salary_target or p.salary_target
                return str(target) if target else None
            return "Negotiable"

        # Affirmative consent.
        if _AFFIRM_RE.search(q):
            return self._yes_no("yes", options)

        return None

    def _yes_no(self, verdict: str, options: list[str] | None) -> str:
        if not options:
            return "Yes" if verdict == "yes" else "No"
        return _match_option("Yes" if verdict == "yes" else "No", options)

    def _eeo_answer(self, q: str, options: list[str] | None) -> str:
        """A voluntary EEO question. Map the candidate's self-ID onto the form's
        options; decline when it's unset or can't be matched confidently. NEVER
        guesses (options[0]) — a wrong veteran/disability pick asserts the OPPOSITE
        of the truth. Binary fields fall back to a polarity match so "I am not a
        veteran" still aligns to the form's "I am not a protected veteran"."""
        p = self.profile
        binary = False
        # Router breadth must match the widened _EEO_RE gate (ethnic\w*, latin[oax])
        # or a question that passed the gate falls through here and wrongly declines.
        if "disab" in q:
            val, binary = p.eeo_disability, True
        elif "veteran" in q:
            val, binary = p.eeo_veteran, True
        elif re.search(r"\b(race|ethnic\w*|hispanic|latin[oax])\b", q):
            val = p.eeo_race
        elif re.search(r"\b(gender|sex)\b", q) or "pronoun" in q:
            val = p.eeo_gender
        else:
            val = ""
        val = val.strip().strip('"').strip("'").strip()   # tolerate quoted setup values
        if not val or _DECLINE_RE.search(val):
            return self._decline(options)
        if not options:
            return val
        matched = _match_option_strict(val, options)
        if not matched and binary:
            matched = _match_polarity(val, options)
        return matched or self._decline(options)

    def _decline(self, options: list[str] | None) -> str:
        if not options:
            return _DECLINE
        # Prefer an explicit decline-style option if one exists.
        for opt in options:
            if _DECLINE_RE.search(opt):
                return opt
        # No decline option: leave it BLANK. Never fall back to options[0] — for a
        # demographic question that would affirm the first value (e.g. "Male"),
        # the exact answer this is meant to avoid. "" makes the fill layer skip.
        return ""

    # ── LLM fallback ───────────────────────────────────────────────────────────

    def _candidate_context(self) -> str:
        """The candidate block for the LLM Q&A: the structured profile PLUS the
        CV/résumé text. Without the CV, experience questions ("years with X",
        "have you used Y") have no work history to answer from and come back 0."""
        block = "CANDIDATE PROFILE:\n" + "\n".join(self.profile.summary_lines())
        cv = self.cv_text.strip()
        if cv:
            block += "\n\nCANDIDATE RESUME / EXPERIENCE:\n" + cv[:_CV_CONTEXT_MAX]
        return block

    def _llm(self, question: str, field_type: str, options: list[str] | None) -> str:
        system = (
            "You fill job-application forms as the candidate described below. "
            "Answer the single question as the candidate would, truthfully and "
            "concisely. Never invent credentials, citizenship, or work "
            "authorization. Never state a specific salary, pay rate, or "
            "compensation figure — if asked about pay, say it is negotiable. "
            "For multiple-choice questions, reply with EXACTLY one "
            "of the provided options and nothing else. For open-ended questions, "
            "reply with 1-3 sentences, no preamble.\n\n"
            + self._candidate_context()
        )
        parts = [f"Question: {question}", f"Field type: {field_type}"]
        if field_type == "numeric":
            parts.append("This field accepts ONLY a number. Reply with a single "
                         "number — digits only, no words, units, ranges, or symbols — "
                         "your best truthful estimate from the candidate's background.")
        if options:
            parts.append("Options (choose exactly one):\n- " + "\n- ".join(options))
        if self.job_context:
            parts.append(f"Job context: {self.job_context}")
        parts.append("Answer:")
        return self._llm_raw(system, "\n\n".join(parts))

    def _llm_raw(self, system: str, user: str) -> str:
        """One LLM call with bounded retry (1,2,4,8,16s → ~31s worst case, so a
        busy provider doesn't appear hung). Caller built/reused from env."""
        self.llm_calls += 1
        from pipeline.batch_evaluate import _call_with_retry
        return _call_with_retry(
            self._ensure_caller(), system, user, max_attempts=6, base_delay=1.0,
        ).strip()

    def _ensure_caller(self) -> Caller:
        if self._caller is not None:
            return self._caller
        # Shared resolver: APPLY_MODEL lets these light tasks use a faster/more-
        # available model than the (possibly heavy) evaluation model in BATCH_MODEL.
        from pipeline.batch_evaluate import resolve_caller
        self._caller = resolve_caller(disable_thinking=thinking_disabled())
        return self._caller

    # ── cache persistence ───────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, str]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Discard the whole cache when the CV it was grounded in has changed —
        # LLM answers depend on cv.md, so a stale cache would silently replay
        # pre-edit answers (e.g. an old "years with X"). Deterministic answers
        # aren't cached, so re-asking the rest costs one LLM call apiece and only
        # on a CV edit. The fingerprint lives under _CV_KEY, stripped from the
        # in-memory map so it's never returned as an answer.
        if data.get(_CV_KEY) != _cv_fingerprint(self.cv_text):
            return {}
        loaded = {str(k): str(v) for k, v in data.items()}
        loaded.pop(_CV_KEY, None)
        return loaded

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(self.cache)
            payload[_CV_KEY] = _cv_fingerprint(self.cv_text)
            self.cache_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8",
            )
        except OSError:
            pass  # cache is a best-effort optimization, never fatal
