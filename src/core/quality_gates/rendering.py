"""HTML/email rendering helpers extracted from ``src/core/quality_gates``.

This leaf owns the shared parsing and Outlook-safety checks used by multiple
quality-gate clusters as the D-09 decomposition continues.
"""
from __future__ import annotations

from html import unescape
import re

from src.core.jinja_filters import DELTA_COLORS, RISK_COLORS, TOP_ITEM_TOKENS
from src.core.quality_gates.models import GateEvaluation

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_BODY_RE = re.compile(r"<body\b[^>]*>(.*)</body>", re.DOTALL | re.IGNORECASE)
_HTML_HIDDEN_BLOCK_RE = re.compile(
    r"<([a-z0-9]+)\b[^>]*(display\s*:\s*none|max-height\s*:\s*0|mso-hide\s*:\s*all)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WHITESPACE_RE = re.compile(r"\s+")
_HTML_BLOCK_RE = re.compile(r'data-vertex-block="([^"]+)"')
_HTML_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_HTML_STYLE_ATTR_RE = re.compile(r'style\s*=\s*([\'"])(.*?)\1', re.DOTALL | re.IGNORECASE)
_HTML_TABLE_TAG_RE = re.compile(r"<table\b([^>]*)>", re.IGNORECASE)
_HTML_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
_HTML_CLASS_ATTR_RE = re.compile(r'\bclass\s*=\s*([\'"]).*?\1', re.DOTALL | re.IGNORECASE)
_VISIBLE_TOOL_RE = re.compile(r"\b(vertex|manifest)\b", re.IGNORECASE)
_VISIBLE_MANIFEST_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_EMAIL_COMPAT_ALLOWED_HEX_COLORS = frozenset(
    color.lower()
    for color in (
        "#000000",
        "#020617",
        "#047857",
        "#059669",
        "#075985",
        "#082F49",
        "#0B1120",
        "#0F172A",
        "#111827",
        "#13233D",
        "#14532D",
        "#166534",
        "#16A34A",
        "#172554",
        "#1D4ED8",
        "#1E293B",
        "#1E3A5F",
        "#1E3A8A",
        "#1E40AF",
        "#1F2937",
        "#2563EB",
        "#334155",
        "#34D399",
        "#374151",
        "#3B82F6",
        "#450A0A",
        "#451A03",
        "#475569",
        "#4B5563",
        "#4EA72E",
        "#4F46E5",
        "#60A5FA",
        "#605E5C",
        "#6366F1",
        "#64748B",
        "#6B21A8",
        "#6B7280",
        "#78350F",
        "#7C2D12",
        "#7F1D1D",
        "#86EFAC",
        "#92400E",
        "#93C5FD",
        "#94A3B8",
        "#991B1B",
        "#9A3412",
        "#9CA3AF",
        "#A4262C",
        "#B4E5A2",
        "#B7E1CD",
        "#B91C1C",
        "#BAE6FD",
        "#BBF7D0",
        "#BC7C00",
        "#BF8F00",
        "#BFBFBF",
        "#BFDBFE",
        "#C00000",
        "#CBD5E1",
        "#D1D5DB",
        "#D1FAE5",
        "#D6DDE8",
        "#D97706",
        "#DBEAFE",
        "#DC2626",
        "#DCFCE7",
        "#E0F2FE",
        "#E1E1E1",
        "#E2E8F0",
        "#E5E7EB",
        "#E5EEFB",
        "#E97132",
        "#EEF2FF",
        "#EF4444",
        "#EFF6FF",
        "#F0F4FF",
        "#F0FFF4",
        "#F1F5F9",
        "#F3E8FF",
        "#F3F4F6",
        "#F4F7FB",
        "#F59E0B",
        "#F8FAFC",
        "#F9FAFB",
        "#FAFAFA",
        "#FB923C",
        "#FCA5A5",
        "#FCD34D",
        "#FDE68A",
        "#FECACA",
        "#FEE2E2",
        "#FEF2F2",
        "#FEF3C7",
        "#FFF7ED",
        "#FFFBEB",
        "#FFFFFF",
    )
)


def evaluate_outlook_compatibility_gate(html_content: str) -> GateEvaluation:
    if _HTML_STYLE_BLOCK_RE.search(html_content):
        return GateEvaluation(
            "QG-18",
            False,
            "Email HTML contains a <style> block; Outlook-safe output requires inline styles only.",
            2,
            forceable=True,
        )

    for match in _HTML_STYLE_ATTR_RE.finditer(html_content):
        style_value = match.group(2)
        lowered = style_value.lower()
        if "display:flex" in lowered or "display: flex" in lowered:
            return GateEvaluation(
                "QG-18",
                False,
                "Email HTML uses display:flex in an inline style; Outlook-safe output must stay table-based.",
                2,
                forceable=True,
            )
        if "display:grid" in lowered or "display: grid" in lowered:
            return GateEvaluation(
                "QG-18",
                False,
                "Email HTML uses display:grid in an inline style; Outlook-safe output must stay table-based.",
                2,
                forceable=True,
            )

        invalid_colors = sorted(
            {
                color.lower()
                for color in _HTML_HEX_COLOR_RE.findall(style_value)
                if color.lower() not in allowed_email_hex_colors()
            }
        )
        if invalid_colors:
            preview = ", ".join(invalid_colors[:5])
            return GateEvaluation(
                "QG-18",
                False,
                f"Email HTML uses non-canonical inline colors: {preview}.",
                2,
                forceable=True,
            )

    for table_match in _HTML_TABLE_TAG_RE.finditer(html_content):
        attributes = table_match.group(1)
        if not _HTML_STYLE_ATTR_RE.search(attributes):
            return GateEvaluation(
                "QG-18",
                False,
                "Email HTML contains a <table> without inline style attributes.",
                2,
                forceable=True,
            )
        if _HTML_CLASS_ATTR_RE.search(attributes):
            return GateEvaluation(
                "QG-18",
                False,
                "Email HTML contains a <table> with class-based styling; Outlook-safe output requires inline table styling.",
                2,
                forceable=True,
            )

    return GateEvaluation("QG-18", True, "Outlook compatibility gate passed.", 2, forceable=True)


def allowed_email_hex_colors() -> frozenset[str]:
    risk_colors = {
        value.lower()
        for palette in RISK_COLORS.values()
        for key, value in palette.items()
        if key in {"bg", "fg"} and isinstance(value, str) and value.startswith("#")
    }
    delta_colors = {
        value.lower()
        for palette in DELTA_COLORS.values()
        for value in palette.values()
        if isinstance(value, str) and value.startswith("#")
    }
    token_colors = {
        value.lower()
        for palette in TOP_ITEM_TOKENS.values()
        for value in palette.values()
        if isinstance(value, str) and value.startswith("#")
    }
    return frozenset(risk_colors | delta_colors | token_colors | set(_EMAIL_COMPAT_ALLOWED_HEX_COLORS))


def continuity_block_positions(html_content: str) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for match in _HTML_BLOCK_RE.finditer(_HTML_COMMENT_RE.sub(" ", html_content)):
        positions.setdefault(match.group(1), []).append(match.start())
    return positions


def visible_html_text(html_content: str) -> str:
    body_match = _HTML_BODY_RE.search(html_content)
    visible_scope = body_match.group(1) if body_match is not None else html_content
    without_comments = _HTML_COMMENT_RE.sub(" ", visible_scope)
    without_scripts = _HTML_SCRIPT_STYLE_RE.sub(" ", without_comments)
    without_hidden = _HTML_HIDDEN_BLOCK_RE.sub(" ", without_scripts)
    without_tags = _HTML_TAG_RE.sub(" ", without_hidden)
    return _HTML_WHITESPACE_RE.sub(" ", unescape(without_tags)).strip()


def find_visible_tool_attribution(visible_text: str) -> str | None:
    tool_match = _VISIBLE_TOOL_RE.search(visible_text)
    manifest_match = _VISIBLE_MANIFEST_ID_RE.search(visible_text)
    if tool_match is not None:
        return tool_match.group(0)
    if manifest_match is not None:
        return manifest_match.group(0)
    return None
