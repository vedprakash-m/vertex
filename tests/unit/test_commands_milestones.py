from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import milestones as milestones_module
from src.core.exceptions import ConfigError
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, Milestone, MilestoneStatus, Program, TrajectoryPoint, Workstream
from src.core.milestone_engine import load_milestones, save_milestones
from src.core.sqlite_stores import SQLiteTrajectoryStore
from src.core.trajectory import backfill_trajectory_points


runner = CliRunner()


def test_milestones_list_cli(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones.PROGRAMS_ROOT", programs_root)
    _seed_milestones(programs_root)

    result = runner.invoke(app, ["milestones", "list", "--program", "demo"])

    assert result.exit_code == 0
    assert "MILESTONES — demo (2)" in result.stdout
    assert "M1 - Code Complete" in result.stdout


def test_milestones_list_cli_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones.PROGRAMS_ROOT", programs_root)
    _seed_milestones(programs_root)

    result = runner.invoke(app, ["milestones", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["id"] == "m1"
    assert rows[0]["name"] == "M1 - Code Complete"


def test_milestones_assess_cli_renders_health(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.milestones._load_program_context", _fake_program_loader)
    monkeypatch.setattr("src.commands.milestones._load_live_items", _fake_item_loader)
    _seed_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    backfill_trajectory_points(
        "demo",
        1001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="demo",
                target_date=date(2026, 5, 8),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Demo\\Core",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 5),
                state="Active",
                assigned_to="demo",
                target_date=date(2026, 5, 14),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Demo\\Core",
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["milestones", "assess", "--program", "demo", "--as-of", "2026-05-08"])

    assert result.exit_code == 0
    assert "MILESTONE HEALTH — demo (2)" in result.stdout
    assert "computed at_risk" in result.stdout
    assert "Schedule: Tracking 2026-05-14 (4 days late vs target)" in result.stdout
    assert "Completion date: 2026-05-08" in result.stdout
    assert "Completion history 2026-05-07 -> 2026-05-08" in result.stdout
    assert "Target history 2026-05-08 -> 2026-05-10" in result.stdout
    assert "Blockers:" in result.stdout


def test_milestones_assess_cli_json_includes_schedule_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.milestones._load_program_context", _fake_program_loader)
    monkeypatch.setattr("src.commands.milestones._load_live_items", _fake_item_loader)
    _seed_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    backfill_trajectory_points(
        "demo",
        1001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="demo",
                target_date=date(2026, 5, 8),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Demo\\Core",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 5),
                state="Active",
                assigned_to="demo",
                target_date=date(2026, 5, 14),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Demo\\Core",
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        ["milestones", "assess", "--program", "demo", "--as-of", "2026-05-08", "--format", "json"],
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["rows"][0]["schedule_summary"] == "Tracking 2026-05-14 (4 days late vs target)"
    assert payload["rows"][0]["target_date_history"] == ["2026-05-08", "2026-05-10"]
    assert payload["rows"][1]["completion_date"] == "2026-05-08"
    assert payload["rows"][1]["completion_date_history"] == ["2026-05-07", "2026-05-08"]


def test_milestones_assess_cli_csv_includes_schedule_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.milestones._load_program_context", _fake_program_loader)
    monkeypatch.setattr("src.commands.milestones._load_live_items", _fake_item_loader)
    _seed_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    backfill_trajectory_points(
        "demo",
        1001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="demo",
                target_date=date(2026, 5, 8),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Demo\\Core",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 5),
                state="Active",
                assigned_to="demo",
                target_date=date(2026, 5, 14),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Demo\\Core",
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        ["milestones", "assess", "--program", "demo", "--as-of", "2026-05-08", "--format", "csv"],
    )
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["schedule_summary"] == "Tracking 2026-05-14 (4 days late vs target)"
    assert rows[0]["target_date_history"] == "2026-05-08|2026-05-10"
    assert rows[1]["completion_date"] == "2026-05-08"


def test_build_milestone_assessment_report_ignores_unrelated_action_loader_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones._load_program_context", _fake_program_loader)
    monkeypatch.setattr("src.commands.milestones._load_live_items", _fake_item_loader)
    _seed_milestones(programs_root)

    def _boom(*args, **kwargs):
        raise ConfigError("actions broken")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    report = milestones_module.build_milestone_assessment_report(
        "demo",
        as_of=datetime(2026, 5, 8, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert report.program_id == "demo"
    assert len(report.rows) == 2


def test_build_milestone_assessment_report_reads_milestones_from_program_facts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    sentinel_snapshot = object()

    monkeypatch.setattr("src.commands.milestones._load_program_context", _fake_program_loader)
    monkeypatch.setattr("src.commands.milestones._load_live_items", _fake_item_loader)
    monkeypatch.setattr(
        milestones_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.update(
            {"program_id": program_id, "programs_root": programs_root, "fact_types": fact_types}
        )
        or sentinel_snapshot,
    )
    monkeypatch.setattr(
        milestones_module,
        "project_milestones",
        lambda snapshot: (
            Milestone(
                id="m1",
                program_id="demo",
                name="Launch readiness",
                target_date=date(2026, 5, 10),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Dry run complete",),
                linked_workstream_ids=("core",),
                linked_work_item_ids=(1001,),
            ),
        )
        if snapshot is sentinel_snapshot
        else (),
    )
    monkeypatch.setattr(
        milestones_module,
        "project_dependencies",
        lambda snapshot: () if snapshot is sentinel_snapshot else (),
    )

    report = milestones_module.build_milestone_assessment_report(
        "demo",
        as_of=datetime(2026, 5, 8, tzinfo=timezone.utc),
        programs_root=tmp_path / "programs",
    )

    assert captured == {
        "program_id": "demo",
        "programs_root": tmp_path / "programs",
        "fact_types": ("milestone.entry", "dependency.link"),
    }
    assert report.program_id == "demo"
    assert len(report.rows) == 1


def test_load_trajectory_map_reads_sqlite_backed_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_milestones(programs_root)
    _set_program_storage_backend(programs_root, program_id="demo", storage_backend="sqlite")

    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    trajectory_store.append(
        "demo",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="demo",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Demo\\Core",
        ),
    )
    trajectory_store.append(
        "demo",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 5),
            state="Active",
            assigned_to="demo",
            target_date=date(2026, 5, 14),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Demo\\Core",
        ),
    )

    trajectory_map = milestones_module._load_trajectory_map(
        program_id="demo",
        milestones=load_milestones("demo", programs_root=programs_root),
        programs_root=programs_root,
    )

    assert [point.target_date for point in trajectory_map[1001]] == [date(2026, 5, 8), date(2026, 5, 14)]


def test_milestones_update_persists_changes_and_backup(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.milestones.PROGRAMS_ROOT", programs_root)
    _seed_milestones(programs_root)

    result = runner.invoke(
        app,
        [
            "milestones",
            "update",
            "--program",
            "demo",
            "--id",
            "m1",
            "--status",
            "completed",
            "--notes",
            "Completed on schedule.",
        ],
    )

    milestones = load_milestones("demo", programs_root=programs_root)
    milestones_path = programs_root / "demo" / "milestones.yaml"

    assert result.exit_code == 0
    assert milestones[0].status == MilestoneStatus.COMPLETED
    assert milestones[0].notes == "Completed on schedule."
    assert milestones_path.with_suffix(".yaml.bak").exists()


def _seed_milestones(programs_root: Path) -> None:
    save_milestones(
        "demo",
        (
            Milestone(
                id="m1",
                program_id="demo",
                name="M1 - Code Complete",
                target_date=date(2026, 5, 10),
                owner_alias="demo",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Code complete",),
                linked_workstream_ids=("core",),
                linked_work_item_ids=(1001,),
            ),
            Milestone(
                id="m2",
                program_id="demo",
                name="M2 - Readiness Review",
                target_date=date(2026, 5, 20),
                owner_alias="demo",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Review completed",),
                linked_workstream_ids=("core",),
                linked_work_item_ids=(1002,),
            ),
        ),
        programs_root=programs_root,
    )


def _fake_program_loader(program_id: str, programs_root: Path) -> tuple[Program, tuple[Workstream, ...]]:
    del programs_root
    return (
        Program(
            schema_version="1.0",
            id=program_id,
            name="Demo Program",
            ado=ADOConfig(
                organization="demo-org",
                project="demo-project",
                area_paths=("One\\Demo\\Core",),
                work_item_types=("Feature",),
                excluded_states=("Closed",),
                date_window_days=30,
            ),
        ),
        (
            Workstream(
                id="core",
                name="Core",
                area_paths=("One\\Demo\\Core",),
                pm_owner="demo",
                eng_owner="demo",
            ),
        ),
    )


def _fake_item_loader(program: Program, workstreams: tuple[Workstream, ...], as_of: datetime) -> tuple[tuple[WorkItem, ...], int]:
    del program, workstreams
    return (
        (
            WorkItem(
                id=1001,
                type="Feature",
                title="Core code path",
                state="Active",
                assigned_to="Demo Owner",
                assigned_to_email="demo@example.com",
                area_path="One\\Demo\\Core",
                iteration_path="Sprint 1",
                target_date=date(2026, 5, 14),
                risk_level=RiskLevel.HIGH,
                tags=["demo"],
                custom_fields={},
                revisions=[],
                comments=[],
                fetched_at=as_of,
            ),
            WorkItem(
                id=1002,
                type="Feature",
                title="Readiness review",
                state="Resolved",
                assigned_to="Demo Owner",
                assigned_to_email="demo@example.com",
                area_path="One\\Demo\\Core",
                iteration_path="Sprint 1",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.LOW,
                tags=["demo"],
                custom_fields={},
                revisions=[],
                comments=[],
                fetched_at=as_of,
            ),
        ),
        0,
    )


def _seed_milestone_archive(programs_root: Path) -> None:
    archive_dir = programs_root / "demo" / "archive" / "demo_weekly"
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_one = archive_dir / "issue_001.manifest.json"
    manifest_two = archive_dir / "issue_002.manifest.json"
    manifest_one.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "manifest_id": "manifest-001",
                "issue_number": 1,
                "edition": "demo_weekly",
                "started_at": "2026-05-01T08:00:00+00:00",
                "ended_at": "2026-05-01T09:00:00+00:00",
                "config_hash": "config",
                "snapshot_hash": "snapshot",
                "html_hash": "html",
                "md_hash": "md",
                "ado_calls": 0,
                "ai_calls": 0,
                "ai_cost_usd": 0.0,
                "freshness_summary": {},
                "qg_results": {},
                "git_sha": None,
                "metadata": {
                    "milestone_assessments": [
                        {
                            "milestone_id": "m1",
                            "target_date": "2026-05-08",
                        },
                        {
                            "milestone_id": "m2",
                            "completion_date": "2026-05-07",
                        }
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest_two.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "manifest_id": "manifest-002",
                "issue_number": 2,
                "edition": "demo_weekly",
                "started_at": "2026-05-05T08:00:00+00:00",
                "ended_at": "2026-05-05T09:00:00+00:00",
                "config_hash": "config",
                "snapshot_hash": "snapshot",
                "html_hash": "html",
                "md_hash": "md",
                "ado_calls": 0,
                "ai_calls": 0,
                "ai_cost_usd": 0.0,
                "freshness_summary": {},
                "qg_results": {},
                "git_sha": None,
                "metadata": {
                    "milestone_assessments": [
                        {
                            "milestone_id": "m1",
                            "target_date": "2026-05-10",
                        },
                        {
                            "milestone_id": "m2",
                            "completion_date": "2026-05-08",
                        }
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
                        "generated_at": "2026-05-01T09:00:00+00:00",
                        "kind": "confirmed",
                        "manifest_path": str(manifest_one),
                    },
                    {
                        "issue_number": 2,
                        "generated_at": "2026-05-05T09:00:00+00:00",
                        "kind": "confirmed",
                        "manifest_path": str(manifest_two),
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


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