"""The daily's artifacts stay bounded, and every workflow agrees on the cache.

Issue #129: the account's Actions storage quota filled up, and an exhausted
storage quota stops GitHub creating workflow runs *at all* — so the Tests
workflow silently stopped running on pull requests, across three separate
trigger events, while its own file was demonstrably fine.

Two properties had to hold and neither was written down anywhere:

  1. **A retention, at or under a ceiling.** The 90 days that caused it was the
     *default* — nobody chose it, nobody reviewed it, and it does not appear in
     the file. That is exactly the shape review misses and a guard catches.
  2. **No SCHEDULED upload names cached state.** `career-ops/reports/` is
     restored from the state cache every run and only grows, so uploading it
     put the entire report history into every daily artifact. Retention caps
     how many artifacts are alive, never how big each one is; without this half
     the storage still grows without bound, just more slowly. Scheduled, not
     all: export-reports.yml exports the whole history on purpose, and a human
     asking for it once is bounded by the asking in a way a cron job is not.

A third rule, from issue #133, is guarded here because it concerns the same
cache and reuses the same table. Every workflow that restores or saves
`pipeline-state-v1` must name the SAME path set. GitHub derives a cache's
"version" from the paths and matches a restore on key prefix AND version, so a
drifted list does not restore *less* — it matches nothing and silently starts
its own lineage. export-reports.yml shipped naming two of the nine paths and so
never restored anything, which made #129's recovery path dead on arrival;
seed-reports.yml named seven and spent months repairing into a cache the daily
never read. edit-tracker.yml carried a prose warning about precisely this,
written after it had already caused one outage. Prose is not a guard.

Guarding the rule rather than today's two upload steps, in the spirit of
tests/test_stdio.py: the failure mode is a *new* upload added later by pasting
an older one, and both halves of the rule are invisible in the paste.

This is also the only place either property can be checked. The repository that
holds the code is public — public repos don't consume the account's storage
quota — and it is the template, where daily-pipeline.yml skips by design. The
daily only really runs in the private copies made via "Use this template", so
the bug is structurally unobservable from here.
"""

from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# A week. Long enough for the delivery mechanism of a DAILY job — the README
# tells users to download the pipeline-output-* artifact — and short enough
# that a copy of this template cannot bank three months of them. Raising this
# is a decision; that is the point of it being a number in a test.
RETENTION_CEILING = 7

# Every path the daily caches, and what an artifact may do with it.
#
#   "delta" — restored at the start of every run and never truncated. Only the
#             files THIS run added may ship; uploading the directory is the bug.
#   "whole" — must ship entire because a consumer needs its current value, and
#             the growth that comes with that is accepted and named here.
#   "local" — never leaves the cache.
#
# A table rather than a blocklist, and asserted EXACT against the workflow
# below: a blocklist lets a cache path added later slip through unclassified,
# which is the same "nobody chose it" failure this file exists to catch.
#
# Note applications.md is "whole" even though it grows: the UI's Refresh MERGES
# it, so a diff is useless to it. That is irreducible, and it is ~200 bytes per
# evaluated role against ~4KB per report. pipeline.md is "local" for the
# opposite reason — it is append-only at ~600 bytes for EVERY bridged offer,
# many more than are ever evaluated, and nothing reads it out of the artifact.
CACHED_PATHS = {
    "career-ops/reports":                    "delta",
    "career-ops/data/applications.md":       "whole",
    "career-ops/data/scan-history.tsv":      "local",
    "career-ops/data/pipeline.md":           "local",
    "career-ops/data/recheck-state.tsv":     "local",
    "career-ops/data/easy-apply-urls.txt":   "local",
    "career-ops/batch/batch-input.tsv":      "local",
    "career-ops/batch/batch-api-state.json": "local",
    "career-ops/batch/jds":                  "local",
}

DAILY = "daily-pipeline.yml"
CAREER_OPS = "career-ops"


class Step(NamedTuple):
    workflow: str
    label: str          # "<workflow>:<job>:<step name>", pre-built for messages
    block: dict         # the step's `with:` mapping
    scheduled: bool     # does its workflow have a `schedule:` trigger?


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    """A workflow's `on:` block. PyYAML resolves the bare key `on` to the
    boolean True (YAML 1.1), so reading doc["on"] finds nothing — and a
    scheduled workflow would then look manual to the rule below."""
    trig = doc.get("on", doc.get(True))
    return trig if isinstance(trig, dict) else {}


def _steps(name: str, job: str) -> list[dict]:
    return _doc(name)["jobs"][job]["steps"]


def _step_index(steps: list[dict], predicate) -> int:
    return next(i for i, s in enumerate(steps) if predicate(s))


def _norm(entry: str) -> str:
    """A path entry as the repo writes it: no `./` prefix, no trailing slash."""
    entry = entry.strip().rstrip("/")
    while entry.startswith("./"):
        entry = entry[2:]
    return entry


def _path_entries(with_block: dict) -> list[str]:
    """A step's `path:` value as a list of normalized entries."""
    return [_norm(ln) for ln in str(with_block.get("path") or "").splitlines()
            if ln.strip()]


def _raw_path_lines(with_block: dict) -> list[str]:
    """A step's `path:` exactly as GitHub reads it — trimmed lines, in order,
    nothing normalized.

    This is the shape the cache version is computed from: getCacheVersion does
    `sha256(paths.join('|'))` over the list as given, with no sort and no
    tidying of `./` or trailing slashes. So comparing normalized SETS would
    green-light a reordering, or one list writing `career-ops/reports/`, both of
    which change the hash and fork the lineage — the exact bug #133 was. The
    workflow comments promise "byte-identical"; this is that promise."""
    return [ln.strip() for ln in str(with_block.get("path") or "").splitlines()
            if ln.strip()]


def _common_root(entries: list[str]) -> str:
    """The directory an upload-artifact archive roots at, given >1 search path:
    the longest shared leading run of path segments."""
    out = []
    for seg in zip(*[_norm(e).split("/") for e in entries]):
        if len(set(seg)) != 1:
            break
        out.append(seg[0])
    return "/".join(out)


def _covers(entry: str, target: str) -> bool:
    """Does uploading `entry` put `target` in the artifact?

    Both directions, because both are ways to reintroduce the bug: naming the
    path itself, and naming a PARENT of it (`path: career-ops` ships reports/
    just as surely as `path: career-ops/reports`). A one-directional startswith
    catches only the first, which is the one nobody would write by accident."""
    entry, target = _norm(entry).rstrip("*").rstrip("/"), _norm(target)
    return entry == target or target.startswith(entry + "/") \
        or entry.startswith(target + "/")


def _run_artifact_args(mode: str) -> dict[str, list[str]]:
    """The daily's `python -m pipeline.run_artifact <mode>` invocation, parsed
    into {flag: [values]}. Reads the parsed step's `run` rather than splitting
    the raw YAML, so indentation and step order can change freely."""
    steps = _steps(DAILY, "scrape-and-evaluate")
    run = steps[_step_index(
        steps, lambda s: f"run_artifact {mode}" in str(s.get("run") or ""))]["run"]
    tokens = run.replace("\\\n", " ").split()
    args: dict[str, list[str]] = {}
    flag = None
    for tok in tokens:
        if tok.startswith("--"):
            flag = tok[2:]
            args.setdefault(flag, [])
        elif flag is not None:
            args[flag].append(tok)
    return args


def _stage_args() -> dict[str, list[str]]:
    return _run_artifact_args("stage")


def _steps_using(action: str) -> list[Step]:
    """Every step across every workflow whose `uses` names `action`, pre-labelled
    and tagged with whether its workflow runs on a schedule.

    One walker for both rules in this file, so the collection comment below
    cannot end up attached to only one of them."""
    out = []
    # Both extensions: GitHub honours .yaml as readily as .yml, and a workflow
    # this scan cannot see is a workflow exempt from every rule in this file.
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        scheduled = "schedule" in _triggers(doc)
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job.get("steps") or []):
                if str(step.get("uses") or "").startswith(action):
                    out.append(Step(
                        path.name,
                        f"{path.name}:{job_name}:{step.get('name', '?')}",
                        step.get("with") or {},
                        scheduled))
    return out


def _all_upload_steps() -> list[Step]:
    return _steps_using("actions/upload-artifact")


def _daily_cache_step() -> dict:
    """The daily's restore step — the reference every other cache step copies."""
    steps = _steps(DAILY, "scrape-and-evaluate")
    return steps[_step_index(
        steps, lambda s: str(s.get("uses") or "").startswith(
            "actions/cache/restore"))].get("with") or {}


def _key_prefixes(block: dict) -> list[str]:
    """Every literal cache-key prefix a step names — `key` plus each
    `restore-keys` line, cut at the first ${{ }} expression.

    Both, not just `key`. Every restore step here keys on `github.run_id`, which
    by construction can never match an existing entry, so what a restore
    actually finds is decided entirely by `restore-keys`. Checking `key` alone
    would wave through a bump of the half that does the matching: the restore
    then finds nothing and the paired save still writes under the OLD prefix,
    handing the next daily a cache with one run's work in it."""
    raw = [str(block.get("key") or ""),
           *str(block.get("restore-keys") or "").splitlines()]
    return [r.split("${{")[0].strip() for r in raw if r.strip()]


def _all_cache_steps() -> list[Step]:
    """Every actions/cache step that touches the shared pipeline state.

    Matched on what it CACHES, not on the key it names. Keying the scan off the
    key looks natural and is backwards: a step that drifts its key would drop
    out of the scan entirely and so escape the very rules below, leaving a
    hand-tuned "we expect N steps" count as the only thing standing between a
    half-bumped key and a silently forked lineage. A cache of something
    unrelated names none of these paths and is still free to key itself however
    it likes.

    Matched through _covers, so naming a PARENT counts too. Collapsing the nine
    lines to the one directory that contains them (`path: career-ops`) is the
    tempting simplification — it is the shape the upload comment in
    export-reports.yml warns about — and on exact membership it would match none
    of the nine and drop out of the scan, escaping both rules below."""
    return [s for s in _steps_using("actions/cache")
            if any(_covers(e, c)
                   for e in _path_entries(s.block) for c in CACHED_PATHS)]


class TestRetentionIsDeclaredAndBounded:
    def test_the_scan_finds_the_upload_steps(self):
        """A collection bug that returned [] would make this file pass forever
        — which is the same failure it exists to prevent, one level up."""
        found = _all_upload_steps()
        assert len(found) >= 2, f"only found {len(found)} upload-artifact steps"
        assert {u.workflow for u in found} >= {DAILY, "edit-tracker.yml"}

    def test_every_upload_sets_retention_days(self):
        missing = [u.label for u in _all_upload_steps()
                   if "retention-days" not in u.block]
        assert not missing, (
            "upload-artifact steps with no retention-days (they inherit the "
            f"repository default, which is 90 days): {missing}"
        )

    def test_no_upload_exceeds_the_ceiling(self):
        over = [(u.label, u.block["retention-days"]) for u in _all_upload_steps()
                if int(u.block.get("retention-days", 0)) > RETENTION_CEILING]
        assert not over, (
            f"upload-artifact steps above the {RETENTION_CEILING}-day ceiling: {over}. "
            "Raising it is a decision about a copy's Actions storage quota — "
            "change RETENTION_CEILING here deliberately if that is what you mean."
        )


class TestNoUploadCarriesAccumulatedState:
    def test_every_cached_path_is_classified(self):
        """CACHED_PATHS must name the daily's cache list exactly. A path added
        to the cache but not here would be unclassified — and so exempt from the
        check below, silently, which is the shape of the original bug."""
        assert sorted(_path_entries(_daily_cache_step())) == sorted(CACHED_PATHS), (
            "the daily's cached paths and CACHED_PATHS have diverged. Classify "
            "the new one as 'grows' (restored and never truncated — must not be "
            "uploaded whole) or 'whole' (small, current value is the point). "
            "Making that call is the point of this test."
        )

    def test_no_scheduled_upload_names_a_cached_path(self):
        """Cached state reaches a scheduled artifact through the staging step or
        not at all — that is what bounds the artifact to one run's work. A
        manual export is exempt and says so in its own header."""
        offenders = [
            f"{u.label} -> {entry}"
            for u in _all_upload_steps() if u.scheduled
            for entry in _path_entries(u.block)
            for cached in CACHED_PATHS
            if _covers(entry, cached)
        ]
        assert not offenders, (
            "these SCHEDULED upload steps name state restored from the "
            f"pipeline-state cache: {offenders}. Stage this run's own output "
            "instead — see pipeline/run_artifact.py and the 'Stage this run's "
            "output for upload' step in daily-pipeline.yml. If the whole history "
            "really is wanted, it belongs in a workflow_dispatch-only workflow "
            "like export-reports.yml, not on a cron."
        )

    def test_the_stage_step_matches_the_classification(self):
        """CACHED_PATHS is only a claim unless the staging step obeys it: every
        "delta" path deltaed, every "whole" path shipped entire, and nothing
        classified "local" named at all."""
        stage = _stage_args()
        deltaed = {f"{CAREER_OPS}/{r}" for r in stage["delta"]}
        wholed = {f"{CAREER_OPS}/{r}" for r in stage["whole"]}

        assert deltaed == {p for p, k in CACHED_PATHS.items() if k == "delta"}
        assert wholed & set(CACHED_PATHS) == {
            p for p, k in CACHED_PATHS.items() if k == "whole"}
        leaked = sorted(p for p in wholed if CACHED_PATHS.get(p) == "local")
        assert not leaked, (
            f"{leaked} are classified 'local' but the daily ships them whole. "
            "Both pipeline.md and easy-apply-urls.txt are append-only and were "
            "removed for exactly that reason (issue #129)."
        )

    def test_the_daily_stages_before_it_uploads(self):
        """The staging step and the manifest it needs are what make the daily
        artifact O(one run) instead of O(all history), and the upload has to be
        pointed at the staged tree for that to mean anything."""
        steps = _steps(DAILY, "scrape-and-evaluate")

        def at(predicate):
            return _step_index(steps, predicate)

        def runs(needle):
            return lambda s: needle in str(s.get("run") or "")

        def uses(action):
            return lambda s: str(s.get("uses") or "").startswith(action)

        restore = at(uses("actions/cache/restore"))
        snapshot = at(runs("run_artifact snapshot"))
        pipeline = at(runs("orchestrate.py"))
        stage = at(runs("run_artifact stage"))
        upload = at(uses("actions/upload-artifact"))

        # The snapshot has exactly one correct slot. Before the restore it sees
        # an empty tree, so every restored report looks new and the artifact is
        # unbounded again; after the pipeline it sees this run's reports too and
        # the artifact is empty. Matched by content and asserted as relative
        # order, so renaming or inserting a step doesn't churn this.
        assert restore < snapshot < pipeline < stage < upload, (
            "step order is wrong for the snapshot/stage pair: "
            f"{[s.get('name', '') for s in steps]}")

        upload_path = str(steps[upload]["with"]["path"])
        assert "pipeline-artifact" in upload_path, (
            f"the daily must upload the staged tree, not {upload_path!r}")

    def test_the_staged_tree_keeps_the_layout_the_ui_reads(self):
        """`gh.download_artifact` looks for reports/ or data/ at the artifact
        root and `data.sync_pulled_tracker` reads data/applications.md from
        there. The staging step's arguments are what decide that now."""
        stage = _stage_args()
        assert stage["root"] == [CAREER_OPS]
        assert "reports" in stage["delta"]
        assert "data/applications.md" in stage["whole"]

    def test_the_export_artifact_extracts_over_career_ops(self):
        """The manual export's half of the layout rule the daily already has
        above. upload-artifact roots the archive at the common ancestor of the
        paths it is GIVEN, and only when more than one is named — so dropping
        `data/applications.md` re-roots it to career-ops/reports and it extracts
        as loose *.md instead of overlaying a local career-ops/. Nothing about
        that is visible in the diff that does it."""
        upload = next(u for u in _all_upload_steps()
                      if u.workflow == "export-reports.yml")
        entries = _path_entries(upload.block)
        assert len(entries) >= 2 and _common_root(entries) == CAREER_OPS, (
            f"the export names {entries}, which roots its artifact at "
            f"{_common_root(entries)!r} rather than {CAREER_OPS!r}. Extracting "
            "it over a local career-ops/ is what the workflow header promises "
            "and what run-ui.sh --data expects."
        )

    def test_snapshot_and_stage_agree_on_what_the_manifest_covers(self):
        """Different --root or --delta between the two steps and the manifest's
        keys stop lining up: every restored report reads as new and the whole
        history goes back into the artifact. run_artifact refuses a mismatch at
        runtime; this catches it at review time, when it is still free."""
        snap = _run_artifact_args("snapshot")
        stage = _stage_args()
        assert snap["root"] == stage["root"], (snap["root"], stage["root"])
        assert snap["delta"] == stage["delta"], (snap["delta"], stage["delta"])
        assert snap["manifest"] == stage["manifest"], "same manifest file, too"


class TestEveryCacheStepSharesOnePathSet:
    """Issue #133. The path set IS the cache version, so this is not a style
    rule about keeping files tidy — a workflow that names a different set is
    talking to a different cache, and says nothing about it in its logs."""

    def test_the_scan_finds_the_cache_steps(self):
        """Same reason as the upload scan: a collector that returned [] would
        make the rules below pass forever. Naming the workflows rather than
        counting steps — a count says nothing about WHICH ones went missing, and
        an empty scan fails this just as surely."""
        found = {c.workflow for c in _all_cache_steps()}
        want = {DAILY, "edit-tracker.yml", "seed-reports.yml", "export-reports.yml"}
        assert found >= want, (
            f"the cache-step scan missed {sorted(want - found)}. Either the "
            "workflow stopped touching the pipeline-state cache (then drop it "
            "from this list, deliberately) or the collector no longer sees it "
            "— which would exempt it from every rule below."
        )

    def test_every_cache_step_names_the_same_path_set(self):
        """Against the daily's own list rather than CACHED_PATHS, so this test
        and test_every_cached_path_is_classified fail on disjoint causes: adding
        a path to every workflow but not the table is that test's job, and its
        message is the one worth reading for it."""
        daily = _raw_path_lines(_daily_cache_step())
        wrong = []
        for c in _all_cache_steps():
            entries = _raw_path_lines(c.block)
            if entries != daily:
                missing, extra = set(daily) - set(entries), set(entries) - set(daily)
                why = (f"missing={sorted(missing)}, extra={sorted(extra)}"
                       if (missing or extra) else "same paths, different ORDER")
                wrong.append(f"{c.label} ({why})")
        assert not wrong, (
            "these cache steps name a different path set than the daily, so "
            f"they restore from and save to a lineage of their own: {wrong}. "
            "GitHub hashes the path list into the cache version and matches a "
            "restore on key prefix AND version — a shorter list restores "
            "NOTHING, it does not restore a subset. Narrow at the upload step "
            "instead, the way export-reports.yml does. The list is hashed as "
            "written, so order and trailing slashes count as much as contents."
        )

    def test_every_cache_step_shares_the_daily_key_prefix(self):
        """The other half of "same cache". Paths decide the version; the key
        prefix decides which entries a restore will even look at, and half a
        key bump forks the lineage exactly as a path edit does."""
        want = set(_key_prefixes(_daily_cache_step()))
        assert len(want) == 1, f"the daily names more than one prefix: {want}"
        wrong = [f"{c.label} (names {sorted(set(_key_prefixes(c.block)))})"
                 for c in _all_cache_steps()
                 if set(_key_prefixes(c.block)) != want]
        assert not wrong, (
            f"these cache steps do not share the daily's key prefix {want}: "
            f"{wrong}. Bumping the cache lineage is a deliberate act — move "
            "every step in the same commit, or the ones left behind keep "
            "reading and writing the old entries."
        )


class TestTheGuardsCatchWhatTheyForbid:
    """A guard is only worth having if it fails on the thing it forbids."""

    def _fake(self, monkeypatch, with_block, *, scheduled=True):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_upload_steps",
            lambda: [Step("fake.yml", "fake.yml:job:Upload", with_block, scheduled)])

    def test_missing_retention_is_caught(self, monkeypatch):
        self._fake(monkeypatch, {"path": "out/"})
        with pytest.raises(AssertionError, match="no retention-days"):
            TestRetentionIsDeclaredAndBounded().test_every_upload_sets_retention_days()

    def test_over_ceiling_retention_is_caught(self, monkeypatch):
        self._fake(monkeypatch, {"path": "out/", "retention-days": 90})
        with pytest.raises(AssertionError, match="ceiling"):
            TestRetentionIsDeclaredAndBounded().test_no_upload_exceeds_the_ceiling()

    def test_uploading_the_reports_dir_is_caught(self, monkeypatch):
        self._fake(monkeypatch, {
            "path": "career-ops/reports/\ncareer-ops/data/applications.md\n",
            "retention-days": 7,
        })
        with pytest.raises(AssertionError, match="restored from the"):
            TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_uploading_a_parent_of_the_reports_dir_is_caught(self, monkeypatch):
        """`path: career-ops` ships reports/ just as surely, and is what someone
        reaching for "upload everything" would actually write."""
        self._fake(monkeypatch, {"path": "career-ops", "retention-days": 7})
        with pytest.raises(AssertionError, match="restored from the"):
            TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_a_dot_slash_prefix_does_not_evade_the_check(self, monkeypatch):
        self._fake(monkeypatch, {"path": "./career-ops/reports/", "retention-days": 7})
        with pytest.raises(AssertionError, match="restored from the"):
            TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def test_a_manual_export_of_the_same_path_is_allowed(self, monkeypatch):
        """The rule is about crons, not about the path. export-reports.yml
        uploads exactly this, on purpose, and must not trip the guard."""
        self._fake(monkeypatch, {"path": "career-ops", "retention-days": 7},
                   scheduled=False)
        TestNoUploadCarriesAccumulatedState().test_no_scheduled_upload_names_a_cached_path()

    def _daily_paths(self) -> list[str]:
        """The reference list, in the daily's own order — the fakes below have
        to differ from it in exactly one way to test what they claim to."""
        return _raw_path_lines(_daily_cache_step())

    def _fake_cache(self, monkeypatch, path, key="pipeline-state-v1-${{ x }}",
                    restore_keys="pipeline-state-v1-"):
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._all_cache_steps",
            lambda: [Step("fake.yml", "fake.yml:job:Restore",
                          {"path": path, "key": key,
                           "restore-keys": restore_keys}, False)])

    def test_a_short_cache_path_list_is_caught(self, monkeypatch):
        """export-reports.yml's actual bug: two of the nine paths, which reads
        like "restore only what I need" and in fact restores nothing."""
        self._fake_cache(
            monkeypatch, "career-ops/data/applications.md\ncareer-ops/reports\n")
        with pytest.raises(AssertionError, match="lineage of their own"):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_names_the_same_path_set()

    def test_a_cache_path_list_with_an_extra_entry_is_caught(self, monkeypatch):
        """Drift in the other direction changes the version just as much."""
        self._fake_cache(
            monkeypatch, "\n".join([*self._daily_paths(), "career-ops/data/extra.tsv"]))
        with pytest.raises(AssertionError, match="extra="):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_names_the_same_path_set()

    def test_a_reordered_cache_path_list_is_caught(self, monkeypatch):
        """The same nine paths in a different order hash to a different version,
        so this forks the lineage while looking identical to any comparison that
        sorts or sets. It is also the most innocent-looking edit in the class."""
        self._fake_cache(monkeypatch, "\n".join(reversed(self._daily_paths())))
        with pytest.raises(AssertionError, match="different ORDER"):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_names_the_same_path_set()

    def test_a_trailing_slash_is_caught(self, monkeypatch):
        """Nothing normalizes the list before it is hashed, so `reports/` and
        `reports` are two different caches."""
        self._fake_cache(monkeypatch, "\n".join(e + "/" for e in self._daily_paths()))
        with pytest.raises(AssertionError, match="lineage of their own"):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_names_the_same_path_set()

    def test_a_bumped_restore_key_alone_is_caught(self, monkeypatch):
        """restore-keys is what a restore actually matches on — every restore
        here keys on github.run_id, which can never hit an existing entry. Bump
        only this half and the restore finds nothing while the paired save still
        writes under the old prefix, which is worse than the drift in #133."""
        self._fake_cache(monkeypatch, "\n".join(self._daily_paths()),
                         restore_keys="pipeline-state-v2-")
        with pytest.raises(AssertionError, match="key prefix"):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_shares_the_daily_key_prefix()

    def test_a_parent_directory_path_stays_in_the_scan(self, monkeypatch):
        """Collapsing the nine lines to the directory that holds them is the
        tempting simplification. On exact membership it matched none of the nine
        and left the scan entirely — escaping both rules rather than failing."""
        monkeypatch.setattr(
            "tests.test_workflow_artifacts._steps_using",
            lambda action: [Step("fake.yml", "fake.yml:job:Restore",
                                 {"path": CAREER_OPS, "key": "pipeline-state-v1-"},
                                 False)])
        assert len(_all_cache_steps()) == 1, "a parent path escaped the scan"
        with pytest.raises(AssertionError, match="lineage of their own"):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_names_the_same_path_set()

    def test_a_half_bumped_cache_key_is_caught(self, monkeypatch):
        """The path set can be perfect and the lineage still fork. Before this
        check, such a step simply left the scan and nothing failed but a
        hand-tuned count of how many steps we expected to find."""
        self._fake_cache(monkeypatch, "\n".join(CACHED_PATHS),
                         key="pipeline-state-v2-${{ x }}")
        with pytest.raises(AssertionError, match="key prefix"):
            TestEveryCacheStepSharesOnePathSet().test_every_cache_step_shares_the_daily_key_prefix()

    def test_an_unclassified_cache_path_is_caught(self, monkeypatch):
        """The exhaustiveness half: a path added to the cache with no entry in
        CACHED_PATHS must fail here rather than quietly skip the checks above."""
        monkeypatch.setitem(CACHED_PATHS, "career-ops/data/something-new.tsv", "local")
        with pytest.raises(AssertionError, match="diverged"):
            TestNoUploadCarriesAccumulatedState().test_every_cached_path_is_classified()
