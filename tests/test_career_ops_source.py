"""Guard on WHERE career-ops comes from — one file, and every site that fetches
it held to what that file says.

career-ops used to be cloned from a fork (FrameAutomata/career-ops, branch
`dev/batch-local-llm`) that existed for two features: `batch-runner.sh --cli <id>`
with a model flag that reaches every CLI, and `modes/text.md`'s text output
format. Both are upstream now (career-ops#738 and #3343), and the fork's runner
had fallen strictly BEHIND upstream's — it still dropped `--model` for gemini and
qwen — so the checkout tracks upstream and the fork is retired.

Three sites fetch it, in three host languages with no shared step: setup.sh,
setup.ps1 and the daily workflow. Nothing stops one of them drifting from the
others — an old line pasted back from a branch that predates the move, or a pin
done in one site and not the others — and the drift is invisible where it lands:
a person's setup and the cloud daily would run different runners against the same
tracker, and the daily would say nothing. So `career-ops.ref` states the source
once, shell-sourceable, and the three sites are parsed back out and held to it.

The two audiences deliberately take different refs out of that one file. A
person's checkout tracks `CAREER_OPS_BRANCH`: it is updated in place — by a
`git pull`, or by career-ops' own update-system.mjs — and neither works on a
detached commit, so pinning it would freeze them off upstream's fixes. The daily
fetches `CAREER_OPS_COMMIT` exactly: it runs unattended against real queues, and
an upstream merge-tracker that starts refusing our rows still exits 0, so the
evaluations are lost with a warning in a log nobody opens and a digest that
cannot show a row which never reached the tracker. Bumping that pin is one line
in career-ops.ref (which documents the contract tests to run against a candidate
commit first) — true only while no site, and no test, restates a URL, a branch or
a SHA of its own. This module therefore carries none either, beyond its own
synthetic fixtures and the owner below.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

REF_FILE = "career-ops.ref"

# The one value not taken from career-ops.ref, because this is the check that
# file cannot make about itself: a ref pointed back at the retired fork would
# certify itself under every other test here. Everything else — the URL, the
# branch, the pinned commit — is the file's to say.
CAREER_OPS_OWNER = "career-ops-hq"

WORKFLOW = ".github/workflows/daily-pipeline.yml"
SETUP_SITES = ["setup.sh", "setup.ps1"]

# Where a person or a run could pick up a career-ops URL. The docs are here
# because a reader copies a clone URL out of README as readily as out of setup.
SOURCE_FILES = SETUP_SITES + [WORKFLOW, REF_FILE,
                              "README.md", "QUICKSTART.md", "CLAUDE.md"]
# Files known to name career-ops by URL, so the owner check below cannot pass by
# finding nothing. Two are deliberately absent for opposite reasons: CLAUDE.md
# may carry no URL at all, and the workflow must carry none (TestDailyFetch).
FILES_WITH_A_URL = SETUP_SITES + [REF_FILE, "README.md", "QUICKSTART.md"]

_CLONE_RE = re.compile(r"\bgit\s+clone\b")
# Anchored on `git` so actions/checkout and a YAML `fetch-depth:` are not read
# as the daily running one.
_GIT_FETCH_RE = re.compile(r"\bgit\b.*\bfetch\b")
_GIT_CHECKOUT_RE = re.compile(r"\bgit\b.*\bcheckout\b")
# `[/:]` so an ssh remote (git@github.com:owner/career-ops) is held too.
_OWNER_RE = re.compile(r"github\.com[/:]([^/\s)\]\"'<>]+)/career-ops\b")
# A line the setup scripts only SHOW the person, in either host language.
_PRINT_RE = re.compile(r"^\s*(?:echo|Write-Host)\b", re.IGNORECASE)

_REF_ASSIGN_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(\S*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# A 40-hex object name anywhere in a file, for the rule that only career-ops.ref
# may hold one.
_ANY_SHA_RE = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{40}(?![0-9a-zA-Z])")


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _logical_lines(text: str) -> list[str]:
    """Physical lines with every trailing-backslash continuation folded into the
    line it continues. A clone or fetch may put its URL on a line of its own, so
    a line-local reading sees one with no URL. The trailing `rstrip` is also what
    keeps a CRLF checkout (`\\` then `\\r`) continuing.

    A comment ending in a backslash continues nothing (in bash, or in a YAML
    `run:` block handed to bash), so it is never joined — otherwise it would
    swallow the real line under it and the site would read as having none.
    PowerShell continues with a backtick, which is not folded: a ps1 clone split
    that way reads as having no URL, which fails loudly."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        if stripped.endswith("\\") and (pending or not _is_comment(raw)):
            pending += stripped[:-1] + " "
            continue
        out.append(pending + raw)
        pending = ""
    if pending:
        out.append(pending)
    return out


def _matching_lines(text: str, pattern: re.Pattern) -> list[str]:
    """Every non-comment logical line matching `pattern`. A line that only PRINTS
    a command counts too, on purpose — a pasted clone is still a clone, the same
    reading TestMigrationHint gives a printed `npm install`. It is also what
    keeps the daily's own comment (which spells out why its fetch is not a
    `git clone`) from reading as a second source."""
    return [line.strip() for line in _logical_lines(text)
            if pattern.search(line) and not _is_comment(line)]


def _parse_clone(line: str) -> tuple[str | None, str | None]:
    """(URL, --branch) of one clone line; None for whichever it does not name."""
    url = re.search(r"https://[^\s\"']+", line)
    branch = re.search(r"(?<!\S)(?:--branch|-b)(?:=|\s+)([^\s\"']+)", line)
    return (url.group(0) if url else None, branch.group(1) if branch else None)


def _printed_and_run(text: str) -> tuple[list[str], list[str]]:
    """Split a setup script's non-comment lines into the ones it prints and the
    ones it executes. Comments are neither: a recipe written only in a comment
    never reaches the person, and a detection written only in one never runs."""
    code = [line for line in text.splitlines() if not _is_comment(line)]
    return ([line for line in code if _PRINT_RE.match(line)],
            [line for line in code if not _PRINT_RE.match(line)])


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _parse_ref(text: str) -> dict[str, str]:
    """career-ops.ref's `KEY=value` lines, comments and blanks dropped.

    Anything else is a failure rather than a skipped line, because the daily
    SOURCES this file: a stray line is not unparseable here, it is a command
    that runs in the cloud. The narrow `(\\S*)` value is the same rule from the
    other side — a value with a space, a quote or a `$` in it expands to
    something other than what is written, and the shape checks below are what
    keep this small a parser honest."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or _is_comment(line):
            continue
        m = _REF_ASSIGN_RE.match(line)
        assert m, (
            f"{REF_FILE}: not a plain KEY=value assignment, and the daily "
            f"sources this file verbatim:\n  {line}")
        out[m.group(1)] = m.group(2)
    return out


def _ref() -> dict[str, str]:
    """The source of truth, read per call rather than frozen into constants at
    import: a missing or malformed file is then the sanity test below failing
    with a message, not every test in this module erroring during collection."""
    ref = _parse_ref(_read(REF_FILE))
    missing = [k for k in ("CAREER_OPS_URL", "CAREER_OPS_BRANCH", "CAREER_OPS_COMMIT")
               if k not in ref]
    assert not missing, f"{REF_FILE} does not set {missing}"
    return ref


def _site_clone(name: str) -> tuple[str, tuple[str | None, str | None]]:
    lines = _matching_lines(_read(name), _CLONE_RE)
    # Exactly one, not "at least one": a second clone line in a site is a
    # second source of career-ops, and only the first would be checked.
    assert len(lines) == 1, (
        f"{name}: expected exactly one `git clone` line, found {len(lines)}:\n  "
        + "\n  ".join(lines)
        + "\nIf the site now clones something other than career-ops as well, "
        "narrow _CLONE_RE rather than dropping this check."
    )
    return lines[0], _parse_clone(lines[0])


class TestRefFile:
    """career-ops.ref is trusted by everything else here, so its own values are
    checked for the shapes the two consumers need."""

    def test_url_is_an_https_github_url(self):
        url = _ref()["CAREER_OPS_URL"]
        assert _OWNER_RE.search(url), (
            f"{REF_FILE}: CAREER_OPS_URL={url!r} is not a github.com "
            "<owner>/career-ops URL, so the owner check below reads nothing")
        assert url.startswith("https://"), (
            f"{REF_FILE}: CAREER_OPS_URL={url!r} — the cloud runner has no ssh "
            "key, so an ssh remote fetches nothing there")

    def test_branch_is_a_plain_ref_name(self):
        """`git clone --branch` takes a ref NAME. A SHA here fails the clone
        outright ("Remote branch <sha> not found in upstream origin"), which is
        at least loud — but it would also mean a person's checkout was pinned,
        and the branch exists precisely so it is not."""
        branch = _ref()["CAREER_OPS_BRANCH"]
        assert _REF_NAME_RE.match(branch), f"{REF_FILE}: CAREER_OPS_BRANCH={branch!r}"
        assert not _SHA_RE.match(branch), (
            f"{REF_FILE}: CAREER_OPS_BRANCH={branch!r} is a commit; the branch "
            "is what a person's checkout tracks and pulls")

    def test_commit_is_a_full_lowercase_sha(self):
        """Load-bearing, not cosmetic: `git fetch <url> <sha>` will not serve an
        ABBREVIATED object name — GitHub answers `couldn't find remote ref
        aac998c` — so a short SHA pasted here is not a shorter fetch, it is a red
        daily every morning until someone opens the Actions tab. Lowercase
        because that is what the `git rev-parse HEAD` in this file's own bump
        procedure prints, and what the server is asked to match."""
        commit = _ref()["CAREER_OPS_COMMIT"]
        assert _SHA_RE.match(commit), (
            f"{REF_FILE}: CAREER_OPS_COMMIT={commit!r} is not 40 lowercase hex "
            "characters; `git fetch` cannot serve an abbreviated commit")

    def test_has_no_carriage_returns(self):
        """The daily SOURCES this file, so a CRLF copy makes every line a command
        ending in `$'\\r'` — `command not found`, a red run every morning, on the
        one path the pin exists to make deterministic. `.gitattributes` pins it
        to LF; this is what notices the pin has stopped applying, because
        `_parse_ref` splits with `splitlines()`, which eats the `\\r` and reads a
        CRLF file as perfectly clean."""
        assert "\r" not in _read(REF_FILE), (
            f"{REF_FILE} has CRLF line endings and the daily sources it on a "
            "Linux runner — see the `career-ops.ref text eol=lf` rule in "
            ".gitattributes")


@pytest.mark.parametrize("name", SETUP_SITES)
class TestSetupClonesTheBranch:
    """A person's setup, the audience that gets upstream's fixes."""

    def test_clones_the_one_source_at_the_branch(self, name):
        line, (url, branch) = _site_clone(name)
        ref = _ref()
        assert (url, branch) == (ref["CAREER_OPS_URL"], ref["CAREER_OPS_BRANCH"]), (
            f"{name} clones {url} at {branch!r}; both setup scripts must clone "
            f"{ref['CAREER_OPS_URL']} at {ref['CAREER_OPS_BRANCH']!r}.\n  {line}\n"
            f"Moving either is one line in {REF_FILE} plus both clone lines.")

    def test_clone_keeps_history(self, name):
        """A person's checkout is not throwaway: it is updated in place, by a
        `git pull` or by career-ops' own `update-system.mjs`. The updater's
        warning about local edits to the system files it is about to overwrite is
        computed from history — its last update commit, else the merge-base with
        upstream — and its own comment names a shallow clone among the unreadable
        refs on which that warning degrades rather than aborts. So what a shallow
        checkout risks losing is the warning, quietly, not the update. The
        daily's `--depth 1` is right for the daily and wrong to copy here."""
        line, _ = _site_clone(name)
        assert not re.search(r"--depth|--shallow", line), (
            f"{name} makes a shallow clone:\n  {line}")


class TestDailyFetchesThePin:
    """The cloud daily, the audience that gets determinism — and the half of
    "bumping the pin is one line" that only a guard can hold: every value in
    these lines is a variable out of career-ops.ref, so the workflow itself
    names no source at all."""

    def test_does_not_clone(self):
        """`git clone --branch` takes a ref name, so a clone cannot land on the
        pinned commit — it would be back on whatever the branch is at 12:00 UTC,
        which is the thing this design gave up."""
        lines = _matching_lines(_read(WORKFLOW), _CLONE_RE)
        assert not lines, (
            f"{WORKFLOW} clones career-ops instead of fetching the pin:\n  "
            + "\n  ".join(lines))

    def test_sources_the_ref_file(self):
        run = [line for line in _logical_lines(_read(WORKFLOW))
               if not _is_comment(line)]
        # `.` or bash's `source`: the check is that the file is sourced at all,
        # and a guard that calls a working line missing is one that gets deleted
        # rather than fixed.
        assert any(re.search(rf"(?:^|\s)(?:\.|source)\s+\./{re.escape(REF_FILE)}(?!\S)", line)
                   for line in run), (
            f"{WORKFLOW} does not source ./{REF_FILE}, so the variables its "
            "fetch expands are empty")

    def _fetch_line(self) -> str:
        lines = _matching_lines(_read(WORKFLOW), _GIT_FETCH_RE)
        assert len(lines) == 1, (
            f"{WORKFLOW}: expected exactly one `git … fetch`, found "
            f"{len(lines)}:\n  " + "\n  ".join(lines))
        return lines[0]

    def test_fetches_the_pinned_commit_and_not_a_branch(self):
        line = self._fetch_line()
        assert re.search(r"\$\{?CAREER_OPS_COMMIT\b", line), (
            f"{WORKFLOW} does not fetch \"$CAREER_OPS_COMMIT\":\n  {line}\n"
            f"A branch tip, or a SHA pasted here, is a second source of truth "
            f"that {REF_FILE} cannot move.")
        assert not re.search(r"\$\{?CAREER_OPS_BRANCH\b", line), (
            f"{WORKFLOW} fetches the branch, which is the one ref this job must "
            f"not run: it evaluates real queues unattended.\n  {line}")

    def test_fetch_is_shallow(self):
        """The daily fetches on every run into a throwaway runner and only ever
        executes the checkout, so history is minutes and bytes spent on
        nothing."""
        line = self._fetch_line()
        assert re.search(r"(?<!\S)--depth(?:=|\s+)1(?!\S)", line), line

    def test_checks_out_what_it_fetched(self):
        """A shallow fetch of one commit leaves no branch to be on; naming
        FETCH_HEAD (or the same variable again) is what makes the checkout the
        commit that was fetched rather than whatever `git init` left behind."""
        lines = _matching_lines(_read(WORKFLOW), _GIT_CHECKOUT_RE)
        assert any(re.search(r"\bFETCH_HEAD\b|\$\{?CAREER_OPS_COMMIT\b", line)
                   for line in lines), (
            f"{WORKFLOW} does not check out the fetched commit:\n  "
            + "\n  ".join(lines))

    def test_names_no_source_of_its_own(self):
        """Comments included. A URL or a SHA in a comment is still a second copy
        that goes stale silently, and it is the paste a later edit promotes into
        the `run:` block."""
        text = _read(WORKFLOW)
        assert not _OWNER_RE.findall(text), (
            f"{WORKFLOW} names a career-ops URL; it must take it from "
            f"$CAREER_OPS_URL so {REF_FILE} stays the only place to move it")
        # `uses:` excluded: pinning an ACTION by digest is GitHub's own
        # supply-chain advice, and it has nothing to do with the career-ops pin.
        # Left in, this fired on a correct change with a message blaming the
        # wrong subject, on the one file where the right move is non-obvious.
        body = [line for line in text.splitlines()
                if not re.match(r"\s*(?:-\s*)?uses:", line)]
        assert not _ANY_SHA_RE.findall("\n".join(body)), (
            f"{WORKFLOW} carries a literal commit; the pin lives in {REF_FILE} "
            "and bumping it must not mean editing the workflow too")


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_every_career_ops_url_names_the_one_owner(name):
    """Held by owner rather than by exact URL, so a link to an upstream PR or a
    file in the repo passes while any other owner's copy — the retired fork, or
    the pre-transfer santifer path GitHub still redirects — does not."""
    owners = _OWNER_RE.findall(_read(name))
    if name in FILES_WITH_A_URL:
        assert owners, (
            f"{name}: no github.com/<owner>/career-ops URL found — either the "
            "URL was removed (drop it from FILES_WITH_A_URL) or the pattern "
            "stopped matching, and this check is passing on nothing."
        )
    stray = sorted({o for o in owners if o != CAREER_OPS_OWNER})
    assert not stray, f"{name} names career-ops under {stray}, not {CAREER_OPS_OWNER!r}"


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_retired_fork_branch_is_gone(name):
    assert "dev/batch-local-llm" not in _read(name), (
        f"{name} still names the retired fork's branch `dev/batch-local-llm`")


@pytest.mark.parametrize("name", SETUP_SITES)
class TestMigrationHint:
    """A checkout cloned from the fork is left alone — setup skips the clone
    when career-ops/ exists, and its git state is the person's, so setup never
    rewrites a remote. What it does instead is detect the fork's origin and
    print the move.

    Where each piece lives is checked, not just that the string is somewhere in
    the file. setup already RUNS `npm install --ignore-scripts` for its own
    install, so a file-wide search found the recipe's install step even with it
    deleted; and a recipe step that setup executed rather than printed would
    satisfy a file-wide search while doing the one thing setup must not."""

    # Run by setup. The bare path, not a URL, so an ssh origin is detected too.
    DETECTION = ["remote get-url origin", "FrameAutomata/career-ops"]

    # Printed by setup, each a step the move does not work without. Written as
    # templates, filled from career-ops.ref: the recipe moves a checkout onto
    # the branch a fresh clone would have landed on, so it cannot hold values of
    # its own — and the move is branch-shaped, which is the pin's absence here
    # rather than an oversight.
    RECIPE_GIT = [
        "remote set-url origin {url}",
        # Without it `origin/{branch}` is still the FORK's — the fork has one, at
        # the 2026-08-25 merge-base — so the checkout below lands on the old
        # runner under the new remote's name, and nothing errors.
        "fetch origin",
        # `-b`, never `-B`: a local branch with commits of its own must be
        # refused, not silently moved to upstream's.
        "checkout -b {branch} origin/{branch}",
    ]
    # merge-tracker imports js-yaml, and upstream's postinstall downloads a
    # browser setup installs on its own terms. Printed-only is all that can be
    # asked of it: setup's own install runs the same string.
    RECIPE = RECIPE_GIT + ["npm install --ignore-scripts"]

    @staticmethod
    def _fill(step: str) -> str:
        ref = _ref()
        return step.format(url=ref["CAREER_OPS_URL"], branch=ref["CAREER_OPS_BRANCH"])

    @pytest.mark.parametrize("needle", DETECTION)
    def test_detects_the_fork(self, name, needle):
        # Not the clone line: the pre-move one named the fork too, and would
        # have stood in for a detection that did not exist.
        _, run = _printed_and_run(_read(name))
        assert any(needle in line for line in run if not _CLONE_RE.search(line)), (
            f"{name}'s retired-fork detection does not run {needle!r}")

    @pytest.mark.parametrize("step", RECIPE)
    def test_prints_the_move(self, name, step):
        needle = self._fill(step)
        printed, _ = _printed_and_run(_read(name))
        assert any(needle in line for line in printed), (
            f"{name}'s retired-fork hint does not print {needle!r}")

    def test_never_makes_the_move_itself(self, name):
        _, run = _printed_and_run(_read(name))
        steps = [self._fill(step) for step in self.RECIPE_GIT]
        ran = [line.strip() for line in run if any(step in line for step in steps)]
        assert not ran, (
            f"{name} rewrites the person's career-ops checkout instead of "
            "printing the move:\n  " + "\n  ".join(ran))


# The readers above are only worth having if they fail on the things they
# forbid, and each of these fixtures is synthetic for that reason — they are the
# one place in this module allowed to spell a URL, a ref or a SHA.
def test_parser_reads_a_continued_clone_and_skips_a_commented_one():
    # The commented line ends in a backslash on purpose: joined, it would eat
    # the real clone beneath it and the site would read as having none.
    text = (
        "# git clone --branch old https://github.com/old/career-ops dest \\\n"
        "git clone --depth 1 --branch main \\\n"
        "    https://github.com/x/career-ops dest\n"
    )
    lines = _matching_lines(text, _CLONE_RE)
    assert len(lines) == 1
    assert _parse_clone(lines[0]) == ("https://github.com/x/career-ops", "main")


def test_guard_catches_the_retired_clone_line():
    old = "git clone --branch dev/batch-local-llm https://github.com/FrameAutomata/career-ops dest"
    # Parsed exactly, first: a parser returning (None, None) is also "not the
    # pair", and would pass the second assertion on its own.
    assert _parse_clone(old) == ("https://github.com/FrameAutomata/career-ops",
                                 "dev/batch-local-llm")
    ref = _ref()
    assert _parse_clone(old) != (ref["CAREER_OPS_URL"], ref["CAREER_OPS_BRANCH"])
    assert _OWNER_RE.findall(old) == ["FrameAutomata"]


def test_guard_catches_a_pin_pasted_into_a_workflow():
    """The shapes `TestDailyFetchesThePin` forbids, read the way it reads them:
    an inlined SHA is a fetch that no longer names the variable AND a literal in
    the file, and a fetch of the branch is neither of those but still wrong."""
    inlined = 'git -C career-ops fetch --depth 1 "$CAREER_OPS_URL" 0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c'
    assert not re.search(r"\$\{?CAREER_OPS_COMMIT\b", inlined)
    assert _ANY_SHA_RE.findall(inlined) == ["0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"]

    branch_fetch = 'git -C career-ops fetch --depth 1 "$CAREER_OPS_URL" "$CAREER_OPS_BRANCH"'
    assert not re.search(r"\$\{?CAREER_OPS_COMMIT\b", branch_fetch)
    assert re.search(r"\$\{?CAREER_OPS_BRANCH\b", branch_fetch)
    # The abbreviation `git fetch` cannot serve, and a 12-hex prefix is not a
    # literal SHA to the file-wide check either — which is why the ref file's
    # own shape check is the one that has to catch it.
    assert not _SHA_RE.match("aac998c7ed72")
    assert not _ANY_SHA_RE.findall("CAREER_OPS_COMMIT=aac998c7ed72")


def test_ref_parser_takes_assignments_and_nothing_else():
    text = (
        "# a comment, and a blank line follow\n"
        "\n"
        "CAREER_OPS_URL=https://github.com/x/career-ops\n"
        "CAREER_OPS_BRANCH=main\n"
    )
    assert _parse_ref(text) == {
        "CAREER_OPS_URL": "https://github.com/x/career-ops",
        "CAREER_OPS_BRANCH": "main",
    }
    # Sourced verbatim by the daily, so a line that is not an assignment is a
    # command that runs in the cloud, and a value the shell would re-expand is
    # not the value written.
    with pytest.raises(AssertionError):
        _parse_ref("rm -rf /\n")
    with pytest.raises(AssertionError):
        _parse_ref("CAREER_OPS_BRANCH=$(git rev-parse --abbrev-ref HEAD)\n")


def test_guard_tells_a_printed_step_from_a_run_one():
    """Same reason, for the hint: an install step setup runs must not count as
    printed, and a recipe step setup runs must not hide behind a comment."""
    text = (
        '  echo "      git -C career-ops fetch origin"\n'
        "  # echo \"      git -C career-ops checkout -B main origin/main\"\n"
        "git -C career-ops remote set-url origin https://example.invalid\n"
        '(cd "$root/career-ops" && npm install --ignore-scripts)\n'
    )
    printed, run = _printed_and_run(text)
    assert [line.strip() for line in printed] == ['echo "      git -C career-ops fetch origin"']
    assert any("remote set-url origin" in line for line in run)
    assert not any("npm install" in line for line in printed)
    assert not any("checkout -B" in line for line in printed + run)
