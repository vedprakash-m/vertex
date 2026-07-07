# Adapted from Shiproom src/report/filters.py
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from html import escape
from typing import Any

from markupsafe import Markup

from src.core.models import DeltaKind, RiskLevel, ScorecardEvidencePacket
from src.core.view_models import KpiTile

# Module-level ADO web URL — set by callers that have program config context.
# When empty, ADO# tokens are rendered as plain text (no clickable link).
_ADO_WEB_ORG: str = ""
_ADO_WEB_PROJECT: str = ""


def configure_ado_web_url(*, org: str, project: str) -> None:
    """Configure the ADO web URL used by ADO# token linkification filters."""
    global _ADO_WEB_ORG, _ADO_WEB_PROJECT
    _ADO_WEB_ORG = org
    _ADO_WEB_PROJECT = project


_SCORECARD_SHORT_LABELS: dict[str, str] = {}


def configure_scorecard_labels(labels: Mapping[str, Any] | None) -> None:
    """Configure per-program scorecard short labels for Jinja filters."""
    global _SCORECARD_SHORT_LABELS
    if labels is None:
        _SCORECARD_SHORT_LABELS = {}
        return
    _SCORECARD_SHORT_LABELS = {
        str(key).strip(): str(value).strip()
        for key, value in labels.items()
        if str(key).strip() and isinstance(value, (str, int, float)) and str(value).strip()
    }


RISK_COLORS: dict[RiskLevel, dict[str, str]] = {
    RiskLevel.BLOCKED: {"bg": "#C00000", "fg": "#FFFFFF", "border": "#FFFFFF", "icon": "🔴", "label": "Blocked"},
    RiskLevel.HIGH:    {"bg": "#E97132", "fg": "#FFFFFF", "border": "#FFFFFF", "icon": "🔴", "label": "High"},
    RiskLevel.MEDIUM:  {"bg": "#FFE699", "fg": "#000000", "border": "#BF8F00", "icon": "🟡", "label": "Medium"},
    RiskLevel.LOW:     {"bg": "#B4E5A2", "fg": "#000000", "border": "#4EA72E", "icon": "🟢", "label": "Low"},
    RiskLevel.DONE:    {"bg": "#4EA72E", "fg": "#FFFFFF", "border": "#FFFFFF", "icon": "✅", "label": "Done"},
    RiskLevel.UNKNOWN: {"bg": "#C00000", "fg": "#FFFFFF", "border": "#FFFFFF", "icon": "⚪", "label": "Needs Input"},
}

DELTA_COLORS: dict[DeltaKind, dict[str, str]] = {
    DeltaKind.RISK_UP: {"bg": "#FEE2E2", "fg": "#B91C1C"},
    DeltaKind.RISK_DOWN: {"bg": "#D1FAE5", "fg": "#047857"},
    DeltaKind.NEW: {"bg": "#DBEAFE", "fg": "#1E40AF"},
    DeltaKind.CLOSED: {"bg": "#D1FAE5", "fg": "#047857"},
    DeltaKind.ETA_CHANGED: {"bg": "#FEF3C7", "fg": "#92400E"},
    DeltaKind.OWNER_CHANGED: {"bg": "#FEF3C7", "fg": "#92400E"},
    DeltaKind.UNCHANGED: {"bg": "#F3F4F6", "fg": "#4B5563"},
}

TOP_ITEM_TOKENS: dict[str, dict[str, str]] = {
    "decision": {"icon": "🔴", "border": "#991B1B"},
    "ask": {"icon": "🔴", "border": "#991B1B"},
    "risk": {"icon": "🟡", "border": "#92400E"},
    "watch": {"icon": "🟡", "border": "#92400E"},
    "improved": {"icon": "🟢", "border": "#047857"},
    "win": {"icon": "🟢", "border": "#047857"},
}

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_INLINE_BOLD_RE = re.compile(r"__([^_]+)__|[*][*]([^*]+)[*][*]")
_ADO_TOKEN_RE = re.compile(r"\bADO#(\d+)\b")


def _linkify_ado_tokens_html(value: str) -> str:
    def _make_link(match: re.Match[str]) -> str:
        work_item_id = match.group(1)
        if _ADO_WEB_ORG and _ADO_WEB_PROJECT:
            url = f"https://dev.azure.com/{_ADO_WEB_ORG}/{_ADO_WEB_PROJECT}/_workitems/edit/{work_item_id}"
            return (
                f'<a href="{url}" '
                f'style="color:#2563EB; text-decoration:underline;">ADO#{work_item_id}</a>'
            )
        return f"ADO#{work_item_id}"
    return _ADO_TOKEN_RE.sub(_make_link, escape(value))


def _coerce_risk(level: RiskLevel | str | None) -> RiskLevel:
    if isinstance(level, RiskLevel):
        return level
    if level is None:
        return RiskLevel.UNKNOWN
    normalized = str(level).strip().lower()
    if normalized in {"high", RiskLevel.HIGH.value}:
        return RiskLevel.HIGH
    if normalized in {"medium", RiskLevel.MEDIUM.value}:
        return RiskLevel.MEDIUM
    if normalized in {"low", RiskLevel.LOW.value}:
        return RiskLevel.LOW
    if normalized in {"done", RiskLevel.DONE.value}:
        return RiskLevel.DONE
    if normalized in {"unknown", "needs input", RiskLevel.UNKNOWN.value}:
        return RiskLevel.UNKNOWN
    raise ValueError(f"Unsupported RiskLevel value: {level!r}")


def _coerce_delta(kind: DeltaKind | str | None) -> DeltaKind:
    if isinstance(kind, DeltaKind):
        return kind
    if kind is None:
        return DeltaKind.UNCHANGED
    return DeltaKind.from_string(str(kind))


def _coerce_datetime(value: datetime | date | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def format_date(value: datetime | date | str | None, fmt: str = "%b %d, %Y") -> str:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    return parsed.strftime(fmt)


def format_datetime(value: datetime | date | str | None, fmt: str = "%b %d %Y, %H:%M UTC") -> str:
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    return parsed.strftime(fmt)


def format_datetime_pacific(value: datetime | date | str | None) -> str:
    from datetime import timedelta
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    # Determine PDT (UTC-7, Mar–Nov) vs PST (UTC-8, Nov–Mar) by month
    pacific_offset = -7 if 3 <= parsed.month <= 10 else -8
    tz_label = "PDT" if pacific_offset == -7 else "PST"
    pacific_dt = parsed + timedelta(hours=pacific_offset)
    return pacific_dt.strftime(f"%b %d %Y, %I:%M %p") + f" {tz_label}"


def build_anchor(value: str | None) -> str:
    if not value:
        return "section"
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-") or "section"


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def risk_label(level: RiskLevel | str | None) -> str:
    return RISK_COLORS[_coerce_risk(level)]["label"]


def risk_short_label(level: RiskLevel | str | None) -> str:
    resolved = _coerce_risk(level)
    if resolved == RiskLevel.BLOCKED:
        return "Blocked"
    if resolved == RiskLevel.HIGH:
        return "H"
    if resolved == RiskLevel.MEDIUM:
        return "M"
    if resolved == RiskLevel.LOW:
        return "L"
    if resolved == RiskLevel.DONE:
        return "Done"
    return "--"


def risk_icon(level: RiskLevel | str | None) -> str:
    return RISK_COLORS[_coerce_risk(level)]["icon"]


def risk_bg(level: RiskLevel | str | None) -> str:
    return RISK_COLORS[_coerce_risk(level)]["bg"]


def risk_fg(level: RiskLevel | str | None) -> str:
    return RISK_COLORS[_coerce_risk(level)]["fg"]


def risk_border_color(level: RiskLevel | str | None) -> str:
    return RISK_COLORS[_coerce_risk(level)]["border"]


def delta_label(kind: DeltaKind | str | None, old: Any = None, new: Any = None) -> str:
    delta_kind = _coerce_delta(kind)
    if delta_kind == DeltaKind.RISK_UP:
        return f"▲ was {risk_label(old)}"
    if delta_kind == DeltaKind.RISK_DOWN:
        return f"▼ was {risk_label(old)}"
    if delta_kind == DeltaKind.NEW:
        return "● NEW"
    if delta_kind == DeltaKind.CLOSED:
        return "✓ Closed"
    if delta_kind == DeltaKind.ETA_CHANGED:
        return f"{format_date(old, '%m/%d')} → {format_date(new, '%m/%d')}"
    if delta_kind == DeltaKind.OWNER_CHANGED:
        return "↺ Owner changed"
    return "— no change"


def delta_bg(kind: DeltaKind | str | None) -> str:
    return DELTA_COLORS[_coerce_delta(kind)]["bg"]


def delta_fg(kind: DeltaKind | str | None) -> str:
    return DELTA_COLORS[_coerce_delta(kind)]["fg"]


def qg_summary(qg_results: Mapping[str, bool] | None) -> str:
    if not qg_results:
        return "QG: pending"
    failures = sorted(gate for gate, passed in qg_results.items() if not passed)
    if not failures:
        return "QG: All gates passed"
    return f"QG: failed {', '.join(failures)}"


def top_item_icon(item_type: str | None) -> str:
    normalized = (item_type or "").strip().lower()
    return TOP_ITEM_TOKENS.get(normalized, TOP_ITEM_TOKENS["risk"])["icon"]


def top_item_border(item_type: str | None) -> str:
    normalized = (item_type or "").strip().lower()
    return TOP_ITEM_TOKENS.get(normalized, TOP_ITEM_TOKENS["risk"])["border"]


def scorecard_short_label(value: str | None) -> str:
    if value is None:
        return ""
    return _SCORECARD_SHORT_LABELS.get(value, value)


def risk_vector_label(value: str | None) -> str:
    if value in (None, ""):
        return "Low"
    return str(value)


def evidence_tooltip(packet: ScorecardEvidencePacket | None) -> str:
    if packet is None:
        return "No evidence available"
    high_count = packet.items_by_risk.get("high", 0)
    prior = risk_label(packet.prior_confirmed_risk) if packet.prior_confirmed_risk is not None else "None"
    return (
        f"{packet.total_items} items · {high_count} High · {packet.stale_count} stale · "
        f"{packet.overdue_count} overdue · Prior: {prior}"
    )


def risk_load_bar_width(risk_load: float, width: int = 80) -> int:
    normalized = max(0.0, min(risk_load / 3.0, 1.0))
    return int(round(normalized * width))


def ordinal(value: int) -> str:
    remainder_hundred = value % 100
    if 11 <= remainder_hundred <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _render_inline_links(value: str) -> str:
    rendered: list[str] = []
    cursor = 0
    for match in _MARKDOWN_LINK_RE.finditer(value):
        rendered.append(_linkify_ado_tokens_html(value[cursor:match.start()]))
        label = escape(match.group(1))
        url = escape(match.group(2), quote=True)
        rendered.append(f'<a href="{url}" style="color:#2563EB; text-decoration:underline;">{label}</a>')
        cursor = match.end()
    rendered.append(_linkify_ado_tokens_html(value[cursor:]))
    result = "".join(rendered)
    # Process __bold__ and **bold** after links and ADO tokens are resolved.
    result = _INLINE_BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", result)
    return result


def linkify_ado_tokens_markdown(value: str | None) -> str:
    if value is None or not value:
        return ""
    def _make_link(match: re.Match[str]) -> str:
        work_item_id = match.group(1)
        if _ADO_WEB_ORG and _ADO_WEB_PROJECT:
            url = f"https://dev.azure.com/{_ADO_WEB_ORG}/{_ADO_WEB_PROJECT}/_workitems/edit/{work_item_id}"
            return f"[ADO#{work_item_id}]({url})"
        return f"ADO#{work_item_id}"
    return _ADO_TOKEN_RE.sub(_make_link, value)


def rich_text_html(value: str | None) -> Markup:
    if value is None or not value.strip():
        return Markup("")

    fragments: list[str] = []
    paragraph_lines: list[str] = []
    list_items: list[str] = []
    html_block_lines: list[str] = []
    in_html_block: bool = False

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        body = " ".join(paragraph_lines)
        fragments.append(f'<p style="margin:0 0 10px 0;">{_render_inline_links(body)}</p>')
        paragraph_lines.clear()

    def flush_list() -> None:
        if not list_items:
            return
        fragments.append('<ul style="margin:0 0 10px 18px; padding:0;">')
        for item in list_items:
            fragments.append(f'<li style="margin:0 0 6px 0;">{_render_inline_links(item)}</li>')
        fragments.append('</ul>')
        list_items.clear()

    for raw_line in value.splitlines():
        line = raw_line.strip()

        # Pass through raw HTML table blocks without processing.
        if in_html_block:
            html_block_lines.append(raw_line)
            if '</table>' in line.lower():
                fragments.append('\n'.join(html_block_lines))
                html_block_lines.clear()
                in_html_block = False
            continue

        if line.lower().startswith('<table'):
            flush_paragraph()
            flush_list()
            html_block_lines.append(raw_line)
            if '</table>' in line.lower():
                fragments.append(raw_line)
                html_block_lines.clear()
            else:
                in_html_block = True
            continue

        # Pass through single-line HTML block elements (e.g. <p style="...">...</p>) verbatim.
        # These are explicitly authored HTML and must not be escaped by _render_inline_links.
        if line.lower().startswith('<p') and '</p>' in line.lower():
            flush_paragraph()
            flush_list()
            fragments.append(raw_line)
            continue

        if not line:
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("- ") or line.startswith("* "):
            flush_paragraph()
            list_items.append(line[2:].strip())
            continue
        flush_list()
        paragraph_lines.append(line)

    flush_paragraph()
    flush_list()
    return Markup("".join(fragments))


def kusto_tile_data(value: KpiTile | Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, KpiTile):
        tile = value
    else:
        tile = KpiTile(
            query_id=str(value.get("query_id") or ""),
            label=str(value.get("label") or value.get("query_id") or "KPI"),
            value=str(value.get("value") or ""),
            unit=value.get("unit") if isinstance(value.get("unit"), str) else None,
            trend=value.get("trend") if isinstance(value.get("trend"), str) else None,
            confidence=str(value.get("confidence") or "medium"),
            as_of=_coerce_datetime(value.get("as_of")),
            source_signal_id=value.get("source_signal_id") if isinstance(value.get("source_signal_id"), str) else None,
            render_mode=str(value.get("render_mode") or "metric_highlight"),
            validated=bool(value.get("validated", True)),
            refresh_on_gather=bool(value.get("refresh_on_gather", False)),
            owner_alias=value.get("owner_alias") if isinstance(value.get("owner_alias"), str) else None,
            reference_url=value.get("reference_url") if isinstance(value.get("reference_url"), str) else None,
            catalog_source=value.get("catalog_source") if isinstance(value.get("catalog_source"), dict) else None,
            result_payload=value.get("result_payload") if isinstance(value.get("result_payload"), dict) else None,
        )

    aggregate = _aggregate_incident_tile_data(tile)
    if aggregate is not None:
        return aggregate
    if not tile.validated and not tile.refresh_on_gather:
        return {
            "variant": "awaiting_data",
            "label": tile.label,
            "status": "Awaiting data",
            "owner": tile.owner_alias,
            "reference_url": tile.reference_url,
            "catalog_source": tile.catalog_source,
        }
    if not tile.validated and tile.refresh_on_gather:
        return {
            "variant": "awaiting_validation",
            "label": tile.label,
            "status": "Awaiting validation - gather pending",
            "reference_url": tile.reference_url,
            "catalog_source": tile.catalog_source,
        }
    if tile.render_mode == "table" and tile.refresh_on_gather:
        return {
            "variant": "table_notice",
            "label": tile.label,
            "status": f"Data available - see CLI inspector (python cli.py inspect kusto --query {tile.query_id}).",
            "reference_url": tile.reference_url,
            "catalog_source": tile.catalog_source,
        }
    return {
        "variant": "metric",
        "label": tile.label,
        "value": tile.value,
        "as_of": tile.as_of,
        "reference_url": tile.reference_url,
        "catalog_source": tile.catalog_source,
    }


def _aggregate_incident_tile_data(tile: KpiTile) -> dict[str, Any] | None:
    payload = tile.result_payload or {}
    sev0 = _coerce_int(payload.get("Sev0Count"), payload.get("sev0_count"), payload.get("sev0"))
    sev1 = _coerce_int(payload.get("Sev1Count"), payload.get("sev1_count"), payload.get("sev1"))
    sev2 = _coerce_int(payload.get("Sev2Count"), payload.get("sev2_count"), payload.get("sev2"))
    oldest_age_hours = _coerce_int(payload.get("OldestAgeHours"), payload.get("oldest_age_hours"))
    oldest_incident_id = _coerce_str(payload.get("OldestIncidentId"), payload.get("oldest_incident_id"))
    oldest_url = _coerce_str(payload.get("OldestUrl"), payload.get("oldest_url"))
    if sev0 is None or sev1 is None or sev2 is None:
        return None

    total = sev0 + sev1 + sev2
    if total == 0:
        suffix = ""
        return {
            "variant": "aggregate_clear",
            "label": tile.label,
            "status": f"\u2713 No active Sev 0-2{suffix}",
            "reference_url": tile.reference_url,
            "catalog_source": tile.catalog_source,
        }

    return {
        "variant": "aggregate_badges",
        "label": tile.label,
        "badges": (
            {"label": "Sev0", "value": sev0, "bg": "#a4262c", "fg": "#FFFFFF"},
            {"label": "Sev1", "value": sev1, "bg": "#bc7c00", "fg": "#FFFFFF"},
            {"label": "Sev2", "value": sev2, "bg": "#605e5c", "fg": "#FFFFFF"},
        ),
        "footer": None if oldest_age_hours is None or oldest_incident_id is None else f"Oldest: {oldest_age_hours}h - {oldest_incident_id}",
        "footer_href": oldest_url,
        "reference_url": tile.reference_url,
        "catalog_source": tile.catalog_source,
    }


def _coerce_int(*values: Any) -> int | None:
    for value in values:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _coerce_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def attribution_tier4(value: Mapping[str, Any] | None) -> Markup:
    if not value:
        return Markup("")

    dashboard_name = _coerce_str(value.get("dashboard_name"), value.get("dashboard"))
    page_name = _coerce_str(value.get("page_name"), value.get("page"))
    query_ref = _coerce_str(value.get("query_ref"), value.get("query"))
    dashboard_id = _coerce_str(value.get("dashboard_id"))
    if dashboard_name is None or page_name is None or query_ref is None:
        return Markup("")

    if dashboard_id is not None:
        dashboard_url = escape(f"https://dataexplorer.azure.com/dashboards/{dashboard_id}", quote=True)
        dashboard_html = f'<a href="{dashboard_url}" style="color:#2563EB; text-decoration:underline;">{escape(dashboard_name)}</a>'
    else:
        dashboard_html = escape(dashboard_name)
    return Markup(f'Source: Dashboard "{dashboard_html}" page "{escape(page_name)}" query {escape(query_ref)}.')


def filesizeformat(value: int | float | None) -> str:
    """Format a byte count as a human-readable size string (e.g. 80 KB)."""
    if value is None:
        return "0 B"
    try:
        bytes_val = int(value)
    except (ValueError, TypeError):
        return "0 B"
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB".rstrip("0").rstrip(".")
    else:
        return f"{bytes_val / (1024 * 1024):.1f} MB".rstrip("0").rstrip(".")


JINJA_FILTERS: dict[str, Any] = {
    "attribution_tier4": attribution_tier4,
    "build_anchor": build_anchor,
    "delta_bg": delta_bg,
    "delta_fg": delta_fg,
    "delta_label": delta_label,
    "filesizeformat": filesizeformat,
    "format_date": format_date,
    "format_datetime": format_datetime,
    "format_datetime_pacific": format_datetime_pacific,
    "kusto_tile_data": kusto_tile_data,
    "linkify_ado_tokens_markdown": linkify_ado_tokens_markdown,
    "ordinal": ordinal,
    "pluralize": pluralize,
    "qg_summary": qg_summary,
    "rich_text_html": rich_text_html,
    "evidence_tooltip": evidence_tooltip,
    "risk_bg": risk_bg,
    "risk_fg": risk_fg,
    "risk_border_color": risk_border_color,
    "risk_icon": risk_icon,
    "risk_label": risk_label,
    "risk_short_label": risk_short_label,
    "risk_load_bar_width": risk_load_bar_width,
    "risk_vector_label": risk_vector_label,
    "scorecard_short_label": scorecard_short_label,
    "top_item_border": top_item_border,
    "top_item_icon": top_item_icon,
}

JINJA_GLOBALS: dict[str, Any] = {
    "risk_colors": RISK_COLORS,
}