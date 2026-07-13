"""Contract for the aria-snapshot parser behind the tiered apply runner.

Phase 1 of the deterministic/agent/human apply ladder: parse the OpenClaw
browser CLI's `snapshot` output into a label-queryable index. Labels are the
stable API of a form (refs are minted per snapshot), so the deterministic
tier plans actions by label and resolves refs at act time.

Fixture lines are verbatim shapes from live captures (2026-07-13 spike):
the Clover Health Greenhouse form and the Relevant Search LinkedIn Easy
Apply dialog.
"""

import re

import pytest

from pipeline.openclaw_browser import parse_snapshot

# A condensed but structurally faithful Greenhouse application form: nesting,
# links with /url property lines, valued and empty fields, quoted values,
# [invalid] flags, duplicate "Attach" buttons, a plain text node.
GREENHOUSE = """\
- generic [active] [ref=e1]:
  - main [ref=e2]:
    - generic [ref=e3]:
      - img "Clover Health Logo" [ref=e5]
      - generic [ref=e6]:
        - link "Back to jobs" [ref=e7] [cursor=pointer]:
          - /url: https://job-boards.greenhouse.io/cloverhealth?gh_src=ecf85ead1us
          - img [ref=e8]
          - text: Back to jobs
      - textbox "First Name" [ref=e126]: Thomas
      - textbox "Last Name" [ref=e131]: Thirlwall
      - textbox "Email" [ref=e136]: thomas.thirlwall.dev@gmail.com
      - combobox "Country" [ref=e151]
      - textbox "Phone" [ref=e161]: (956) 525-3015
      - combobox "Location (City)" [invalid] [ref=e173]
      - group "Resume/CV*" [ref=e176]:
        - generic [ref=e177]: Resume/CV*
        - paragraph [ref=e446]: Thomas_Thirlwall_Clover_Health_Engineering_Productivity.docx
        - button "Remove file" [ref=e447] [cursor=pointer]:
          - generic [ref=e448]: Remove file
        - button "Attach" [ref=e182] [cursor=pointer]
        - generic [ref=e183]: Attach
        - button "Attach" [ref=e201] [cursor=pointer]
      - textbox "How did you hear about this job?" [ref=e219]: LinkedIn
      - combobox "Do you have a legal right to work in the US?" [invalid] [ref=e252]: "Yes"
      - combobox "Will you now or in the future require immigration sponsorship in the United States?" [invalid] [ref=e269]
      - textbox "Please indicate the base salary you expect for the role that you are applying for:" [ref=e278]: "150000"
      - button "Submit application" [ref=e424] [cursor=pointer]
"""

# The LinkedIn Easy Apply dialog rendered OVER the posting page: elements
# both inside and outside the dialog, a heading with [level=...], a note
# whose value follows the attrs, and a button that also exists outside.
EASY_APPLY = """\
- generic [ref=e1]:
  - region "Toast message":
    - heading "0 notifications total" [level=2] [ref=e3]
  - button "Easy Apply" [ref=e90] [cursor=pointer]
  - dialog "Apply to Relevant Search" [active] [ref=e5]:
    - heading "Apply to Relevant Search" [level=2] [ref=e12]
    - region "Your job application progress is at 0 percent." [ref=e14]:
      - note "Your job application progress is at 0 percent." [ref=e19]: 0%
    - textbox "Mobile phone number" [ref=e40]: +1 (956) 525-3015
    - button "Easy Apply" [ref=e91] [cursor=pointer]
    - button "Next" [ref=e60] [cursor=pointer]
  - paragraph [ref=e54]: Submitting this application won't change your LinkedIn profile.
"""

# Grammar edges: a quoted label containing a colon (the value split must
# respect quotes), an element with no ref, extra whitespace-only and
# non-grammar lines that must be tolerated, and a bare text node.
EDGES = """\
- document:
  - textbox "Salary: base (USD)" [ref=e9]: 100
  - generic: orphan value with no ref

  - text: standalone text node
  ...display truncation artifact line that matches no grammar
  - checkbox "Follow Relevant Search" [checked] [ref=e77]
"""


@pytest.fixture
def gh():
    return parse_snapshot(GREENHOUSE)


@pytest.fixture
def ea():
    return parse_snapshot(EASY_APPLY)


@pytest.fixture
def edges():
    return parse_snapshot(EDGES)


# ── structure ────────────────────────────────────────────────────────────────

def test_elements_in_document_order(gh):
    roles = [e.role for e in gh.elements]
    assert roles.index("main") < roles.index("img")
    first = next(e for e in gh.elements if e.label == "First Name")
    submit = next(e for e in gh.elements if e.label == "Submit application")
    assert gh.elements.index(first) < gh.elements.index(submit)


def test_role_label_ref_parsed(gh):
    el = gh.find("textbox", "First Name")
    assert el.role == "textbox"
    assert el.label == "First Name"
    assert el.ref == "e126"


def test_element_without_ref_has_none(gh):
    # `- text: Back to jobs` carries neither label nor ref
    texts = [e for e in gh.elements if e.role == "text"]
    assert texts and all(e.ref is None for e in texts)


def test_depth_increases_with_nesting(gh):
    root = gh.elements[0]
    first_name = gh.find("textbox", "First Name")
    attach = gh.find("button", "Attach")
    assert root.depth == 0
    assert first_name.depth > root.depth
    assert attach.depth > first_name.depth  # nested inside the group


def test_parent_links_follow_nesting(gh):
    attach = gh.find("button", "Attach")
    group = gh.find("group", "Resume/CV*")
    assert attach.parent is group


# ── values ───────────────────────────────────────────────────────────────────

def test_inline_value_parsed(gh):
    assert gh.value_of("textbox", "First Name") == "Thomas"
    assert gh.value_of("textbox", "Phone") == "(956) 525-3015"


def test_quoted_value_is_unquoted(gh):
    label = re.compile(r"base salary")
    assert gh.value_of("textbox", label) == "150000"
    assert gh.value_of("combobox", re.compile(r"legal right")) == "Yes"


def test_empty_field_value_is_none(gh):
    assert gh.value_of("combobox", "Country") is None
    assert gh.value_of("combobox", re.compile(r"immigration sponsorship")) is None


def test_value_split_respects_quoted_label_colon(edges):
    el = edges.find("textbox", "Salary: base (USD)")
    assert el is not None
    assert el.value == "100"


def test_text_node_value(edges):
    texts = [e for e in edges.elements if e.role == "text"]
    assert any(e.value == "standalone text node" for e in texts)


# ── flags and attributes ─────────────────────────────────────────────────────

def test_invalid_flag(gh):
    city = gh.find("combobox", "Location (City)")
    assert city.attrs.get("invalid") is True
    country = gh.find("combobox", "Country")
    assert country.attrs.get("invalid") is None


def test_keyed_attrs_parsed(ea):
    heading = ea.find("heading", "0 notifications total")
    assert heading.attrs.get("level") == "2"


def test_boolean_flags_parsed(edges):
    box = edges.find("checkbox", "Follow Relevant Search")
    assert box.attrs.get("checked") is True


def test_invalid_fields_lists_all(gh):
    labels = {e.label for e in gh.invalid_fields()}
    assert labels == {
        "Location (City)",
        "Do you have a legal right to work in the US?",
        "Will you now or in the future require immigration sponsorship in the United States?",
    }


# ── links ────────────────────────────────────────────────────────────────────

def test_url_property_attaches_to_link(gh):
    link = gh.find("link", "Back to jobs")
    assert link.url == "https://job-boards.greenhouse.io/cloverhealth?gh_src=ecf85ead1us"


# ── queries ──────────────────────────────────────────────────────────────────

def test_find_is_case_insensitive_exact(gh):
    assert gh.find("textbox", "first name").ref == "e126"


def test_find_missing_returns_none(gh):
    assert gh.find("textbox", "Cover Letter") is None


def test_find_regex(gh):
    el = gh.find("combobox", re.compile(r"legal right to work", re.I))
    assert el.ref == "e252"


def test_find_by_role_only(gh):
    # label=None enumerates every element of a role — how the Phase 2 fill
    # planner diffs the live form against a field map to spot unmapped fields.
    labels = {e.label for e in gh.find_all("combobox")}
    assert {"Country", "Location (City)"} <= labels
    assert gh.find("button").ref == "e447"  # first button in document order


def test_find_all_in_document_order(gh):
    refs = [e.ref for e in gh.find_all("button", "Attach")]
    assert refs == ["e182", "e201"]


def test_find_does_not_cross_roles(gh):
    # a generic "Attach" node sits between the buttons; role filter must hold
    assert all(e.role == "button" for e in gh.find_all("button", "Attach"))


def test_within_scopes_to_subtree(ea):
    dialog = ea.find("dialog", "Apply to Relevant Search")
    inside = ea.within(dialog)
    # "Easy Apply" exists both outside (e90) and inside (e91) the dialog
    assert inside.find("button", "Easy Apply").ref == "e91"
    assert inside.find("paragraph", re.compile(r"LinkedIn profile")) is None
    assert inside.find("textbox", "Mobile phone number").ref == "e40"


# ── robustness ───────────────────────────────────────────────────────────────

def test_non_grammar_lines_are_ignored(edges):
    # blank line + truncation artifact must not raise or produce elements
    assert all(e.role != "" for e in edges.elements)
    assert not any("truncation" in (e.label or "") for e in edges.elements)


def test_large_real_shape_smoke():
    body = GREENHOUSE + "".join(
        f'      - textbox "Question {i}" [ref=e{500 + i}]: answer {i}\n' for i in range(200)
    )
    idx = parse_snapshot(body)
    assert idx.find("textbox", "Question 199").value == "answer 199"


# ── hostile-input robustness (2026-07-13 review fixes) ───────────────────────

def test_siblings_share_parent_across_indent_widths():
    # Nesting is relative: 2-, 3-, or 4-space steps must all read b as a sibling
    # of a (both children of root), never as a's child.
    for step in ("  ", "   ", "    "):
        idx = parse_snapshot(f"- root [ref=e1]:\n{step}- alpha [ref=e2]\n{step}- beta [ref=e3]\n")
        assert idx.find("alpha").parent is idx.find("root")
        assert idx.find("beta").parent is idx.find("root")


def test_wrapper_offset_preserves_dialog_scope():
    # A uniform 2-space leading offset must not fold the trailing sibling into
    # the dialog subtree (the within() mis-scoping that could click outside it).
    snap = '  - dialog "D" [ref=e5]:\n    - button "Inside" [ref=e6]\n  - button "Outside" [ref=e7]\n'
    idx = parse_snapshot(snap)
    inside = idx.within(idx.find("dialog", "D"))
    assert inside.find("button", "Inside").ref == "e6"
    assert inside.find("button", "Outside") is None


def test_unterminated_label_line_is_skipped():
    # A capture cut mid-label must not become a phantom labelless element.
    idx = parse_snapshot('- textbox "First Name" [ref=e1]: Ada\n- textbox "How did you he\n')
    assert [e.ref for e in idx.find_all("textbox")] == ["e1"]


def test_invalid_fields_matches_keyed_forms():
    snap = (
        '- combobox "A" [invalid=grammar] [ref=e1]\n'
        '- combobox "B" [invalid=true] [ref=e2]\n'
        '- combobox "C" [invalid] [ref=e3]\n'
        '- combobox "D" [invalid=false] [ref=e4]\n'
        '- combobox "E" [ref=e5]\n'
    )
    assert {e.label for e in parse_snapshot(snap).invalid_fields()} == {"A", "B", "C"}


def test_ansi_codes_are_stripped():
    snap = (
        '\x1b[32m- textbox "First Name" [ref=e126]: Thomas\x1b[0m\n'
        '- combobox "City" \x1b[31m[invalid]\x1b[0m [ref=e173]\n'
    )
    idx = parse_snapshot(snap)
    assert idx.find("textbox", "First Name").ref == "e126"
    assert idx.value_of("textbox", "First Name") == "Thomas"
    assert {e.label for e in idx.invalid_fields()} == {"City"}


def test_bracket_inside_quoted_attr_value():
    idx = parse_snapshot('- link "x" [title="a]b"] [ref=e9]: v\n')
    el = idx.find("link", "x")
    assert el.ref == "e9"
    assert el.value == "v"
    assert el.attrs["title"] == "a]b"


def test_escaped_quote_in_label():
    el = parse_snapshot(r'- button "Say \"Yes\"" [ref=e5]' + "\n").find("button", 'Say "Yes"')
    assert el is not None and el.ref == "e5"


def test_keyed_attr_value_is_unquoted():
    idx = parse_snapshot('- heading "H" [level="2"] [ref=e1]\n')
    assert idx.find("heading", "H").attrs["level"] == "2"


def test_snapshot_id_stable_and_propagates():
    a, b = parse_snapshot(EASY_APPLY), parse_snapshot(EASY_APPLY)
    assert a.snapshot_id and a.snapshot_id == b.snapshot_id
    assert parse_snapshot(GREENHOUSE).snapshot_id != a.snapshot_id
    dialog = a.find("dialog", "Apply to Relevant Search")
    assert a.within(dialog).snapshot_id == a.snapshot_id
