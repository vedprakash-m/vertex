from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict
from pathlib import Path

from typer.testing import CliRunner
import yaml
import pytest

from cli import app
from src.commands import milestones as milestones_module
from src.core.exceptions import ConfigError
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, Milestone, MilestoneStatus, Program, TrajectoryPoint, Workstream
from src.core.milestone_engine import load_milestones, save_milestones
from src.core.sqlite_stores import SQLiteTrajectoryStore
from src.core.trajectory import backfill_trajectory_points
from src.core.program_fact_store import ProgramFactInput, ProgramFactSnapshot, ProgramFactStore, load_program_facts


runner = CliRunner()


@pytest.mark.parametrize("with_archive", (False, True))
def test_assess_additive_commitment_revisions_preserve_existing_outputs(monkeypatch, tmp_path: Path, with_archive: bool) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(milestones_module, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(milestones_module, "_load_program_context", _fake_program_loader)
    monkeypatch.setattr(milestones_module, "_load_live_items", _fake_item_loader)
    _seed_milestones(programs_root)
    _set_program_storage_backend(programs_root, program_id="demo", storage_backend="sqlite")
    if with_archive:
        _seed_milestone_archive(programs_root)
    store = ProgramFactStore("demo", db_root=tmp_path)
    first = next(fact for fact in store.snapshot().facts if fact.payload["id"] == "m1")
    expected_revisions = [first]
    for target in ("2027-05-10", "2026-05-10"):
        result = runner.invoke(app, [
            "milestones", "update", "--program", "demo", "--id", "m1", "--target-date", target,
        ])
        assert result.exit_code == 0, result.output
        expected_revisions.append(next(fact for fact in store.snapshot().facts if fact.payload["id"] == "m1"))
    report = milestones_module.build_milestone_assessment_report(
        "demo", programs_root=programs_root, as_of=datetime(2026, 5, 10, 12, tzinfo=timezone.utc),
    )
    old_rows = [{key: value for key, value in asdict(row).items() if key != "commitment_date_history"}
                for row in report.rows]
    before = {path: path.read_bytes() for path in (store.db_path, programs_root / "demo" / "milestones.yaml")}
    outputs = {}
    for fmt in ("json", "human", "csv"):
        result = runner.invoke(app, [
            "milestones", "assess", "--program", "demo", "--as-of", "2026-05-10", "--format", fmt,
        ])
        assert result.exit_code == 0, result.output
        outputs[fmt] = result.stdout
    assert all(path.read_bytes() == content for path, content in before.items())
    payload = json.loads(outputs["json"])
    for actual, old in zip(payload["rows"], old_rows, strict=True):
        expected_old = json.loads(json.dumps(old))
        assert {key: value for key, value in actual.items()
                if key not in {"commitment_date_history", "longest_dependency_chain"}} == expected_old
    row = next(row for row in payload["rows"] if row["id"] == "m1")
    assert "commitment_date_history" in row
    expected = [{
        "target_date": revision.payload["target_date"],
        "recorded_at": revision.recorded_at.isoformat(),
        "superseded_at": expected_revisions[index + 1].recorded_at.isoformat()
        if index + 1 < len(expected_revisions) else None,
        "created_by": revision.created_by,
        "revision_id": revision.revision_id,
    } for index, revision in enumerate(expected_revisions)]
    assert row["commitment_date_history"] == expected
    assert row["target_date_history"] == (
        ["2026-05-08", "2026-05-10"] if with_archive else ["2026-05-10"]
    )
    assert "Commitment revisions:" in outputs["human"]
    for revision in expected:
        assert revision["revision_id"] in outputs["human"]
        assert revision["recorded_at"] in outputs["human"]
        assert revision["created_by"] in outputs["human"]
    csv_rows = list(csv.DictReader(outputs["csv"].splitlines()))
    csv_row = next(row for row in csv_rows if row["id"] == "m1")
    assert tuple(csv_row)[-1] == "commitment_date_history"
    assert json.loads(csv_row["commitment_date_history"]) == expected
    assert csv_row["target_date_history"] == "|".join(row["target_date_history"])


def test_assess_commitment_revisions_use_selected_snapshot_cutoff(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_milestones(programs_root)
    _set_program_storage_backend(programs_root, program_id="demo", storage_backend="sqlite")
    snapshot = load_program_facts("demo", programs_root=programs_root)
    first = next(fact for fact in snapshot.facts if fact.payload["id"] == "m1")
    store = ProgramFactStore("demo", db_root=tmp_path)
    later = store.append_fact(ProgramFactInput(
        fact_type=first.fact_type, entity_refs=first.entity_refs, payload={**first.payload, "target_date": "2030-05-10"},
        precedence=first.precedence, created_by="synthetic-later",
    ), recorded_at=snapshot.as_of + timedelta(days=1)).revision
    monkeypatch.setattr(milestones_module, "load_program_facts", lambda *args, **kwargs: snapshot)
    report = milestones_module.build_milestone_assessment_report(
        "demo", programs_root=programs_root, program_loader=_fake_program_loader,
        item_loader=_fake_item_loader, as_of=snapshot.as_of - timedelta(days=30),
    )
    row = next(row for row in report.to_payload()["rows"] if row["id"] == "m1")
    assert "commitment_date_history" in row
    assert [entry["revision_id"] for entry in row["commitment_date_history"]] == [first.revision_id]
    assert row["commitment_date_history"][0]["superseded_at"] == later.recorded_at.isoformat()
    assert row["target_date"] == first.payload["target_date"]


def test_assess_shim_does_not_fabricate_commitment_revisions(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_milestones(programs_root)
    store = ProgramFactStore("demo", db_root=tmp_path)
    with store._connect() as connection:
        connection.execute("DELETE FROM program_fact_revisions")
    report = milestones_module.build_milestone_assessment_report(
        "demo", programs_root=programs_root, program_loader=_fake_program_loader, item_loader=_fake_item_loader,
    )
    assert report.rows
    assert all("commitment_date_history" in row for row in report.to_payload()["rows"])
    assert all(row["commitment_date_history"] == () for row in report.to_payload()["rows"])
    assert "Commitment revisions:" not in milestones_module.render_milestone_assessment_report(report)
    csv_rows = list(csv.DictReader(milestones_module.render_milestone_assessment_csv(report).splitlines()))
    assert all(row["commitment_date_history"] == "[]" for row in csv_rows)


def test_assess_commitment_read_errors_surface(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_milestones(programs_root)
    monkeypatch.setattr(milestones_module, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(milestones_module, "_load_program_context", _fake_program_loader)
    monkeypatch.setattr(milestones_module, "_load_live_items", _fake_item_loader)

    def fail_read(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic history read failed")

    monkeypatch.setattr(ProgramFactStore, "list_accepted_milestone_revisions", fail_read, raising=False)
    result = runner.invoke(app, ["milestones", "assess", "--program", "demo", "--format", "json"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "Synthetic history read failed" in str(result.exception)


@pytest.mark.parametrize("malformed_date", ("bad-date", None, 123))
def test_assess_malformed_historical_commitment_date_is_visible(monkeypatch, tmp_path: Path, malformed_date: object) -> None:
    programs_root = tmp_path / "programs"
    _seed_milestones(programs_root)
    store = ProgramFactStore("demo", db_root=tmp_path)
    current = next(row for row in store.snapshot().facts if row.payload["id"] == "m1")
    fact = ProgramFactInput(
        fact_type=current.fact_type, entity_refs=current.entity_refs,
        payload={**current.payload, "target_date": malformed_date},
        precedence=current.precedence, created_by="synthetic",
    )
    store.append_fact(fact)
    store.append_fact(ProgramFactInput(
        fact_type=current.fact_type, entity_refs=current.entity_refs,
        payload=current.payload, precedence=current.precedence, created_by="synthetic",
    ))
    monkeypatch.setattr(milestones_module, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(milestones_module, "_load_program_context", _fake_program_loader)
    monkeypatch.setattr(milestones_module, "_load_live_items", _fake_item_loader)
    before = store.db_path.read_bytes()
    with pytest.raises(ConfigError, match="target_date"):
        milestones_module.build_milestone_assessment_report("demo", programs_root=programs_root)
    result = runner.invoke(app, ["milestones", "assess", "--program", "demo", "--format", "json"])
    assert result.exit_code == 2
    assert "target_date" in result.output
    assert "bad-date" not in result.output
    assert store.db_path.read_bytes() == before


@pytest.mark.parametrize("emitter", ("deck", "health", "review", "triage", "prep", "timeline"))
@pytest.mark.parametrize("on_chain", (True, False))
def test_node_count_emitters_use_longest_dependency_chain(monkeypatch, tmp_path: Path, emitter: str, on_chain: bool) -> None:
    from dataclasses import asdict
    from src.commands import prep, report_deck, report_health, review_full, triage
    from src.core.models import Confidence
    from src.core.models_v2 import MilestoneAssessment
    from src.core.reviewer_renderer import ReviewerMilestoneTimelineRow

    milestone = Milestone(
        id="m1", program_id="demo", name="Launch", target_date=date(2026, 10, 26),
        owner_alias="operator", status=MilestoneStatus.ON_TRACK, exit_criteria=("Ready",),
        linked_workstream_ids=(), linked_work_item_ids=(),
    )
    assessment = MilestoneAssessment(
        milestone_id="m1", computed_health=MilestoneStatus.ON_TRACK, blocked_criteria=(),
        slip_probability=0.0, critical_path=on_chain, confidence=Confidence.HIGH, reasoning="Ready",
    )
    if emitter == "deck":
        rendered = report_deck._format_milestone_status_label(milestone.status, critical_path=on_chain)
        unchanged = "on track"
    elif emitter == "health":
        rendered = report_health._build_milestone_health_summary((milestone,), (assessment,))
        unchanged = "Milestones: 1 on track."
    elif emitter == "review":
        rendered = review_full._format_milestone_row_title(milestone, assessment, critical_path=on_chain)
        unchanged = "m1 · declared on_track · computed on_track · high confidence"
    elif emitter == "triage":
        rendered = triage._format_milestone_summary(milestone, assessment)
        unchanged = "Launch — computed on track, declared on track; no blocked signals; 0% slip probability"
    elif emitter == "prep":
        monkeypatch.setattr(prep, "get_program_output_dir", lambda *a, **k: tmp_path)
        monkeypatch.setattr(prep, "_load_manifest_milestone_assessments", lambda path: ({
            **asdict(assessment), "computed_health": "on_track", "milestone_name": milestone.name,
            "target_date": milestone.target_date.isoformat(),
        },))
        rendered = "\n".join(prep._build_prep_milestone_lines(
            programs_root=tmp_path, edition_name="demo", issue_number=1,
        ))
        unchanged = "- Launch | on track | target 2026-10-26"
    else:
        rendered = ReviewerMilestoneTimelineRow(
            milestone_id="m1", name="Launch", target_date_label="Oct 26",
            declared_status="on_track", computed_status="on_track", critical_path=on_chain,
        ).status_summary
        unchanged = "computed on_track · declared on_track"
    assert rendered is not None
    assert "critical path" not in rendered.lower()
    if on_chain:
        assert "longest dependency chain" in rendered.lower()
    else:
        assert rendered == unchanged


def test_date_margin_cli_all_formats_once_per_dependency(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(milestones_module, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(milestones_module, "_load_program_context", _fake_program_loader)
    monkeypatch.setattr(milestones_module, "_load_live_items", _fake_item_loader)
    _seed_milestones(programs_root)
    dependency_path = programs_root / "demo" / "dependencies.yaml"
    dependency_path.write_text(yaml.safe_dump({
        "schema_version": "1.1", "dependencies": [{
            "id": "margin", "from_milestone_id": "m1", "to_milestone_id": "partner:m2",
            "dependency_type": "blocks", "risk_if_broken": "Blocked", "status": "active",
            "provider_forecast_ready_date": "2026-10-23",
            "consumer_required_by_date": "2026-10-26",
            "date_margin_calendar": "calendar_days", "date_margin_calendar_version": "1",
            "date_margin_timezone": "UTC", "date_margin_unit": "days", "date_margin_lag": 0,
        }, {
            "id": "unknown", "from_milestone_id": "m1", "to_milestone_id": "m2",
            "dependency_type": "informs", "risk_if_broken": "Unknown", "status": "active",
        }],
    }), encoding="utf-8")
    outputs = {}
    for format_name in ("human", "json", "csv"):
        result = runner.invoke(app, [
            "milestones", "assess", "--program", "demo", "--as-of", "2026-10-26",
            "--format", format_name,
        ])
        assert result.exit_code == 0, result.output
        outputs[format_name] = result.stdout
    payload = json.loads(outputs["json"])
    margins = {entry["dependency_id"]: entry for entry in payload["dependency_date_margins"]}
    assert margins["margin"]["margin_days"] == 3
    assert margins["margin"]["to_program_id"] == "partner"
    assert margins["margin"]["provider_forecast_ready_date"] == "2026-10-23"
    assert margins["unknown"]["margin_days"] is None
    assert margins["unknown"]["reasons"]
    assert payload["longest_dependency_chain_ids"] == payload["critical_path_ids"]
    assert all(row["longest_dependency_chain"] == row["critical_path"] for row in payload["rows"])
    assert "Longest dependency chain:" in outputs["human"]
    assert "Critical path:" not in outputs["human"]
    assert outputs["human"].count("Dependency date margin margin:") == 1
    assert "unknown" in outputs["human"]
    csv_rows = list(csv.DictReader(outputs["csv"].splitlines()))
    margin_rows = [row for row in csv_rows if row["record_type"] == "dependency_date_margin"]
    assert len(margin_rows) == 2
    assert {json.loads(row["dependency_date_margin"])["dependency_id"] for row in margin_rows} == {"margin", "unknown"}
    assert all(row["commitment_date_history"] == "" for row in margin_rows)


def test_date_margins_visible_without_milestones(monkeypatch, tmp_path: Path) -> None:
    from src.core.program_fact_store import ProgramFactSnapshot
    from tests.unit.test_milestone_engine import _date_margin_dependency

    monkeypatch.setattr(milestones_module, "load_program_facts", lambda *a, **k: ProgramFactSnapshot(
        program_id="demo", as_of=datetime.now(timezone.utc), facts=(),
    ))
    monkeypatch.setattr(milestones_module, "project_dependencies", lambda snapshot: (_date_margin_dependency(),))
    report = milestones_module.build_milestone_assessment_report("demo", programs_root=tmp_path / "programs")
    assert report.rows == ()
    assert len(report.dependency_date_margins) == 1
    assert "Dependency date margin margin: 3 days" in milestones_module.render_milestone_assessment_report(report)


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
    sentinel_snapshot = ProgramFactSnapshot(
        program_id="demo", as_of=datetime(2026, 5, 8, tzinfo=timezone.utc), facts=(),
    )

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