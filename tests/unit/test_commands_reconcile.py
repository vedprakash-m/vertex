from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

import cli
from src.commands.reconcile import generate_reconcile_report, render_reconcile_report
from src.core.analytics_store import load_contradiction_state
from src.core.claim_tracker import append_claim_entry
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, ClaimEntry, ForecastCalibrationModifier, Program, Signal, SignalReviewDecision, Workstream, WorkstreamSignalSources


runner = CliRunner()


def test_generate_reconcile_report_refreshes_and_persists_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_reconcile_inputs(programs_root)

    artifacts = generate_reconcile_report(
        "demo",
        refresh=True,
        dry_run=False,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        program_loader=lambda program_id, root: (_build_program(program_id), _build_workstreams()),
        item_loader=lambda program, workstreams, as_of: (_build_items(), 0),
        calibration_loader=lambda program_id, root: ForecastCalibrationModifier(
            workstream_modifiers={"deployment": 0.18},
            dri_modifiers={"priya": 0.16},
            confidence=Confidence.HIGH,
        ),
    )

    assert artifacts.cached is False
    assert len(artifacts.packets) == 1
    cached = load_contradiction_state("demo", programs_root=programs_root)
    assert len(cached) == 1
    assert cached[0].recommended_resolution is not None


def test_generate_reconcile_report_wires_dependency_loader_into_contradiction_packets(tmp_path: Path) -> None:
    """ADF-W2.10 P6: `generate_reconcile_report`'s `dependency_loader` DI
    parameter must actually flow into `build_contradiction_packets` so a
    dependency-status claim can surface a second contradiction alongside
    the existing target-date one."""
    from src.core.models_v2 import Dependency, DependencyStatus, DependencyType

    programs_root = tmp_path / "programs"
    _seed_reconcile_inputs(programs_root)
    append_claim_entry(
        ClaimEntry(
            id="claim-dep-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="The dependency on team Rome is now broken.",
            entity_refs=("DEP:dep-rome",),
            claim_date=date(2026, 5, 20),
            owner_alias=None,
            due_date=None,
            claimed_status_family="dependency",
            claimed_status_value="broken",
        ),
        programs_root=programs_root,
    )
    dependency = Dependency(
        id="dep-rome",
        from_program_id="demo",
        from_workstream_id=None,
        from_item_id=1001,
        from_milestone_id=None,
        to_program_id="demo",
        to_workstream_id=None,
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Downstream execution slips.",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias=None,
    )

    artifacts = generate_reconcile_report(
        "demo",
        refresh=True,
        dry_run=True,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        program_loader=lambda program_id, root: (_build_program(program_id), _build_workstreams()),
        item_loader=lambda program, workstreams, as_of: (_build_items(), 0),
        calibration_loader=lambda program_id, root: ForecastCalibrationModifier(
            workstream_modifiers={"deployment": 0.18},
            dri_modifiers={"priya": 0.16},
            confidence=Confidence.HIGH,
        ),
        dependency_loader=lambda program_id, root: (dependency,),
    )

    assert len(artifacts.packets) == 1
    fields = {c.field for c in artifacts.packets[0].contradictions}
    assert "dependency_status" in fields


def test_reconcile_report_matches_golden_fixture(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_reconcile_inputs(programs_root)

    artifacts = generate_reconcile_report(
        "demo",
        refresh=True,
        dry_run=True,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        program_loader=lambda program_id, root: (_build_program(program_id), _build_workstreams()),
        item_loader=lambda program, workstreams, as_of: (_build_items(), 0),
        calibration_loader=lambda program_id, root: ForecastCalibrationModifier(
            workstream_modifiers={"deployment": 0.18},
            dri_modifiers={"priya": 0.16},
            confidence=Confidence.HIGH,
        ),
    )

    rendered = render_reconcile_report(artifacts)
    fixture_path = Path(__file__).resolve().parents[1] / "golden" / "reconcile_output.txt"

    assert rendered == fixture_path.read_text(encoding="utf-8").rstrip("\n")


def test_reconcile_command_dry_run_renders_without_cache_write(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_reconcile_inputs(programs_root)

    monkeypatch.setattr("src.commands.reconcile.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.reconcile.gather_helpers._load_program_context", lambda program_id, root: (_build_program(program_id), _build_workstreams()))
    monkeypatch.setattr("src.commands.reconcile._load_live_program_items", lambda program, workstreams, as_of: (_build_items(), 0))
    monkeypatch.setattr("src.commands.reconcile.load_forecast_calibration_modifier", lambda program_id, programs_root: ForecastCalibrationModifier(
        workstream_modifiers={"deployment": 0.18},
        dri_modifiers={"priya": 0.16},
        confidence=Confidence.HIGH,
    ))
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["reconcile", "--program", "demo", "--refresh", "--dry-run"])

    assert result.exit_code == 0
    assert "Active Contradictions - demo" in result.stdout
    assert "Dry-run: skipped updating contradiction_state cache." in result.stdout
    assert load_contradiction_state("demo", programs_root=programs_root) == ()


def _seed_reconcile_inputs(programs_root: Path) -> None:
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="Expected by 2026-06-01",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 20),
            owner_alias="priya",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )
    signal = Signal(
        id="signal-1",
        timestamp=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
        source="workiq/risk",
        program_id="demo",
        workstream_id="deployment",
        entity_refs=("WI:1001",),
        text="The team is now talking about June 24 as the likely landing date.",
        raw_ref="workiq:1",
        confidence=Confidence.MEDIUM,
    )
    append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    append_review_decision(
        "demo",
        SignalReviewDecision(
            signal_id="signal-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )


def _build_program(program_id: str) -> Program:
    return Program(
        schema_version="2.0",
        id=program_id,
        name="Demo Program",
        ado=ADOConfig(
            organization="org",
            project="proj",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Closed",),
            date_window_days=30,
        ),
    )


def _build_workstreams() -> tuple[Workstream, ...]:
    return (
        Workstream(
            id="deployment",
            name="Deployment",
            area_paths=("One\\Demo\\Deployment",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )


def _build_items() -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1001,
            type="Feature",
            title="Deployment chunking",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Demo\\Deployment",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 10),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={},
        ),
    )