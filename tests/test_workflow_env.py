"""The daily's environment contract, and every job's clock.

A workflow's `env:` block is a mirror of constants that live in Python — the
digest's secret and setting names (`pipeline/daily_digest.py`), the re-check's
defaults (`pipeline/recheck.py`), the free-tier knobs (`pipeline/gemini_limits.py`)
— and a mirror with no guard is a bug with a delay on it (the rule
tests/test_app_onboard.py applies to the sites list). So the expectations here
are DERIVED from those constants rather than restated: rename a setting in the
module and this fails naming the workflow line, not six weeks later when a
user's `DIGEST_LIMIT` variable silently stops doing anything.

The second half is the account's Actions budget. GitHub's default job timeout
is 360 minutes, and the Free plan is 2,000 Linux minutes a month per ACCOUNT
for private repositories — one hung LinkedIn scrape at the default is 18% of
the month. Every job therefore declares `timeout-minutes`, and the daily's is
held under a ceiling the way tests/test_workflow_artifacts.py holds retention.

Reuses that file's readers (`_steps`, `_run_artifact_args`, `_triggers`) so
the two guards parse the workflow the same way.
"""

import re
from pathlib import Path

import yaml

from pipeline import daily_digest, recheck
from tests.test_workflow_artifacts import (
    DAILY,
    WORKFLOWS,
    _doc,
    _run_artifact_args,
    _step_index,
    _steps,
)

JOB = "scrape-and-evaluate"

# The daily's job may not exceed this. The workflow's value (150) sits above
# the real run length rather than at the budget: on Gemini's free tier the
# recommended row (16,000 TPM, ~9,500 tokens a call) paces 160–190 evaluations
# to ~95–115 minutes on their own, and a timeout cancel does not delay those
# evaluations, it loses them — the batch exits before the merge and
# tracker-additions is not cached (see the comment on the job). The budget is
# guarded by the digest's health line, not this clock. Raising the ceiling is
# a decision about minutes; lowering it below a real run is a decision to
# throw evaluations away.
DAILY_TIMEOUT_CEILING = 180


def _job_steps():
    return _steps(DAILY, JOB)


def _step(name_fragment: str) -> dict:
    steps = _job_steps()
    return steps[_step_index(steps, lambda s: name_fragment in str(s.get("name", "")))]


def _digest_step() -> dict:
    return _step("digest")


def _run_step() -> dict:
    steps = _job_steps()
    return steps[_step_index(steps, lambda s: "orchestrate.py" in str(s.get("run") or ""))]


def _digest_args() -> dict[str, list[str]]:
    """The digest step's `python -m pipeline.daily_digest …`, parsed the way
    `_run_artifact_args` parses the snapshot/stage steps."""
    tokens = str(_digest_step()["run"]).replace("\\\n", " ").split()
    args: dict[str, list[str]] = {}
    flag = None
    for tok in tokens:
        if tok.startswith("--"):
            flag = tok[2:]
            args.setdefault(flag, [])
        elif flag is not None:
            args[flag].append(tok)
    return args


def _expr(kind: str, name: str, default: str | None = None) -> re.Pattern:
    """`${{ secrets.NAME }}` or `${{ vars.NAME || '<default>' }}`, whitespace-tolerant."""
    if kind == "secrets":
        return re.compile(r"^\$\{\{\s*secrets\." + re.escape(name) + r"\s*\}\}$")
    return re.compile(r"^\$\{\{\s*vars\." + re.escape(name)
                      + r"\s*\|\|\s*'" + re.escape(default or "") + r"'\s*\}\}$")


class TestDigestStep:
    def test_runs_right_after_the_pipeline_and_before_the_cache_save(self):
        """After the run so it sees this run's reports; before the save because
        the save is the end of the critical section and the notice should
        never wait on it. `if: always()` because a FAILED run is the one thing
        it must not stay quiet about."""
        steps = _job_steps()

        def at(pred):
            return _step_index(steps, pred)

        run = at(lambda s: "orchestrate.py" in str(s.get("run") or ""))
        digest = at(lambda s: "pipeline.daily_digest" in str(s.get("run") or ""))
        save = at(lambda s: str(s.get("uses") or "").startswith("actions/cache/save"))
        assert run + 1 == digest < save, [s.get("name") for s in steps]
        assert str(steps[digest].get("if")).strip() == "always()"

    def test_env_is_derived_from_the_module_constants(self):
        env = {k: str(v) for k, v in (_digest_step().get("env") or {}).items()}
        wrong = []
        for name in daily_digest.SECRET_VARS:
            if not _expr("secrets", name).match(env.get(name, "")):
                wrong.append(f"{name}: want secrets.{name}, got {env.get(name)!r}")
        for name, default in daily_digest.SETTING_VARS.items():
            if not _expr("vars", name, default).match(env.get(name, "")):
                wrong.append(f"{name}: want vars.{name} || '{default}', got {env.get(name)!r}")
        assert not wrong, wrong

    def test_no_digest_name_outside_the_constants(self):
        """Exhaustive in both directions: a DIGEST_* the workflow sets that the
        module never reads is a setting that silently does nothing."""
        env = _digest_step().get("env") or {}
        known = set(daily_digest.SECRET_VARS) | set(daily_digest.SETTING_VARS) \
            | set(daily_digest.RUN_VARS)
        assert set(env) == known, (set(env) ^ known)

    def test_run_facts_reach_the_digest(self):
        env = {k: str(v) for k, v in (_digest_step().get("env") or {}).items()}
        assert "github.run_id" in env["RUN_URL"] and "actions/runs" in env["RUN_URL"]
        assert _run_step().get("id") == "run", "the run step needs `id: run` for its outcome"
        assert re.fullmatch(r"\$\{\{\s*steps\.run\.outcome\s*\}\}", env["RUN_OUTCOME"])
        m = re.fullmatch(r"\$\{\{\s*steps\.(\w+)\.outputs\.(\w+)\s*\}\}", env["RUN_STARTED_AT"])
        assert m, env["RUN_STARTED_AT"]
        steps = _job_steps()
        started = steps[_step_index(steps, lambda s: s.get("id") == m.group(1))]
        assert steps.index(started) == 0, "the clock starts before checkout, where billing does"
        assert f"{m.group(2)}=$(date +%s)" in str(started["run"])

    def test_reads_the_snapshot_manifest(self):
        """`--root`/`--manifest` must be the snapshot step's, and `DELTA` must
        be among what the snapshot covers — otherwise the digest's "new this
        run" is a diff of two different things. run_artifact refuses the
        mismatch at runtime; this catches it at review time."""
        snap = _run_artifact_args("snapshot")
        digest = _digest_args()
        assert digest["root"] == snap["root"], (digest["root"], snap["root"])
        assert digest["manifest"] == snap["manifest"], (digest["manifest"], snap["manifest"])
        assert daily_digest.DELTA in snap["delta"], snap["delta"]

    def test_the_read_only_note_is_a_yaml_comment_above_the_step(self):
        """The artifact guard forbids a scheduled UPLOAD of cached paths; the
        digest only reads them, and says so where a reviewer of the YAML sees
        it rather than inside the shell script."""
        text = (WORKFLOWS / DAILY).read_text(encoding="utf-8")
        before = text[: text.index("- name: Send the daily digest")]
        comment = before[before.rfind("\n\n"):]
        assert "READS cached paths" in comment and "writes nothing" in comment
        assert all(l.strip().startswith("#") for l in comment.strip().splitlines())


class TestRunStepEnv:
    def _env(self) -> dict[str, str]:
        return {k: str(v) for k, v in (_run_step().get("env") or {}).items()}

    def test_recheck_vars_fall_back_to_the_code_default(self, monkeypatch):
        """`|| ''`, not `|| '100'`: recheck.py reads both through env_float,
        which returns ITS default on an empty string. A digit here would be a
        second copy of that default, free to drift."""
        env = self._env()
        for name in ("RECHECK_BUDGET", "RECHECK_MIN_AGE_HOURS"):
            assert re.fullmatch(r"\$\{\{\s*vars\." + name + r"\s*\|\|\s*''\s*\}\}", env[name]), env[name]
            assert not re.search(r"\|\|\s*'\d", env[name])
        # And the property the empty fallback rests on, against the real reader.
        monkeypatch.setenv("RECHECK_BUDGET", "")
        assert recheck._resolve_budget(None) == recheck._DEFAULT_BUDGET

    def test_gemini_free_tier_knobs_reach_the_eval_step(self):
        env = self._env()
        assert _expr("vars", "GEMINI_FREE_TIER", "").match(env["GEMINI_FREE_TIER"]), env["GEMINI_FREE_TIER"]
        assert re.fullmatch(r"\$\{\{\s*runner\.temp\s*\}\}/gemini-limits\.json", env["GEMINI_LIMITS_FILE"])

    def test_gemini_limits_are_written_under_runner_temp_before_the_run(self):
        steps = _job_steps()
        write = _step_index(steps, lambda s: "gemini-limits.json" in str(s.get("run") or ""))
        run = _step_index(steps, lambda s: "orchestrate.py" in str(s.get("run") or ""))
        assert write < run
        step = steps[write]
        assert re.search(r"vars\.GEMINI_LIMITS_JSON\s*!=\s*''", str(step.get("if")))
        assert '"$RUNNER_TEMP/gemini-limits.json"' in str(step["run"])
        assert "GEMINI_LIMITS_JSON" in (step.get("env") or {})


class TestEveryJobHasAClock:
    def _jobs(self):
        for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                yield f"{path.name}:{job_name}", job

    def test_the_scan_finds_the_jobs(self):
        assert len(list(self._jobs())) >= 8

    def test_every_job_declares_timeout_minutes(self):
        missing = [label for label, job in self._jobs()
                   if not isinstance(job.get("timeout-minutes"), int)]
        assert not missing, (
            f"jobs with no timeout-minutes (GitHub's default is 360, against a "
            f"2,000-minute monthly budget per account): {missing}")

    def test_the_daily_is_under_the_ceiling(self):
        timeout = _doc(DAILY)["jobs"][JOB]["timeout-minutes"]
        assert 0 < timeout <= DAILY_TIMEOUT_CEILING, timeout

    def test_every_other_job_is_bounded_tighter(self):
        """Every job that neither scrapes nor evaluates: 30 minutes covers a
        cache restore, a merge, an export — and the PR gate, which is not
        dispatched by hand but runs in ~3 minutes on wheels. The one way it
        could exceed this is a pin whose Windows wheel disappears, turning an
        install into a source build; that would be reported here as a timeout
        rather than as the build failure it is, so raise this deliberately
        rather than reading the red X as flakiness."""
        for label, job in self._jobs():
            if label.startswith(DAILY):
                continue
            assert job["timeout-minutes"] <= 30, (label, job["timeout-minutes"])


class TestTheGuardsCatchWhatTheyForbid:
    def test_a_digit_fallback_for_recheck_is_caught(self, monkeypatch):
        monkeypatch.setattr("tests.test_workflow_env._run_step",
                            lambda: {"env": {"RECHECK_BUDGET": "${{ vars.RECHECK_BUDGET || '100' }}",
                                             "RECHECK_MIN_AGE_HOURS": "${{ vars.RECHECK_MIN_AGE_HOURS || '' }}"}})
        try:
            TestRunStepEnv().test_recheck_vars_fall_back_to_the_code_default(monkeypatch)
        except AssertionError:
            return
        raise AssertionError("a digit fallback slipped through")

    def test_a_renamed_setting_is_caught(self, monkeypatch):
        monkeypatch.setitem(daily_digest.SETTING_VARS, "DIGEST_RENAMED", "1")
        try:
            TestDigestStep().test_env_is_derived_from_the_module_constants()
        except AssertionError as exc:
            assert "DIGEST_RENAMED" in str(exc)
            return
        raise AssertionError("a setting the workflow does not wire slipped through")

    def test_a_missing_timeout_is_caught(self, monkeypatch):
        monkeypatch.setattr("tests.test_workflow_env.TestEveryJobHasAClock._jobs",
                            lambda self: iter([("fake.yml:job", {"runs-on": "ubuntu-latest"})]))
        try:
            TestEveryJobHasAClock().test_every_job_declares_timeout_minutes()
        except AssertionError as exc:
            assert "fake.yml:job" in str(exc)
            return
        raise AssertionError("a job with no clock slipped through")
