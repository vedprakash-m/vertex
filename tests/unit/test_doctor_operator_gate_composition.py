from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.doctor_checks.operator_gate_composition import run_operator_gates_doctor


def test_run_operator_gates_doctor_summarizes_blocking_and_warning_gate_labels(tmp_path: Path) -> None:
    report = run_operator_gates_doctor(
        edition_name="demo_weekly",
        reports_root=tmp_path / "reports",
        archive_root=tmp_path / "archive",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        ado_probe=None,
        kusto_probe=None,
        metric_binding_probe=None,
        metric_definitions=None,
        reality_db_root=None,
        now=None,
        resolve_edition_fn=lambda edition_name, **kwargs: SimpleNamespace(
            program=SimpleNamespace(id="demo", m365=SimpleNamespace(enabled=True)),
        ),
        run_auth_doctor=lambda **kwargs: DoctorReport("demo_weekly", (DoctorCheck("Kusto Access", "ok", "ok"),)),
        run_channel_doctor=lambda **kwargs: DoctorReport("demo_weekly", (DoctorCheck("Channel:transcript", "warn", "warn"), DoctorCheck("Source Health", "warn", "warn"))),
        run_metric_binding_doctor=lambda **kwargs: DoctorReport("demo_weekly", (DoctorCheck("Metric Bindings", "ok", "ok"), DoctorCheck("Metric Rollout", "ok", "ok"))),
        run_checkpoint_doctor=lambda **kwargs: DoctorReport("demo_weekly", (DoctorCheck("Checkpoint Inventory", "ok", "ok"), DoctorCheck("Checkpoint Coverage", "warn", "warn"))),
        build_m365_registry_review_metadata=lambda program_id, **kwargs: {"pending": 1},
        load_gather_state_fn=lambda program_id, **kwargs: SimpleNamespace(m365_discovery={"active": True}),
        agency_bridge_factory=lambda: SimpleNamespace(probe=lambda: SimpleNamespace()),
        operator_gate_m365_ids_check=lambda **kwargs: DoctorCheck("Gate:M365 IDs", "fail", "fix ids"),
        operator_gate_transcript_health_check=lambda **kwargs: DoctorCheck("Gate:Transcript Health", "warn", "fix transcripts"),
        operator_gate_kusto_validation_check=lambda **kwargs: DoctorCheck("Gate:Kusto Validation", "ok", "ok"),
        operator_gate_checkpoint_creation_check=lambda **kwargs: DoctorCheck("Gate:Checkpoint Creation", "ok", "ok"),
        operator_gate_rollback_drill_check=lambda **kwargs: DoctorCheck("Gate:Rollback Drill", "warn", "drill pending"),
    )

    assert report.checks[0].label == "Operator Gates"
    assert report.checks[0].status == "fail"
    assert report.checks[0].metadata == {
        "blocking_gate_labels": ["Gate:M365 IDs"],
        "warning_gate_labels": ["Gate:Transcript Health", "Gate:Rollback Drill"],
        "program_id": "demo",
        "edition": "demo_weekly",
    }
