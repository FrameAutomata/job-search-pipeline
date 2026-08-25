"""Single source of truth for the boards the pipeline supports.

Kept in its own dependency-free leaf module (imports nothing) so the UI code
can share it without pulling in jobspy, which `pipeline.scrape` imports at
module top and the UI venv deliberately doesn't install.

Glassdoor and ZipRecruiter sit behind a Cloudflare wall that 403s every
scripted request (they contributed zero rows across months of runs), and
Google Jobs serves degraded responses then drops the connection mid-body —
jobspy's Google scraper doesn't catch that, so one truncated response used to
kill the whole run and discard every row already scraped.

Three files hand-mirror SUPPORTED_SITES because they can't import it, and a
test guards each: `setup-profile.mjs` and `pipeline/app/static/onboard.html`
(tests/test_app_onboard.py), and `config/search.example.yml` — three separate
`sites:` blocks — (tests/test_example_config.py). Prose names the boards too —
the example config's comment above the first block, README.md, QUICKSTART.md,
CLAUDE.md — and no test reads documentation, so walk those by hand as well when
this constant changes.
"""

SUPPORTED_SITES = ("indeed", "linkedin")


def as_site_list(sites) -> list:
    """A config's `sites` value as a list of entries.

    YAML allows a bare scalar — `sites: indeed` — which JobSpy accepts as a
    `site_name`. Iterating that directly walks it one character at a time.

    A comma-separated scalar (`sites: indeed, linkedin`) is split on commas.
    That is the shape the CLI wizard prompts for ("Which boards? Comma-
    separated"), and unbracketed it is one YAML string — read whole it matches
    no board, so a pass naming both supported boards would be dropped entirely.

    A scalar that isn't iterable at all (`sites: 5`, `sites: true`) is wrapped
    rather than exploded. `list(5)` raises TypeError, which would abort the
    scrape stage with a traceback and turn the save endpoint's 400 into a 500 —
    both callers promise to degrade to a warning instead.
    """
    if isinstance(sites, str):
        return sites.split(",")
    try:
        return list(sites)
    except TypeError:
        return [sites]


def partition_sites(sites) -> tuple[list, list]:
    """`sites` split into (supported, unsupported), each trimmed and de-duplicated.

    Callers want both halves — the scraper prints what it drops, the UI warns
    about it — so they are produced together rather than each caller inverting
    the filter for itself.

    Case is left as written — JobSpy resolves the board with `Site[name.upper()]`
    — but surrounding whitespace is stripped, because that same lookup raises
    KeyError on `" LINKEDIN "`. Case-variant repeats collapse so a board named
    twice is not scraped twice.
    """
    kept, dropped, seen = [], [], set()
    for s in as_site_list(sites):
        # A null entry (`- ~`) or the blank left by a trailing comma names no
        # board; reporting it would send the user hunting for one called "None".
        if s is None:
            continue
        name = str(s).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        (kept if key in SUPPORTED_SITES else dropped).append(name)
    return kept, dropped


def keep_supported(sites) -> list:
    """Just the supported entries of `sites`, trimmed and de-duplicated."""
    return partition_sites(sites)[0]


def resolve_sites(cfg: dict) -> tuple[list, list]:
    """The (supported, unsupported) boards one search pass would actually scrape.

    The per-pass rule itself, so the scraper (which enforces it) and the UI
    validator (which has to predict it before writing a config) can't drift.

    A missing or explicitly null `sites` inherits the supported set rather than
    passing through: left as None, jobspy's get_site_type() falls back to
    list(Site) — every board, including the ones retired here — and the
    mutex check below raises TypeError iterating it first.
    """
    sites = cfg.get("sites")
    if sites is None:
        return list(SUPPORTED_SITES), []
    return partition_sites(sites)


# The options JobSpy rejects in combination, keyed by board. Each board maps to
# its display name plus the groups it allows only ONE of — two options in the
# same group coexist (Indeed takes job_type with is_remote), two *active* groups
# are the conflict.
# NOTE (unverified against python-jobspy 1.1.82): the "linkedin" entry below
# does not reproduce there. LinkedIn's builder puts BOTH options into one params
# dict — `"f_AL": "true" if easy_apply else None` and, a few lines down,
# `params["f_TPR"] = f"r{seconds_old}"` from hours_old — then strips only the
# None values and sends what's left. No raise, no precedence, both filters
# applied. So a LinkedIn-only pass setting both is skipped here over a
# combination jobspy handles fine. Retiring the group is a behaviour change with
# the same open question as the Indeed one, tracked in #115; left in place so
# this stays a truthiness fix.
MUTEX_GROUPS = {
    "indeed": ("Indeed", [("hours_old",), ("job_type", "is_remote"), ("easy_apply",)]),
    "linkedin": ("LinkedIn", [("hours_old",), ("easy_apply",)]),
}


# The union of the MUTEX_GROUPS keys, in first-seen order. Derived rather than
# restated so a group that gains an option can't leave normalize_pass behind.
MUTEX_KEYS = tuple(
    dict.fromkeys(k for _, groups in MUTEX_GROUPS.values() for group in groups for k in group)
)

# Sentinel for "no value jobspy would accept", kept distinct from None because
# None is itself a legitimate result (an option explicitly nulled out).
_UNREADABLE = object()

# The lax-mode bool spellings pydantic accepts, which is what ScraperInput is.
_BOOL_TOKENS = {
    "1": True, "t": True, "true": True, "y": True, "yes": True, "on": True,
    "0": False, "f": False, "false": False, "n": False, "no": False, "off": False,
}


def _as_bool(value):
    """`value` as the bool jobspy's pydantic model will coerce it to.

    ScraperInput is a pydantic BaseModel, so it accepts the lax-mode spellings
    of a bool — the tokens above, case-insensitively, plus 0/1 as int or float.
    Anything else pydantic rejects outright, and so do we.

    Surrounding whitespace is stripped, which pydantic does NOT do (`" true "`
    is a ValidationError there). That is deliberate and cannot cause a
    divergence: normalize_pass replaces the value with the bool returned here,
    so jobspy is handed `True`, never the padded string. It turns a run-killing
    ValidationError into the filter the user plainly meant.
    """
    # bool before int: isinstance(True, int) is True, and `True in (0, 1)` too.
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _BOOL_TOKENS.get(value.strip().lower(), _UNREADABLE)
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return _UNREADABLE


def _as_is(value):
    """Identity. For a key jobspy resolves itself, where only truthiness matters."""
    return value


def _as_int(value):
    """`value` as the int jobspy's pydantic model will coerce it to.

    Lax mode takes an int-valued str or float ("168", 168.0) and — worth knowing
    before calling it a typo — a bool, where True becomes 1. `hours_old: true`
    really does search the last one hour. Non-integral values are rejected.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            # Matches pydantic for signs, underscores and leading zeros, and
            # rejects "0x10" and "" exactly as it does.
            return int(text)
        except ValueError:
            pass
        # Pydantic also takes an integral decimal string ("168.0"). It does NOT
        # take the other things float() would stretch to — "1e3", "1.", ".5",
        # "inf", "nan" — so this splits on the point rather than calling
        # float(), which keeps all of them out for one reason instead of five.
        # The drift test against TypeAdapter(int) is what surfaced the gap.
        whole, point, frac = text.partition(".")
        if not point or not frac.isdigit():
            return _UNREADABLE
        try:
            return int(whole) if int(frac) == 0 else _UNREADABLE
        except ValueError:
            return _UNREADABLE
    if isinstance(value, float):
        # Also how inf and nan get rejected, as pydantic rejects them.
        return int(value) if value.is_integer() else _UNREADABLE
    return _UNREADABLE


# How jobspy reads each mutex key, so normalize_pass can rewrite the value to
# what the scrape will actually act on. Every MUTEX_KEYS entry needs one; the
# pairing is pinned in tests/test_scrape.py so a new group option can't quietly
# skip normalization.
#
# The scoping to these four keys is the whole reason this is safe.
# pipeline.scrape.OPTIONAL_PARAMS forwards 16 keys on `is not None` precisely
# because falsy values there are deliberate settings — `distance: 0`,
# `offset: 0`, `verbose: 0`, `linkedin_fetch_description: false` — and dropping
# them would silently restore jobspy's defaults. None of those are mutex keys.
# For these four, falsy *is* "send no filter", because truthiness is exactly how
# jobspy reads them. Dropping them here says what the config already said.
_MUTEX_KEY_READERS = {
    "hours_old": _as_int,    # ScraperInput.hours_old: int | None
    "job_type": _as_is,      # ScraperInput.job_type: JobType | None — jobspy
                             # resolves the string itself, so only truthiness
                             # matters and there is nothing to coerce.
    "is_remote": _as_bool,   # ScraperInput.is_remote: bool = False
    "easy_apply": _as_bool,  # ScraperInput.easy_apply: bool | None
}


def unreadable_options(cfg: dict) -> str | None:
    """The mutex options whose values JobSpy cannot read, or None.

    normalize_pass is the one place that *knows* a value is unusable — pydantic
    would reject it, so `scrape_jobs` raises a ValidationError while building
    ScraperInput, before any network call. Left to reach that point it aborts
    the whole scrape stage from inside run()'s pass loop, discarding the rows
    every healthy pass already returned. That is precisely the failure
    strip_unsupported_sites and drop_conflicting_passes exist to prevent, so the
    knowledge is surfaced here instead of thrown away, and the same
    warn-and-skip / refuse-the-save mechanism handles it.

    Reported rather than guessed at: coercing `easy_apply: "maybe"` to True
    would invent a filter the user never asked for, and to False would hide the
    typo. Naming the key and its value sends them to the right line.

    Note this also catches the handful of pathological strings where the
    coercion mirror is deliberately not exhaustive — pydantic-core's string
    parsing accepts a few shapes Python's own int() does not (`"0-0"` reads as
    0 there). Chasing exact parity on those is a losing game; classifying them
    unreadable costs the user that pass plus a warning naming the option, which
    is a great deal better than the silent misdiagnosis of reading a truthy
    string as an active filter.
    """
    where = f"[{cfg['name']}] " if cfg.get("name") else ""
    bad = [
        f"{key}: {cfg[key]!r}"
        for key, read in _MUTEX_KEY_READERS.items()
        if key in cfg and cfg[key] is not None and read(cfg[key]) is _UNREADABLE
    ]
    if not bad:
        return None
    return (
        f"{where}JobSpy cannot read {', '.join(bad)} — it would reject the value "
        f"and abort the scrape. Use a plain YAML value (true/false, or a whole "
        f"number of hours)."
    )


def normalize_pass(cfg: dict) -> dict:
    """`cfg` with its mutually-exclusive options rewritten to what jobspy acts on.

    One config key used to be read three different ways in one scrape flow:
    truthiness here in limitation_conflict, `is not None` in
    pipeline.scrape.OPTIONAL_PARAMS, and `is True` in scrape's filter_passes and
    its per-row easy_apply tag. A value that was truthy but not `True` — the
    quoted `easy_apply: "true"` a templated SEARCH_CONFIG_B64 or a hand edit
    produces — was a live filter to jobspy, a conflict to us, and *not* an
    easy-apply pass to `--easy-apply-only` or to the tag the UI's apply-button
    gating reads. The mirror image was worse: `is_remote: "false"` is a truthy
    Python str to a raw read and a falsy bool to pydantic, so we skipped a whole
    pass over a filter jobspy would never have sent.

    Normalizing once, on the way into a run, retires all of that. Each of the
    four MUTEX_KEYS is rewritten to its jobspy-effective value and dropped when
    that value is falsy — jobspy reads every one of them through truthiness, so
    a falsy option sends no filter and an absent key sends no filter. Afterwards
    `is not None`, truthiness and `is True` coincide on these keys and each call
    site's local test stops mattering.

    A value pydantic itself would reject (`easy_apply: maybe`) is left verbatim,
    because guessing at it would either invent a filter the user never asked for
    or hide the typo. It reaches jobspy's own validation, exactly as today.

    Returns a new dict; `cfg` is not mutated.
    """
    out = dict(cfg)
    for key, read in _MUTEX_KEY_READERS.items():
        if key not in out:
            continue
        # An explicitly nulled option (`easy_apply:` with nothing after it) is
        # read as absent, the same reading resolve_sites gives `sites:`.
        raw = out[key]
        value = None if raw is None else read(raw)
        if value is _UNREADABLE:
            continue
        if value:
            out[key] = value
        else:
            del out[key]
    return out


def limitation_conflict(cfg: dict) -> str | None:
    """The JobSpy mutual-exclusion rule this search pass breaks, or None.

    Indeed accepts only ONE of (A) hours_old, (B) job_type and/or is_remote,
    (C) easy_apply per search; LinkedIn only one of hours_old or easy_apply.
    Combining them used to raise out of the scrape stage. Neither board does
    that in python-jobspy 1.1.82: Indeed's builder is a precedence chain
    (hours_old, elif easy_apply, elif job_type/is_remote) that drops the loser
    in silence, and LinkedIn's sends both filters happily — see the note on
    MUTEX_GROUPS. An Indeed pass therefore doesn't search what it says it
    searches; a LinkedIn one does, and is skipped anyway. Both are #115.

    An option counts as set only when its value is TRUTHY, because truthiness
    is what jobspy reads it through: `elif self.scraper_input.easy_apply:`
    (indeed), `"f_AL": "true" if scraper_input.easy_apply else None` and
    `hours_old * 3600 if hours_old else None` (linkedin). `easy_apply: false`
    and `hours_old: 0` therefore send no filter at all, and testing
    `is not None` here cost the user a whole pass over an option that never
    reached the wire — turning a filter off being the obvious reason to
    write `false` in the first place.

    Note this deliberately does NOT match how pipeline.scrape forwards the same
    keys: OPTIONAL_PARAMS keeps `is not None`, because it spans 16 keys whose
    falsy values are meaningful settings a user typed on purpose (`distance: 0`,
    `offset: 0`, `verbose: 0`, `linkedin_fetch_description: false`) and dropping
    those would silently restore jobspy's defaults. The two tests answer
    different questions — "did the user supply a value to forward?" there,
    "will jobspy act on it?" here — so they are not to be unified.

    Lives here, in the dependency-free leaf, so the UI venv — which installs
    neither jobspy nor pandas and so cannot import pipeline.scrape at all — can
    predict the rule by running it rather than by restating it. Returns the
    message instead of raising so each caller shapes its own consequence: the
    scraper skips the pass, the save endpoint answers 400.

    The boards checked are resolve_sites()'s, not `cfg["sites"]` verbatim — an
    omitted `sites` inherits the supported boards, so a pass that never names
    Indeed is still bound by Indeed's rule, and a retired board is stripped
    before the scrape so nothing it would have accepted matters here.
    """
    # Normalized here rather than by the caller, for the same reason the boards
    # come from resolve_sites() below rather than from cfg["sites"] verbatim:
    # this function is the anti-drift device — the scraper, the UI's save
    # endpoint and the example-config guard all run it precisely so they cannot
    # disagree — and a predicate whose answer depends on the caller having
    # remembered a preparatory call is not that. normalize_pass is pure and
    # idempotent, so callers that already normalized (scrape.run must, because
    # the forwarded kwargs and the per-row tag need the rewritten dict) pay only
    # a dict copy.
    cfg = normalize_pass(cfg)
    sites = {s.lower() for s in resolve_sites(cfg)[0]}
    where = f"[{cfg['name']}] " if cfg.get("name") else ""

    for board, (label, groups) in MUTEX_GROUPS.items():
        if board not in sites:
            continue
        active = [g for g in groups if any(cfg.get(k) for k in g)]
        if len(active) < 2:
            continue
        set_here = ", ".join(k for g in active for k in g if cfg.get(k))
        allowed = " | ".join(" and/or ".join(g) for g in groups)
        return (
            f"{where}{label} limitation: only ONE of [{allowed}] may be set per "
            f"search, but this pass sets {set_here}. Remove all but one."
        )
    return None
