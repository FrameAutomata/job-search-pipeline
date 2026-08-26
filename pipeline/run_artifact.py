"""Stage the daily workflow's artifact so it holds ONE RUN'S output.

`career-ops/reports/` is restored from the pipeline state cache at the start of
every run and only ever grows. Uploading that directory therefore put the whole
report history to date into *every* daily artifact, and retention keeps N of
them alive at once — so live Actions storage grew with the SQUARE of how long
the pipeline had been running. It exhausted the account's storage quota, and an
exhausted quota stops GitHub creating workflow runs at all, which is how the
Tests workflow silently stopped running on pull requests (issue #129).

Cutting retention 90 → 7 divided that by thirteen; it did not change the shape.
Retention bounds how many artifacts are alive, never how big each one is, so
`7·R` with `R` growing forever is the same wall further out.

This bounds the size instead. The workflow takes a manifest of the reports
directory right after the cache restore, and after the run stages only the
files that weren't in it — the reports this run actually minted — next to the
small whole-file state the artifact also carries. The artifact becomes
O(one day's work) rather than O(all history), which is also what it always
claimed to be: "what this run produced". The complete history keeps living in
the two places that are responsible for it — the `pipeline-state-v1` cache and
the user's local `career-ops/` — neither of which this touches.

Why a manifest rather than mtimes, or reading `batch/batch-api-state.json` to
learn which report numbers this pass minted:

  - mtimes alone can't be trusted across a `tar`-based cache restore, and
    `find -newer` compares at one-second granularity against a marker file.
  - batch-api-state.json knows about the *batch evaluator's* reports. The UI's
    Add-Job path writes reports through the same `write_job_result`, and a
    future writer would too; a manifest diff sees every report however it got
    there, so it can't quietly under-report.

Identity is `(size, mtime_ns)` per path — the rsync quick-check. Both sides of
the comparison are taken inside a single job, on one filesystem, so it needs no
assumption about what the restore did to timestamps: anything the run wrote has
a timestamp from this run.

Comparing rather than just diffing path sets matters for the rewrite case: a
job retried after failing in an earlier run reuses its `<job_id>.tsv` name, and
a path-set diff would call the rewritten file old and drop it.

Structurally unverifiable where it lives, so it is tested here: this repository
is public — public repos don't consume the account's Actions storage quota —
and it's the template, where `daily-pipeline.yml` skips by design. The daily
only really runs, and only accumulates artifacts, in the private copies made
via "Use this template".
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from pipeline.stdio import line_buffer_stdout

# What the manifest records per file. Kept as a list because that is what JSON
# round-trips to; the comparison is `==` on the pair, not on either half.
Stat = list[int]


def scan(root: Path, rels: list[str]) -> dict[str, Stat]:
    """Manifest of every file under each `root/rel`, keyed by path relative to
    `root` so one manifest can cover several directories.

    A `rel` that doesn't exist yet contributes nothing rather than raising: the
    first run in a fresh copy has no reports directory, and that is the case
    where every report is new, not an error."""
    out: dict[str, Stat] = {}
    for rel in rels:
        base = root / rel
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                st = f.stat()
                out[f.relative_to(root).as_posix()] = [st.st_size, st.st_mtime_ns]
    return out


def _copy_into(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # copy2, not copy: preserving mtime keeps the staged tree comparable to the
    # source, which is what makes a re-run of `stage` idempotent.
    shutil.copy2(src, dest)


def stage(
    root: Path,
    into: Path,
    manifest: dict[str, Stat],
    deltas: list[str],
    wholes: list[str],
) -> tuple[list[str], list[str]]:
    """Build the artifact tree under `into`, mirroring the layout under `root`.

    `deltas` are directories contributed file-by-file, and only where the file
    is absent from `manifest` or differs from it. `wholes` are paths copied
    entire — the small state files whose whole current value is the point
    (applications.md is the tracker; a diff of it would be useless to the UI's
    Refresh) and the per-run directories that start empty anyway.

    Returns (staged delta paths, staged whole paths), both relative to `root`,
    for the workflow log."""
    staged_delta: list[str] = []
    for key, st in sorted(scan(root, deltas).items()):
        if manifest.get(key) == st:
            continue
        _copy_into(root / key, into / key)
        staged_delta.append(key)

    staged_whole: list[str] = []
    for rel in wholes:
        src = root / rel
        # Missing is normal, not an error: a run that evaluated nothing writes
        # no tracker-additions, and easy-apply-urls.txt only exists when a pass
        # produced them. The upload step's if-no-files-found used to absorb this.
        if src.is_file():
            _copy_into(src, into / rel)
            staged_whole.append(rel)
        elif src.is_dir():
            shutil.copytree(src, into / rel, dirs_exist_ok=True)
            staged_whole.append(rel)
    return staged_delta, staged_whole


def _read_manifest(path: Path, root: Path, deltas: list[str]) -> dict[str, Stat]:
    """Load the pre-run manifest, or fail loudly.

    Not "treat a missing manifest as empty": that silently restores the
    unbounded upload this module exists to prevent, and would do it on the path
    where nobody is looking. The snapshot step runs before the pipeline does, so
    a manifest missing at staging time means the run never got as far as writing
    a report — there is nothing to lose by refusing.

    A manifest of the WRONG thing fails the same way and for the same reason.
    The two workflow steps have to agree on --root and --delta or the keys don't
    line up, every restored report reads as new, and the whole history goes back
    into the artifact — the exact outcome refusing a missing manifest prevents.
    So the manifest records its own scope and this checks it, rather than
    leaving the agreement to two argument lists that happen to match today."""
    if not path.is_file():
        raise SystemExit(
            f"run_artifact: no manifest at {path}. The snapshot step must run "
            "after the cache restore and before the pipeline — without it there "
            "is no way to tell this run's reports from the whole restored history."
        )
    doc = json.loads(path.read_text(encoding="utf-8"))
    scope, want = doc.get("scope"), _scope(root, deltas)
    if scope != want:
        raise SystemExit(
            f"run_artifact: manifest at {path} was taken of {scope}, but staging "
            f"asked for {want}. The snapshot and stage steps must pass the same "
            "--root and --delta; mismatched, every restored file reads as new "
            "and the artifact carries the whole history again."
        )
    return doc["files"]


def _scope(root: Path, deltas: list[str]) -> dict:
    """What a manifest is a manifest OF. Compared verbatim between the two steps."""
    return {"root": Path(root).as_posix(), "delta": sorted(deltas)}


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.run_artifact")
    ap.add_argument("mode", choices=("snapshot", "stage"))
    ap.add_argument("--root", type=Path, required=True,
                    help="directory the manifest keys and the staged layout are relative to")
    ap.add_argument("--manifest", type=Path, required=True,
                    help="manifest file to write (snapshot) or read (stage)")
    ap.add_argument("--delta", action="append", default=[], metavar="REL",
                    help="directory contributed as new-or-changed files only; repeatable")
    ap.add_argument("--whole", action="append", default=[], metavar="REL",
                    help="file or directory copied entire (stage only); repeatable")
    ap.add_argument("--into", type=Path,
                    help="directory to build the artifact tree in (stage only)")
    args = ap.parse_args(argv)

    if args.mode == "snapshot":
        manifest = scan(args.root, args.delta)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps({"scope": _scope(args.root, args.delta), "files": manifest}),
            encoding="utf-8")
        print(f"[artifact] snapshot: {len(manifest)} file(s) already present in "
              f"{', '.join(args.delta) or '(nothing)'}")
        return 0

    if args.into is None:
        ap.error("stage requires --into")
    # Read the manifest BEFORE touching --into: both failures above are fatal,
    # and wiping an already-staged tree on the way to erroring would destroy the
    # only copy of work a re-run was meant to inspect.
    manifest = _read_manifest(args.manifest, args.root, args.delta)
    # Fresh every time: a leftover tree from a re-run of the step would upload
    # files this run neither produced nor knows about.
    if args.into.exists():
        shutil.rmtree(args.into)
    delta, whole = stage(args.root, args.into, manifest, args.delta, args.whole)
    print(f"[artifact] staged {len(delta)} new file(s) from this run "
          f"+ {len(whole)} whole path(s) into {args.into}")
    for key in delta:
        print(f"[artifact]   new: {key}")
    return 0


if __name__ == "__main__":
    line_buffer_stdout()

    raise SystemExit(_main(sys.argv[1:]))
