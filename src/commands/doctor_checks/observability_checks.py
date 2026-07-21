"""WS-17: doctor checks for diagnose + perf (per-channel P50/P95).

These two checks are exposed via:
  - ``vertex observability diagnose --program <program-id>`` — explain the last gather failure
  - ``vertex observability perf --program <program-id>`` — per-channel latency + SLO status

Design:
- diagnose: reads the latest ``gather_state.json`` (if any), the
  latest row in ``run_telemetry.jsonl`` (if any), and the open
  alerts; classifies any failure category seen; surfaces the operator
  next command.
- perf: aggregates per-channel P50/P95 over a recent window of
  ``run_telemetry.jsonl`` rows; marks each channel as ok/warn/fail
  against ``DEFAULT_SLO_MS`` (operator-overridable).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.alerts import AlertSeverity, read_alerts
from src.core.failure_taxonomy import (
    FailureCategory,
    classify_exception,
)
from src.core.gather_state_store import (
    GatherState,
    load_gather_state,
)
from src.core.run_telemetry import (
    build_channel_perf_summary,
    read_run_telemetry,
)


@dataclass(frozen=True, slots=True)
class DiagnoseFinding:
    """A single line item in the diagnose output."""
    severity: str        # "ok" | "warn" | "fail" | "info"
    label: str
    detail: str
    next_command: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnoseReport:
    program_id: str
    findings: tuple[DiagnoseFinding, ...]
    last_gather_at: datetime | None = None
    last_failure_category: FailureCategory | None = None
    last_failure_retryable: bool | None = None
    last_failure_detail: str | None = None
    last_failure_next_command: str | None = None
    open_alert_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "findings": [
                {"severity": f.severity, "label": f.label, "detail": f.detail, "next_command": f.next_command}
                for f in self.findings
            ],
            "last_gather_at": self.last_gather_at.isoformat() if self.last_gather_at is not None else None,
            "last_failure_category": self.last_failure_category.value if self.last_failure_category is not None else None,
            "last_failure_retryable": self.last_failure_retryable,
            "last_failure_detail": self.last_failure_detail,
            "last_failure_next_command": self.last_failure_next_command,
            "open_alert_count": self.open_alert_count,
        }


@dataclass(frozen=True, slots=True)
class PerfReport:
    program_id: str
    channel_count: int
    run_count: int
    summaries: tuple[Any, ...] = ()  # ChannelPerfSummary; avoids circular import
    slo_status_overall: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "channel_count": self.channel_count,
            "run_count": self.run_count,
            "slo_status_overall": self.slo_status_overall,
            "channels": [s.to_dict() for s in self.summaries],
        }


def build_diagnose_report(
    program_id: str,
    *,
    programs_root: Path,
    window: int = 1,
) -> DiagnoseReport:
    """Walk the latest run evidence and produce a diagnose report.

    The output is a stack-ranked list of findings — failure taxonomy
    classification up top, open alerts next, then baseline OK info."""
    findings: list[DiagnoseFinding] = []
    last_failure_category: FailureCategory | None = None
    last_failure_retryable: bool | None = None
    last_failure_detail: str | None = None
    last_failure_next_command: str | None = None
    last_gather_at: datetime | None = None

    # 1. Latest gather_state.json
    gather_state: GatherState | None = load_gather_state(program_id, programs_root=programs_root)
    if gather_state is None:
        findings.append(
            DiagnoseFinding(
                severity="warn",
                label="gather_state",
                detail="No gather_state.json found for this program. Run `vertex gather` first.",
                next_command=f"vertex gather --program {program_id}",
            )
        )
    else:
        last_gather_at = gather_state.gathered_at
        if gather_state.integration_errors > 0:
            # Classify the first error and surface it.
            first = gather_state.integration_error_details[0] if gather_state.integration_error_details else None
            if first is not None:
                classification = classify_exception(first.message)
                last_failure_category = classification.category
                last_failure_retryable = classification.retryable
                last_failure_detail = first.message
                last_failure_next_command = classification.next_command
                severity = "fail"
                findings.append(
                    DiagnoseFinding(
                        severity=severity,
                        label="last_failure",
                        detail=(
                            f"{first.source}/{first.stage}: {first.message}. "
                            f"Category: {classification.category.value}. "
                            f"{'Retryable' if classification.retryable else 'Persistent'}."
                        ),
                        next_command=classification.next_command,
                    )
                )
            else:
                findings.append(
                    DiagnoseFinding(
                        severity="warn",
                        label="last_failure",
                        detail=f"{gather_state.integration_errors} integration error(s) recorded.",
                        next_command=f"vertex observability diagnose --program {program_id}",
                    )
                )
        else:
            findings.append(
                DiagnoseFinding(
                    severity="ok",
                    label="last_gather",
                    detail=f"Last gather at {gather_state.gathered_at.isoformat()} had 0 integration errors.",
                    next_command=None,
                )
            )

    # 2. Latest run_telemetry row
    records = read_run_telemetry(program_id, programs_root=programs_root, window=window)
    if not records:
        findings.append(
            DiagnoseFinding(
                severity="info",
                label="run_telemetry",
                detail="No run_telemetry.jsonl yet (no completed runs recorded).",
                next_command=None,
            )
        )
    else:
        latest = records[-1]
        failed_channels = tuple(
            stats.channel for stats in latest.channels
            if stats.failures > 0 or stats.failure_categories
        )
        if failed_channels:
            # Use the first failure category in the failure list.
            for stats in latest.channels:
                if stats.failure_categories:
                    classification = classify_exception(stats.failure_categories[0])
                    if last_failure_category is None:
                        last_failure_category = classification.category
                        last_failure_retryable = classification.retryable
                        last_failure_next_command = classification.next_command
                    break
            findings.append(
                DiagnoseFinding(
                    severity="warn",
                    label="last_run_channels",
                    detail=(
                        f"Last run ({latest.run_id}) saw failures in: "
                        f"{', '.join(failed_channels)}. "
                        f"Category: {last_failure_category.value if last_failure_category else 'unknown'}."
                    ),
                    next_command=last_failure_next_command or f"vertex observability diagnose --program {program_id}",
                )
            )
        else:
            findings.append(
                DiagnoseFinding(
                    severity="ok",
                    label="last_run_channels",
                    detail=f"Last run ({latest.run_id}) had no channel failures. wall_time={latest.wall_time_seconds:.1f}s.",
                    next_command=None,
                )
            )

    # 3. Open alerts
    open_alerts = read_alerts(program_id, programs_root=programs_root, include_resolved=False)
    if open_alerts:
        crit = sum(1 for a in open_alerts if a.severity == AlertSeverity.CRITICAL)
        errs = sum(1 for a in open_alerts if a.severity == AlertSeverity.ERROR)
        findings.append(
            DiagnoseFinding(
                severity="fail" if crit else ("warn" if errs else "info"),
                label="open_alerts",
                detail=f"{len(open_alerts)} unresolved alert(s) (critical={crit}, error={errs}).",
                next_command=f"vertex alerts show --program {program_id}",
            )
        )
    else:
        findings.append(
            DiagnoseFinding(
                severity="ok",
                label="open_alerts",
                detail="0 unresolved alerts.",
                next_command=None,
            )
        )

    return DiagnoseReport(
        program_id=program_id,
        findings=tuple(findings),
        last_gather_at=last_gather_at,
        last_failure_category=last_failure_category,
        last_failure_retryable=last_failure_retryable,
        last_failure_detail=last_failure_detail,
        last_failure_next_command=last_failure_next_command,
        open_alert_count=len(open_alerts),
    )


def build_perf_report(
    program_id: str,
    *,
    programs_root: Path,
    window: int = 10,
    slo_overrides: dict[str, int] | None = None,
) -> PerfReport:
    """Aggregate per-channel P50/P95 from recent run_telemetry rows.

    slo_status_overall is "ok" only if every channel is "ok"; "fail"
    if any channel is "fail"; "warn" otherwise; "unknown" if no data."""
    summaries = build_channel_perf_summary(
        program_id,
        programs_root=programs_root,
        window=window,
        slo_overrides=slo_overrides,
    )
    records = read_run_telemetry(program_id, programs_root=programs_root, window=window)
    if not summaries:
        slo_status = "unknown"
    elif any(s.slo_status == "fail" for s in summaries):
        slo_status = "fail"
    elif any(s.slo_status == "warn" for s in summaries):
        slo_status = "warn"
    elif all(s.slo_status == "ok" for s in summaries):
        slo_status = "ok"
    else:
        slo_status = "unknown"
    return PerfReport(
        program_id=program_id,
        channel_count=len(summaries),
        run_count=len(records),
        summaries=summaries,
        slo_status_overall=slo_status,
    )
