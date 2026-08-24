"""Single source of truth for the boards the pipeline supports.

Kept in its own dependency-free leaf module (imports nothing) so the UI code
can share it without pulling in jobspy, which `pipeline.scrape` imports at
module top and the UI venv deliberately doesn't install.

Glassdoor and ZipRecruiter sit behind a Cloudflare wall that 403s every
scripted request (they contributed zero rows across months of runs), and
Google Jobs serves degraded responses then drops the connection mid-body —
jobspy's Google scraper doesn't catch that, so one truncated response used to
kill the whole run and discard every row already scraped.
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
    list(Site) — every board, including the ones retired here — and
    validate_limitations raises TypeError iterating it first.
    """
    sites = cfg.get("sites")
    if sites is None:
        return list(SUPPORTED_SITES), []
    return partition_sites(sites)
