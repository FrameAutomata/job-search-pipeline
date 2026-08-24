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


def is_supported(site) -> bool:
    """Whether `site` names a supported board, ignoring case and surrounding space.

    The one place the match is defined. Config files, the wizard form and
    hand-written JSON all spell boards slightly differently, and every caller
    needs to agree on which spellings count.
    """
    return str(site).strip().lower() in SUPPORTED_SITES


def as_site_list(sites) -> list:
    """A config's `sites` value as a list.

    YAML allows a bare scalar — `sites: indeed` — which JobSpy accepts as a
    `site_name`. Iterating that directly walks it one character at a time.
    """
    return [sites] if isinstance(sites, str) else list(sites)


def keep_supported(sites) -> list:
    """The supported entries of `sites`, trimmed and de-duplicated.

    Case is left as written — JobSpy resolves the board with `Site[name.upper()]`
    — but surrounding whitespace is stripped, because that same lookup raises
    KeyError on `" LINKEDIN "`. Case-variant repeats collapse so a board named
    twice is not scraped twice.
    """
    kept, seen = [], set()
    for s in as_site_list(sites):
        if not is_supported(s):
            continue
        name = str(s).strip()
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        kept.append(name)
    return kept
