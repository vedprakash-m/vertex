from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from difflib import ndiff

from src.core.models import ConfirmedDimension, DeltaKind, DeltaSet, RiskLevel, SnapshotItem, WorkItem
from src.core.models_v2 import Signal
from src.core.scorecard_trends import ScorecardTrend
from src.core.trajectory_analyzer import DriftPattern


@dataclass(frozen=True, slots=True)
class PublishDiffReport:
    header: str
    item_lines: tuple[str, ...]
    scorecard_lines: tuple[str, ...]
    narrative_lines: tuple[str, ...]
    signal_lines: tuple[str, ...]


def build_publish_diff_report(
    *,
    current_issue_number: int,
    reference_issue_number: int,
    reference_generated_at: datetime | None,
    current_items: tuple[WorkItem, ...],
    reference_items: tuple[SnapshotItem, ...],
    deltas: DeltaSet,
    current_dimension_risks: Mapping[tuple[str, str], RiskLevel],
    reference_scorecards: tuple[ConfirmedDimension, ...],
    scorecard_trends: Mapping[tuple[str, str], ScorecardTrend],
    current_narratives: Mapping[str, str],
    previous_narratives: Mapping[str, str],
    approved_signals: Sequence[Signal],
    drift_patterns: Sequence[DriftPattern],
) -> PublishDiffReport:
    reference_label = f"Issue #{reference_issue_number:03d}"
    header = f"VERTEX DIFF - Changes since {reference_label}"
    if reference_generated_at is not None:
        header += f" (confirmed {reference_generated_at.date().isoformat()})"
    header += f" for Issue #{current_issue_number:03d}:"

    return PublishDiffReport(
        header=header,
        item_lines=_build_item_lines(
            current_items=current_items,
            reference_items=reference_items,
            deltas=deltas,
        ),
        scorecard_lines=_build_scorecard_lines(
            current_dimension_risks=current_dimension_risks,
            reference_scorecards=reference_scorecards,
            scorecard_trends=scorecard_trends,
        ),
        narrative_lines=_build_narrative_lines(
            current_narratives=current_narratives,
            previous_narratives=previous_narratives,
        ),
        signal_lines=_build_signal_lines(
            reference_label=reference_label,
            approved_signals=approved_signals,
            drift_patterns=drift_patterns,
        ),
    )


def render_publish_diff_report(report: PublishDiffReport) -> str:
    lines = [report.header, "", "Items:"]
    lines.extend(f"  {line}" for line in report.item_lines)
    lines.extend(["", "Scorecards:"])
    lines.extend(f"  {line}" for line in report.scorecard_lines)
    lines.extend(["", "Narratives:"])
    lines.extend(f"  {line}" for line in report.narrative_lines)
    lines.extend(["", "Signals:"])
    lines.extend(f"  {line}" for line in report.signal_lines)
    return "\n".join(lines) + "\n"


def _build_item_lines(
    *,
    current_items: tuple[WorkItem, ...],
    reference_items: tuple[SnapshotItem, ...],
    deltas: DeltaSet,
) -> tuple[str, ...]:
    current_by_id = {item.id: item for item in current_items}
    reference_by_id = {item.id: item for item in reference_items}
    lines: list[str] = []

    for delta in sorted(deltas.new_items, key=lambda entry: entry.work_item_id):
        item: WorkItem | SnapshotItem | None = current_by_id.get(delta.work_item_id)
        title = item.title if item is not None else f"Work item {delta.work_item_id}"
        risk = _risk_label(item.risk_level if item is not None else delta.new_risk)
        lines.append(f'+ NEW: WI:{delta.work_item_id} "{title}" (Risk: {risk})')

    for delta in sorted(deltas.closed_items, key=lambda entry: entry.work_item_id):
        item = reference_by_id.get(delta.work_item_id)
        title = item.title if item is not None else f"Work item {delta.work_item_id}"
        prior_risk = _risk_label(item.risk_level if item is not None else delta.old_risk)
        lines.append(f'- CLOSED: WI:{delta.work_item_id} "{title}" (was: {prior_risk})')

    for delta in sorted(deltas.risk_changes, key=lambda entry: entry.work_item_id):
        item = current_by_id.get(delta.work_item_id) or reference_by_id.get(delta.work_item_id)
        title = item.title if item is not None else f"Work item {delta.work_item_id}"
        label = "RISK UP" if delta.kind == DeltaKind.RISK_UP else "RISK DOWN"
        details = f"{_risk_label(delta.old_risk)} -> {_risk_label(delta.new_risk)}"
        if delta.old_eta is not None or delta.new_eta is not None:
            details += f", ETA: {_date_label(delta.old_eta)} -> {_date_label(delta.new_eta)}"
        lines.append(f'{label}: WI:{delta.work_item_id} "{title}" ({details})')

    for delta in sorted(deltas.eta_changes, key=lambda entry: entry.work_item_id):
        item = current_by_id.get(delta.work_item_id) or reference_by_id.get(delta.work_item_id)
        title = item.title if item is not None else f"Work item {delta.work_item_id}"
        lines.append(
            f'ETA CHANGED: WI:{delta.work_item_id} "{title}" ({_date_label(delta.old_eta)} -> {_date_label(delta.new_eta)})'
        )

    for delta in sorted(deltas.owner_changes, key=lambda entry: entry.work_item_id):
        item = current_by_id.get(delta.work_item_id) or reference_by_id.get(delta.work_item_id)
        title = item.title if item is not None else f"Work item {delta.work_item_id}"
        prior_owner, new_owner = delta.field_changes.get("Assigned To", (None, None))
        if prior_owner is None and new_owner is None:
            prior_owner, new_owner = delta.field_changes.get("System.AssignedTo", (None, None))
        lines.append(
            f'OWNER CHANGED: WI:{delta.work_item_id} "{title}" ({_owner_label(prior_owner)} -> {_owner_label(new_owner)})'
        )

    if not lines:
        return ("No item deltas detected.",)
    return tuple(lines)


def _build_scorecard_lines(
    *,
    current_dimension_risks: Mapping[tuple[str, str], RiskLevel],
    reference_scorecards: tuple[ConfirmedDimension, ...],
    scorecard_trends: Mapping[tuple[str, str], ScorecardTrend],
) -> tuple[str, ...]:
    reference_risks = {
        (entry.scorecard_name, entry.name): entry.risk
        for entry in reference_scorecards
    }
    lines: list[str] = []
    all_keys = sorted(set(reference_risks) | set(current_dimension_risks), key=lambda entry: (entry[1].lower(), entry[0].lower()))
    for key in all_keys:
        dimension_name = key[1]
        prior_risk = reference_risks.get(key)
        current_risk = current_dimension_risks.get(key)
        trend = scorecard_trends.get(key)
        annotation = trend.annotation if trend is not None else None
        if prior_risk == current_risk and annotation is None:
            continue
        if prior_risk is None and current_risk is not None:
            line = f"{dimension_name}: added as {_risk_label(current_risk)}"
        elif current_risk is None:
            line = f"{dimension_name}: removed (was {_risk_label(prior_risk)})"
        elif prior_risk == current_risk:
            line = f"{dimension_name}: {_risk_label(current_risk)}"
        else:
            line = f"{dimension_name}: {_risk_label(prior_risk)} -> {_risk_label(current_risk)}"
        if annotation is not None:
            line += f" ({annotation})"
        lines.append(line)
    if not lines:
        return ("No scorecard risk changes.",)
    return tuple(lines)


def _build_narrative_lines(
    *,
    current_narratives: Mapping[str, str],
    previous_narratives: Mapping[str, str],
) -> tuple[str, ...]:
    changed: list[str] = []
    unchanged: list[str] = []
    for name in sorted(set(previous_narratives) | set(current_narratives)):
        previous_text = previous_narratives.get(name, "").strip()
        current_text = current_narratives.get(name, "").strip()
        if previous_text == current_text:
            unchanged.append(f"{name}: unchanged")
            continue
        if not previous_text and current_text:
            changed.append(f"{name}: added")
            continue
        if previous_text and not current_text:
            changed.append(f"{name}: removed")
            continue
        changed.append(f"{name}: {_line_edit_count(previous_text, current_text)} line-level edits")

    if unchanged and len(unchanged) > 3:
        changed.extend(unchanged[:2])
        changed.append(f"+{len(unchanged) - 2} more unchanged sections")
    else:
        changed.extend(unchanged)

    if not changed:
        return ("No narrative sections found.",)
    return tuple(changed)


def _build_signal_lines(
    *,
    reference_label: str,
    approved_signals: Sequence[Signal],
    drift_patterns: Sequence[DriftPattern],
) -> tuple[str, ...]:
    lines: list[str] = []
    if approved_signals:
        counts = Counter(_signal_bucket(signal.source) for signal in approved_signals)
        ordered_labels = [label for label in ("ADO", "WorkIQ", "Kusto", "IcM") if label in counts]
        ordered_labels.extend(sorted(label for label in counts if label not in ordered_labels))
        breakdown = ", ".join(f"{counts[label]} {label}" for label in ordered_labels)
        lines.append(f"{len(approved_signals)} new approved signals since {reference_label} ({breakdown}).")
    else:
        lines.append(f"No new approved signals since {reference_label}.")

    drift_count = len(drift_patterns)
    if drift_count == 0:
        lines.append("No new drift patterns detected.")
    elif drift_count == 1:
        lines.append("1 new drift pattern detected.")
    else:
        lines.append(f"{drift_count} new drift patterns detected.")
    return tuple(lines)


def _risk_label(value: RiskLevel | None) -> str:
    if value is None:
        return "None"
    return value.value.title()


def _date_label(value: date | None) -> str:
    if value is None:
        return "None"
    return value.isoformat()


def _owner_label(value: object) -> str:
    if value in (None, ""):
        return "Unassigned"
    return " ".join(str(value).split())


def _line_edit_count(previous_text: str, current_text: str) -> int:
    return sum(
        1
        for line in ndiff(previous_text.splitlines(), current_text.splitlines())
        if line.startswith(("- ", "+ "))
    )


def _signal_bucket(source: str) -> str:
    normalized = source.strip().lower()
    if normalized.startswith("ado"):
        return "ADO"
    if normalized.startswith("workiq"):
        return "WorkIQ"
    if normalized.startswith("kusto"):
        return "Kusto"
    if normalized.startswith("icm"):
        return "IcM"
    head = normalized.split("/", maxsplit=1)[0].split(":", maxsplit=1)[0].strip()
    if not head:
        return "Other"
    return head.replace("_", " ").title()