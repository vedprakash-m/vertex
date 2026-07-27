"""specs/backlog.md BL-H2: UX certification, automatable slice.

BL-H2 asks for "contrast ratios, alt text, heading structure, table
semantics" to be automated as CI checks where possible. Alt text and table
semantics (tree-aware ``role="presentation"``-or-``<th>`` check) were the
first two closed, both pure static-source scans.

Contrast, 2026-07-26: closed for the two real dynamic-color surfaces this
session found and fixed (``templates/partials/health_banner.j2``'s
per-health-state banner, ``src/core/cockpit_html.py``'s ``code`` rule) via
a real automated scan (axe-core 4.4.3, run against actual rendered HTML,
not source text) that a static scan structurally cannot do -- it caught a
dark-mode-only bug (white text auto-inherited over a fixed light
background) and three real WCAG-AA contrast failures in production-shaped
report/cockpit output. The two fixed surfaces now have a permanent,
dependency-free regression guard below (plain WCAG-formula math against
the actual rendered/known color values, no browser needed at test time).
Every OTHER template's inline colors remain unaudited -- this is a
targeted fix for two confirmed-broken surfaces, not a platform-wide
contrast certification; see specs/backlog.md BL-H2 for the full
accounting and why a blanket audit was not attempted.

Heading structure remains open: Jinja partials are conditionally included
at varying nesting depth, so a static per-file heading-level check cannot
see the assembled document's actual heading order without rendering every
archetype x conditional-branch combination.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader

from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS
from src.core.models import RiskLevel
from src.core.view_models import HealthSummary

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


# ---------------------------------------------------------------------------
# Contrast (WCAG 2 AA, >=4.5:1 for normal-size text). A plain implementation
# of the relative-luminance formula (WCAG 2.x sec 1.4.3/G18) -- no browser or
# axe-core dependency at test time; those were used once, interactively, to
# find the real bugs these two checks now guard against.
# ---------------------------------------------------------------------------

def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(r), channel(g), channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1, l2 = _relative_luminance(fg_hex), _relative_luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


_WCAG_AA_NORMAL_TEXT_MIN_CONTRAST = 4.5

_HEALTH_STATES: tuple[tuple[str, HealthSummary], ...] = (
    ("HEALTHY", HealthSummary(overall_risk=RiskLevel.LOW, high_count=0, medium_count=0, low_count=3, done_count=1, total_count=4, delta_direction="unchanged", prior_counts=None)),
    ("CRITICAL", HealthSummary(overall_risk=RiskLevel.HIGH, high_count=3, medium_count=1, low_count=0, done_count=0, total_count=4, delta_direction="unchanged", prior_counts=None)),
    (
        "AT RISK",
        HealthSummary(
            overall_risk=RiskLevel.HIGH, high_count=1, medium_count=1, low_count=1, done_count=1, total_count=4,
            delta_direction="unchanged", prior_counts=None,
            # Exercises every conditionally-rendered body-text line in the
            # banner (bluf, risk warning, risk/milestone/telemetry/forecast
            # summaries) so this state's contrast check covers all of them,
            # not just the always-rendered leadership_ask/read-time lines.
            bluf="1 high-severity risk needs attention this week.",
            risk_render_warning="Risk register data is 9 days stale.",
            risk_register_summary="3 open risks, 1 at high probability*impact.",
            milestone_summary="2 of 5 milestones on track.",
            telemetry_summary="analytics, 5 scope, 2 completed", telemetry_confidence="high",
            forecast_summary="Forecast: likely to slip by 1 week.", forecast_confidence="medium",
        ),
    ),
    ("ON TRACK", HealthSummary(overall_risk=RiskLevel.MEDIUM, high_count=0, medium_count=2, low_count=1, done_count=1, total_count=4, delta_direction="unchanged", prior_counts=None)),
    ("NEEDS INPUT", HealthSummary(overall_risk=RiskLevel.LOW, high_count=0, medium_count=0, low_count=0, done_count=0, total_count=0, delta_direction="unchanged", prior_counts=None)),
)


def _render_health_banner(health: HealthSummary) -> str:
    environment = Environment(loader=FileSystemLoader(str(_TEMPLATES_ROOT)), trim_blocks=True, lstrip_blocks=True)
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)
    return environment.get_template("partials/health_banner.j2").render(health=health, milestone_rows=())


_INLINE_COLOR_PAIR_RE = re.compile(
    r"""background-color:\s*(\#[0-9A-Fa-f]{6})\s*;[^"']*?\bcolor:\s*(\#[0-9A-Fa-f]{6})""",
)
_INLINE_COLOR_PAIR_REVERSED_RE = re.compile(
    r"""color:\s*(\#[0-9A-Fa-f]{6})\s*;[^"']*?background-color:\s*(\#[0-9A-Fa-f]{6})""",
)


def test_health_banner_text_meets_wcag_aa_contrast_for_every_state() -> None:
    """specs/bklg.md BL-H2, 2026-07-26: real bug found via axe-core -- AT
    RISK and ON TRACK both set state_fg="#FFFFFF" against a background too
    light for white text (2.9-3.1:1, needs 4.5:1), and 2 of 4 priority-tally
    tiles (Med/Done) paired white text with a background in the same failing
    range. Fixed (state_fg now WCAG-verified per state; every body-text line
    inherits it instead of a hardcoded literal; Med/Done tiles switched to
    black). This test renders the real template for all 5 health states and
    checks every same-element background-color/color inline-style pair --
    it would have caught the original bug and fails again if either
    regresses."""
    violations: list[str] = []
    for state_name, health in _HEALTH_STATES:
        html = _render_health_banner(health)
        pairs = {(m.group(1).upper(), m.group(2).upper()) for m in _INLINE_COLOR_PAIR_RE.finditer(html)}
        pairs |= {(m.group(2).upper(), m.group(1).upper()) for m in _INLINE_COLOR_PAIR_REVERSED_RE.finditer(html)}
        for bg, fg in pairs:
            ratio = _contrast_ratio(fg, bg)
            if ratio < _WCAG_AA_NORMAL_TEXT_MIN_CONTRAST:
                violations.append(f"{state_name}: color {fg} on background {bg} = {ratio:.2f} (need >= {_WCAG_AA_NORMAL_TEXT_MIN_CONTRAST})")
    assert not violations, "Insufficient contrast in health_banner.j2:\n" + "\n".join(violations)


def test_health_banner_priority_tally_tiles_meet_wcag_aa_contrast() -> None:
    """specs/bklg.md BL-H2, 2026-07-26: the High/Med/Low/Done priority-count
    tiles pair a background-color on the parent <td> with a color on the
    child <p> -- a different element than the state banner above, so it
    needs its own check (a same-attribute regex cannot see a parent/child
    color pairing). Real bug: Med (#BF8F00 bg) and Done (#4EA72E bg) both
    used white text (2.94:1 / 3.05:1); fixed to black (7.14:1 / 6.89:1).
    High (#C00000, white) and Low (#B4E5A2, black) were already correct."""
    html = _render_health_banner(_HEALTH_STATES[1][1])  # CRITICAL: guarantees all 4 tiles have a non-zero, stable label
    violations: list[str] = []
    for label in ("High", "Med", "Low", "Done"):
        pattern = re.compile(
            r"""background-color:(\#[0-9A-Fa-f]{6})[^>]*>\s*"""
            rf"""<p style="[^"]*color:(\#[0-9A-Fa-f]{{6}});">{label}</p>""",
        )
        match = pattern.search(html)
        assert match is not None, f"could not locate the {label!r} priority tile in rendered health_banner.j2 output"
        bg, fg = match.group(1).upper(), match.group(2).upper()
        ratio = _contrast_ratio(fg, bg)
        if ratio < _WCAG_AA_NORMAL_TEXT_MIN_CONTRAST:
            violations.append(f"{label} tile: color {fg} on background {bg} = {ratio:.2f} (need >= {_WCAG_AA_NORMAL_TEXT_MIN_CONTRAST})")
    assert not violations, "Insufficient contrast in health_banner.j2 priority tiles:\n" + "\n".join(violations)


def test_cockpit_html_code_element_meets_wcag_aa_contrast() -> None:
    """specs/bklg.md BL-H2, 2026-07-26: real bug found via axe-core --
    ``code`` had a fixed light background (#F6F8FA) with no explicit
    foreground color; a dark-mode client (this page opts into
    ``color-scheme: light dark``) auto-inverted the inherited text to
    white, giving a 1.06:1 contrast (needs 4.5:1) -- invisible to any
    static source scan since the failure only exists post-render, in one
    specific color-scheme. Fixed with an explicit, scheme-independent
    foreground. This pins the specific pair rather than re-rendering (no
    browser/dark-mode emulation available at test time)."""
    from src.core.cockpit_html import render_cockpit_html
    from tests.golden.test_cockpit_html_golden import _fixture_snapshot

    html = render_cockpit_html(_fixture_snapshot())
    match = re.search(r"""code\s*\{[^}]*background:\s*(\#[0-9A-Fa-f]{6})[^}]*color:\s*(\#[0-9A-Fa-f]{6})""", html)
    assert match is not None, "expected the 'code' CSS rule to declare both background and color explicitly"
    bg, fg = match.group(1).upper(), match.group(2).upper()
    ratio = _contrast_ratio(fg, bg)
    assert ratio >= _WCAG_AA_NORMAL_TEXT_MIN_CONTRAST, f"code color {fg} on background {bg} = {ratio:.2f} (need >= {_WCAG_AA_NORMAL_TEXT_MIN_CONTRAST})"
