from __future__ import annotations

from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck
from src.core.trusted_baseline_store import load_trusted_baseline


def operator_gate_transcript_health_check(
    *,
    edition_name: str,
    transcript_check: DoctorCheck | None,
    source_health_check: DoctorCheck | None,
) -> DoctorCheck:
    commands = [
        f"vertex doctor --channels --edition {edition_name}",
        "vertex registry set-id --program <program> --artifact-id <meeting-artifact> --series-id <id>",
    ]
    transcript_blocked = transcript_check is not None and transcript_check.status != "ok"
    source_health_detail = source_health_check.detail if source_health_check is not None and "vertex/transcript:transcript=auth_failed" in source_health_check.detail else None
    detail_parts: list[str] = []
    if transcript_check is not None:
        detail_parts.append(transcript_check.detail)
    if source_health_detail is not None:
        detail_parts.append(source_health_detail)
    if not detail_parts:
        detail_parts.append("Transcript source health is currently clear.")
    action_category = None
    if transcript_blocked:
        combined_detail = " ".join(part.lower() for part in detail_parts)
        if "auth_failed" in combined_detail:
            action_category = "auth-admin-required"
        elif "missing series_id" in combined_detail or "missing thread_id" in combined_detail:
            action_category = "operator-seed-required"
        else:
            action_category = "pm-decision-required"
    detail = " ".join(detail_parts)
    if action_category is not None:
        detail = f"{detail} Action category: {action_category}."
    return DoctorCheck(
        "Gate:Transcript Health",
        "fail" if transcript_blocked else "ok",
        detail,
        metadata={
            "owner": ["pm", "operator"],
            "action_category": action_category,
            "commands": commands,
            "evidence_to_gather": [
                "Confirmed meeting series IDs for the transcript-backed meetings",
                "At least one recent transcribed occurrence after ID seeding",
            ],
            "decisions": [
                "Decide whether each meeting should remain transcript-required or be formally deferred/waived.",
            ],
            "llm_support": "Can summarize transcript-derived workstream updates and verify whether the meeting contains useful signal, but should not decide whether a source waiver is acceptable.",
            "transcript_check": transcript_check.detail if transcript_check is not None else None,
            "source_health_check": source_health_detail,
        },
    )


def operator_gate_kusto_validation_check(
    *,
    edition_name: str,
    kusto_access_check: DoctorCheck | None,
    kusto_validation_check: DoctorCheck | None,
    metric_bindings_check: DoctorCheck | None,
    metric_rollout_check: DoctorCheck | None,
) -> DoctorCheck:
    commands = [
        f"vertex doctor --check-auth --edition {edition_name}",
        f"vertex doctor --metric-bindings --edition {edition_name}",
        "vertex admin metric validate --program <program> --all",
    ]
    relevant_checks = [check for check in (kusto_access_check, kusto_validation_check, metric_bindings_check, metric_rollout_check) if check is not None]
    blocking = any(check.status != "ok" for check in relevant_checks)
    action_category = None
    if blocking:
        if kusto_access_check is not None and kusto_access_check.status != "ok":
            action_category = "auth-admin-required"
        elif any(check is not None and check.status != "ok" for check in (metric_bindings_check, metric_rollout_check)):
            action_category = "pm-decision-required"
        else:
            action_category = "config-mismatch"
    detail = " ".join(check.detail for check in relevant_checks) if relevant_checks else "Kusto is not configured for this program."
    if action_category is not None:
        detail = f"{detail} Action category: {action_category}."
    return DoctorCheck(
        "Gate:Kusto Validation",
        "fail" if blocking else "ok",
        detail,
        metadata={
            "owner": ["operator", "data_platform_owner", "pm"],
            "action_category": action_category,
            "commands": commands,
            "evidence_to_gather": [
                "Successful Kusto probe against the required live clusters/databases",
                "Validated query set and any active metric binding/rollout records",
                "Decision on which KPI rollouts are required now vs deferred",
            ],
            "decisions": [
                "Confirm the minimum required Kusto-backed KPIs for this program and who owns each remaining validation gap.",
            ],
            "llm_support": "Can compare query definitions, summarize metric-binding gaps, and prepare a candidate validation plan; operator/PM must decide which KPIs are required for go-live.",
            "kusto_access": None if kusto_access_check is None else {"status": kusto_access_check.status, "detail": kusto_access_check.detail},
            "kusto_validation": None if kusto_validation_check is None else {"status": kusto_validation_check.status, "detail": kusto_validation_check.detail},
            "metric_bindings": None if metric_bindings_check is None else {"status": metric_bindings_check.status, "detail": metric_bindings_check.detail},
            "metric_rollout": None if metric_rollout_check is None else {"status": metric_rollout_check.status, "detail": metric_rollout_check.detail},
        },
    )


def operator_gate_checkpoint_creation_check(*, edition_name: str, checkpoint_inventory_check: DoctorCheck | None) -> DoctorCheck:
    commands = [
        f"vertex doctor --channels --edition {edition_name}",
        f"vertex confirm --edition {edition_name}",
        f"vertex doctor --checkpoints --edition {edition_name}",
    ]
    if checkpoint_inventory_check is None:
        return DoctorCheck(
            "Gate:Checkpoint Creation",
            "fail",
            "Checkpoint inventory is unavailable, so rollback readiness cannot be assessed. Action category: config-mismatch.",
            metadata={"owner": ["operator", "pm"], "commands": commands, "action_category": "config-mismatch"},
        )
    blocked = checkpoint_inventory_check.status != "ok"
    action_category = "auto-resolvable" if blocked else None
    detail = checkpoint_inventory_check.detail
    if action_category is not None:
        detail = f"{detail} Action category: {action_category}."
    return DoctorCheck(
        "Gate:Checkpoint Creation",
        "fail" if blocked else "ok",
        detail,
        metadata={
            "owner": ["operator", "pm"],
            "action_category": action_category,
            "commands": commands,
            "evidence_to_gather": [
                "Checkpoint name created by the first non-dry-run confirm",
                "Confirmed issue number and archived output for that run",
            ],
            "decisions": [
                "Choose the first live non-dry-run confirm window once source health is acceptable.",
            ],
            "llm_support": "Can review the proposed confirm output and summarize risks before the operator runs the live confirm, but the operator decides when to execute it.",
            "checkpoint_inventory": {"status": checkpoint_inventory_check.status, "detail": checkpoint_inventory_check.detail},
        },
    )


def operator_gate_rollback_drill_check(
    *,
    edition_name: str,
    checkpoint_inventory_check: DoctorCheck | None,
    checkpoint_coverage_check: DoctorCheck | None,
    editions_root: Path,
    programs_root: Path,
) -> DoctorCheck:
    commands = [
        f"vertex doctor --checkpoints --edition {edition_name}",
        f"vertex rollback --edition {edition_name} --to <checkpoint_name>",
        f"vertex doctor --consistency --edition {edition_name}",
        f"vertex admin baseline --edition {edition_name} --record-rollback-drill --checkpoint-name <checkpoint_name> --rollback-exit-code 0 --consistency-exit-code 0",
    ]
    baseline = load_trusted_baseline(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    latest_drill = None if baseline is None else next((entry for entry in reversed(baseline.history) if entry.action == "rollback_drill_passed"), None)
    blocked = (
        checkpoint_inventory_check is None
        or checkpoint_inventory_check.status != "ok"
        or checkpoint_coverage_check is None
        or checkpoint_coverage_check.status != "ok"
        or latest_drill is None
    )
    detail_parts: list[str] = []
    if checkpoint_inventory_check is not None:
        detail_parts.append(checkpoint_inventory_check.detail)
    if checkpoint_coverage_check is not None:
        detail_parts.append(checkpoint_coverage_check.detail)
    if latest_drill is None:
        detail_parts.append("No `rollback_drill_passed` entry is recorded in trusted_baseline.yaml yet.")
    else:
        detail_parts.append(f"Latest recorded rollback drill: {latest_drill.at.isoformat()} ({latest_drill.reason or 'no reason recorded'}).")
    action_category = "auto-resolvable" if blocked else None
    detail = " ".join(detail_parts)
    if action_category is not None:
        detail = f"{detail} Action category: {action_category}."
    return DoctorCheck(
        "Gate:Rollback Drill",
        "fail" if blocked else "ok",
        detail,
        metadata={
            "owner": ["operator", "pm"],
            "action_category": action_category,
            "commands": commands,
            "evidence_to_gather": [
                "Chosen checkpoint name",
                "Successful rollback and post-restore consistency exit codes",
                "Recorded trusted_baseline.yaml history entry with action=rollback_drill_passed",
            ],
            "decisions": [
                "Choose the rollback rehearsal window and the checkpoint to restore.",
            ],
            "llm_support": "Can compare pre/post doctor output, verify the baseline history entry, and summarize any diff, but the operator must execute the drill and sign off on recovery fidelity.",
            "latest_rollback_drill": (
                None
                if latest_drill is None
                else {
                    "issue": latest_drill.issue,
                    "at": latest_drill.at.isoformat(),
                    "by": latest_drill.by,
                    "reason": latest_drill.reason,
                }
            ),
        },
    )
