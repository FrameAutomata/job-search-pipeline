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

# EEO / demographic questions are always declined.
_EEO_RE = re.compile(
    r"\b(gender|sex|race|ethnic|hispanic|latino|veteran|disab|sexual orientation|"
    r"pronoun)\b", re.IGNORECASE,
)
_DECLINE = "Prefer not to say"

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


# A salary range in an evaluation report ("$150-220K", "$150K-$220K",
# "$150,000 to $220,000"). The 30K floor rejects year/headcount ranges.
_SALARY_RANGE_RE = re.compile(
    r"\$?\s?(\d{2,3}(?:,\d{3})?)\s*([kK]?)\s*(?:[-–—]|to)\s*\$?\s?(\d{2,3}(?:,\d{3})?)\s*([kK]?)"
)


def _to_dollars(digits: str, suffix: str) -> int:
    x = float(digits.replace(",", ""))
    if suffix.lower() == "k" or x < 1000:   # "150K" or a bare "150" meaning 150k
        x *= 1000
    return int(x)


def salary_from_report(report_text: str) -> int | None:
    """Midpoint of the role's posted comp range as researched by career-ops in
    its evaluation report (a 'publicly known' figure to state). None if no
    plausible salary range is found."""
    for m in _SALARY_RANGE_RE.finditer(report_text or ""):
        lo = _to_dollars(m.group(1), m.group(2) or m.group(4))
        hi = _to_dollars(m.group(3), m.group(4) or m.group(2))
        if 30_000 <= lo <= hi <= 1_000_000:
            return (lo + hi) // 2
    return None


def thinking_disabled() -> bool:
    """Reasoning/thinking is unnecessary for short application answers and cover
    letters (and slows/garbles vLLM-served reasoning models like MiMo/Qwen3), so
    disable it by default for those use cases. Set APPLY_ENABLE_THINKING=true to
    keep it on (e.g. if a provider rejects the toggle)."""
    return os.environ.get("APPLY_ENABLE_THINKING", "").strip().lower() not in ("1", "true", "yes")


def _match_option(answer: str, options: list[str]) -> str:
    """Map a free-form answer onto one of the allowed options (exact →
    case-insensitive → whole-word containment either direction). Falls back to
    the first option so a select always gets a valid value rather than crashing.

    Containment is matched on word boundaries so a short option like "No" doesn't
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
        if not ol or not al:
            continue
        if re.search(rf"\b{re.escape(ol)}\b", al) or re.search(rf"\b{re.escape(al)}\b", ol):
            return opt
    return options[0]


class AnswerEngine:
    def __init__(
        self,
        profile: ApplyProfile,
        cache_path: Path,
        caller: Caller | None = None,
        job_context: str = "",
    ):
        self.profile = profile
        self.cache_path = Path(cache_path)
        self.job_context = job_context
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

        key = _cache_key(question, field_type)
        if key in self.cache:
            self.cache_hits += 1
            cached = self.cache[key]
            return _match_option(cached, options) if options else cached

        # A missing provider is a setup error — surface it (raises). A provider
        # that's configured but fails the call (overloaded after retries) is
        # transient: fall back to a best-effort placeholder flagged for review,
        # so the form proceeds rather than the whole job getting skipped.
        self._ensure_caller()
        try:
            raw = self._llm(question, field_type, options)
        except Exception:
            self.unanswered.append(question)
            return self._fallback(field_type, options)
        value = _match_option(raw, options) if options else raw
        self.cache[key] = value
        self._save_cache()
        return value

    def _fallback(self, field_type: str, options: list[str] | None) -> str:
        """Best-effort value when the LLM is unavailable — never a fabricated
        credential. Choice fields decline (or take the first option); numeric →
        0; free text is left blank for the human to complete in review."""
        if options:
            return self._decline(options)
        if field_type == "numeric":
            return "0"
        return ""

    # ── deterministic layer ───────────────────────────────────────────────────

    def _deterministic(self, question: str, field_type: str,
                        options: list[str] | None) -> str | None:
        """Answer standard fields straight from the profile. Returns None when
        the question isn't one we recognize (caller falls through to cache/LLM)."""
        q = question.lower()
        p = self.profile

        # EEO / demographics → always decline (map onto an option if a select).
        if _EEO_RE.search(q):
            return self._decline(options)

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

    def _decline(self, options: list[str] | None) -> str:
        if not options:
            return _DECLINE
        # Prefer an explicit decline-style option if one exists.
        for opt in options:
            if re.search(r"prefer not|decline|not wish|don.?t wish", opt, re.IGNORECASE):
                return opt
        return _match_option(_DECLINE, options)

    # ── LLM fallback ───────────────────────────────────────────────────────────

    def _llm(self, question: str, field_type: str, options: list[str] | None) -> str:
        caller = self._ensure_caller()
        system = (
            "You fill job-application forms as the candidate described below. "
            "Answer the single question as the candidate would, truthfully and "
            "concisely. Never invent credentials, citizenship, or work "
            "authorization. For multiple-choice questions, reply with EXACTLY one "
            "of the provided options and nothing else. For open-ended questions, "
            "reply with 1-3 sentences, no preamble.\n\n"
            "CANDIDATE PROFILE:\n" + "\n".join(self.profile.summary_lines())
        )
        parts = [f"Question: {question}", f"Field type: {field_type}"]
        if options:
            parts.append("Options (choose exactly one):\n- " + "\n- ".join(options))
        if self.job_context:
            parts.append(f"Job context: {self.job_context}")
        parts.append("Answer:")
        self.llm_calls += 1
        # Reuse the evaluator's retry wrapper. Bounded backoff (1,2,4,8,16s →
        # ~31s worst case) so a busy provider doesn't make the run appear hung;
        # past that we fall back to a best-effort answer flagged for review.
        from pipeline.batch_evaluate import _call_with_retry
        return _call_with_retry(
            caller, system, "\n\n".join(parts), max_attempts=6, base_delay=1.0,
        ).strip()

    def _ensure_caller(self) -> Caller:
        if self._caller is not None:
            return self._caller
        from pipeline.batch_evaluate import _build_caller, _detect_provider, PROVIDER_DEFAULTS
        provider = _detect_provider()
        if not provider:
            raise RuntimeError(
                "no LLM provider configured for screening questions — set a "
                "provider key (GEMINI_API_KEY, etc.) or BATCH_PROVIDER in .env"
            )
        # APPLY_MODEL lets these light tasks use a faster/more-available model
        # than the (possibly heavy/overloaded) evaluation model in BATCH_MODEL.
        model = (os.environ.get("APPLY_MODEL") or os.environ.get("BATCH_MODEL")
                 or PROVIDER_DEFAULTS[provider])
        self._caller = _build_caller(provider, model, disable_thinking=thinking_disabled())
        return self._caller

    # ── cache persistence ───────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, str]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8",
            )
        except OSError:
            pass  # cache is a best-effort optimization, never fatal
