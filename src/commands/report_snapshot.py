from __future__ import annotations

from datetime import datetime
from typing import Any

from src.core.config_loader import ReportBundle
from src.core.models import ConfirmedDimension, DeltaKind, ReportData, RiskLevel, ScorecardDelta, Snapshot, SnapshotItem


def _group_scorecard_deltas(
    deltas: tuple[ScorecardDelta, ...],
) -> dict[str, dict[str, ScorecardDelta]]:
    grouped: dict[str, dict[str, ScorecardDelta]] = {}
    for delta in deltas:
        grouped.setdefault("default", {})[delta.dimension] = delta
    return grouped


def _build_snapshot(
    report: ReportData,
    scorecard_packets: dict[str, dict[str, Any]],
) -> Snapshot:
    confirmed_dimensions: list[ConfirmedDimension] = []
    risks_by_name = {dimension.name: dimension.risk for dimension in report.scorecard}
    for scorecard_name, packet_map in scorecard_packets.items():
        for dimension_name, packet in packet_map.items():
            risk = risks_by_name.get(dimension_name)
            if risk is None:
                continue
            confirmed_dimensions.append(
                ConfirmedDimension(
                    scorecard_name=scorecard_name,
                    name=dimension_name,
                    risk=risk,
                    prior_risk=packet.prior_confirmed_risk,
                    item_count=packet.total_items,
                    ado_query_url=packet.ado_query_url,
                )
            )

    return Snapshot(
        issue_number=report.issue_number,
        generated_at=report.generated_at,
        ado_data_as_of=report.ado_data_as_of,
        edition_type=report.edition,
        items=tuple(
            SnapshotItem(
                id=item.id,
                type=item.type,
                title=item.title,
                state=item.state,
                assigned_to=item.assigned_to,
                area_path=item.area_path,
                target_date=item.target_date,
                risk_level=item.risk_level,
                tags=list(item.tags),
            )
            for item in report.items
        ),
        scorecards=tuple(confirmed_dimensions),
    )


def _scorecard_delta_kind(old_risk: RiskLevel, new_risk: RiskLevel) -> DeltaKind:
    ordering = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.DONE: -1}
    return DeltaKind.RISK_UP if ordering.get(new_risk, 0) > ordering.get(old_risk, 0) else DeltaKind.RISK_DOWN


def _format_edition_title(bundle: ReportBundle, issue_number: int, as_of: datetime) -> str:
    try:
        return bundle.config.edition.title.format(issue_number=issue_number, date=as_of.date().isoformat())
    except KeyError:
        return bundle.config.edition.title


def _format_ban_violation(violation: Any) -> str:
    return f"{violation.location}: banned phrase '{violation.phrase}' matched '{violation.matched_text}'."


def _derive_qg_status(has_blockers: bool, has_warnings: bool) -> str:
    if has_blockers:
        return "blocked"
    if has_warnings:
        return "warn"
    return "pass"