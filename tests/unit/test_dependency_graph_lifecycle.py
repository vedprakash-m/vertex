from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.dependency_graph import load_dependencies, save_dependencies
from src.core.fact_lineage_coverage import has_fact_provenance
from src.core.models_v2 import Dependency, DependencyScheduleStatus, DependencyStatus, DependencyType
from src.core.program_fact_store import load_program_facts, project_dependencies


def test_date_margin_inputs_round_trip_all_ingress(tmp_path: Path) -> None:
    from dataclasses import replace

    fields = {
        "provider_forecast_ready_date": "2026-10-23",
        "consumer_required_by_date": "2026-10-26",
        "date_margin_calendar": "calendar_days",
        "date_margin_calendar_version": "1",
        "date_margin_timezone": "UTC",
        "date_margin_unit": "days",
        "date_margin_lag": 0,
    }
    programs_root = _write_deps(tmp_path, [{
        "id": "margin", "from_milestone_id": "provider", "to_milestone_id": "consumer",
        "dependency_type": "blocks", "risk_if_broken": "Blocked", "status": "active",
        "planned_resolution_date": "2026-12-01", **fields,
    }])
    dependency, = load_dependencies("acme", programs_root=programs_root)
    assert dependency.provider_forecast_ready_date == date(2026, 10, 23)
    shim = load_program_facts("acme", programs_root=programs_root, fact_types=("dependency.link",))
    assert project_dependencies(shim) == (dependency,)
    save_dependencies("acme", (dependency,), programs_root=programs_root)
    raw = yaml.safe_load((programs_root / "acme" / "dependencies.yaml").read_text())
    assert raw["schema_version"] == "1.0"
    assert all(raw["dependencies"][0][key] == value for key, value in fields.items())
    persisted = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)
    assert project_dependencies(persisted) == (dependency,)
    assert persisted.facts[0].payload["provider_forecast_ready_date"] == "2026-10-23"
    raw["dependencies"][0]["consumer_required_by_date"] = "2026-10-22"
    (programs_root / "acme" / "dependencies.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert project_dependencies(load_program_facts(
        "acme", programs_root=programs_root, fact_types=("dependency.link",),
    )) == (dependency,)
    cleared = replace(dependency, **dict.fromkeys(fields))
    save_dependencies("acme", (cleared,), programs_root=programs_root)
    raw = yaml.safe_load((programs_root / "acme" / "dependencies.yaml").read_text())
    assert all(raw["dependencies"][0][key] is None for key in fields)
    assert project_dependencies(load_program_facts(
        "acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent,
    )) == (cleared,)
    assert cleared.planned_resolution_date == date(2026, 12, 1)


@pytest.mark.parametrize("field,value", [
    ("date_margin_calendar", "working_days"), ("date_margin_calendar_version", "2"),
    ("date_margin_timezone", "Europe/London"), ("date_margin_unit", "hours"),
    ("date_margin_lag", 1),
])
def test_unsupported_declared_semantics_round_trip_unknown(tmp_path: Path, field: str, value: object) -> None:
    from dataclasses import replace
    from src.core.milestone_engine import assess_dependency_date_margin
    from tests.unit.test_milestone_engine import _date_margin_dependency

    dependency = replace(_date_margin_dependency(), from_program_id="acme", **{field: value})
    programs_root = tmp_path / "programs"
    save_dependencies("acme", (dependency,), programs_root=programs_root)
    loaded, = load_dependencies("acme", programs_root=programs_root)
    projected, = project_dependencies(load_program_facts("acme", programs_root=programs_root))
    assert loaded == projected == dependency
    assert assess_dependency_date_margin(projected).reasons == (f"unsupported_{field}",)


@pytest.mark.parametrize("field,value", [
    ("provider_forecast_ready_date", "not-a-date"),
    ("consumer_required_by_date", "2026-10-26T12:00:00Z"),
    ("date_margin_lag", True),
    ("date_margin_lag", "0"),
    ("date_margin_calendar_version", 1),
    ("provider_forecast_ready_date", "2026-W43-1"),
])
def test_malformed_date_margin_inputs_fail_admission(tmp_path: Path, field: str, value: object) -> None:
    from src.core.exceptions import ConfigError

    programs_root = _write_deps(tmp_path, [{
        "id": "margin", "from_milestone_id": "provider", "to_milestone_id": "consumer",
        "dependency_type": "blocks", "risk_if_broken": "Blocked", "status": "active",
        "provider_forecast_ready_date": "2026-10-23",
        "consumer_required_by_date": "2026-10-26",
        "date_margin_calendar": "calendar_days", "date_margin_calendar_version": "1",
        "date_margin_timezone": "UTC", "date_margin_unit": "days", "date_margin_lag": 0,
        field: value,
    }])
    with pytest.raises(ConfigError, match=field):
        load_dependencies("acme", programs_root=programs_root)


def test_shared_dependency_date_margin_retains_scoped_endpoints(tmp_path: Path) -> None:
    from src.core.milestone_engine import assess_dependency_date_margin

    programs_root = _write_deps(tmp_path, [])
    (programs_root / "acme" / "dependencies.yaml").unlink()
    (programs_root / "dependencies.yaml").write_text(yaml.safe_dump({
        "schema_version": "1.1", "dependencies": [{
            "id": "shared", "from_milestone_id": "acme:provider",
            "to_milestone_id": "partner:consumer", "dependency_type": "blocks",
            "risk_if_broken": "Blocked", "status": "active",
            "provider_forecast_ready_date": date(2026, 10, 23),
            "consumer_required_by_date": date(2026, 10, 26),
            "date_margin_calendar": "calendar_days", "date_margin_calendar_version": "1",
            "date_margin_timezone": "UTC", "date_margin_unit": "days", "date_margin_lag": 0,
        }],
    }), encoding="utf-8")
    dependency, = load_dependencies("acme", programs_root=programs_root)
    assert (dependency.from_program_id, dependency.from_milestone_id) == ("acme", "provider")
    assert (dependency.to_program_id, dependency.to_milestone_id) == ("partner", "consumer")
    assert assess_dependency_date_margin(dependency).margin_days == 3
    snapshot = load_program_facts("acme", programs_root=programs_root, fact_types=("dependency.link",))
    assert project_dependencies(snapshot) == (dependency,)


def test_invalid_relationship_still_fails_configuration(tmp_path: Path) -> None:
    from src.core.exceptions import ConfigError

    programs_root = _write_deps(tmp_path, [{
        "id": "invalid", "from_milestone_id": "provider", "to_milestone_id": "consumer",
        "dependency_type": "finish_to_finish", "risk_if_broken": "Blocked", "status": "active",
    }])
    with pytest.raises(ConfigError, match="dependency"):
        load_dependencies("acme", programs_root=programs_root)


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


def test_save_dependencies_threads_evidence_refs_onto_the_fact_revision(tmp_path: Path) -> None:
    # ADF-W2.4/W2.5: a scout-derived dependency's evidence_refs must land on
    # the ProgramFactRevision's own top-level source_signal_ids field (what
    # fact_lineage_coverage.py's classifier actually inspects), not just
    # inside the payload dict the Dependency projection round-trips through.
    programs_root = tmp_path / "programs"
    dependency = Dependency(
        id="dep-1", from_program_id="acme", from_workstream_id="ws-launch", from_item_id=12345,
        from_milestone_id=None, to_program_id="fabrikam", to_workstream_id="ws-buildouts", to_item_id=67890,
        to_milestone_id=None, dependency_type=DependencyType.BLOCKS, risk_if_broken="Launch slips.",
        mitigation=None, status=DependencyStatus.ACTIVE, owner_alias="operator",
        evidence_refs=("sig-abc-123",),
    )

    save_dependencies("acme", (dependency,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)
    dependency_facts = [fact for fact in snapshot.facts if fact.fact_type == "dependency.link"]

    assert len(dependency_facts) == 1
    assert dependency_facts[0].source_signal_ids == ("sig-abc-123",)
    assert has_fact_provenance(dependency_facts[0]) is True


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
