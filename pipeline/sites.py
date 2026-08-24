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
MUTEX_GROUPS = {
    "indeed": ("Indeed", [("hours_old",), ("job_type", "is_remote"), ("easy_apply",)]),
    "linkedin": ("LinkedIn", [("hours_old",), ("easy_apply",)]),
}


def limitation_conflict(cfg: dict) -> str | None:
    """The JobSpy mutual-exclusion rule this search pass breaks, or None.

    Indeed accepts only ONE of (A) hours_old, (B) job_type and/or is_remote,
    (C) easy_apply per search; LinkedIn only one of hours_old or easy_apply.
    Combining them used to raise out of the scrape stage; in python-jobspy
    1.1.82 Indeed's builder is a precedence chain instead (hours_old, elif
    easy_apply, elif job_type/is_remote), so the loser is dropped in silence.
    Either way the pass does not search what it says it searches.

    An option counts as set only when its value is TRUTHY, because truthiness
    is what jobspy reads it through: `elif self.scraper_input.easy_apply:`
    (indeed), `"f_AL": "true" if scraper_input.easy_apply else None` and
    `hours_old * 3600 if hours_old else None` (linkedin). `easy_apply: false`
    and `hours_old: 0` therefore send no filter at all, and testing
    `is not None` here cost the user a whole pass over an option that never
    reached the wire — turning a filter off being the obvious reason to
    write `false` in the first place.

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
