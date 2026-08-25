"""The one contract for "the previous CSV stage produced no rows".

Every stage in the scrape -> filter -> screen -> bridge chain can legitimately
produce nothing — an empty or rate-limited scrape, a config that ages out every
row, nothing scoring above min_score, every surviving URL already seen, liveness
dropping them all. That condition used to be encoded independently at each end
and the two halves had drifted: producers wrote three different things (zero
bytes, a header-only CSV, nothing at all) and consumers tested three different
ways (`not exists() or st_size == 0`, counting rows off a DictReader, and
raising FileNotFoundError). A header-only file is not zero bytes, so a stage
that wrote one was not "empty" to a stage testing size.

The rule is deliberately asymmetric, and the asymmetry is the point:

    producers write exactly zero bytes; readers accept anything with no data rows

Writing one shape keeps the output unambiguous. Accepting three keeps the
contract working against files this module didn't write — a header-only
filtered_jobs.csv left by an older run, and scrape.py's happy path, where pandas
owns the column set and writes via to_csv.

Truncating rather than leaving the file alone is the load-bearing half. A stage
that produced nothing today must not leave yesterday's output in place, or the
next stage re-processes it as today's results — and `--skip-scrape` /
`--skip-filter` reuse whatever is on disk, so the stale rows would reach
evaluation looking fresh.

Scope is the four CSV stages, and two neighbours are deliberately outside it:

- The tab-separated queue and state files (`batch-input.tsv`, `scan-history.tsv`)
  test `st_size == 0` for a different question — "should I write a header" on a
  file that accumulates across runs — not "did upstream produce nothing". Same
  expression, different invariant; converting them would be a mistake.
- pipeline.handoff's work-orders are jsonl + markdown. The jsonl half already
  lands at zero bytes and agrees with this rule by construction; the markdown
  half deliberately renders an empty table, because its reader is a human or an
  agent for whom "0 fresh roles, plus the status legend" beats an empty file.

Stdlib only, so nothing here constrains which venv can import it.
"""

import csv
import os
from pathlib import Path


def read_rows(path) -> list[dict]:
    """The data rows of a stage's CSV output; [] when it produced none.

    Missing, zero-byte and header-only all read as [] — the caller's question is
    "is there anything to work on", and all three answer no. Reading a missing
    file as empty rather than raising is deliberate for the mid-chain stages: a
    stage that ran and found nothing and a stage that was skipped both leave
    downstream with nothing to do. pipeline.filter is the one exception and says
    so at its call site, because a missing jobs.csv there means scrape never ran
    at all, which is worth a message rather than a silent no-op.

    Rows are materialized rather than streamed. That is a real trade, not a
    free one: jobs.csv runs to thousands of rows carrying pre-filter
    descriptions, so filter's peak is now proportional to the whole scrape
    rather than to the survivors, and it drops the reference once it has the
    count. It is bought because screen needs three passes over the rows (the
    description default, the seen-URL filter, then classify_each) and a
    generator would force it back to a private list anyway — one read shape
    beats a streaming one plus a materializing one.

    Decoded as utf-8-sig so a byte-order mark is consumed rather than glued to
    the first header name. A BOM turns "title" into "\ufefftitle", which no
    stage would find: filter's target-title bonus and negative-title exclusion
    would both stop firing and bridge would drop every row as malformed, all
    without an error. Excel writes one on save, and `--skip-scrape` exists to
    reuse whatever is on disk. utf-8-sig also reads plain utf-8 unchanged.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_rows(path, rows, fieldnames=None) -> None:
    """Write `rows` as a stage's CSV output, truncating to zero bytes if there are none.

    The truncation is the point, not a detail — see the module docstring. Every
    "nothing survived" exit goes through here, so a new one cannot be added
    without it, and no consumer has to know whether the producer got as far as
    writing a header.

    `fieldnames` defaults to the first row's keys, which is the right answer
    whenever the rows came from read_rows or from a DictReader. Pass it
    explicitly to fix a column order the rows don't already carry.

    Written to a sibling temp file and moved into place, because `open(path,
    "w")` truncates before the first row is written. screen writes over the
    file it just read, so a raise mid-write (a ragged row, ENOSPC) or a Ctrl-C
    would leave a header plus however many rows got out — neither zero bytes
    nor complete, and so indistinguishable from a genuine short result to the
    reader above. Truncation on the empty path needs no such care: it *is* the
    intended end state, with nothing following it that can fail.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames or list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
