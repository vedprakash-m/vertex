from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.core.models import DRISummary, ReportData, RiskLevel, WorkItem
from src.core.reviewer_renderer import ReviewerSectionData


@dataclass(frozen=True, slots=True)
class NudgeCardItem:
    work_item_id: int
    title: str
    freshness_days: int
    workstream_name: str | None
    item_url: str


@dataclass(frozen=True, slots=True)
class AdaptiveCardRenderer:
    def render_section_review(
        self,
        *,
        edition_name: str,
        issue_number: int,
        section: ReviewerSectionData,
        review_html_url: str,
    ) -> dict[str, Any]:
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": f"{edition_name} section review",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"Issue {issue_number:03d} | {section.title}",
                "wrap": True,
                "spacing": "Small",
                "isSubtle": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "State", "value": section.state_label},
                    {"title": "Reviewer", "value": section.reviewer_display},
                    {"title": "Delta rows", "value": str(len(section.delta_rows))},
                    {"title": "Evidence rows", "value": str(len(section.evidence_rows))},
                ],
            },
        ]

        note_text = _normalize_card_text(section.note or "")
        if note_text:
            body.append(
                {
                    "type": "TextBlock",
                    "text": f"Reviewer note: {note_text}",
                    "wrap": True,
                    "spacing": "Medium",
                }
            )

        summary_text = _normalize_card_text(section.published_text)
        if summary_text:
            body.append(
                {
                    "type": "TextBlock",
                    "text": summary_text,
                    "wrap": True,
                    "spacing": "Small",
                }
            )

        if section.override_rows:
            body.append(
                {
                    "type": "TextBlock",
                    "text": "Risk chips",
                    "weight": "Bolder",
                    "spacing": "Medium",
                }
            )
            body.extend(_build_section_risk_chip_blocks(section))

        if review_html_url:
            body.append(
                {
                    "type": "ActionSet",
                    "spacing": "Medium",
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Review in browser",
                            "url": review_html_url,
                        }
                    ],
                }
            )

        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "msteams": {"width": "Full"},
            "body": body,
        }

    def render_weekly_summary(
        self,
        *,
        edition_name: str,
        issue_number: int,
        report: ReportData,
        item_urls: Mapping[int, str],
        report_html_url: str | None = None,
    ) -> dict[str, Any]:
        high_dimensions = sum(1 for dimension in report.scorecard if dimension.risk is RiskLevel.HIGH)
        medium_dimensions = sum(1 for dimension in report.scorecard if dimension.risk is RiskLevel.MEDIUM)
        changed_dimensions = len(report.scorecard_deltas)
        summary_text = _normalize_card_text(report.exec_summary_text)
        focus_items = _select_weekly_summary_items(report.items)

        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": f"{edition_name} weekly summary",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"Issue {issue_number:03d} | As of {report.ado_data_as_of.strftime('%Y-%m-%d %H:%M UTC')}"
                ),
                "wrap": True,
                "spacing": "Small",
                "isSubtle": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "High dimensions", "value": str(high_dimensions)},
                    {"title": "Medium dimensions", "value": str(medium_dimensions)},
                    {"title": "Changed dimensions", "value": str(changed_dimensions)},
                    {"title": "Freshness blocks", "value": str(report.freshness.blocks)},
                ],
            },
        ]
        if summary_text:
            body.append(
                {
                    "type": "TextBlock",
                    "text": summary_text,
                    "wrap": True,
                    "spacing": "Medium",
                }
            )

        if report_html_url:
            body.append(
                {
                    "type": "ActionSet",
                    "spacing": "Medium",
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Full newsletter",
                            "url": report_html_url,
                        }
                    ],
                }
            )

        for item in focus_items:
            item_url = item_urls.get(item.id, "#")
            detail_parts = [f"Risk: {item.risk_level.value}"]
            if item.assigned_to:
                detail_parts.append(f"Owner: {item.assigned_to}")
            if item.target_date is not None:
                detail_parts.append(f"Target: {item.target_date.isoformat()}")
            container: dict[str, Any] = {
                "type": "Container",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"ADO#{item.id} {item.title}",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": " | ".join(detail_parts),
                        "wrap": True,
                        "spacing": "Small",
                        "isSubtle": True,
                    },
                ],
            }
            if item_url and item_url != "#":
                container["items"].append(
                    {
                        "type": "ActionSet",
                        "spacing": "Small",
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "Open in ADO",
                                "url": item_url,
                            }
                        ],
                    }
                )
            body.append(container)

        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "msteams": {"width": "Full"},
            "body": body,
        }

    def render_nudge_alert(
        self,
        *,
        program_name: str,
        recipient_name: str,
        items: tuple[NudgeCardItem, ...],
    ) -> dict[str, Any]:
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": f"{program_name} stale item nudge",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"Hi {recipient_name}. {len(items)} stale item(s) would benefit from a quick ADO refresh.",
                "wrap": True,
                "spacing": "Small",
            },
        ]

        for item in items:
            detail_parts = [f"No ADO update in {item.freshness_days} days"]
            if item.workstream_name:
                detail_parts.append(f"Workstream: {item.workstream_name}")
            container: dict[str, Any] = {
                "type": "Container",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"ADO#{item.work_item_id} {item.title}",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": " | ".join(detail_parts),
                        "wrap": True,
                        "spacing": "Small",
                        "isSubtle": True,
                    },
                    {
                        "type": "ActionSet",
                        "spacing": "Small",
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "Update in ADO",
                                "url": item.item_url,
                            }
                        ],
                    },
                ],
            }
            body.append(container)

        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "msteams": {"width": "Full"},
            "body": body,
        }

    def render_freshness_alert(
        self,
        *,
        edition_name: str,
        summary: DRISummary,
        items_by_id: Mapping[int, WorkItem],
        item_urls: Mapping[int, str],
    ) -> dict[str, Any]:
        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "text": f"{edition_name} freshness alert",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"Hi {summary.dri_name}. {summary.open_count} open item(s), "
                    f"{summary.overdue_count} overdue, {summary.stale_count} stale."
                ),
                "wrap": True,
                "spacing": "Small",
            },
        ]

        for finding in summary.items:
            item = items_by_id.get(finding.work_item_id)
            if item is None:
                continue
            item_url = item_urls.get(item.id, "#")
            container: dict[str, Any] = {
                "type": "Container",
                "separator": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"ADO#{item.id} {item.title}",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": finding.message,
                        "wrap": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": f"Suggested action: {finding.action_message or finding.message}",
                        "wrap": True,
                        "isSubtle": True,
                        "spacing": "None",
                    },
                ],
            }
            if item_url and item_url != "#":
                container["items"].append(
                    {
                        "type": "ActionSet",
                        "spacing": "Small",
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "Update in ADO",
                                "url": item_url,
                            }
                        ],
                    }
                )
            body.append(container)

        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "msteams": {"width": "Full"},
            "body": body,
        }


def _normalize_card_text(value: str) -> str:
    collapsed = " ".join(segment.strip() for segment in value.replace("<!-- state -->", " ").split())
    return collapsed[:500]


def _build_section_risk_chip_blocks(section: ReviewerSectionData) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for row in section.override_rows:
        blocks.append(
            {
                "type": "Container",
                "style": _risk_chip_style(row.current_risk),
                "spacing": "Small",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"{row.dimension_name}: {row.current_risk.value.upper()}",
                        "weight": "Bolder",
                        "wrap": True,
                        "size": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": _normalize_card_text(row.summary),
                        "wrap": True,
                        "spacing": "None",
                        "size": "Small",
                    },
                ],
            }
        )
    return blocks


def _risk_chip_style(risk: RiskLevel) -> str:
    if risk is RiskLevel.HIGH:
        return "attention"
    if risk is RiskLevel.MEDIUM:
        return "warning"
    if risk in {RiskLevel.LOW, RiskLevel.DONE}:
        return "good"
    return "accent"


def _select_weekly_summary_items(items: tuple[WorkItem, ...]) -> tuple[WorkItem, ...]:
    ranked_items = sorted(items, key=_weekly_summary_item_sort_key)
    return tuple(ranked_items[:3])


def _weekly_summary_item_sort_key(item: WorkItem) -> tuple[int, int, str, int]:
    risk_rank = {
        RiskLevel.HIGH: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.LOW: 2,
        RiskLevel.DONE: 3,
        RiskLevel.UNKNOWN: 4,
    }
    target_rank = item.target_date.toordinal() if item.target_date is not None else 999999999
    owner = item.assigned_to or ""
    return (risk_rank.get(item.risk_level, 5), target_rank, owner.lower(), item.id)