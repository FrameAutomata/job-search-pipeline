"""Scrape job boards via JobSpy. Reads search params from config/search.yml,
narrows each pass to the boards JobSpy would really search it on (retired boards
and ones whose mutually exclusive options the pass combines are dropped), skips
the passes left with none and the passes it cannot run at all, and writes
output/jobs.csv."""

import sys
from pathlib import Path

import pandas as pd
import yaml
from jobspy import scrape_jobs

from pipeline.rowio import write_rows
from pipeline.sites import (
    SUPPORTED_SITES,
    board_conflicts,
    normalize_pass,
    resolve_sites,
    unreadable_options,
)
from pipeline.stdio import line_buffer_stdout

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "output" / "jobs.csv"

# All optional JobSpy kwargs that map 1-to-1 from config keys.
# Passed through only when explicitly set (not None/missing) — which is the
# right test for the twelve keys whose falsy values are deliberate settings
# (`distance: 0`, `verbose: 0`, `linkedin_fetch_description: false`); dropping
# those would silently restore jobspy's defaults.
#
# The four MUTEX_KEYS in this list are the exception, and they never reach the
# `is not None` test carrying a falsy value: pipeline.sites.normalize_pass has
# already deleted the key when jobspy would act on it as "no filter". So
# `is_remote: false` and `hours_old: 0` are absent here rather than forwarded,
# which is the same thing on the wire.
OPTIONAL_PARAMS = [
    "location",
    "distance",
    "job_type",
    "is_remote",
    "easy_apply",
    "user_agent",
    "description_format",
    "offset",
    "hours_old",
    "verbose",
    "linkedin_fetch_description",
    "linkedin_company_ids",
    "country_indeed",
    "enforce_annual_salary",
    "ca_cert",
    "proxies",
]


def resolve_pass_sites(searches: list[dict]) -> list[dict]:
    """Every pass JobSpy can actually run, with `sites` narrowed to the boards it
    will really search — passes it cannot run at all, and passes left with no
    board, are dropped.

    Three rules, all pipeline.sites', so the UI validator can predict them by
    running the same code rather than restating it. Two narrow the board list:

      - a **retired** board (resolve_sites) — glassdoor and friends are
        Cloudflare-walled or serve responses that kill the run;
      - a board whose **mutual-exclusion rule** this pass breaks
        (board_conflicts) — Indeed acts on only one of hours_old /
        job_type+is_remote / easy_apply, and silently drops the loser.

    The two narrowing rules share one loop because they answer one question —
    which boards does this pass actually scrape — about one field, and the second
    subtracts from what the first resolved. Split across two loops the mutex rule
    had to resolve the board list a second time to have something to subtract
    from, and would then strip a retired board with none of the warning the first
    rule owes it. (`board_conflicts` still resolves the boards internally, on
    purpose — it is the anti-drift device the UI runs too, so it must not depend
    on a caller having prepared its input. What the shared loop buys is one
    `kept` list that both rules narrow, not one call to resolve_sites.)

    Applied on the way into a run rather than only when a config is authored, so
    a config that never met the UI's save-time validator — a fork's stale
    SEARCH_CONFIG_B64 cloud secret, a hand-edited search.yml — degrades with a
    warning instead of wasting requests or aborting the stage and taking every
    healthy pass (in the cloud, the whole day's run) with it.

    The mutex rule degrades PER BOARD (#126). It is a property of a board, not
    of a pass: every pass in the shipped example config — and everything
    setup-profile.mjs generates — names `[indeed, linkedin]`, so adding
    `easy_apply: true` to an `hours_old:` pass conflicts on Indeed and on
    nothing else. Skipping the pass discarded the LinkedIn search that would
    have run exactly as configured, which is verbatim the harm #115 cited when
    it retired the LinkedIn mutex group, reached one level up. Every single-board
    case still loses the pass, `sites: [indeed]` included — that is the same
    rule, not an exception to it.

    The third rule answers a different question — can this pass run at all? — and
    so has a different consequence: an **unreadable option value** (`easy_apply:
    maybe`) drops the whole pass, boards and all. That asymmetry is real rather
    than an oversight: `scrape_jobs` builds one ScraperInput before it dispatches
    to any board, so a value pydantic rejects raises a ValidationError no board
    survives. There is no healthy half to keep, which is why the rule shares this
    loop rather than narrowing anything. tests/test_jobspy_contract.py pins that
    reading against the library, since it is what makes the two consequences
    differ.

    Where this and the UI validator deliberately differ is the *consequence* of
    a mutex conflict — a run degrades, the save endpoint refuses — and that is
    written down at the UI's call site.
    """
    result = []
    for cfg in searches:
        name = cfg.get("name", "pass")
        kept, retired = resolve_sites(cfg)
        if retired:
            print(f"[scrape] [{name}] dropping unsupported sites: "
                  f"{', '.join(retired)} "
                  f"(supported: {', '.join(SUPPORTED_SITES)})")
        if not kept:
            print(f"[scrape] [{name}] skipping pass — no supported sites left")
            continue

        unreadable = unreadable_options(cfg)
        if unreadable:
            print(f"[scrape] skipping pass — {unreadable}")
            continue

        # Judged against the whole pass, but answered per board: board_conflicts
        # resolves the same boards as `kept` above, so subtracting its keys is
        # exact. Its messages already carry the pass name.
        conflicts = board_conflicts(cfg)
        if conflicts:
            # Both halves of the message name boards as the CONFIG spelled them.
            # conflicts' keys are MUTEX_GROUPS' lower-case ones, so reporting
            # those would print `dropping indeed ... Still searching: LinkedIn`
            # for a pass that wrote both capitalised — half a log line a user
            # can grep for with the spelling they used.
            blocked = [s for s in kept if s.lower() in conflicts]
            kept = [s for s in kept if s.lower() not in conflicts]
            why = " ".join(conflicts.values())
            if not kept:
                print(f"[scrape] skipping pass — {why}")
                continue
            print(f"[scrape] dropping {', '.join(blocked)} from this pass — {why} "
                  f"Still searching: {', '.join(kept)}.")

        result.append({**cfg, "sites": kept})
    return result


# The tail every malformed-config message ends with. Shared because the ways
# this file can be wrong differ only in WHAT is wrong — the repair is one
# repair, and its cloud half is the part a user cannot guess: in Actions there
# is no config/search.yml to open, because the daily writes it on the runner by
# decoding the SEARCH_CONFIG_B64 secret at the start of the run.
_CONFIG_HELP = (
    "A search config is a mapping with a `searches:` list of passes — see "
    "config/search.example.yml. In the cloud there is no local file to open: "
    "the daily writes this one by decoding the SEARCH_CONFIG_B64 secret, so "
    "repair config/search.yml locally, re-encode it and update the secret."
)

# Python's names for these describe the parser, not the file. `NoneType` in
# particular is what a key written with nothing under it parses to — the
# likeliest mistake in a hand-indented config, and the last word its author
# should have to translate.
_YAML_KINDS = {
    type(None): "empty",
    dict: "a mapping",
    list: "a list",
    str: "a string",
    bool: "a boolean",
    int: "a number",
    float: "a number",
}


def _kind(value) -> str:
    return _YAML_KINDS.get(type(value), f"a {type(value).__name__}")


def _config_error(path: Path, problem: str) -> ValueError:
    """A malformed-config error naming the file, the problem and the repair."""
    return ValueError(f"{path}: {problem}. {_CONFIG_HELP}")


def load_searches(path: Path) -> list[dict]:
    """Return a list of search configs. Supports `searches:` (list) and legacy `search:` (single).

    Anything that isn't one of those two shapes raises a ValueError naming the
    file, what is wrong with it and how to repair it, rather than reaching run()
    as a bare `TypeError: argument of type 'NoneType' is not iterable` one frame
    up. The failures aren't hypothetical and the cloud is where they land:
    config/search.yml is *written* on the runner from the SEARCH_CONFIG_B64
    secret, so a mis-encoded or truncated secret becomes a file nobody authored
    and nobody can look at. The daily's decode_secret guard already refuses a
    secret that decodes to ZERO BYTES, by name — but a comments-only file is not
    zero bytes, walks past that guard, parses to None here, and used to take the
    whole day's run down with an exception naming neither the file nor the
    secret. What follows is that same guard one layer further in, where the
    shape rather than the byte count is finally visible.

    ValueError rather than SystemExit, matching filter_passes below and
    resume_text._extractor_for: this is a library function — tests/
    test_example_config.py reads the shipped example through it — and a leaf
    that exits the process takes the decision away from every caller. run() does
    not catch it, so a broken config still stops the stage; what changes is what
    the last line of the traceback says.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # safe_load returns None for an empty file AND for one holding nothing but
    # comments or whitespace. The second is the one that gets here, since the
    # first is what decode_secret already catches upstream.
    if raw is None:
        raise _config_error(
            path, "parsed to nothing — the file is empty, or holds only comments and whitespace"
        )

    # `"searches" in raw` is a KEY test only on a mapping. On a list it asks
    # whether "searches" is an element, and on a string whether it is a
    # substring — so passes written at the top level with the `searches:` key
    # left off fell through to raw["search"] and raised KeyError, and a stray
    # line of prose containing the word could answer yes and hand run() the
    # characters of a string as if they were search passes.
    if not isinstance(raw, dict):
        raise _config_error(path, f"the top level is {_kind(raw)}, not a mapping")

    if "searches" in raw:
        key, entries = "searches", raw["searches"]
        # `searches:` with nothing indented under it parses to None; one pass
        # written without its leading `- ` parses to a mapping. Both used to be
        # returned as the caller's list and blow up in run()'s comprehension.
        if not isinstance(entries, list):
            raise _config_error(
                path, f"`searches:` is {_kind(entries)}, not a list of search passes"
            )
    elif "search" in raw:
        key, entries = "search", [raw["search"]]
    else:
        found = ", ".join(str(k) for k in raw) or "none"
        raise _config_error(
            path,
            "has neither a `searches:` list nor a legacy `search:` mapping "
            f"(top-level keys: {found})",
        )

    # Deliberately not a check on len(entries). `searches: []` names zero passes
    # rather than a broken one, and run() already treats that as a clean nothing
    # to do — it truncates jobs.csv and says why, the same exit a user with no
    # easy_apply pass takes under --easy-apply-only. The UI's save endpoint does
    # refuse an empty list, and that divergence points the safe way: it is
    # repairable while the config is on screen, and un-runnable is not
    # un-loadable.
    #
    # The per-entry check is not optional in the same way. normalize_pass and
    # every stage after it read dict fields off each pass, so a scalar or null
    # entry is the None case again wearing a different type — and it is the only
    # thing standing between a bare `search:` key and the identical crash the
    # `searches:` branch above is guarded against. pipeline/app/server.py's
    # _search_entries predicts exactly this at save time; now the run agrees.
    for i, entry in enumerate(entries, 1):
        if isinstance(entry, dict):
            continue
        where = "`search:`" if key == "search" else f"`searches:` pass {i}"
        raise _config_error(
            path, f"{where} is {_kind(entry)}, not a mapping of search options"
        )
    return entries


def filter_passes(
    searches: list[dict],
    only_passes: list[str] | None = None,
    *,
    easy_apply_only: bool = False,
    no_easy_apply: bool = False,
) -> list[dict]:
    """Return the subset of `searches` matching the given selectors.

    Selectors (any combination, applied as AND):
      - `only_passes`: keep passes whose `name:` matches (case-insensitive).
        Raises ValueError on no match — protects against `--only-pass` typos
        from the CLI.
      - `easy_apply_only`: keep only passes with `easy_apply: true`.
      - `no_easy_apply`: keep only passes without `easy_apply: true`.

    The `is True` tests below are exact rather than merely conventional: run()
    puts every pass through pipeline.sites.normalize_pass first, which rewrites
    easy_apply to the bool jobspy will act on and drops it when that is False.
    So a quoted `easy_apply: "true"` selects here just like an unquoted one, and
    `easy_apply: false` is indistinguishable from an absent key — which is what
    it means to jobspy. Called with un-normalized passes (tests, other callers),
    these read the raw value.

    `easy_apply_only` and `no_easy_apply` are used by the cloud workflows to
    route passes to the right schedule by JobSpy field rather than pass name.
    When they filter to zero passes, this returns an empty list — the workflow
    treats that as "nothing to do for this run" and exits cleanly. That's
    different from `only_passes` (a CLI typo should be loud, but having no
    easy-apply passes configured is just a valid state)."""
    result = searches

    if only_passes:
        wanted = {p.strip().lower() for p in only_passes if p and p.strip()}
        if wanted:
            selected = [s for s in result if (s.get("name") or "").strip().lower() in wanted]
            if not selected:
                available = ", ".join(repr(s.get("name", "")) for s in result)
                raise ValueError(
                    f"--only-pass matched no searches. Wanted: {sorted(wanted)}; available: {available}"
                )
            result = selected

    if easy_apply_only and no_easy_apply:
        raise ValueError("easy_apply_only and no_easy_apply are mutually exclusive")

    if easy_apply_only:
        result = [s for s in result if s.get("easy_apply") is True]
    elif no_easy_apply:
        result = [s for s in result if s.get("easy_apply") is not True]

    return result


def mark_easy_apply(combined: pd.DataFrame) -> pd.DataFrame:
    """Collapse the per-pass easy_apply flag to a per-URL OR.

    Each row arrives carrying the easy_apply flag of the pass that produced it.
    A job returned by both a broad pass (False) and an easy_apply pass (True)
    must end up True regardless of which duplicate row `drop_duplicates` later
    keeps — so OR the flag across every row sharing a job_url before dedup.
    JobSpy gives no per-job "easy apply" signal, so this pass-level flag is the
    only thing that distinguishes (e.g.) Indeed SmartApply roles downstream."""
    if "easy_apply" not in combined.columns:
        combined["easy_apply"] = False
    flag = combined["easy_apply"].fillna(False).astype(bool)
    # transform drops NaN-keyed groups, so a row with a NaN job_url comes back
    # NaN and would promote the column to object dtype — fillna keeps it bool.
    combined["easy_apply"] = (
        flag.groupby(combined["job_url"]).transform("max").fillna(False).astype(bool)
    )
    return combined


def _no_results(reason: str) -> Path:
    """Report `reason`, truncate jobs.csv, and hand back its path.

    Mirrors pipeline.filter._no_results and exists for the same reason: both of
    run()'s "nothing to write" exits go through here, so a third can't be added
    without the truncation. Why it must truncate — and why zero bytes rather
    than a header — is pipeline.rowio's contract.
    """
    print(f"[scrape] {reason} — writing empty jobs.csv")
    write_rows(OUTPUT_PATH, [])
    return OUTPUT_PATH


def run(
    config_path: Path,
    only_passes: list[str] | None = None,
    *,
    easy_apply_only: bool = False,
    no_easy_apply: bool = False,
) -> Path:
    # Normalize before anything reads the mutually-exclusive options: pass
    # selection, the conflict check and the per-row easy_apply tag each used to
    # test them differently, and a quoted or falsy value made the three
    # disagree. See pipeline.sites.normalize_pass.
    selected = filter_passes(
        [normalize_pass(cfg) for cfg in load_searches(config_path)],
        only_passes,
        easy_apply_only=easy_apply_only,
        no_easy_apply=no_easy_apply,
    )
    searches = resolve_pass_sites(selected)

    if not searches:
        # No matching passes — truncate jobs.csv so downstream stages no-op
        # cleanly instead of re-processing the previous run's rows. This is the
        # workflow-friendly path: e.g. the easy-apply workflow on a user with
        # no easy_apply pass configured.
        reason = (
            "every matching pass was skipped (unsupported boards, or options "
            "JobSpy can't combine)"
            if selected
            else "no searches matched the active filters"
        )
        return _no_results(reason)

    if only_passes or easy_apply_only or no_easy_apply:
        names = ", ".join(repr(s.get("name", "")) for s in searches)
        print(f"[scrape] running passes: {names}")

    all_rows = []
    for cfg in searches:
        name = cfg.get("name", "pass")
        optional = {k: cfg[k] for k in OPTIONAL_PARAMS if cfg.get(k) is not None}

        pass_easy_apply = cfg.get("easy_apply") is True
        for term in cfg["search_terms"]:
            print(f"[scrape] [{name}] searching: {term!r}")
            df = scrape_jobs(
                site_name=cfg["sites"],
                search_term=term,
                results_wanted=cfg.get("results_wanted", 50),
                **optional,
            )
            # Tag every row with the pass's easy_apply flag so it survives the
            # cross-pass merge; mark_easy_apply ORs it per URL below.
            df["easy_apply"] = pass_easy_apply
            all_rows.append(df)

    combined = pd.concat(all_rows, ignore_index=True) if all_rows else None
    if combined is None or combined.empty:
        # Every pass came back empty (rate-limited, network blip, boards down).
        # Truncate rather than leave the previous run's rows behind — same
        # reason as the no-passes branch above.
        #
        # Deliberately no jobs.prev.csv backup, unlike pipeline/app/reset.py,
        # which snapshots before it wipes. Reset is a user action they may
        # regret; this is a routine outcome that would leave a stale snapshot
        # after every throttled morning. Silently re-evaluating yesterday's
        # rows is the worse failure, and losing a raw intermediate costs one
        # re-scrape. Documented so the trade isn't rediscovered from scratch.
        return _no_results("no jobs returned")

    combined = mark_easy_apply(combined)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["job_url"])
    print(f"[scrape] {before} rows -> {len(combined)} after dedup")

    # The happy path is the only writer left that isn't write_rows (pandas owns
    # the column set here), so it is also the only one still needing this.
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUTPUT_PATH, index=False)
    print(f"[scrape] wrote {OUTPUT_PATH}")
    return OUTPUT_PATH


if __name__ == "__main__":
    line_buffer_stdout()

    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "search.yml"
    run(cfg_path)
