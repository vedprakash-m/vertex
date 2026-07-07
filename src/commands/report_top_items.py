from __future__ import annotations

from src.core.jinja_filters import risk_label
from src.core.jinja_filters import build_anchor
from src.core.models import DeltaKind, DimensionRisk, RiskLevel, ScorecardDelta, ScorecardEvidencePacket
from src.core.overrides_store import OverridesDocument
from src.core.view_models import ScorecardData, Top3Item


def _humanize_anchor(value: str | None) -> str:
    if value in (None, ""):
        return "This issue"
    words = str(value).replace("_", " ").replace("-", " ").split()
    return " ".join(word.upper() if word.isupper() else word.capitalize() for word in words) or "This issue"


def _top_item_label(item_type: str) -> str:
    normalized = item_type.strip().lower()
    if normalized in {"decision", "ask"}:
        return "DECISION"
    if normalized in {"improved", "win"}:
        return "IMPROVED"
    return "RISK"


def _risk_from_top_item_type(item_type: str) -> RiskLevel:
    normalized = item_type.strip().lower()
    if normalized in {"decision", "ask"}:
        return RiskLevel.HIGH
    if normalized in {"risk", "watch"}:
        return RiskLevel.MEDIUM
    if normalized in {"improved", "win"}:
        return RiskLevel.LOW
    return RiskLevel.UNKNOWN


def _resolve_forwarding_context(
    overrides_document: OverridesDocument,
    top_items: tuple[Top3Item, ...],
    auto_suggestions: tuple[Top3Item, ...],
) -> str | None:
    if overrides_document.forwarding_context is not None and overrides_document.forwarding_context.strip():
        return _truncate_words(overrides_document.forwarding_context.strip(), 35)
    source_items = top_items or auto_suggestions
    if not source_items:
        return None
    first_item = source_items[0]
    if first_item.item_type.strip().lower() not in {"decision", "ask", "risk", "watch"}:
        return None
    dimension = _humanize_anchor(first_item.anchor)
    if first_item.item_type.strip().lower() in {"decision", "ask"} and first_item.by_date is not None:
        return _truncate_words(
            f"I'm tracking {dimension}. Please confirm {first_item.owner or 'the owner'} has resources needed by {first_item.by_date.strftime('%b %d')}.",
            35,
        )
    if first_item.item_type.strip().lower() in {"decision", "ask"}:
        return _truncate_words(
            f"I'm tracking {dimension}. Please confirm {first_item.owner or 'the owner'} is unblocked.",
            35,
        )
    return _truncate_words(f"I'm watching {dimension} closely - may need attention soon.", 35)


def _truncate_words(text: str, word_limit: int) -> str:
    words = text.split()
    if len(words) <= word_limit:
        return text.strip()
    return " ".join(words[:word_limit]).rstrip(".,;:") + "."


def _build_top_items(
    overrides_document: OverridesDocument,
    scorecards: tuple[ScorecardData, ...] = (),
) -> tuple[Top3Item, ...]:
    anchor_aliases: dict[str, str] = {}
    for scorecard in scorecards:
        scorecard_anchor = build_anchor(scorecard.scorecard_name)
        normalized_name = scorecard.scorecard_name.strip().lower()
        if "contoso" in normalized_name:
            anchor_aliases.setdefault("contoso", scorecard_anchor)
            anchor_aliases.setdefault("dd-on-pf", scorecard_anchor)
    return tuple(
        Top3Item(
            item_type=entry.type,
            text=entry.text,
            owner=entry.owner,
            ado_link=entry.ado_link,
            anchor=anchor_aliases.get(entry.anchor, entry.anchor),
            by_date=entry.by_date,
            label=_top_item_label(entry.type),
        )
        for entry in overrides_document.top_3_now
        if entry.text.strip()
    )


def _subject_signal(
    dimension_risks: tuple[DimensionRisk, ...],
    top_items: tuple[Top3Item, ...],
    auto_suggestions: tuple[Top3Item, ...],
    scorecard_deltas: tuple[ScorecardDelta, ...],
) -> str:
    source_items = top_items or auto_suggestions
    if source_items:
        first_item = source_items[0]
        if first_item.item_type.strip().lower() in {"decision", "ask"} and first_item.by_date is not None:
            return _truncate_words(f"{_humanize_anchor(first_item.anchor)} decision needed by {first_item.by_date.strftime('%b %d')}", 6)
    for delta in scorecard_deltas:
        if delta.new_risk == RiskLevel.HIGH and delta.old_risk != RiskLevel.HIGH:
            return _truncate_words(f"{delta.dimension} new High risk", 5)
    if dimension_risks and all(dimension.risk in {RiskLevel.LOW, RiskLevel.DONE} for dimension in dimension_risks):
        return "all dimensions Low or Done"
    return "no new leadership ask"


def _build_auto_suggested_top_items(
    scorecard_deltas: tuple[ScorecardDelta, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
) -> tuple[Top3Item, ...]:
    suggestions: list[Top3Item] = []
    query_urls = {
        (scorecard_name, dimension_name): packet.ado_query_url
        for scorecard_name, packet_map in scorecard_packets.items()
        for dimension_name, packet in packet_map.items()
    }
    for delta in scorecard_deltas:
        anchor = build_anchor(delta.dimension)
        ado_link = ""
        for (scorecard_name, dimension_name), query_url in query_urls.items():
            if dimension_name == delta.dimension:
                ado_link = query_url
                anchor = build_anchor(f"{scorecard_name}-{dimension_name}")
                break
        if delta.new_risk == RiskLevel.HIGH and delta.old_risk != RiskLevel.HIGH:
            suggestions.append(
                Top3Item(
                    item_type="decision",
                    text=f"{delta.dimension} escalated to High — confirm mitigation plan.",
                    owner="Author",
                    ado_link=ado_link,
                    anchor=anchor,
                    label="DECISION",
                    suggested=True,
                )
            )
            continue
        if delta.delta_kind == DeltaKind.RISK_DOWN:
            suggestions.append(
                Top3Item(
                    item_type="improved",
                    text=f"{delta.dimension} improved from {risk_label(delta.old_risk)} to {risk_label(delta.new_risk)}.",
                    owner="Author",
                    ado_link=ado_link,
                    anchor=anchor,
                    label="IMPROVED",
                    suggested=True,
                )
            )
            continue
        if delta.delta_kind == DeltaKind.RISK_UP:
            suggestions.append(
                Top3Item(
                    item_type="risk",
                    text=f"{delta.dimension} risk increased to {risk_label(delta.new_risk)}.",
                    owner="Author",
                    ado_link=ado_link,
                    anchor=anchor,
                    label="RISK",
                    suggested=True,
                )
            )
        elif delta.delta_kind == DeltaKind.ETA_CHANGED:
            suggestions.append(
                Top3Item(
                    item_type="risk",
                    text=f"{delta.dimension} ETA changed — confirm achievability.",
                    owner="Author",
                    ado_link=ado_link,
                    anchor=anchor,
                    label="RISK",
                    suggested=True,
                )
            )
        if len(suggestions) >= 3:
            break
    return tuple(suggestions[:3])