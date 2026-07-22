"""specs/backlog.md BL-H2: UX certification, automatable slice.

BL-H2 asks for "contrast ratios, alt text, heading structure, table
semantics" to be automated as CI checks where possible. Alt text is the
one of the four with an unambiguous, zero-false-positive rule (every
``<img>`` must carry a non-empty ``alt`` attribute) and no dependency on
rendered output or a live email client, so it is the one implemented
here. Contrast (colors are inline per-template, not centralized, so
pairing foreground/background correctly needs rendered output, not a
static scan) and table semantics (89 ``<table`` tags across the
templates, only 30 files mention ``role="presentation"`` anywhere --
correctly distinguishing genuine data tables from Outlook-compatible
layout tables needs a human editorial pass verified in a real email
client, not a blind static rewrite) remain open per BL-H2's own row --
see specs/backlog.md for the full accounting.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TEMPLATES_ROOT = _REPO_ROOT / "templates"

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ALT_ATTR_RE = re.compile(r"""\balt\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)


class _TableSemanticsParser(HTMLParser):
    """Tree-aware (not file-level) table/th association: a ``<th>`` is only
    credited to the innermost currently-open ``<table>``, so a nested
    layout table cannot borrow semantic cover from an ancestor data table
    (the bug a naive "does this file contain <th> anywhere" scan has)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[dict[str, bool]] = []
        self.closed_tables: list[dict[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "table":
            self._stack.append({"has_role_presentation": attrs_dict.get("role") == "presentation", "has_th": False})
        elif tag == "th" and self._stack:
            self._stack[-1]["has_th"] = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._stack:
            self.closed_tables.append(self._stack.pop())


def _template_files() -> tuple[Path, ...]:
    return tuple(sorted(_TEMPLATES_ROOT.rglob("*.j2")))


def test_templates_root_exists_and_has_files() -> None:
    # Guards against this check silently passing on a broken/moved templates root.
    files = _template_files()
    assert _TEMPLATES_ROOT.is_dir()
    assert len(files) > 10, f"expected many .j2 templates under {_TEMPLATES_ROOT}, found {len(files)}"


def test_every_img_tag_has_non_empty_alt_text() -> None:
    """WCAG 1.1.1 (Non-text Content): every image needs a text alternative.
    A Jinja expression inside the alt value (e.g. ``alt="{{ title }} chart"``)
    counts as present -- this check only catches a missing attribute or a
    literally empty one, which is exactly the class of regression a template
    author could introduce without an email-client visual check catching it
    (an empty alt renders invisibly)."""
    violations: list[str] = []
    for path in _template_files():
        text = path.read_text(encoding="utf-8")
        for match in _IMG_TAG_RE.finditer(text):
            tag = match.group(0)
            alt_match = _ALT_ATTR_RE.search(tag)
            relative = path.relative_to(_REPO_ROOT)
            if alt_match is None:
                violations.append(f"{relative}: <img> tag with no alt attribute: {tag[:100]!r}")
            elif not alt_match.group(2).strip():
                violations.append(f"{relative}: <img> tag with empty alt=\"\": {tag[:100]!r}")
    assert not violations, "Image(s) missing accessible alt text:\n" + "\n".join(violations)


def test_every_table_declares_presentation_role_or_has_header_cells() -> None:
    """Screen readers announce a ``<table>`` with no semantic markup as
    tabular data by default -- Outlook-compatible email HTML relies heavily
    on ``<table>`` purely for layout, so every such table must declare
    ``role="presentation"`` (a no-op for sighted rendering in every email
    client and browser -- this is an assistive-tech-only attribute, never a
    visual change) unless it is a genuine data table with ``<th>`` header
    cells. Tree-aware per-table check, not a per-file scan (a nested layout
    table cannot borrow semantic cover from an unrelated ancestor <th>)."""
    violations: list[str] = []
    for path in _template_files():
        text = path.read_text(encoding="utf-8")
        parser = _TableSemanticsParser()
        parser.feed(text)
        ambiguous_count = sum(
            1 for table in parser.closed_tables if not table["has_role_presentation"] and not table["has_th"]
        )
        if ambiguous_count:
            relative = path.relative_to(_REPO_ROOT)
            violations.append(
                f"{relative}: {ambiguous_count} <table> tag(s) with neither role=\"presentation\" nor <th> "
                "header cells -- classify as layout (add role=\"presentation\") or data (add <th>)."
            )
    assert not violations, "Table(s) with ambiguous semantics:\n" + "\n".join(violations)
