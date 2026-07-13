"""Aria-snapshot parsing for the tiered apply runner.

The OpenClaw browser CLI's `snapshot` verb emits an indented aria tree of the
live page. The deterministic apply tier navigates forms by LABEL — labels are
stable across page loads while [ref=eN] handles are minted per snapshot — so
this module parses the tree once and answers label-based queries; refs are
resolved only at act time.

Grammar (one element per line)::

    <indent>- role ["label"] [attr]... [: value]
    <indent>- /prop: value          # property line, attaches to enclosing element

Nesting is by relative indentation (a deeper line is a child of the nearest
shallower open element), so a uniform leading offset — e.g. a snapshot captured
with a wrapper prefix — parses the same as one flush to column 0. `[ref=eN]`
becomes the element's `ref`; other `[k=v]` / `[flag]` groups and `/prop:` lines
land in `attrs` (`url` gets a convenience property — the one prop the apply tier
reads); quotes and brackets inside quoted spans are respected, and `\\"`-escaped
quotes in labels are honored. Parsing is deliberately tolerant: ANSI color codes
are stripped, and lines that do not match the grammar — blank lines, truncation
artifacts, a line cut off mid-label — are skipped rather than raising, because
the input is CLI output that can be cut off mid-stream or colorized.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_ITEM = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_ROLE = re.compile(r"[^\s:]+")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class AriaElement:
    """One node of the aria snapshot tree."""

    role: str
    label: str | None = None
    ref: str | None = None
    value: str | None = None
    attrs: dict[str, object] = field(default_factory=dict)
    depth: int = 0  # leading-space count; only relative order is meaningful
    parent: "AriaElement | None" = None

    @property
    def url(self) -> str | None:
        v = self.attrs.get("url")
        return v if isinstance(v, str) else None


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return _unescape(v[1:-1])
    return v


def _find_unescaped(s: str, ch: str, start: int) -> int:
    """Index of the next `ch` in s at/after start that is not backslash-escaped."""
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == ch:
            return i
    return -1


def _find_close_bracket(s: str) -> int:
    """s[0] is '['; index of the matching ']' outside any quoted span, else -1."""
    in_quote = esc = False
    for i in range(1, len(s)):
        c = s[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_quote = not in_quote
        elif c == "]" and not in_quote:
            return i
    return -1


def _is_invalid(v: object) -> bool:
    """True for [invalid] and truthy aria-invalid tokens (true/grammar/spelling),
    False for absent or [invalid=false]."""
    if v is True:
        return True
    if isinstance(v, str):
        return v.strip().casefold() not in ("", "false")
    return False


def _split_attrs_and_value(rest: str) -> tuple[str | None, str | None, dict]:
    """From the text after role+label, pull ref, value, and other attrs.

    Brackets are matched quote-aware so a ']' inside a quoted attr value can't
    end a token early; keyed values are unquoted so [k="v"] and [k=v] agree.
    """
    ref: str | None = None
    attrs: dict[str, object] = {}
    rest = rest.lstrip()
    while rest.startswith("["):
        j = _find_close_bracket(rest)
        if j == -1:
            break
        tok = rest[1:j]
        rest = rest[j + 1:].lstrip()
        if "=" in tok:
            key, val = tok.split("=", 1)
            key, val = key.strip(), _unquote(val.strip())
            if key == "ref":
                ref = val
            else:
                attrs[key] = val
        elif tok:
            attrs[tok] = True
    value: str | None = None
    if rest.startswith(":"):
        raw = rest[1:].strip()
        value = _unquote(raw) if raw else None
    return ref, value, attrs


def _parse_line(body: str) -> AriaElement | None:
    """Parse an element body: role, then optional quoted label, then rest.

    Scanning left-to-right in grammar order (rather than locating quotes and
    colons globally) keeps a colon inside a quoted label — or a quote inside a
    later attr value — from being mistaken for the value separator. A line whose
    label quote never closes is a truncated/wrapped capture and is skipped, not
    turned into a phantom labelless element.
    """
    m = _ROLE.match(body)
    if not m:
        return None
    role = m.group(0)
    rest = body[m.end():].lstrip()
    label: str | None = None
    if rest.startswith('"'):
        close = _find_unescaped(rest, '"', 1)
        if close == -1:
            return None
        label = _unescape(rest[1:close])
        rest = rest[close + 1:]
    ref, value, attrs = _split_attrs_and_value(rest)
    return AriaElement(role=role, label=label, ref=ref, value=value, attrs=attrs)


def _matches(el: AriaElement, role: str, label: str | re.Pattern | None) -> bool:
    if el.role.casefold() != role.casefold():
        return False
    if label is None:
        return True
    if el.label is None:
        return False
    if isinstance(label, re.Pattern):
        return label.search(el.label) is not None
    return el.label.casefold() == label.casefold()


class SnapshotIndex:
    """Label-based queries over a parsed snapshot.

    `snapshot_id` identifies the capture the refs belong to, so a later act
    layer can tell "ref from a superseded snapshot" from "element gone."
    """

    def __init__(self, elements: list[AriaElement], snapshot_id: str = ""):
        self.elements = elements
        self.snapshot_id = snapshot_id

    def find(self, role: str, label: str | re.Pattern | None = None) -> AriaElement | None:
        for el in self.elements:
            if _matches(el, role, label):
                return el
        return None

    def find_all(self, role: str, label: str | re.Pattern | None = None) -> list[AriaElement]:
        return [el for el in self.elements if _matches(el, role, label)]

    def within(self, ancestor: AriaElement) -> "SnapshotIndex":
        def descends(el: AriaElement) -> bool:
            node = el.parent
            while node is not None:
                if node is ancestor:
                    return True
                node = node.parent
            return False

        return SnapshotIndex([el for el in self.elements if descends(el)],
                             snapshot_id=self.snapshot_id)

    def value_of(self, role: str, label: str | re.Pattern | None = None) -> str | None:
        el = self.find(role, label)
        return el.value if el else None

    def invalid_fields(self) -> list[AriaElement]:
        return [el for el in self.elements if _is_invalid(el.attrs.get("invalid"))]


def parse_snapshot(text: str) -> SnapshotIndex:
    """Parse the aria snapshot text into a queryable index."""
    text = _ANSI.sub("", text)
    snapshot_id = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    elements: list[AriaElement] = []
    stack: list[AriaElement] = []  # open ancestors, strictly increasing depth
    for line in text.splitlines():
        m = _ITEM.match(line)
        if not m:
            continue
        indent = len(m.group("indent"))
        body = m.group("body")
        while stack and stack[-1].depth >= indent:
            stack.pop()
        parent = stack[-1] if stack else None
        if body.startswith("/"):  # property line → attach to enclosing element
            key, _, val = body[1:].partition(":")
            if parent is not None:
                parent.attrs[key.strip()] = val.strip()
            continue
        el = _parse_line(body)
        if el is None:
            continue
        el.depth = indent
        el.parent = parent
        elements.append(el)
        stack.append(el)
    return SnapshotIndex(elements, snapshot_id=snapshot_id)
