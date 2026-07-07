from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import owner_pack
from src.core.calibration_engine import CalibrationRollup
from src.core.action_tracker import append_action
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, write_proposal_manifest
from src.core.assumption_tracker import save_assumptions
from src.core.claim_tracker import append_decision_ask
from src.core.journal import append_review_decision, append_signal
from src.core.models import RiskLevel, WorkItem
from src.core.feedback.calibration_router import refresh_forecast_calibration
from src.core.owner_pack import OwnerPackMilestoneContribution
from src.core.models_v2 import ADOConfig, ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, Confidence, DecisionAsk, Milestone, MilestoneStatus, Program, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Signal, SignalReviewDecision, TrajectoryPoint, VitalityAggregate, Workstream
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteTrajectoryStore
from src.core.trajectory import backfill_trajectory_points


runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_ado_client(monkeypatch):
    """``owner_pack_command`` eagerly constructs ``ADOClient(...)`` for the default
    item loader, which resolves an Azure DevOps credential (Azure CLI in production).
    These tests mock ``_load_owner_items``, so the client is never used for real
    calls — stub its construction so the tests need no live credential. CI has neither
    an Azure CLI login nor a PAT, so without this the eager construction raises
    AuthError even though the data path is fully mocked."""

    class _StubADOClient:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

    monkeypatch.setattr(owner_pack, "ADOClient", _StubADOClient)


def test_owner_pack_cli_writes_markdown_packet(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    write_proposal_manifest(
        ADOUpdateProposal(
            id="prop-demo",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            update_type="comment",
            created_at=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc),
            entries=(
                ADOUpdateEntry(
                    work_item_id=1001,
                    action="add_comment",
                    field_or_tag="comment",
                    current_value=None,
                    proposed_value="Vertex demo_weekly issue #007",
                    reason="Cited in confirmed issue #007.",
                    revision_id=11,
                ),
            ),
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=7,
            text="Need decision on rollout timing.",
            entity_refs=("WI:1001",),
            ask_date=date(2026, 5, 13),
            owner_alias="priya",
        ),
        programs_root=programs_root,
    )
    append_action(
        "demo",
        ActionItem(
            id="action-1",
            program_id="demo",
            text="Follow up on rollout timing.",
            owner_alias="priya",
            due_date=date(2026, 5, 16),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="ws_demo",
            created_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "demo",
        1001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 16),
                state="Active",
                assigned_to="priya@example.com",
                target_date=date(2026, 6, 1),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Demo\\WS",
            ),
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        "demo",
        (
            RiskEntry(
                id="risk-1",
                program_id="demo",
                title="Rollout dependency may slip",
                description="Shared dependency is still missing a committed delivery date.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="priya",
                mitigation_plan="Escalate dependency review in next sync.",
                mitigation_due_date=date(2026, 5, 18),
                linked_workstream_ids=("ws_demo",),
                linked_work_item_ids=(1001,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=("action-1",),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 13),
                identified_in_vertex_issue=7,
                last_reviewed_date=date(2026, 5, 13),
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Shared dependency will commit by the next sync.",
                validation_method="Dependency owner confirms target date",
                validation_due=date(2026, 5, 1),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id="risk-1",
                linked_milestone_id="m1",
                owner_alias="priya",
                identified_date=date(2026, 5, 13),
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )
    for signal in (
        Signal(
            id="telemetry-analytics",
            timestamp=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1001",),
            text="Analytics snapshot for owner pack telemetry.",
            raw_ref="ado-analytics:telemetry-analytics",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 2,
                "scope_delta_count": 2,
                "open_delta_count": -1,
                "average_cycle_time_days": 5.0,
                "average_lead_time_days": 8.0,
            },
            thread_id=None,
        ),
        Signal(
            id="telemetry-sprint",
            timestamp=datetime(2026, 5, 10, 17, 15, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1001",),
            text="Sprint snapshot for owner pack telemetry.",
            raw_ref="ado-sprint:telemetry-sprint",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 50,
                "open_item_count": 1,
                "team_member_count": 3,
                "total_capacity_per_day": 24.0,
            },
            thread_id=None,
        ),
    ):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
        append_review_decision(
            "demo",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=signal.timestamp,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack.gather_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(owner_pack, "_load_owner_items", lambda client, program, as_of: ((_item(), _completed_item()), 1))
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: _milestones())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _vitality_artifacts(),
    )
    _seed_milestone_archive(programs_root)
    backfill_trajectory_points(
        "demo",
        1002,
        (
            TrajectoryPoint(
                date=date(2026, 5, 18),
                state="Resolved",
                assigned_to="priya@example.com",
                target_date=date(2026, 5, 12),
                risk_level=RiskLevel.LOW,
                area_path="One\\Demo\\WS",
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya"])

    output_path = (tmp_path / "programs" / "acme" / "publications") / "demo" / "owner_packs" / "priya.md"
    assert result.exit_code == 0
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "## Vitality Summary" in content
    assert "## Telemetry" in content
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in content
    assert "## Risk Register Entries" in content
    assert "Rollout dependency may slip" in content
    assert "## Milestone Contributions" in content
    assert "GA readiness" in content
    assert "computed at_risk" in content or "computed missed" in content
    assert "schedule: Tracking 2026-06-01 (10 days late vs target)" in content
    assert "target history 2026-05-18 -> 2026-05-22" in content
    assert "Pilot validation" in content
    assert "computed completed" in content
    assert "schedule: Completed 2026-05-18 (1 day late vs target)" in content
    assert "completion history 2026-05-16 -> 2026-05-18" in content
    assert "## Open Actions" in content
    assert "Follow up on rollout timing." in content
    assert "candidate for resolution" in content
    assert "## Open Assumptions" in content
    assert "Shared dependency will commit by the next sync." in content
    assert "overdue | due 2026-05-01 | owner priya" in content
    assert "method Dependency owner confirms target date | milestone m1 | risk risk-1" in content
    assert "## Proposed ADO Updates" in content
    assert "Need decision on rollout timing." in content
    assert str(output_path) in result.stdout


def test_owner_pack_cli_scopes_raci_accountable_workstream(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "ws_demo",
                        "name": "Demo Workstream",
                        "area_paths": ["One\\Demo\\WS"],
                        "pm_owner": "alex",
                        "eng_owner": "sam",
                        "raci": {
                            "accountable": "priya",
                            "responsible": ["alex"],
                            "consulted": [],
                            "informed": [],
                        },
                    },
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        owner_pack.gather_helpers,
        "_load_program_context",
        lambda program_id, root: (_demo_program(), (_demo_workstream(),)),
    )
    monkeypatch.setattr(
        owner_pack,
        "_load_owner_items",
        lambda client, program, as_of: ((_raci_scoped_item(),), 1),
    )
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_actions", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "load_assumptions",
        lambda program_id, programs_root: (
            Assumption(
                id="assumption-2",
                program_id="demo",
                text="Scoped item will unblock after partner sign-off.",
                validation_method=None,
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="alex",
                identified_date=date(2026, 5, 15),
                entity_refs=("WI:1002",),
            ),
        ),
    )
    monkeypatch.setattr(owner_pack, "load_open_decision_asks", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _empty_vitality_artifacts(),
    )

    result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya"])

    output_path = (tmp_path / "programs" / "acme" / "publications") / "demo" / "owner_packs" / "priya.md"
    assert result.exit_code == 0
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "WI:1002 | high | Active | target 2026-06-01 | Workstream-owned delivery" in content
    assert "## Open Assumptions" in content
    assert "Scoped item will unblock after partner sign-off." in content


def test_enrich_milestone_contributions_reads_sqlite_backed_completion_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_milestone_archive(programs_root)
    _set_program_storage_backend(programs_root, program_id="demo", storage_backend="sqlite")

    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    trajectory_store.append(
        "demo",
        1002,
        TrajectoryPoint(
            date=date(2026, 5, 18),
            state="Resolved",
            assigned_to="priya@example.com",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            area_path="One\\Demo\\WS",
        ),
    )

    enriched = owner_pack._enrich_milestone_contributions(
        (
            OwnerPackMilestoneContribution(
                milestone_id="m2",
                name="Pilot validation",
                target_date=date(2026, 5, 17),
                status="on_track",
                relation="owner",
            ),
        ),
        program_id="demo",
        milestones=_milestones(),
        items=(replace(_completed_item(), state="Resolved", fetched_at=datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)),),
        as_of=datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert enriched[0].schedule_summary == "Completed 2026-05-18 (1 day late vs target)"
    assert enriched[0].completion_history_summary == "completion history 2026-05-16 -> 2026-05-18"


def test_owner_pack_cli_supports_json_and_csv(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"

    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack.gather_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(owner_pack, "_load_owner_items", lambda client, program, as_of: ((_item(),), 1))
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_actions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_assumptions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_open_decision_asks", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _vitality_artifacts(),
    )

    json_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "demo"
    assert payload["owner_alias"] == "priya"
    assert payload["ado_calls"] == 3
    assert payload["counts"]["items"] == 1
    assert payload["counts"]["telemetry"] == 0
    assert payload["items"][0]["id"] == 1001
    assert payload["telemetry_summary"] is None
    assert payload["output_path"].endswith("priya.md")

    csv_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "entry_type,program_id,owner_alias,output_path,ado_calls,ref_id,item_id,title_or_text,status,risk_level,target_date,due_date,detail"
    assert any("summary,demo,priya," in line for line in lines[1:])
    assert any(",1001,1001,High risk delivery,Active,high,2026-06-01," in line for line in lines[1:])


def test_owner_pack_cli_surfaces_owner_calibration_profile(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"

    refresh_forecast_calibration(
        "demo",
        workstream_rows=(),
        dri_rows=(CalibrationRollup(subject_id="priya", met=3, contradicted=1, stale=1),),
        as_of=datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack.gather_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(owner_pack, "_load_owner_items", lambda client, program, as_of: ((_item(),), 1))
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_actions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_assumptions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_open_decision_asks", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _empty_vitality_artifacts(),
    )

    result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya"])

    assert result.exit_code == 0
    content = ((tmp_path / "programs" / "acme" / "publications") / "demo" / "owner_packs" / "priya.md").read_text(encoding="utf-8")
    assert "## Calibration Profile" in content
    assert "priya: 60% met (3/5) | 1 contradicted | 1 stale | slip modifier +0.10" in content

    json_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["counts"]["calibration"] == 1
    assert payload["calibration_summary"]["owner_alias"] == "priya"
    assert payload["calibration_summary"]["sample_size"] == 5

    csv_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "csv"])

    assert csv_result.exit_code == 0
    assert "calibration,demo,priya," in csv_result.stdout
    assert "accuracy=60%, sample=5, slip_modifier=+0.10" in csv_result.stdout


def test_owner_pack_cli_surfaces_telemetry_in_json_and_csv(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"

    analytics_signal = Signal(
        id="telemetry-analytics",
        timestamp=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="demo",
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        text="Analytics snapshot for owner pack telemetry.",
        raw_ref="ado-analytics:telemetry-analytics",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="telemetry-sprint",
        timestamp=datetime(2026, 5, 10, 17, 15, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="demo",
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        text="Sprint snapshot for owner pack telemetry.",
        raw_ref="ado-sprint:telemetry-sprint",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "team_member_count": 3,
            "total_capacity_per_day": 24.0,
        },
        thread_id=None,
    )
    for signal in (analytics_signal, sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
        append_review_decision(
            "demo",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=signal.timestamp,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )

    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack.gather_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(owner_pack, "_load_owner_items", lambda client, program, as_of: ((_item(),), 1))
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_actions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_assumptions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_open_decision_asks", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _vitality_artifacts(),
    )

    json_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["counts"]["telemetry"] == 1
    assert payload["telemetry_summary"] == "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members"

    csv_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert any(
        line.startswith('telemetry,demo,priya,')
        and '"analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members"' in line
        for line in lines[1:]
    )


def _demo_program() -> Program:
    return Program(
        schema_version="2.0",
        id="demo",
        name="Demo Program",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo\\WS",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )


def test_owner_pack_surfaces_snapshot_backed_three_sprint_history_summaries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    analytics_signal = Signal(
        id="telemetry-analytics",
        timestamp=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="demo",
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        text="Analytics snapshot for owner pack telemetry.",
        raw_ref="ado-analytics:telemetry-analytics",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="telemetry-sprint",
        timestamp=datetime(2026, 5, 10, 17, 15, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="demo",
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        text="Sprint snapshot for owner pack telemetry.",
        raw_ref="ado-sprint:telemetry-sprint",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "three_iteration_average_completion_per_business_day": 1.0,
            "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
            "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "three_iteration_throughput_trend_direction": "up",
            "three_iteration_throughput_trend_delta_per_business_day": 1.0,
            "three_iteration_average_open_item_count": 1,
            "three_iteration_open_item_count_history": (2, 1, 0),
            "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "three_iteration_open_trend_direction": "down",
            "three_iteration_open_trend_delta_count": -2,
        },
        thread_id=None,
    )
    for signal in (analytics_signal, sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
        append_review_decision(
            "demo",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=signal.timestamp,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )

    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack.gather_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(owner_pack, "_load_owner_items", lambda client, program, as_of: ((_item(),), 1))
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_actions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_assumptions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_open_decision_asks", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _vitality_artifacts(),
    )

    json_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["counts"]["telemetry"] == 1
    assert payload["telemetry_summary"] == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
    )

    csv_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert any(
        line.startswith('telemetry,demo,priya,')
        and '"analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"' in line
        for line in lines[1:]
    )


def test_owner_pack_surfaces_snapshot_backed_broader_historical_sprint_window(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    analytics_signal = Signal(
        id="telemetry-analytics",
        timestamp=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="demo",
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        text="Analytics snapshot for owner pack telemetry.",
        raw_ref="ado-analytics:telemetry-analytics",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="telemetry-sprint",
        timestamp=datetime(2026, 5, 10, 17, 15, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="demo",
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        text="Sprint snapshot for owner pack telemetry.",
        raw_ref="ado-sprint:telemetry-sprint",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "historical_iteration_window_count": 4,
            "historical_completion_per_business_day_history": (1.0, 0.5, 1.0, 1.5),
            "historical_completed_history_series": ((0, 1, 2), (0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "historical_throughput_trend_direction": None,
            "historical_throughput_trend_delta_per_business_day": None,
            "historical_open_item_count_history": (1, 2, 1, 0),
            "historical_open_history_series": ((3, 2, 1), (3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "historical_open_trend_direction": None,
            "historical_open_trend_delta_count": None,
        },
        thread_id=None,
    )
    for signal in (analytics_signal, sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
        append_review_decision(
            "demo",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=signal.timestamp,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )

    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(owner_pack.gather_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(owner_pack, "_load_owner_items", lambda client, program, as_of: ((_item(),), 1))
    monkeypatch.setattr(owner_pack, "load_milestones", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_actions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_assumptions", lambda program_id, programs_root: ())
    monkeypatch.setattr(owner_pack, "load_open_decision_asks", lambda program_id, programs_root: ())
    monkeypatch.setattr(
        owner_pack,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root, owner_alias: _vitality_artifacts(),
    )

    json_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["counts"]["telemetry"] == 1
    assert payload["telemetry_summary"] == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"
    )

    csv_result = runner.invoke(app, ["owner-pack", "--program", "demo", "--owner", "priya", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert any(
        line.startswith('telemetry,demo,priya,')
        and '"analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"' in line
        for line in lines[1:]
    )


def _item() -> WorkItem:
    return WorkItem(
        id=1001,
        type="Feature",
        title="High risk delivery",
        state="Active",
        assigned_to="priya@example.com",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\WS",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
    )


def _completed_item() -> WorkItem:
    return replace(
        _item(),
        id=1002,
        title="Pilot validation",
        state="Resolved",
        target_date=date(2026, 5, 12),
        risk_level=RiskLevel.LOW,
    )


def _vitality_artifacts():
    class _Artifacts:
        items = (_item(),)
        scored_items = ()
        owner_aggregates = (
            VitalityAggregate(
                scope_id="priya",
                scope_type="owner",
                total_items=1,
                fresh_items=0,
                avg_richness=42.0,
                total_leakage=1,
                workiq_signal_count=2,
                leakage_ratio=0.5,
                composite_score=38,
                trend=None,
            ),
        )
        workstream_aggregates = ()
        ado_calls = 2

    return _Artifacts()


def _milestones() -> tuple[Milestone, ...]:
    return (
        Milestone(
            id="m1",
            program_id="demo",
            name="GA readiness",
            target_date=date(2026, 5, 22),
            owner_alias="priya",
            status=MilestoneStatus.AT_RISK,
            exit_criteria=("Ship dependency cleared",),
            linked_workstream_ids=("ws_demo",),
            linked_work_item_ids=(1001,),
            notes="Coordinate with the shared dependency owner.",
        ),
        Milestone(
            id="m2",
            program_id="demo",
            name="Pilot validation",
            target_date=date(2026, 5, 17),
            owner_alias="priya",
            status=MilestoneStatus.ON_TRACK,
            exit_criteria=("Pilot rollout validated",),
            linked_workstream_ids=("ws_demo",),
            linked_work_item_ids=(1002,),
            notes=None,
        ),
    )


def _seed_milestone_archive(programs_root: Path) -> None:
    archive_dir = programs_root / "demo" / "archive" / "demo_weekly"
    manifests_dir = archive_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / "issue_001.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "manifest_id": "manifest-1",
                "issue_number": 1,
                "edition": "demo_weekly",
                "started_at": "2026-05-16T18:00:00+00:00",
                "ended_at": "2026-05-16T18:00:00+00:00",
                "config_hash": "config",
                "snapshot_hash": "snapshot",
                "html_hash": "html",
                "md_hash": "md",
                "ado_calls": 1,
                "ai_calls": 0,
                "ai_cost_usd": 0.0,
                "freshness_summary": {"blocks": 0, "warns": 0, "infos": 0},
                "qg_results": {"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
                "git_sha": None,
                "metadata": {
                    "milestone_assessments": [
                        {"milestone_id": "m1", "target_date": "2026-05-18"},
                        {"milestone_id": "m2", "completion_date": "2026-05-16"},
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (archive_dir / "index.json").write_text(
        json.dumps(
            {
                "edition": "demo_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": "2026-05-16T18:00:00+00:00",
                        "kind": "confirmed",
                        "manifest_path": str(manifest_path),
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _demo_workstream() -> Workstream:
    return Workstream(
        id="ws_demo",
        name="Demo Workstream",
        area_paths=("One\\Demo\\WS",),
        pm_owner="alex",
        eng_owner="sam",
        accountable_owner="priya",
        responsible_owners=("alex",),
    )


def _raci_scoped_item() -> WorkItem:
    return WorkItem(
        id=1002,
        type="Feature",
        title="Workstream-owned delivery",
        state="Active",
        assigned_to="alex@example.com",
        assigned_to_email="alex@example.com",
        area_path="One\\Demo\\WS",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc),
    )


def _empty_vitality_artifacts():
    class _Artifacts:
        items = ()
        scored_items = ()
        owner_aggregates = ()
        workstream_aggregates = ()
        ado_calls = 0

    return _Artifacts()


def _set_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    if program_path.exists():
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(program_document, dict)
    else:
        program_path.parent.mkdir(parents=True, exist_ok=True)
        program_document = {
            "schema_version": "2.0",
            "id": program_id,
            "name": f"{program_id.title()} Program",
        }
    program_document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False, allow_unicode=False), encoding="utf-8")
