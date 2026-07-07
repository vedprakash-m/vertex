"""Chronic high-risk escalation gate extracted from ``src/core/quality_gates``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.archive_store import get_dimension_history
from src.core.models import DimensionRisk, RiskLevel
from src.core.models_v2 import Scorecard, Signal, Workstream
from src.core.quality_gates.models import GateEvaluation


def evaluate_chronic_high_dimension_gate(
    *,
    dimension_risks: tuple[DimensionRisk, ...],
    edition_name: str | None,
    journal_signals: tuple[Signal, ...],
    program_id: str | None,
    workstreams: tuple[Workstream, ...],
    scorecards: tuple[Scorecard, ...],
    archive_root: Path,
    open_risks: tuple[Any, ...],
    dimension_workstream_ids: dict[str, tuple[str, ...]],
    escalation_source: str,
) -> GateEvaluation:
    if edition_name is None or program_id is None or not dimension_risks:
        return GateEvaluation("QG-12", True, "Chronic high-risk escalation gate passed.", 2, forceable=True)

    failing_dimensions: list[str] = []
    for dimension in dimension_risks:
        if dimension.risk != RiskLevel.HIGH:
            continue
        streak = consecutive_high_count(
            (*dimension_history_levels(edition_name, dimension.name, archive_root=archive_root), dimension.risk)
        )
        if streak < 4:
            continue

        linked_workstream_ids = dimension_workstream_ids.get(dimension.name, ())
        if has_risk_or_escalation_coverage(
            dimension_name=dimension.name,
            linked_workstream_ids=linked_workstream_ids,
            workstreams=workstreams,
            open_risks=open_risks,
            journal_signals=journal_signals,
            escalation_source=escalation_source,
        ):
            continue
        failing_dimensions.append(f"{dimension.name} ({streak} issues)")

    if not failing_dimensions:
        return GateEvaluation("QG-12", True, "Chronic high-risk escalation gate passed.", 2, forceable=True)
    return GateEvaluation(
        "QG-12",
        False,
        "High-risk dimensions remained High for 4+ consecutive issues without escalation coverage or a linked risk register entry: "
        + ", ".join(failing_dimensions),
        2,
        forceable=True,
    )


def dimension_history_levels(edition_name: str, dimension_name: str, *, archive_root: Path) -> tuple[RiskLevel, ...]:
    history: list[RiskLevel] = []
    for entry in get_dimension_history(edition_name, dimension_name, archive_root=archive_root, last_n=3):
        raw_risk = str(entry.get("risk") or "").strip()
        if not raw_risk:
            continue
        try:
            history.append(RiskLevel.from_string(raw_risk))
        except ValueError:
            continue
    return tuple(history)


def consecutive_high_count(history: tuple[RiskLevel, ...]) -> int:
    count = 0
    for risk in reversed(history):
        if risk != RiskLevel.HIGH:
            break
        count += 1
    return count


def has_risk_or_escalation_coverage(
    *,
    dimension_name: str,
    linked_workstream_ids: tuple[str, ...],
    workstreams: tuple[Workstream, ...],
    open_risks: tuple[Any, ...],
    journal_signals: tuple[Signal, ...],
    escalation_source: str,
) -> bool:
    linked_workstream_id_set = set(linked_workstream_ids)
    normalized_dimension_name = dimension_name.strip().lower()
    linked_workstream_names = {
        workstream.name.strip().lower()
        for workstream in workstreams
        if workstream.id in linked_workstream_id_set
    }

    for risk in open_risks:
        if linked_workstream_id_set.intersection(risk.linked_workstream_ids):
            return True
        risk_title = risk.title.strip().lower()
        risk_description = risk.description.strip().lower()
        if normalized_dimension_name in risk_title or normalized_dimension_name in risk_description:
            return True
        if any(name in risk_title or name in risk_description for name in linked_workstream_names):
            return True

    for signal in journal_signals:
        if signal.source.strip().lower() != escalation_source:
            continue
        if signal.workstream_id is not None and signal.workstream_id in linked_workstream_id_set:
            return True
        normalized_text = signal.text.strip().lower()
        if normalized_dimension_name in normalized_text:
            return True
        if any(name in normalized_text for name in linked_workstream_names):
            return True
    return False
