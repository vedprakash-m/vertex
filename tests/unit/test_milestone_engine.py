from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.milestone_engine import assess_milestone_health, build_critical_path, describe_milestone_schedule_variance, detect_milestone_drift, load_milestones, save_milestones
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, LegacyDependency, Milestone, MilestoneStatus, TrajectoryPoint
from src.core.program_fact_store import load_program_facts, project_milestones


def test_load_milestones_reads_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    milestone_path = programs_root / "acme" / "milestones.yaml"
    milestone_path.parent.mkdir(parents=True, exist_ok=True)
    milestone_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
                "    owner_alias: maintainer",
                "    status: at_risk",
                "    exit_criteria:",
                "      - Code complete",
                "    linked_workstream_ids:",
                "      - acme",
                "    linked_work_item_ids:",
                "      - 900001",
            )
        ),
        encoding="utf-8",
    )

    milestones = load_milestones("acme", programs_root=programs_root)

    assert len(milestones) == 1
    assert milestones[0].program_id == "acme"
    assert milestones[0].status == MilestoneStatus.AT_RISK
    assert milestones[0].linked_work_item_ids == (900001,)


def test_load_milestones_returns_empty_tuple_when_file_absent(tmp_path: Path) -> None:
    assert load_milestones("acme", programs_root=tmp_path / "programs") == ()


def test_load_milestones_accepts_authored_empty_list(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "milestones.yaml"
    path.parent.mkdir(parents=True)
    path.write_text('schema_version: "1.0"\nmilestones: []\n', encoding="utf-8")

    assert load_milestones("acme", programs_root=programs_root) == ()


def test_save_empty_milestones_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    save_milestones("acme", (), programs_root=programs_root)

    assert load_milestones("acme", programs_root=programs_root) == ()


@pytest.mark.parametrize("field", ["", "milestones: null", "milestones: false", "milestones: 0", "milestones: ''", "milestones: {}", "milestones: example"])
def test_load_milestones_rejects_non_list_collection(tmp_path: Path, field: str) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "milestones.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f'schema_version: "1.0"\n{field}\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="Expected 'milestones' list"):
        load_milestones("acme", programs_root=programs_root)


def test_load_milestones_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    milestone_path = programs_root / "acme" / "milestones.yaml"
    milestone_path.parent.mkdir(parents=True, exist_ok=True)
    milestone_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
                "    owner_alias: maintainer",
                "    status: 1",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="status must be a string"):
        load_milestones("acme", programs_root=programs_root)


def test_load_milestones_rejects_non_string_exit_criteria(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    milestone_path = programs_root / "acme" / "milestones.yaml"
    milestone_path.parent.mkdir(parents=True, exist_ok=True)
    milestone_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
                "    owner_alias: maintainer",
                "    status: at_risk",
                "    exit_criteria:",
                "      - 1",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="exit_criteria must contain strings only"):
        load_milestones("acme", programs_root=programs_root)


def test_load_milestones_rejects_numeric_string_linked_work_item_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    milestone_path = programs_root / "acme" / "milestones.yaml"
    milestone_path.parent.mkdir(parents=True, exist_ok=True)
    milestone_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
                "    owner_alias: maintainer",
                "    status: at_risk",
                "    linked_work_item_ids:",
                '      - "900001"',
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="linked_work_item_ids must contain integers only"):
        load_milestones("acme", programs_root=programs_root)


def test_save_milestones_dual_writes_current_fact_store_projection(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    milestone = Milestone(
        id="m1",
        program_id="acme",
        name="Launch readiness",
        target_date=date(2026, 6, 10),
        owner_alias="operator",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=("Dry run complete",),
        linked_workstream_ids=("ws-launch",),
        linked_work_item_ids=(12345,),
        notes="Awaiting final rehearsal.",
        last_reviewed_date=date(2026, 6, 6),
    )

    save_milestones("acme", (milestone,), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_milestones(snapshot) == (milestone,)


def test_save_milestones_closes_removed_fact_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    milestone = Milestone(
        id="m1",
        program_id="acme",
        name="Launch readiness",
        target_date=date(2026, 6, 10),
        owner_alias="operator",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=("Dry run complete",),
        linked_workstream_ids=("ws-launch",),
        linked_work_item_ids=(12345,),
        notes="Awaiting final rehearsal.",
        last_reviewed_date=date(2026, 6, 6),
    )

    save_milestones("acme", (milestone,), programs_root=programs_root)
    save_milestones("acme", (), programs_root=programs_root)

    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_milestones(snapshot) == ()


def test_assess_milestone_health_classifies_on_track() -> None:
    milestone = _milestone(target_date=date(2026, 5, 25), status=MilestoneStatus.ON_TRACK)
    items = (_work_item(item_id=900001, state="Active", risk_level=RiskLevel.LOW, target_date=date(2026, 5, 20)),)

    assessment = assess_milestone_health(milestone, items, {900001: ()}, datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc))

    assert assessment.computed_health == MilestoneStatus.ON_TRACK
    assert assessment.blocked_criteria == ()
    assert assessment.confidence == Confidence.MEDIUM


def test_assess_milestone_health_classifies_at_risk() -> None:
    milestone = _milestone(target_date=date(2026, 5, 18), status=MilestoneStatus.ON_TRACK)
    items = (_work_item(item_id=900001, state="Active", risk_level=RiskLevel.HIGH, target_date=date(2026, 5, 17)),)

    assessment = assess_milestone_health(
        milestone,
        items,
        {
            900001: (
                TrajectoryPoint(
                    date=date(2026, 5, 1),
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 12),
                    risk_level=RiskLevel.MEDIUM,
                    area_path="One\\Adventure\\Acme",
                ),
                TrajectoryPoint(
                    date=date(2026, 5, 8),
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 19),
                    risk_level=RiskLevel.HIGH,
                    area_path="One\\Adventure\\Acme",
                ),
            )
        },
        datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    assert assessment.computed_health == MilestoneStatus.AT_RISK
    assert assessment.blocked_criteria
    assert assessment.slip_probability > 0.0
    assert assessment.confidence == Confidence.HIGH


def test_assess_milestone_health_classifies_missed() -> None:
    milestone = _milestone(target_date=date(2026, 5, 5), status=MilestoneStatus.ON_TRACK)
    items = (_work_item(item_id=900001, state="Active", risk_level=RiskLevel.MEDIUM, target_date=date(2026, 5, 4)),)

    assessment = assess_milestone_health(milestone, items, {900001: ()}, datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc))

    assert assessment.computed_health == MilestoneStatus.MISSED
    assert assessment.slip_probability == 1.0


def test_build_critical_path_returns_longest_chain() -> None:
    milestones = (
        _milestone(milestone_id="m1", target_date=date(2026, 5, 10)),
        _milestone(milestone_id="m2", target_date=date(2026, 5, 20)),
        _milestone(milestone_id="m3", target_date=date(2026, 5, 30)),
        _milestone(milestone_id="m4", target_date=date(2026, 5, 15)),
    )
    dependencies = (
        LegacyDependency(from_item="m1", to_item="m2", impact="m2 waits on m1"),
        LegacyDependency(from_item="m2", to_item="m3", impact="m3 waits on m2"),
        LegacyDependency(from_item="m1", to_item="m4", impact="m4 waits on m1"),
    )

    critical_path = build_critical_path(milestones, dependencies)

    assert tuple(milestone.id for milestone in critical_path) == ("m1", "m2", "m3")


def test_build_critical_path_accepts_structured_milestone_dependencies() -> None:
    milestones = (
        _milestone(milestone_id="m1", target_date=date(2026, 5, 10)),
        _milestone(milestone_id="m2", target_date=date(2026, 5, 20)),
        _milestone(milestone_id="m3", target_date=date(2026, 5, 30)),
    )
    dependencies = (
        Dependency(
            id="m1-m2",
            from_program_id="acme",
            from_workstream_id=None,
            from_item_id=None,
            from_milestone_id="m1",
            to_program_id="acme",
            to_workstream_id=None,
            to_item_id=None,
            to_milestone_id="m2",
            dependency_type=DependencyType.BLOCKS,
            risk_if_broken="m2 waits on m1",
            mitigation=None,
            status=DependencyStatus.ACTIVE,
            owner_alias=None,
        ),
        Dependency(
            id="m2-m3",
            from_program_id="acme",
            from_workstream_id=None,
            from_item_id=None,
            from_milestone_id="m2",
            to_program_id="acme",
            to_workstream_id=None,
            to_item_id=None,
            to_milestone_id="m3",
            dependency_type=DependencyType.BLOCKS,
            risk_if_broken="m3 waits on m2",
            mitigation=None,
            status=DependencyStatus.ACTIVE,
            owner_alias=None,
        ),
    )

    critical_path = build_critical_path(milestones, dependencies)

    assert tuple(milestone.id for milestone in critical_path) == ("m1", "m2", "m3")


def test_build_critical_path_raises_on_cycle() -> None:
    milestones = (
        _milestone(milestone_id="m1", target_date=date(2026, 5, 10)),
        _milestone(milestone_id="m2", target_date=date(2026, 5, 20)),
    )
    dependencies = (
        LegacyDependency(from_item="m1", to_item="m2", impact="m2 waits on m1"),
        LegacyDependency(from_item="m2", to_item="m1", impact="m1 waits on m2"),
    )

    with pytest.raises(ConfigError, match="cycle"):
        build_critical_path(milestones, dependencies)


def test_detect_milestone_drift_returns_target_date_history() -> None:
    milestone = _milestone(milestone_id="m1", target_date=date(2026, 5, 20))

    drift = detect_milestone_drift(
        milestone,
        (
            {"milestones": [{"id": "m1", "target_date": "2026-05-10"}]},
            {"milestones": [{"id": "m1", "target_date": "2026-05-15"}]},
            {"milestones": [{"id": "m1", "target_date": "2026-05-15"}]},
            {"milestones": [{"id": "m1", "target_date": "2026-05-20"}]},
        ),
    )

    assert drift == (date(2026, 5, 10), date(2026, 5, 15), date(2026, 5, 20))


def test_describe_milestone_schedule_variance_reports_late_tracking() -> None:
    milestone = _milestone(target_date=date(2026, 5, 25), status=MilestoneStatus.ON_TRACK)
    items = (_work_item(item_id=900001, state="Active", risk_level=RiskLevel.MEDIUM, target_date=date(2026, 5, 24)),)

    summary = describe_milestone_schedule_variance(
        milestone,
        items,
        {
            900001: (
                TrajectoryPoint(
                    date=date(2026, 5, 1),
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 22),
                    risk_level=RiskLevel.MEDIUM,
                    area_path="One\\Adventure\\Acme",
                ),
                TrajectoryPoint(
                    date=date(2026, 5, 8),
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 28),
                    risk_level=RiskLevel.HIGH,
                    area_path="One\\Adventure\\Acme",
                ),
            )
        },
        datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    assert summary == "Tracking 2026-05-28 (3 days late vs target)"


def test_describe_milestone_schedule_variance_reports_completion_variance() -> None:
    milestone = _milestone(target_date=date(2026, 5, 25), status=MilestoneStatus.COMPLETED)
    items = (_work_item(item_id=900001, state="Done", risk_level=RiskLevel.LOW, target_date=date(2026, 5, 25)),)

    summary = describe_milestone_schedule_variance(
        milestone,
        items,
        {
            900001: (
                TrajectoryPoint(
                    date=date(2026, 5, 1),
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 25),
                    risk_level=RiskLevel.MEDIUM,
                    area_path="One\\Adventure\\Acme",
                ),
                TrajectoryPoint(
                    date=date(2026, 5, 24),
                    state="Done",
                    assigned_to="Vertex Maintainer",
                    target_date=date(2026, 5, 25),
                    risk_level=RiskLevel.LOW,
                    area_path="One\\Adventure\\Acme",
                ),
            )
        },
        datetime(2026, 5, 26, 18, 0, tzinfo=timezone.utc),
    )

    assert summary == "Completed 2026-05-24 (1 day early vs target)"


def _milestone(
    *,
    milestone_id: str = "m3-code-complete",
    target_date: date,
    status: MilestoneStatus = MilestoneStatus.ON_TRACK,
) -> Milestone:
    return Milestone(
        id=milestone_id,
        program_id="acme",
        name="M3 - Code Complete",
        target_date=target_date,
        owner_alias="maintainer",
        status=status,
        exit_criteria=("Code complete", "Validation complete"),
        linked_workstream_ids=("acme",),
        linked_work_item_ids=(900001,),
    )


def _work_item(*, item_id: int, state: str, risk_level: RiskLevel, target_date: date | None) -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Feature",
        title="Milestone gate",
        state=state,
        assigned_to="Vertex Maintainer",
        assigned_to_email="maintainer@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 1",
        target_date=target_date,
        risk_level=risk_level,
        tags=["acme"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )
def _date_margin_dependency():
    from src.core.models_v2 import Dependency, DependencyType, DependencyStatus

    return Dependency(
        id="margin", from_program_id="demo", from_workstream_id=None, from_item_id=None,
        from_milestone_id="provider", to_program_id="demo", to_workstream_id=None,
        to_item_id=None, to_milestone_id="consumer", dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Blocked", mitigation=None, status=DependencyStatus.ACTIVE, owner_alias=None,
        provider_forecast_ready_date=date(2026, 10, 23),
        consumer_required_by_date=date(2026, 10, 26),
        date_margin_calendar="calendar_days", date_margin_calendar_version="1",
        date_margin_timezone="UTC", date_margin_unit="days", date_margin_lag=0,
    )


@pytest.mark.parametrize("required,margin,status", [
    (date(2026, 10, 26), 3, "buffer"),
    (date(2026, 10, 23), 0, "no_buffer"),
    (date(2026, 10, 22), -1, "breach"),
])
def test_dependency_date_margin(required: date, margin: int, status: str) -> None:
    from src.core.milestone_engine import assess_dependency_date_margin

    result = assess_dependency_date_margin(replace(
        _date_margin_dependency(), consumer_required_by_date=required,
    ))
    assert (result.margin_days, result.status, result.reasons) == (margin, status, ())
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.status = "unknown"


@pytest.mark.parametrize("field,value", [
    ("provider_forecast_ready_date", None), ("consumer_required_by_date", None),
    ("provider_forecast_ready_date", "invalid"),
    ("consumer_required_by_date", datetime(2026, 10, 26, tzinfo=timezone.utc)),
    ("date_margin_calendar", None), ("date_margin_calendar", "working_days"),
    ("date_margin_calendar_version", None), ("date_margin_calendar_version", "2"),
    ("date_margin_timezone", None), ("date_margin_timezone", "Europe/London"),
    ("date_margin_unit", None), ("date_margin_unit", "hours"),
    ("date_margin_lag", None), ("date_margin_lag", 1), ("date_margin_lag", True),
    ("date_margin_lag", "0"), ("date_margin_lag", 0.0),
    ("dependency_type", DependencyType.INFORMS),
    ("dependency_type", DependencyType.SHARES_RESOURCE),
    ("dependency_type", "blocks"),
    ("date_margin_calendar", 1), ("date_margin_unit", []),
])
def test_dependency_date_margin_unknown(field: str, value: object) -> None:
    from src.core.milestone_engine import assess_dependency_date_margin

    result = assess_dependency_date_margin(replace(_date_margin_dependency(), **{field: value}))
    assert result.status == "unknown"
    assert result.margin_days is None
    assert any(field in reason for reason in result.reasons)


def test_dependency_date_margin_never_uses_legacy_target_date() -> None:
    from src.core.milestone_engine import assess_dependency_date_margin

    dependency = replace(_date_margin_dependency(), provider_forecast_ready_date=None,
                         consumer_required_by_date=None, planned_resolution_date=date(2026, 10, 23))
    assert assess_dependency_date_margin(dependency).margin_days is None
