"""Drift guard: the unrelated-histories flag exists in three places.

A copy made with "Use this template" starts a fresh root commit, so it shares no
ancestor with the template and a bare `git merge` refuses. Every update path
therefore needs --allow-unrelated-histories, and there are three of them, in
three languages, none able to import the others:

  * pipeline/app/self_update.py   — the UI's ⬆ Update button
  * .github/workflows/update-from-template.yml — the dispatch-only cloud path
  * README.md                     — the copy-pasteable manual path, and the
                                    fallback the other two point at on failure

That is the same unavoidable-mirror shape as sites.py <-> setup-profile.mjs
<-> search.example.yml, which this repo guards with a test rather than a
convention. Without this, the README silently kept handing users the command
that fails, months after the code stopped failing.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FLAG = "--allow-unrelated-histories"


def test_self_update_passes_the_flag():
    src = (ROOT / "pipeline" / "app" / "self_update.py").read_text(encoding="utf-8")
    assert FLAG in src, "the UI's Update button would refuse a copy's first merge"


def _merge_invocations(text: str) -> list[str]:
    """Lines that actually RUN `git merge`. Excludes comments (both files
    discuss the merge in prose right next to it) and `--abort`, which is the
    cleanup path and takes no flag."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or "--abort" in line:
            continue
        if re.search(r"(^|\s|`)git merge\b", line):
            out.append(line)
    return out


def test_workflow_passes_the_flag():
    wf = (ROOT / ".github" / "workflows" / "update-from-template.yml").read_text(encoding="utf-8")
    lines = _merge_invocations(wf)
    assert lines, "no git merge in update-from-template.yml"
    assert all(FLAG in l for l in lines), lines


def test_readme_documents_the_flag():
    rd = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = _merge_invocations(rd)
    assert lines, "README no longer shows the manual merge"
    assert all(FLAG in l for l in lines), lines


def _update_doc() -> dict:
    return yaml.safe_load(
        (ROOT / ".github" / "workflows" / "update-from-template.yml").read_text(encoding="utf-8"))


def test_scheduled_runs_are_var_gated():
    """The weekly schedule is opt-in: a merge lands in the copy's history
    unasked, so a SCHEDULED run proceeds only when the repository variable
    AUTO_UPDATE_FROM_TEMPLATE is 'true' (the wizard's "Keep my copy updated
    weekly" box), while a dispatch always does — and the template never
    self-updates. PyYAML resolves the bare key `on` to True (YAML 1.1), hence
    the double lookup."""
    doc = _update_doc()
    triggers = doc.get("on", doc.get(True))
    assert "workflow_dispatch" in triggers
    assert [s["cron"] for s in triggers["schedule"]] == ["0 4 * * 1"]

    cond = " ".join(str(doc["jobs"]["update"]["if"]).split())
    assert "github.repository != 'FrameAutomata/job-search-pipeline'" in cond
    assert re.search(r"github\.event_name\s*==\s*'workflow_dispatch'\s*\|\|\s*"
                     r"vars\.AUTO_UPDATE_FROM_TEMPLATE\s*==\s*'true'", cond), cond
    # The gate is AND-ed with the self-skip, so a dispatch on the template
    # still skips; the `(a || b)` half must be parenthesized for that to hold.
    assert re.search(r"&&\s*\(.*\|\|.*\)", cond), cond
