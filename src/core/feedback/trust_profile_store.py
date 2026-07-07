from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from src.core.jsonl_utils import parse_jsonl_line
from pathlib import Path
import re

from src.core.analytics_store import AutonomyAuditRecord, load_autonomy_audit_records
from src.core.claim_extraction_calibration_store import summarize_claim_extraction_calibration
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback.calibration_router import load_forecast_calibration_modifier
from src.core.feedback.salience_modeler import load_author_salience


@dataclass(frozen=True, slots=True)
class EditorialTrustProfile:
    task_type: str
    label: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float
    trust_level: str


@dataclass(frozen=True, slots=True)
class AutonomyTrustProfile:
    action_type: str
    label: str
    latest_level: str
    sample_count: int
    accepted_count: int
    acceptance_rate: float
    trust_level: str
    last_applied_at: datetime


@dataclass(frozen=True, slots=True)
class AttentionGapProfile:
    workstream_id: str
    slip_modifier: float
    attention_weight: float
    bridge_summary: str


@dataclass(frozen=True, slots=True)
class ClaimExtractionTrustProfile:
    action_type: str
    label: str
    sample_count: int
    calibration_sample_count: int
    agreement_rate: float
    average_difference_count: float
    trust_level: str
    last_recorded_at: datetime


@dataclass(frozen=True, slots=True)
class TrustProfileSnapshot:
    program_id: str
    generated_at: datetime
    window_issues: int
    action_filter: str | None
    editorial_rows: tuple[EditorialTrustProfile, ...]
    claim_extraction_rows: tuple[ClaimExtractionTrustProfile, ...]
    autonomy_rows: tuple[AutonomyTrustProfile, ...]
    attention_gap_rows: tuple[AttentionGapProfile, ...]


@dataclass(frozen=True, slots=True)
class _EditPatternRecord:
    issue_number: int
    recorded_at: datetime
    task_type: str | None
    author_override_magnitude: float | None


def build_trust_profile_snapshot(
    program_id: str,
    *,
    window_issues: int = 10,
    action_filter: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> TrustProfileSnapshot:
    normalized_action_filter = normalize_action_filter(action_filter)
    generated_at = _ensure_utc(as_of or datetime.now(timezone.utc))
    return TrustProfileSnapshot(
        program_id=program_id,
        generated_at=generated_at,
        window_issues=window_issues,
        action_filter=normalized_action_filter,
        editorial_rows=_build_editorial_rows(
            program_id,
            window_issues=window_issues,
            action_filter=normalized_action_filter,
            programs_root=programs_root,
        ),
        claim_extraction_rows=_build_claim_extraction_rows(
            program_id,
            window_issues=window_issues,
            action_filter=normalized_action_filter,
            programs_root=programs_root,
        ),
        autonomy_rows=_build_autonomy_rows(
            program_id,
            action_filter=normalized_action_filter,
            programs_root=programs_root,
        ),
        attention_gap_rows=_build_attention_gap_rows(program_id, programs_root=programs_root),
    )


def filter_autonomy_audit_records_for_action(
    records: tuple[AutonomyAuditRecord, ...],
    *,
    action_filter: str | None,
) -> tuple[AutonomyAuditRecord, ...]:
    normalized_action_filter = normalize_action_filter(action_filter)
    if normalized_action_filter is None:
        return tuple(records)
    return tuple(
        record
        for record in records
        if normalize_action_filter(_autonomy_action_key(record)) == normalized_action_filter
    )


def build_editorial_label(task_type: str) -> str:
    labels = {
        "exec_summary": "Exec summary generation",
        "workstream_blurb": "Blurb generation",
    }
    return labels.get(task_type, humanize_trust_key(task_type))


def compute_editorial_trust_level(*, sample_count: int, calibration_score: float) -> str:
    if sample_count < 3:
        return "bootstrap"
    if calibration_score >= 0.85:
        return "L2"
    return "L1"


def compute_autonomy_trust_level(*, sample_count: int, acceptance_rate: float) -> str:
    if sample_count < 3:
        return "bootstrap"
    if sample_count >= 10 and acceptance_rate >= 0.9:
        return "L3"
    if acceptance_rate >= 0.7:
        return "L2"
    return "L1"


def compute_claim_extraction_trust_level(*, sample_count: int, agreement_rate: float, average_difference_count: float) -> str:
    if sample_count < 3:
        return "bootstrap"
    if sample_count >= 10 and agreement_rate >= 0.85 and average_difference_count <= 2.0:
        return "L2"
    return "L1"


def humanize_trust_key(value: str) -> str:
    normalized = value.replace("_", " ").strip()
    if not normalized:
        return normalized
    return normalized[:1].upper() + normalized[1:]


def normalize_action_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _build_editorial_rows(
    program_id: str,
    *,
    window_issues: int,
    action_filter: str | None,
    programs_root: Path,
) -> tuple[EditorialTrustProfile, ...]:
    recent_patterns = _patterns_within_issue_window(
        _read_edit_patterns(program_id, programs_root=programs_root),
        window_issues=window_issues,
    )
    grouped: dict[str, list[_EditPatternRecord]] = {}
    for pattern in recent_patterns:
        if pattern.task_type is None or pattern.author_override_magnitude is None:
            continue
        if action_filter is not None and normalize_action_filter(pattern.task_type) != action_filter:
            continue
        grouped.setdefault(pattern.task_type, []).append(pattern)

    rows: list[EditorialTrustProfile] = []
    for task_type, patterns in sorted(grouped.items()):
        average_override = round(
            sum(pattern.author_override_magnitude for pattern in patterns if pattern.author_override_magnitude is not None) / len(patterns),
            4,
        )
        calibration_score = round(max(0.0, 1.0 - average_override), 4)
        rows.append(
            EditorialTrustProfile(
                task_type=task_type,
                label=build_editorial_label(task_type),
                sample_count=len(patterns),
                average_override_magnitude=average_override,
                calibration_score=calibration_score,
                trust_level=compute_editorial_trust_level(
                    sample_count=len(patterns),
                    calibration_score=calibration_score,
                ),
            )
        )
    rows.sort(key=lambda row: row.label)
    return tuple(rows)


def _build_autonomy_rows(
    program_id: str,
    *,
    action_filter: str | None,
    programs_root: Path,
) -> tuple[AutonomyTrustProfile, ...]:
    grouped: dict[str, list[AutonomyAuditRecord]] = {}
    records = filter_autonomy_audit_records_for_action(
        load_autonomy_audit_records(program_id, programs_root=programs_root),
        action_filter=action_filter,
    )
    for record in records:
        action_type = _autonomy_action_key(record)
        grouped.setdefault(action_type, []).append(record)

    rows: list[AutonomyTrustProfile] = []
    for action_type, grouped_records in sorted(grouped.items()):
        ordered = sorted(grouped_records, key=lambda record: record.applied_at)
        accepted_count = sum(1 for record in ordered if record.accepted)
        sample_count = len(ordered)
        acceptance_rate = accepted_count / sample_count
        latest = ordered[-1]
        rows.append(
            AutonomyTrustProfile(
                action_type=action_type,
                label=_build_autonomy_label(action_type),
                latest_level=latest.level,
                sample_count=sample_count,
                accepted_count=accepted_count,
                acceptance_rate=round(acceptance_rate, 4),
                trust_level=compute_autonomy_trust_level(
                    sample_count=sample_count,
                    acceptance_rate=acceptance_rate,
                ),
                last_applied_at=_ensure_utc(latest.applied_at),
            )
        )
    rows.sort(key=lambda row: (row.label, row.last_applied_at))
    return tuple(rows)


def _autonomy_action_key(record: AutonomyAuditRecord) -> str:
    base_action_type = (record.action_type or record.policy_rule or "unknown").strip() or "unknown"
    if base_action_type == "decision_ask_nudge" and _has_incident_evidence_ref(record.evidence_refs):
        return "decision_ask_nudge_incident"
    return base_action_type


def _build_autonomy_label(action_type: str) -> str:
    if action_type == "decision_ask_nudge_incident":
        return "Incident-linked decision ask nudge"
    return humanize_trust_key(action_type)


def _has_incident_evidence_ref(evidence_refs: tuple[str, ...]) -> bool:
    return any(re.fullmatch(r"ICM:\d+", ref.strip(), flags=re.IGNORECASE) for ref in evidence_refs)


def _build_claim_extraction_rows(
    program_id: str,
    *,
    window_issues: int,
    action_filter: str | None,
    programs_root: Path,
) -> tuple[ClaimExtractionTrustProfile, ...]:
    normalized_filter = normalize_action_filter(action_filter)
    if normalized_filter not in {None, "claim_extraction"}:
        return ()

    summary = summarize_claim_extraction_calibration(
        program_id,
        recent_cycles=window_issues,
        programs_root=programs_root,
    )
    if summary.recent_sample_count == 0 or summary.last_recorded_at is None:
        return ()

    return (
        ClaimExtractionTrustProfile(
            action_type="claim_extraction",
            label="Claim extraction",
            sample_count=summary.recent_sample_count,
            calibration_sample_count=summary.calibration_sample_count,
            agreement_rate=summary.recent_agreement_rate,
            average_difference_count=summary.recent_average_difference_count,
            trust_level=compute_claim_extraction_trust_level(
                sample_count=summary.recent_sample_count,
                agreement_rate=summary.recent_agreement_rate,
                average_difference_count=summary.recent_average_difference_count,
            ),
            last_recorded_at=_ensure_utc(summary.last_recorded_at),
        ),
    )


def _build_attention_gap_rows(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[AttentionGapProfile, ...]:
    salience = load_author_salience(program_id, programs_root=programs_root)
    modifier = load_forecast_calibration_modifier(program_id, programs_root=programs_root)
    if salience is None or modifier is None:
        return ()

    weights_by_workstream = {
        workstream.workstream_id: workstream.attention_weight
        for workstream in salience.workstreams
    }
    rows = [
        AttentionGapProfile(
            workstream_id=workstream_id,
            slip_modifier=round(slip_modifier, 2),
            attention_weight=round(weights_by_workstream[workstream_id], 2),
            bridge_summary=(
                "Forecast slip pressure is high while editorial attention remains low. "
                "Review salience weighting and calibration inputs together."
            ),
        )
        for workstream_id, slip_modifier in modifier.workstream_modifiers.items()
        if workstream_id in weights_by_workstream
        and slip_modifier > 0.15
        and weights_by_workstream[workstream_id] < 0.4
    ]
    rows.sort(key=lambda row: (-row.slip_modifier, row.attention_weight, row.workstream_id))
    return tuple(rows)


def _read_edit_patterns(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[_EditPatternRecord, ...]:
    path = programs_root / program_id / "journal" / "edit_patterns.jsonl"
    if not path.exists():
        return ()

    patterns: list[_EditPatternRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = parse_jsonl_line(line)
            if not isinstance(payload, dict):
                raise ValueError(f"Edit pattern journal at {path} must contain JSON objects.")
            patterns.append(
                _EditPatternRecord(
                    issue_number=int(payload["issue_number"]),
                    recorded_at=_ensure_utc(datetime.fromisoformat(str(payload["recorded_at"]))),
                    task_type=(str(payload["task_type"]) if payload.get("task_type") is not None else None),
                    author_override_magnitude=(
                        float(payload["author_override_magnitude"])
                        if payload.get("author_override_magnitude") is not None
                        else None
                    ),
                )
            )
    patterns.sort(key=lambda pattern: (pattern.recorded_at, pattern.issue_number))
    return tuple(patterns)


def _patterns_within_issue_window(
    patterns: tuple[_EditPatternRecord, ...],
    *,
    window_issues: int,
) -> tuple[_EditPatternRecord, ...]:
    if window_issues <= 0:
        return ()
    ordered = sorted(patterns, key=lambda pattern: (pattern.issue_number, pattern.recorded_at), reverse=True)
    recent_issue_numbers: list[int] = []
    for pattern in ordered:
        if pattern.issue_number not in recent_issue_numbers:
            recent_issue_numbers.append(pattern.issue_number)
        if len(recent_issue_numbers) >= window_issues:
            break
    allowed_issue_numbers = set(recent_issue_numbers)
    return tuple(pattern for pattern in ordered if pattern.issue_number in allowed_issue_numbers)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
