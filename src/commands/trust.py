from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
from pathlib import Path
import re
from typing import Any

import typer

from src.core.analytics_store import AutonomyAuditRecord, load_autonomy_audit_records
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback.trust_profile_store import TrustProfileSnapshot as TrustReport
from src.core.feedback.trust_profile_store import build_trust_profile_snapshot, compute_autonomy_trust_level, filter_autonomy_audit_records_for_action, normalize_action_filter


@dataclass(frozen=True, slots=True)
class TrustSliceRow:
    slice_key: str
    sample_count: int
    accepted_count: int
    acceptance_rate: float
    trust_level: str


@dataclass(frozen=True, slots=True)
class TrustSliceReport:
    program_id: str
    generated_at: datetime
    slice_name: str
    window: str | None
    action_filter: str | None
    rows: tuple[TrustSliceRow, ...]


@dataclass(frozen=True, slots=True)
class TrustGraduationMetrics:
    program_id: str
    window_issues: int
    rewrite_rate: float | None
    qg_pass_rate: float | None
    proposal_acceptance_rate: float | None
    source_coverage: dict[str, int] = field(default_factory=dict)
    commitment_leakage_rate: float | None = None
    source_diversity: int = 0  # distinct source families contributing in the window (F14 criterion: ≥3 required)


_WINDOW_PATTERN = re.compile(r"^(\d+)w$", re.IGNORECASE)
_ISSUE_REF_PATTERN = re.compile(r"^issue:(\d+)$", re.IGNORECASE)


def trust_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    window_issues: int | None = typer.Option(None, "--window-issues", help="Rolling issue window used for editorial trust calibration."),
    action: str | None = typer.Option(None, "--action", help="Optional action or task type filter, for example decision_ask_escalation."),
    slice: str | None = typer.Option(None, "--slice", help="Optional autonomy slice: workstream, dri, or time."),
    window: str | None = typer.Option(None, "--window", help="Optional time-slice window in weeks, for example 8w."),
    graduation_metrics: bool = typer.Option(False, "--graduation-metrics", help="Emit bridge-graduation metrics over the latest confirmed issue window."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    if window_issues is not None and window_issues <= 0:
        raise typer.BadParameter("--window-issues must be greater than zero.")
    if graduation_metrics:
        if any(value is not None for value in (action, slice, window)):
            raise typer.BadParameter("--graduation-metrics cannot be combined with --action, --slice, or --window.")
        metrics = build_trust_graduation_metrics(
            program.strip(),
            window_issues=window_issues or 5,
            programs_root=PROGRAMS_ROOT,
        )
        typer.echo(json.dumps(_graduation_metrics_to_payload(metrics), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if slice is None:
        if window is not None:
            raise typer.BadParameter("--window requires --slice time.")
        report: TrustReport | TrustSliceReport = build_trust_report(
            program.strip(),
            window_issues=window_issues or 10,
            action_filter=action,
            programs_root=PROGRAMS_ROOT,
        )
    else:
        report = build_trust_slice_report(
            program.strip(),
            slice_name=slice,
            window=window,
            action_filter=action,
            programs_root=PROGRAMS_ROOT,
        )
    if format == "human":
        if isinstance(report, TrustSliceReport):
            typer.echo(render_trust_slice_report(report))
        else:
            typer.echo(render_trust_report(report))
        raise typer.Exit(code=0)
    if format == "json":
        typer.echo(json.dumps(_report_to_payload(report), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    raise typer.BadParameter("--format must be 'human' or 'json'.")


def build_trust_report(
    program_id: str,
    *,
    window_issues: int = 10,
    action_filter: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    ) -> TrustReport:
    return build_trust_profile_snapshot(
        program_id,
        window_issues=window_issues,
        action_filter=action_filter,
        programs_root=programs_root,
        as_of=_ensure_utc(as_of or _utc_now()),
    )


def build_trust_graduation_metrics(
    program_id: str,
    *,
    window_issues: int = 5,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrustGraduationMetrics:
    confirmed_issues = _load_recent_confirmed_issues(
        program_id,
        window_issues=window_issues,
        programs_root=programs_root,
    )
    issue_numbers = tuple(issue["issue_number"] for issue in confirmed_issues)
    issue_number_set = set(issue_numbers)
    confirmed_window_start = min((issue["confirmed_at"] for issue in confirmed_issues), default=None)
    confirmed_window_end = max((issue["confirmed_at"] for issue in confirmed_issues), default=None)

    rewrite_rate = _compute_rewrite_rate(
        program_id,
        issue_numbers=issue_numbers,
        programs_root=programs_root,
    )
    qg_pass_rate = _compute_qg_pass_rate(confirmed_issues)
    proposal_acceptance_rate = _compute_proposal_acceptance_rate(
        program_id,
        issue_number_set=issue_number_set,
        confirmed_window_start=confirmed_window_start,
        confirmed_window_end=confirmed_window_end,
        programs_root=programs_root,
    )

    # Calculate source coverage from signals in the window
    source_coverage: dict[str, int] = {}
    distinct_source_families: set[str] = set()
    if confirmed_window_start is not None and confirmed_window_end is not None:
        try:
            from src.core.store_factory import build_signal_store_for_program_id
            from src.core.signal_ranking import signal_source_family
            signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
            signals_in_window = signal_store.read(program_id, start=confirmed_window_start, end=confirmed_window_end)
            for sig in signals_in_window:
                source_coverage[sig.source] = source_coverage.get(sig.source, 0) + 1
                distinct_source_families.add(signal_source_family(sig.source))
        except Exception:
            pass

    source_diversity = len(distinct_source_families)

    # Calculate commitment leakage rate from the latest confirmed issue's snapshot
    commitment_leakage_rate = None
    if confirmed_issues:
        latest_issue = confirmed_issues[-1]
        manifest_path = Path(latest_issue["path"])
        snapshot_path = manifest_path.parent.parent / "snapshots" / f"issue_{latest_issue['issue_number']:03d}.snapshot.json"
        if snapshot_path.exists():
            try:
                from src.core.snapshot_store import read_snapshot
                from src.core.models import WorkItem
                from src.core.leakage_detector import load_approved_workiq_signals, detect_leakage
                from src.core.store_factory import build_trajectory_store_for_program_id

                snapshot = read_snapshot(snapshot_path)
                work_items = []
                for item in snapshot.items:
                    work_items.append(
                        WorkItem(
                            id=item.id,
                            type=item.type,
                            title=item.title,
                            state=item.state,
                            assigned_to=item.assigned_to,
                            assigned_to_email=None,
                            area_path=item.area_path,
                            iteration_path="",
                            target_date=item.target_date,
                            risk_level=item.risk_level,
                            tags=item.tags,
                            custom_fields={},
                        )
                    )

                trajectory_store = build_trajectory_store_for_program_id(
                    program_id,
                    programs_root=programs_root,
                )

                approved_signals = load_approved_workiq_signals(
                    program_id,
                    as_of=latest_issue["confirmed_at"],
                    window_days=window_issues * 7,
                    programs_root=programs_root,
                )

                leakage_report = detect_leakage(
                    items=tuple(work_items),
                    signals=approved_signals,
                    trajectory_loader=lambda work_item_id: trajectory_store.read(
                        program_id,
                        work_item_id,
                    ),
                )

                total_signals = sum(leakage_report.signal_counts_by_item.values())
                if total_signals > 0:
                    commitment_leakage_rate = round(len(leakage_report.events) / total_signals, 4)
                else:
                    commitment_leakage_rate = 0.0
            except Exception:
                pass

    return TrustGraduationMetrics(
        program_id=program_id,
        window_issues=window_issues,
        rewrite_rate=rewrite_rate,
        qg_pass_rate=qg_pass_rate,
        proposal_acceptance_rate=proposal_acceptance_rate,
        source_coverage=source_coverage,
        commitment_leakage_rate=commitment_leakage_rate,
        source_diversity=source_diversity,
    )


def render_trust_report(report: TrustReport) -> str:
    lines = [
        f"Trust Calibration - {report.program_id}",
        f"Generated: {report.generated_at.isoformat()}",
        f"Window: last {report.window_issues} issue(s)",
    ]
    if report.action_filter is not None:
        lines.append(f"Action filter: {report.action_filter}")
    lines.extend(["", "Editorial", "---------"])
    if report.editorial_rows:
        for row in report.editorial_rows:
            lines.append(
                f"- {row.label}: override={row.average_override_magnitude:.4f} | calibration={row.calibration_score:.4f} | samples={row.sample_count} | trust={row.trust_level}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "Claim extraction", "----------------"])
    if report.claim_extraction_rows:
        for ce_row in report.claim_extraction_rows:
            lines.append(
                f"- {ce_row.label}: agreement={ce_row.agreement_rate:.4f} | avg_difference={ce_row.average_difference_count:.2f} | samples={ce_row.sample_count} | calibration_samples={ce_row.calibration_sample_count} | trust={ce_row.trust_level} | last={ce_row.last_recorded_at.isoformat()}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "Autonomy", "--------"])
    if report.autonomy_rows:
        for auto_row in report.autonomy_rows:
            percent = round(auto_row.acceptance_rate * 100)
            lines.append(
                f"- {auto_row.label}: accepted={auto_row.accepted_count}/{auto_row.sample_count} ({percent:d}%) | level={auto_row.latest_level} | trust={auto_row.trust_level} | last={auto_row.last_applied_at.isoformat()}"
            )
    else:
        lines.append("- None")

    lines.extend(["", "Salience-calibration bridge", "--------------------------"])
    if report.attention_gap_rows:
        for ag_row in report.attention_gap_rows:
            lines.append(
                f"- {ag_row.workstream_id}: slip_modifier=+{ag_row.slip_modifier:.2f} | attention_weight={ag_row.attention_weight:.2f} | {ag_row.bridge_summary}"
            )
    else:
        lines.append("- None")
    return "\n".join(lines)


def build_trust_slice_report(
    program_id: str,
    *,
    slice_name: str,
    window: str | None = None,
    action_filter: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> TrustSliceReport:
    normalized_slice = slice_name.strip().lower()
    if normalized_slice not in {"workstream", "dri", "time"}:
        raise typer.BadParameter("--slice must be one of: workstream, dri, time.")
    if normalized_slice != "time" and window is not None:
        raise typer.BadParameter("--window is only supported with --slice time.")

    generated_at = _ensure_utc(as_of or _utc_now())
    records = filter_autonomy_audit_records_for_action(
        load_autonomy_audit_records(program_id, programs_root=programs_root),
        action_filter=action_filter,
    )
    if normalized_slice == "time":
        rows = _build_time_slice_rows(records, window=window, as_of=generated_at)
    elif normalized_slice == "workstream":
        rows = _build_workstream_slice_rows(records)
    else:
        rows = _build_dri_slice_rows(records)

    return TrustSliceReport(
        program_id=program_id,
        generated_at=generated_at,
        slice_name=normalized_slice,
        window=window,
        action_filter=normalize_action_filter(action_filter),
        rows=rows,
    )


def render_trust_slice_report(report: TrustSliceReport) -> str:
    lines = [
        f"Trust Calibration - {report.program_id}",
        f"Generated: {report.generated_at.isoformat()}",
        f"Slice: {report.slice_name}",
    ]
    if report.window is not None:
        lines.append(f"Window: {report.window}")
    if report.action_filter is not None:
        lines.append(f"Action filter: {report.action_filter}")
    lines.extend(["", "Autonomy", "--------"])
    if report.rows:
        for row in report.rows:
            percent = round(row.acceptance_rate * 100)
            lines.append(
                f"- {row.slice_key}: accepted={row.accepted_count}/{row.sample_count} ({percent:d}%) | trust={row.trust_level}"
            )
    else:
        lines.append("- None")
    return "\n".join(lines)


def _report_to_payload(report: TrustReport | TrustSliceReport) -> dict[str, object]:
    if isinstance(report, TrustSliceReport):
        return {
            "program_id": report.program_id,
            "generated_at": report.generated_at.isoformat(),
            "slice": report.slice_name,
            "window": report.window,
            "action_filter": report.action_filter,
            "rows": [
                {
                    "slice_key": row.slice_key,
                    "sample_count": row.sample_count,
                    "accepted_count": row.accepted_count,
                    "acceptance_rate": row.acceptance_rate,
                    "trust_level": row.trust_level,
                }
                for row in report.rows
            ],
        }
    return {
        "program_id": report.program_id,
        "generated_at": report.generated_at.isoformat(),
        "window_issues": report.window_issues,
        "action_filter": report.action_filter,
        "editorial": [
            {
                "task_type": row.task_type,
                "label": row.label,
                "sample_count": row.sample_count,
                "average_override_magnitude": row.average_override_magnitude,
                "calibration_score": row.calibration_score,
                "trust_level": row.trust_level,
            }
            for row in report.editorial_rows
        ],
        "claim_extraction": [
            {
                "action_type": row.action_type,
                "label": row.label,
                "sample_count": row.sample_count,
                "calibration_sample_count": row.calibration_sample_count,
                "agreement_rate": row.agreement_rate,
                "average_difference_count": row.average_difference_count,
                "trust_level": row.trust_level,
                "last_recorded_at": row.last_recorded_at.isoformat(),
            }
            for row in report.claim_extraction_rows
        ],
        "autonomy": [
            {
                "action_type": row.action_type,
                "label": row.label,
                "latest_level": row.latest_level,
                "sample_count": row.sample_count,
                "accepted_count": row.accepted_count,
                "acceptance_rate": row.acceptance_rate,
                "trust_level": row.trust_level,
                "last_applied_at": row.last_applied_at.isoformat(),
            }
            for row in report.autonomy_rows
        ],
        "attention_gaps": [
            {
                "workstream_id": row.workstream_id,
                "slip_modifier": row.slip_modifier,
                "attention_weight": row.attention_weight,
                "bridge_summary": row.bridge_summary,
            }
            for row in report.attention_gap_rows
        ],
    }


def _graduation_metrics_to_payload(report: TrustGraduationMetrics) -> dict[str, object]:
    return {
        "program_id": report.program_id,
        "window_issues": report.window_issues,
        "rewrite_rate": report.rewrite_rate,
        "qg_pass_rate": report.qg_pass_rate,
        "proposal_acceptance_rate": report.proposal_acceptance_rate,
        "source_coverage": report.source_coverage,
        "commitment_leakage_rate": report.commitment_leakage_rate,
        "source_diversity": report.source_diversity,
        "source_diversity_met": report.source_diversity >= 3,  # F14 graduation criterion
    }


def _build_workstream_slice_rows(records: tuple[AutonomyAuditRecord, ...]) -> tuple[TrustSliceRow, ...]:
    grouped: dict[str, list[AutonomyAuditRecord]] = {}
    for record in records:
        workstream_ids = {
            ref.split(":", 1)[1]
            for ref in record.evidence_refs
            if ref.startswith("workstream:") and ref.split(":", 1)[1]
        }
        for workstream_id in workstream_ids:
            grouped.setdefault(workstream_id, []).append(record)
    return _rows_from_grouped_records(grouped)


def _build_dri_slice_rows(records: tuple[AutonomyAuditRecord, ...]) -> tuple[TrustSliceRow, ...]:
    grouped: dict[str, list[AutonomyAuditRecord]] = {}
    for record in records:
        if record.subject_alias is None:
            continue
        normalized_alias = record.subject_alias.strip().lower()
        if not normalized_alias:
            continue
        grouped.setdefault(normalized_alias, []).append(record)
    return _rows_from_grouped_records(grouped)


def _build_time_slice_rows(
    records: tuple[AutonomyAuditRecord, ...],
    *,
    window: str | None,
    as_of: datetime,
) -> tuple[TrustSliceRow, ...]:
    filtered = list(records)
    if window is not None:
        match = _WINDOW_PATTERN.match(window.strip())
        if match is None:
            raise typer.BadParameter("--window must use the format <N>w, for example 8w.")
        weeks = int(match.group(1))
        if weeks <= 0:
            raise typer.BadParameter("--window must be greater than zero.")
        cutoff = as_of - timedelta(weeks=weeks)
        filtered = [record for record in filtered if _ensure_utc(record.applied_at) >= cutoff]

    grouped: dict[str, list[AutonomyAuditRecord]] = {}
    for record in filtered:
        applied_at = _ensure_utc(record.applied_at)
        iso_year, iso_week, _ = applied_at.isocalendar()
        grouped.setdefault(f"{iso_year}-W{iso_week:02d}", []).append(record)
    return _rows_from_grouped_records(grouped)


def _rows_from_grouped_records(
    grouped: dict[str, list[AutonomyAuditRecord]],
) -> tuple[TrustSliceRow, ...]:
    rows: list[TrustSliceRow] = []
    for slice_key, records in grouped.items():
        sample_count = len(records)
        accepted_count = sum(1 for record in records if record.accepted)
        acceptance_rate = accepted_count / sample_count
        rows.append(
            TrustSliceRow(
                slice_key=slice_key,
                sample_count=sample_count,
                accepted_count=accepted_count,
                acceptance_rate=round(acceptance_rate, 4),
                trust_level=compute_autonomy_trust_level(
                    sample_count=sample_count,
                    acceptance_rate=acceptance_rate,
                ),
            )
        )
    rows.sort(key=lambda row: row.slice_key)
    return tuple(rows)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_recent_confirmed_issues(
    program_id: str,
    *,
    window_issues: int,
    programs_root: Path,
) -> tuple[dict[str, Any], ...]:
    if window_issues <= 0:
        return ()
    manifests_root = programs_root / program_id / "archive"
    if not manifests_root.exists():
        return ()

    issues_by_number: dict[int, dict[str, Any]] = {}
    for path in manifests_root.glob("*/manifests/issue_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_issue_number = payload.get("issue_number")
        raw_confirmed_at = payload.get("ended_at") or payload.get("confirmed_at")
        if isinstance(raw_issue_number, bool) or not isinstance(raw_issue_number, int):
            continue
        if not isinstance(raw_confirmed_at, str):
            continue
        try:
            confirmed_at = _ensure_utc(datetime.fromisoformat(raw_confirmed_at))
        except ValueError:
            continue

        candidate = {
            "issue_number": raw_issue_number,
            "confirmed_at": confirmed_at,
            "qg_results": payload.get("qg_results"),
            "path": path,
        }
        existing = issues_by_number.get(raw_issue_number)
        if existing is None or confirmed_at >= existing["confirmed_at"]:
            issues_by_number[raw_issue_number] = candidate

    ordered = sorted(
        issues_by_number.values(),
        key=lambda issue: (issue["issue_number"], issue["confirmed_at"]),
    )
    return tuple(ordered[-window_issues:])


def _compute_rewrite_rate(
    program_id: str,
    *,
    issue_numbers: tuple[int, ...],
    programs_root: Path,
) -> float | None:
    if not issue_numbers:
        return None
    issue_number_set = set(issue_numbers)
    path = programs_root / program_id / "journal" / "edit_patterns.jsonl"
    if not path.exists():
        return None

    override_values: list[float] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = parse_jsonl_line(line)
            if not isinstance(payload, dict):
                continue
            raw_issue_number = payload.get("issue_number")
            if isinstance(raw_issue_number, bool) or not isinstance(raw_issue_number, int):
                continue
            if raw_issue_number not in issue_number_set:
                continue
            raw_override = payload.get("author_override_magnitude")
            if raw_override is None:
                continue
            override_values.append(float(raw_override))
    if not override_values:
        return None
    return round(sum(override_values) / len(override_values), 4)


def _compute_qg_pass_rate(confirmed_issues: tuple[dict[str, Any], ...]) -> float | None:
    passed = 0
    total = 0
    for issue in confirmed_issues:
        raw_qg_results = issue.get("qg_results")
        if not isinstance(raw_qg_results, dict):
            continue
        for value in raw_qg_results.values():
            if isinstance(value, bool):
                total += 1
                if value:
                    passed += 1
    if total == 0:
        return None
    return round(passed / total, 4)


def _compute_proposal_acceptance_rate(
    program_id: str,
    *,
    issue_number_set: set[int],
    confirmed_window_start: datetime | None,
    confirmed_window_end: datetime | None,
    programs_root: Path,
) -> float | None:
    records = load_autonomy_audit_records(program_id, programs_root=programs_root)
    scoped_records = tuple(
        record
        for record in records
        if _record_matches_confirmed_window(
            record,
            issue_number_set=issue_number_set,
            confirmed_window_start=confirmed_window_start,
            confirmed_window_end=confirmed_window_end,
        )
    )
    if not scoped_records:
        return None
    accepted = sum(1 for record in scoped_records if record.accepted)
    return round(accepted / len(scoped_records), 4)


def _record_matches_confirmed_window(
    record: AutonomyAuditRecord,
    *,
    issue_number_set: set[int],
    confirmed_window_start: datetime | None,
    confirmed_window_end: datetime | None,
) -> bool:
    scoped_issue_numbers = _extract_issue_numbers(record.evidence_refs)
    if scoped_issue_numbers:
        return any(issue_number in issue_number_set for issue_number in scoped_issue_numbers)
    applied_at = _ensure_utc(record.applied_at)
    if confirmed_window_start is None or confirmed_window_end is None:
        return False
    return confirmed_window_start <= applied_at <= confirmed_window_end


def _extract_issue_numbers(evidence_refs: tuple[str, ...]) -> tuple[int, ...]:
    issue_numbers: list[int] = []
    for ref in evidence_refs:
        match = _ISSUE_REF_PATTERN.fullmatch(ref.strip())
        if match is None:
            continue
        issue_numbers.append(int(match.group(1)))
    return tuple(issue_numbers)


# ---------------------------------------------------------------------------
# WI-3.1: vertex trust bootstrap
# ---------------------------------------------------------------------------


def trust_bootstrap_command(
    program: str = typer.Option(..., "--program", help="Program ID."),
    granted_by: str = typer.Option(..., "--granted-by", help="Operator identity granting the bootstrap."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be applied without writing."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Apply cold-start trust grants from trust_policy.yaml to a program.

    Writes trust.bootstrap_grant facts for each provenance class defined in
    vertex/policies/trust_policy.yaml. Idempotent — skips existing grants.
    Never synthesises reviews; human-reviewed flow is never blocked.
    """
    from src.core.source_trust import apply_bootstrap_grants, load_trust_policy

    policy = load_trust_policy()
    grants = apply_bootstrap_grants(
        program.strip(),
        granted_by=granted_by.strip(),
        programs_root=PROGRAMS_ROOT,
        policy=policy,
        dry_run=dry_run,
    )
    if format == "json":
        payload = [
            {
                "source": g.source,
                "signal_class": g.signal_class,
                "grant_score": g.grant_score,
                "granted_by": g.granted_by,
                "granted_at": g.granted_at,
                "rationale": g.rationale,
                "dry_run": dry_run,
            }
            for g in grants
        ]
        typer.echo(json.dumps(payload, indent=2))
    else:
        prefix = "[DRY RUN] " if dry_run else ""
        for g in grants:
            typer.echo(f"{prefix}bootstrap grant: source={g.source!r} score={g.grant_score:.2f} by={g.granted_by!r}")
        if not grants:
            typer.echo("No bootstrap grants defined in trust_policy.yaml.")
    raise typer.Exit(code=0)
