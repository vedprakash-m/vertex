from __future__ import annotations

from src.commands.doctor_checks.models import DoctorCheck
from src.commands.doctor_checks.operator_gate_followup_checks import (
    operator_gate_checkpoint_creation_check,
    operator_gate_kusto_validation_check,
    operator_gate_rollback_drill_check,
    operator_gate_transcript_health_check,
)


def test_operator_gate_transcript_health_classifies_auth_blocker() -> None:
    check = operator_gate_transcript_health_check(
        edition_name="demo",
        transcript_check=DoctorCheck("Channel:transcript", "warn", "missing series_id and auth_failed"),
        source_health_check=DoctorCheck("Source Health", "warn", "vertex/transcript:transcript=auth_failed"),
    )

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["action_category"] == "auth-admin-required"


def test_operator_gate_kusto_validation_prioritizes_metric_binding_gaps() -> None:
    check = operator_gate_kusto_validation_check(
        edition_name="demo",
        kusto_access_check=DoctorCheck("Kusto Access", "ok", "ok"),
        kusto_validation_check=DoctorCheck("Kusto Validation", "ok", "ok"),
        metric_bindings_check=DoctorCheck("Metric Bindings", "warn", "validated=false"),
        metric_rollout_check=None,
    )

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["action_category"] == "pm-decision-required"


def test_operator_gate_checkpoint_creation_flags_missing_inventory() -> None:
    check = operator_gate_checkpoint_creation_check(
        edition_name="demo",
        checkpoint_inventory_check=None,
    )

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["action_category"] == "config-mismatch"


def test_operator_gate_rollback_drill_requires_recorded_drill(tmp_path) -> None:
    check = operator_gate_rollback_drill_check(
        edition_name="demo",
        checkpoint_inventory_check=DoctorCheck("Checkpoint Inventory", "ok", "ok"),
        checkpoint_coverage_check=DoctorCheck("Checkpoint Coverage", "ok", "ok"),
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["action_category"] == "auto-resolvable"
    assert "rollback_drill_passed" in check.detail
