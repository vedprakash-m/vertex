from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.dependency_graph import load_dependencies, save_dependencies
from src.core.models_v2 import Dependency, DependencyScheduleStatus, DependencyStatus, DependencyType
from src.core.program_fact_store import load_program_facts, project_dependencies


def _write_deps(tmp_path: Path, deps: list) -> Path:
    programs_root = tmp_path / "programs"
    dep_path = programs_root / "acme" / "dependencies.yaml"
    dep_path.parent.mkdir(parents=True, exist_ok=True)
    dep_path.write_text(
        yaml.safe_dump({"schema_version": "1.0", "dependencies": deps}, sort_keys=False),
        encoding="utf-8",
    )
    return programs_root


def test_lifecycle_fields_round_trip(tmp_path: Path) -> None:
    programs_root = _write_deps(
        tmp_path,
        [
            {
                "id": "dep-lifecycle",
                "from_milestone_id": "m3-code-complete",
                "to_workstream_id": "fabrikam:buildouts",
                "dependency_type": "blocks",
                "risk_if_broken": "Delivery blocked.",
                "status": "active",
                "planned_resolution_date": "2026-08-15",
                "schedule_status": "at_risk",
            }
        ],
    )

    deps = load_dependencies("acme", programs_root=programs_root)

    assert len(deps) == 1
    dep = deps[0]
    assert dep.planned_resolution_date == date(2026, 8, 15)
    assert dep.schedule_status == DependencyScheduleStatus.AT_RISK


def test_legacy_entry_gets_none_for_lifecycle_fields(tmp_path: Path) -> None:
    programs_root = _write_deps(
        tmp_path,
        [
            {
                "id": "dep-legacy",
                "from_milestone_id": "m1-design",
                "to_workstream_id": "fabrikam:buildouts",
                "dependency_type": "informs",
                "risk_if_broken": "Fabrikam sequencing stays provisional.",
                "status": "active",
            }
        ],
    )

    deps = load_dependencies("acme", programs_root=programs_root)

    assert len(deps) == 1
    dep = deps[0]
    assert dep.planned_resolution_date is None
    assert dep.schedule_status is None


def test_unknown_schedule_status_is_silently_ignored(tmp_path: Path) -> None:
    programs_root = _write_deps(
        tmp_path,
        [
            {
                "id": "dep-future",
                "from_milestone_id": "m1-design",
                "to_workstream_id": "fabrikam:buildouts",
                "dependency_type": "blocks",
                "risk_if_broken": "Future status value.",
                "status": "active",
                "schedule_status": "unknown_future_value",
            }
        ],
    )

    deps = load_dependencies("acme", programs_root=programs_root)

    assert len(deps) == 1
    assert deps[0].schedule_status is None


def test_malformed_planned_resolution_date_is_silently_ignored(tmp_path: Path) -> None:
    programs_root = _write_deps(
        tmp_path,
        [
            {
                "id": "dep-bad-date",
                "from_milestone_id": "m1-design",
                "to_workstream_id": "fabrikam:buildouts",
                "dependency_type": "blocks",
                "risk_if_broken": "Date is malformed.",
                "status": "active",
                "planned_resolution_date": "not-a-date",
            }
        ],
    )

    deps = load_dependencies("acme", programs_root=programs_root)

    assert len(deps) == 1
    assert deps[0].planned_resolution_date is None


def test_all_schedule_status_values_parse(tmp_path: Path) -> None:
    for value, expected in [
        ("ok", DependencyScheduleStatus.OK),
        ("at_risk", DependencyScheduleStatus.AT_RISK),
        ("slipped", DependencyScheduleStatus.SLIPPED),
        ("blocked", DependencyScheduleStatus.BLOCKED),
    ]:
        programs_root = _write_deps(
            tmp_path / value,
            [
                {
                    "id": f"dep-{value}",
                    "from_milestone_id": "m1-design",
                    "to_workstream_id": "fabrikam:buildouts",
                    "dependency_type": "blocks",
                    "risk_if_broken": "Risk text.",
                    "status": "active",
                    "schedule_status": value,
                }
            ],
        )
        deps = load_dependencies("acme", programs_root=programs_root)
        assert deps[0].schedule_status == expected


def test_save_dependencies_dual_writes_current_fact_store_projection(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    dependency = Dependency(
        id="dep-1",
        from_program_id="acme",
        from_workstream_id="ws-launch",
        from_item_id=12345,
        from_milestone_id=None,
        to_program_id="fabrikam",
        to_workstream_id="ws-buildouts",
        to_item_id=67890,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Launch slips.",
        mitigation="Escalate through partner PMs.",
        status=DependencyStatus.ACTIVE,
        owner_alias="operator",
        resolution_path="Weekly sync",
        planned_resolution_date=date(2026, 8, 15),
        schedule_status=DependencyScheduleStatus.AT_RISK,
        linked_risk_ids=("risk-1",),
    )

    save_dependencies("acme", (dependency,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_dependencies(snapshot) == (dependency,)


def test_save_dependencies_closes_removed_fact_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    dependency = Dependency(
        id="dep-1",
        from_program_id="acme",
        from_workstream_id="ws-launch",
        from_item_id=12345,
        from_milestone_id=None,
        to_program_id="fabrikam",
        to_workstream_id="ws-buildouts",
        to_item_id=67890,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Launch slips.",
        mitigation="Escalate through partner PMs.",
        status=DependencyStatus.ACTIVE,
        owner_alias="operator",
        resolution_path="Weekly sync",
        planned_resolution_date=date(2026, 8, 15),
        schedule_status=DependencyScheduleStatus.AT_RISK,
        linked_risk_ids=("risk-1",),
    )

    save_dependencies("acme", (dependency,), programs_root=programs_root)
    save_dependencies("acme", (), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_dependencies(snapshot) == ()
