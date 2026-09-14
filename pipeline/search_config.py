"""Which search config is in effect, and what is in it.

`orchestrate` has always owned this question, and while it was the only asker
that was the right home. #180 added two more — the daily digest and the handoff
build both need the user's non-remote pass locations to know which metros they
can actually work in — and `orchestrate` imports scrape, filter, screen and
batch_evaluate at module scope, so neither of them can ask it there without
pulling yake, pandas and jobspy into a process that wants none of them.

So the resolution lives here, in a leaf, and `orchestrate` re-exports it. The
alternative was each caller re-deriving "local override, else shared", which is
the mirror this repo keeps learning about: three answers to one question, and
the drift only visible on the copy that set an override.

PyYAML is imported lazily inside `load_search_config` rather than at module
scope, so `pipeline.daily_digest` — which is guarded to import wherever
`pipeline.app.data` does, without the pipeline's own dependencies — can import
this module unconditionally and simply get `{}` back when yaml is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

# (resolved path, mtime_ns) -> parsed mapping. `data.parse_applications` reaches
# `load_search_config` once per CALL and the UI calls it per request, so an
# uncached read re-parsed the same small YAML on every tracker render. Same
# shape and the same reason as `tracker_layout.load_contract`, which this cannot
# reuse: that one resolves under the career-ops checkout, and this file is ours.
_CACHE: dict[str, tuple[int, dict]] = {}

ROOT = Path(__file__).resolve().parent.parent

# The cloud-shared search config (populated from the SEARCH_CONFIG_B64 secret in
# the daily workflow) and the optional LOCAL override. A local run auto-prefers
# the override so a user can search different terms locally than the cloud daily,
# without touching the secret. The cloud checkout never has search.local.yml —
# it's gitignored and the daily only ever decodes the secret into search.yml.
SHARED_SEARCH_CONFIG = Path("config/search.yml")
LOCAL_SEARCH_CONFIG = Path("config/search.local.yml")


def resolve_search_config(explicit: str | Path | None, root: Path = ROOT) -> Path:
    """Pick the search config, resolved against `root`.

    Precedence: explicit --config > a *custom* SEARCH_CONFIG env >
    config/search.local.yml (if present) > config/search.yml.

    Note the "custom" qualifier: .env.example ships `SEARCH_CONFIG=./config/search.yml`,
    so nearly every local .env has the var set to the shared default. Honoring that
    literally would defeat the local override for everyone, so a SEARCH_CONFIG that
    resolves to the shared config/search.yml is treated as boilerplate (equivalent to
    unset) and the override still wins. A SEARCH_CONFIG pointing anywhere else is a
    deliberate choice and takes precedence. The cloud daily sets no SEARCH_CONFIG env
    (it relies on the default path), so it always uses the decoded search.yml.
    """
    def _resolve(p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (root / p).resolve()

    if explicit:
        return _resolve(explicit)
    shared = _resolve(SHARED_SEARCH_CONFIG)
    env = os.environ.get("SEARCH_CONFIG")
    if env and (env_path := _resolve(env)) != shared:
        return env_path
    local = _resolve(LOCAL_SEARCH_CONFIG)
    if local.exists():
        return local
    return shared


def load_search_config(path: str | Path | None = None, root: Path = ROOT) -> dict:
    """The resolved search config as a mapping — `{}` for anything unreadable.

    Deliberately total: every caller here wants a setting out of it (the
    commutable-area lists), never to validate it. The stages that DO validate a
    config load it themselves and report their own errors, so a missing file, a
    syntax error or a PyYAML-less environment must degrade to "no setting" and
    not to a traceback in the digest, which runs `if: always()` and must never
    be why a day shows red.
    """
    try:
        import yaml
    except ImportError:
        return {}
    resolved = str(resolve_search_config(path, root))
    try:
        mtime = os.stat(resolved).st_mtime_ns
    except OSError:
        return {}
    hit = _CACHE.get(resolved)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        with open(resolved, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except (OSError, ValueError, yaml.YAMLError):
        cfg = None
    # A malformed file caches its {} too: the next tracker row would fail on it
    # again, and re-reading per row is what this cache exists to stop.
    cfg = cfg if isinstance(cfg, dict) else {}
    _CACHE[resolved] = (mtime, cfg)
    return cfg
