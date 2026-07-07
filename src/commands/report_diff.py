from __future__ import annotations

import difflib
from datetime import datetime, timezone
from typing import Any

from src.core.jinja_filters import risk_label
from src.core.models import RiskLevel
from src.core.overrides_store import OverridesDocument


def build_report_diff_summary(
    *,
    previous_dry_run_state: dict[str, Any] | None,
    current_issue_number: int,
    current_override_snapshot: dict[str, dict[str, dict[str, Any]]],
    current_top_3_now: tuple[str, ...],
    current_exec_summary_text: str,
    ado_lines: tuple[str, ...],
) -> str:
    if previous_dry_run_state is None:
        return f"VERTEX DIFF - No previous dry-run found for Issue {current_issue_number:03d}.\n"

    header = "VERTEX DIFF - Changes since last dry-run"
    previous_generated_at = _parse_datetime(previous_dry_run_state.get("generated_at"))
    if previous_generated_at is not None:
        header += f" ({previous_generated_at.strftime('%b %d %H:%M UTC')})"

    override_lines, unchanged_dimension_count, total_dimension_count = _build_override_diff_lines(
        previous_dry_run_state=previous_dry_run_state,
        current_override_snapshot=current_override_snapshot,
    )
    exec_summary_lines = _build_exec_summary_diff_lines(
        previous_dry_run_state=previous_dry_run_state,
        current_exec_summary_text=current_exec_summary_text,
    )

    lines = [header, "", "SCORECARD OVERRIDES APPLIED:"]
    lines.extend(f"  {line}" for line in override_lines)
    lines.extend(["", "ADO DATA CHANGES:"])
    lines.extend(f"  {line}" for line in ado_lines)
    lines.extend(["", "EXEC SUMMARY:"])
    lines.extend(f"  {line}" for line in exec_summary_lines)

    no_changes_summary = _build_report_diff_no_change_summary(
        previous_dry_run_state=previous_dry_run_state,
        current_top_3_now=current_top_3_now,
        unchanged_dimension_count=unchanged_dimension_count,
        total_dimension_count=total_dimension_count,
    )
    if no_changes_summary is not None:
        lines.extend(["", no_changes_summary])

    return "\n".join(lines) + "\n"


def build_override_snapshot(
    overrides_document: OverridesDocument,
) -> dict[str, dict[str, dict[str, Any]]]:
    snapshot: dict[str, dict[str, dict[str, Any]]] = {}
    for scorecard in overrides_document.scorecards:
        dimensions: dict[str, dict[str, Any]] = {}
        for dimension in scorecard.dimensions:
            dimensions[dimension.name] = {
                "risk": (dimension.risk.value if dimension.risk is not None else None),
                "label": _normalize_optional_string(dimension.label),
                "summary": _normalize_optional_string(dimension.summary),
                "eta": dimension.eta.isoformat() if dimension.eta is not None else None,
                "hide_details": True if dimension.hide_details else None,
            }
        snapshot[scorecard.name] = dimensions
    return snapshot


def _build_override_diff_lines(
    *,
    previous_dry_run_state: dict[str, Any],
    current_override_snapshot: dict[str, dict[str, dict[str, Any]]],
) -> tuple[tuple[str, ...], int, int]:
    previous_override_snapshot = previous_dry_run_state.get("override_snapshot")
    if not isinstance(previous_override_snapshot, dict):
        return ("No previous override snapshot is available.",), 0, 0

    lines: list[str] = []
    unchanged_dimension_count = 0
    total_dimension_count = 0
    scorecard_names = sorted(set(previous_override_snapshot) | set(current_override_snapshot))
    for scorecard_name in scorecard_names:
        previous_dimensions = previous_override_snapshot.get(scorecard_name, {})
        current_dimensions = current_override_snapshot.get(scorecard_name, {})
        dimension_names = sorted(set(previous_dimensions) | set(current_dimensions))
        for dimension_name in dimension_names:
            total_dimension_count += 1
            previous_payload = previous_dimensions.get(dimension_name, {}) if isinstance(previous_dimensions, dict) else {}
            current_payload = current_dimensions.get(dimension_name, {}) if isinstance(current_dimensions, dict) else {}
            previous_risk = previous_payload.get("risk") if isinstance(previous_payload, dict) else None
            current_risk = current_payload.get("risk") if isinstance(current_payload, dict) else None
            previous_label = _normalize_optional_string(previous_payload.get("label") if isinstance(previous_payload, dict) else None)
            current_label = _normalize_optional_string(current_payload.get("label") if isinstance(current_payload, dict) else None)
            previous_summary = _normalize_optional_string(previous_payload.get("summary") if isinstance(previous_payload, dict) else None)
            current_summary = _normalize_optional_string(current_payload.get("summary") if isinstance(current_payload, dict) else None)
            previous_eta = _normalize_optional_string(previous_payload.get("eta") if isinstance(previous_payload, dict) else None)
            current_eta = _normalize_optional_string(current_payload.get("eta") if isinstance(current_payload, dict) else None)
            previous_hide_details = bool(previous_payload.get("hide_details")) if isinstance(previous_payload, dict) else False
            current_hide_details = bool(current_payload.get("hide_details")) if isinstance(current_payload, dict) else False
            changed = False

            if previous_risk != current_risk:
                lines.append(
                    f"{dimension_name}: {_diff_risk_label(previous_risk)} -> {_diff_risk_label(current_risk)} (author set in overrides.yaml)"
                )
                changed = True
            if previous_label != current_label:
                if current_label is None:
                    lines.append(f"{dimension_name}: label override cleared")
                elif previous_label is None:
                    lines.append(f"{dimension_name}: label override set to {current_label}")
                else:
                    lines.append(f"{dimension_name}: label override {previous_label} -> {current_label}")
                changed = True
            if previous_hide_details != current_hide_details:
                lines.append(f"{dimension_name}: detail section {'hidden' if current_hide_details else 'shown'}")
                changed = True
            if previous_eta != current_eta:
                if current_eta is None:
                    lines.append(f"{dimension_name}: ETA override cleared")
                elif previous_eta is None:
                    lines.append(f"{dimension_name}: ETA override set to {current_eta}")
                else:
                    lines.append(f"{dimension_name}: ETA override {previous_eta} -> {current_eta}")
                changed = True
            if previous_summary != current_summary:
                if current_summary is None:
                    lines.append(f"{dimension_name}: summary cleared")
                elif previous_summary is None:
                    lines.append(f"{dimension_name}: summary added")
                else:
                    lines.append(f"{dimension_name}: summary updated")
                changed = True
            if not changed:
                unchanged_dimension_count += 1

    if not lines:
        lines.append("No override changes detected.")
    return tuple(lines), unchanged_dimension_count, total_dimension_count


def _build_exec_summary_diff_lines(
    *,
    previous_dry_run_state: dict[str, Any],
    current_exec_summary_text: str,
) -> tuple[str, ...]:
    if "exec_summary_text" not in previous_dry_run_state:
        return ("No previous exec summary snapshot is available.",)

    previous_exec_summary_text = str(previous_dry_run_state.get("exec_summary_text", ""))
    if previous_exec_summary_text == current_exec_summary_text:
        return ("No exec summary changes detected.",)

    diff_lines = [
        line
        for line in difflib.ndiff(previous_exec_summary_text.splitlines(), current_exec_summary_text.splitlines())
        if line.startswith("- ") or line.startswith("+ ")
    ]
    if not diff_lines:
        return ("No exec summary changes detected.",)
    return tuple(diff_lines)


def _build_report_diff_no_change_summary(
    *,
    previous_dry_run_state: dict[str, Any],
    current_top_3_now: tuple[str, ...],
    unchanged_dimension_count: int,
    total_dimension_count: int,
) -> str | None:
    no_change_parts: list[str] = []
    previous_top_3_now = previous_dry_run_state.get("top_3_now")
    if isinstance(previous_top_3_now, list) and tuple(str(item) for item in previous_top_3_now) == current_top_3_now:
        no_change_parts.append("Top 3 Now")
    if total_dimension_count and unchanged_dimension_count:
        no_change_parts.append(f"{unchanged_dimension_count} of {total_dimension_count} scorecard dimensions")
    if not no_change_parts:
        return None
    return "No changes to: " + ", ".join(no_change_parts)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _diff_risk_label(value: str | None) -> str:
    if value in (None, ""):
        return risk_label(RiskLevel.UNKNOWN)
    try:
        return risk_label(RiskLevel.from_string(value))
    except ValueError:
        return str(value)


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None