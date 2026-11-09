from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import partial
from io import StringIO
import json
from pathlib import Path
import shutil

import pytest
import typer
import yaml
from typer.testing import CliRunner

from cli import app
from src.ai.exec_summary_drafter import _describe_owner_change
from src.commands.diff import build_offline_diff_summary
from src.commands.report import generate_report_draft
from src.commands.report_history import _format_draft_item_delta
from src.core import metric_registry, narrative_store
from src.core.ado_narrative_hint_engine import HintKind, generate_delta_hints
from src.core.chapter_contract_loader import canonical_dimension_binding_id
from src.core.delta_engine import build_deltas
from src.core.html_renderer import _delta_row_detail
from src.core.jinja_filters import build_anchor
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from src.core.archive_store import write_confirmed_issue
from src.core.journal import append_review_decision, append_signal
from src.core.models import (
    AttributionTier, Comment, Confidence, DeltaKind, DeltaSet, EditionType,
    EvidencePacket, ItemDelta, Revision, RiskLevel, Snapshot, SnapshotItem, WorkItem,
)
from src.core.models_v2 import Program, Signal, SignalReviewDecision, TrajectoryPoint
from src.core.publish_diff import _build_item_lines
from src.core.trajectory import append_trajectory_point
from tests.unit.test_config_loader import _write_demo_v2_layout


runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_diff_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.commands.diff.REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr("src.commands.diff.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.diff.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(narrative_store, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(
        metric_registry, "load_metric_definition_map",
        partial(metric_registry.load_metric_definition_map, metrics_root=tmp_path / "knowledge" / "metrics"),
    )
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))


def test_diff_cli_since_last_confirmed_renders_semantic_v2_sections(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    as_of_issue_1 = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    as_of_issue_2 = datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)
    baseline_items = tuple(
        replace(item, assigned_to=None, assigned_to_email=None) if item.id == 900002 else item
        for item in _sample_items(as_of_issue_1)
    )

    baseline = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of_issue_1,
        work_item_loader=lambda bundle, timestamp: (baseline_items, 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    baseline_exec_summary = baseline.narratives_dir / "exec_summary.md"
    baseline_exec_summary.write_text("Baseline weekly summary.\n", encoding="utf-8")
    workstream_path = baseline.narratives_dir / "ws_deployment_readiness.md"
    workstream_path.write_text("Baseline deployment workstream summary.\n", encoding="utf-8")

    adjusted_snapshot = replace(
        baseline.snapshot,
        items=tuple(
            replace(item, risk_level=RiskLevel.LOW) if item.id == 900001 else item
            for item in baseline.snapshot.items
        ),
        scorecards=tuple(
            replace(scorecard, risk=RiskLevel.LOW)
            for scorecard in baseline.snapshot.scorecards
        ),
    )
    write_confirmed_issue(
        edition="acme_weekly",
        issue_number=1,
        snapshot=adjusted_snapshot,
        html_body=baseline.html_body,
        markdown_body=baseline.markdown_body,
        manifest=baseline.manifest,
        overrides_source=baseline.overrides_path,
        review_status_source=baseline.review_status_path,
        narratives_source_dir=baseline.narratives_dir,
        archive_root=archive_root,
    )

    current = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of_issue_2,
        work_item_loader=lambda bundle, timestamp: (_sample_items_with_diff(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    assert {
        delta.work_item_id: delta.field_changes["assigned_to"]
        for delta in current.report.deltas.owner_changes
    } == {
        900001: ("Vertex Maintainer", "next-maintainer@example.com"),
        900002: (None, "Second Owner"),
    }

    current_exec_summary = current.narratives_dir / "exec_summary.md"
    current_exec_summary.write_text("Updated weekly summary with rollout risk.\n", encoding="utf-8")
    current_workstream_path = current.narratives_dir / "ws_deployment_readiness.md"
    current_workstream_path.write_text("Deployment workstream updated with new safeguard.\n", encoding="utf-8")

    programs_root = reports_root.parent / "programs"
    _append_approved_signal(
        programs_root=programs_root,
        signal=Signal(
            id="sig-ado-1",
            timestamp=datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Deployment target moved out one sprint.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )
    _append_approved_signal(
        programs_root=programs_root,
        signal=Signal(
            id="sig-kusto-1",
            timestamp=datetime(2026, 5, 7, 11, 0, tzinfo=timezone.utc),
            source="kusto",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900004",),
            text="Telemetry shows new failure spike.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
        ),
    )

    for point in (
        TrajectoryPoint(date=date(2026, 5, 6), state="Active", assigned_to="owner-a", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        TrajectoryPoint(date=date(2026, 5, 7), state="Active", assigned_to="owner-b", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        TrajectoryPoint(date=date(2026, 5, 8), state="Active", assigned_to="owner-c", target_date=date(2026, 5, 14), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme\\Deployment"),
        TrajectoryPoint(date=date(2026, 5, 9), state="Active", assigned_to="owner-d", target_date=date(2026, 5, 16), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Acme\\Deployment"),
    ):
        append_trajectory_point("acme", 900001, point, programs_root=programs_root)

    monkeypatch.setattr("src.commands.diff.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.diff.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.diff.ARCHIVE_ROOT", archive_root)

    archived_files = {
        path.relative_to(archive_root): path.read_bytes()
        for path in archive_root.rglob("*") if path.is_file()
    }
    result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--since", "last-confirmed"])
    explicit_issue_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--since", "issue-1"])
    json_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--since", "last-confirmed", "--format", "json"])
    csv_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--since", "last-confirmed", "--format", "csv"])

    assert result.exit_code == 0
    assert explicit_issue_result.exit_code == 0
    assert json_result.exit_code == 0
    assert csv_result.exit_code == 0
    assert "VERTEX DIFF - Changes since Issue #001" in result.stdout
    assert "VERTEX DIFF - Changes since Issue #001" in explicit_issue_result.stdout
    assert "Items:" in result.stdout
    assert '+ NEW: WI:900004 "New cache warmup safeguard"' in result.stdout
    assert 'RISK UP: WI:900001 "Deployment velocity telemetry stabilization"' in result.stdout
    expected_owner = (
        'OWNER CHANGED: WI:900001 "Deployment velocity telemetry stabilization" '
        "(Vertex Maintainer -> next-maintainer@example.com)"
    )
    expected_new_owner = (
        'OWNER CHANGED: WI:900002 "Fleet pilot dependency on capacity allocation" '
        "(Unassigned -> Second Owner)"
    )
    assert [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith("OWNER CHANGED: WI:900001")
    ] == [expected_owner]
    assert expected_owner in explicit_issue_result.stdout
    assert expected_owner in json.loads(json_result.stdout)["summary"]
    assert expected_owner in next(csv.DictReader(StringIO(csv_result.stdout)))["summary"]
    assert expected_new_owner in result.stdout
    assert expected_new_owner in explicit_issue_result.stdout
    assert expected_new_owner in json.loads(json_result.stdout)["summary"]
    assert expected_new_owner in next(csv.DictReader(StringIO(csv_result.stdout)))["summary"]
    assert "Scorecards:" in result.stdout
    assert "Narratives:" in result.stdout
    assert f"{current_workstream_path.name}:" in result.stdout
    assert "Signals:" in result.stdout
    assert "2 new approved signals since Issue #001 (1 ADO, 1 Kusto)." in result.stdout
    assert "new drift pattern" in result.stdout
    assert {
        path.relative_to(archive_root): path.read_bytes()
        for path in archive_root.rglob("*") if path.is_file()
    } == archived_files


@pytest.mark.parametrize(
    ("field_changes", "expected"),
    [
        ({"assigned_to": ("Old Owner", "New Owner")}, "Old Owner -> New Owner"),
        ({"assigned_to": (None, "New Owner"), "Assigned To": ("Wrong", "Wrong")}, "Unassigned -> New Owner"),
        ({"assigned_to": ("Old Owner", None), "System.AssignedTo": ("Wrong", "Wrong")}, "Old Owner -> Unassigned"),
        ({"assigned_to": (None, None), "Assigned To": ("Wrong", "Wrong")}, "Unassigned -> Unassigned"),
        ({"Assigned To": ("Legacy Old", "Legacy New")}, "Legacy Old -> Legacy New"),
        ({"System.AssignedTo": ("ADO Old", "ADO New")}, "ADO Old -> ADO New"),
        ({"Assigned To": (None, None), "System.AssignedTo": ("ADO Old", "ADO New")}, "ADO Old -> ADO New"),
        ({}, "Unassigned -> Unassigned"),
    ],
)
def test_publish_diff_owner_fields_preserve_canonical_and_legacy_values(
    field_changes: dict[str, tuple[str | None, str | None]], expected: str,
) -> None:
    delta = ItemDelta(
        work_item_id=900001,
        kind=DeltaKind.OWNER_CHANGED,
        field_changes=field_changes,
        old_risk=None,
        new_risk=None,
        old_eta=None,
        new_eta=None,
        evidence=EvidencePacket(
            work_item_id=900001,
            revisions=(),
            comments=(),
            enrichments=(),
            confidence=Confidence.NONE,
            tier=AttributionTier.TIER3,
            summary_for_reviewer="Synthetic.",
        ),
    )
    deltas = DeltaSet(
        issue_number=2,
        previous_issue_number=1,
        new_items=(),
        closed_items=(),
        risk_changes=(),
        eta_changes=(),
        unchanged_count=0,
        owner_changes=(delta,),
    )

    lines = _build_item_lines(current_items=(), reference_items=(), deltas=deltas)

    assert lines == (f'OWNER CHANGED: WI:900001 "Work item 900001" ({expected})',)


@pytest.mark.parametrize(
    ("prior_owner", "current_name", "current_email", "expected"),
    [
        ("Old Owner", "New Owner", "new@example.com", "Old Owner -> new@example.com"),
        ("Old Owner", "New Owner", None, "Old Owner -> New Owner"),
        (None, "New Owner", None, "Unassigned -> New Owner"),
        (None, None, "new@example.com", "Unassigned -> new@example.com"),
        ("", "New Owner", None, "Unassigned -> New Owner"),
        ("Old Owner", None, None, "Old Owner -> Unassigned"),
        (None, None, None, None),
        ("", None, "", None),
        ("Old Owner", "Old Owner", "new@example.com", None),
        ("old@example.com", "Old Owner", "old@example.com", None),
    ],
)
def test_publish_diff_renders_owner_transitions_from_delta_engine(
    prior_owner: str | None, current_name: str | None,
    current_email: str | None, expected: str | None,
) -> None:
    as_of = datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)
    current = replace(_sample_items(as_of)[0], assigned_to=current_name, assigned_to_email=current_email)
    prior = SnapshotItem(
        id=current.id, type=current.type, title=current.title, state=current.state,
        assigned_to=prior_owner, area_path=current.area_path, target_date=current.target_date,
        risk_level=current.risk_level, tags=current.tags,
    )
    snapshot = Snapshot(
        issue_number=1, generated_at=as_of, ado_data_as_of=as_of,
        edition_type=EditionType.DETAILED, items=(prior,), scorecards=(),
    )

    deltas = build_deltas((current,), snapshot, issue_number=2, previous_issue_number=1)
    lines = _build_item_lines(current_items=(current,), reference_items=(prior,), deltas=deltas)

    if expected is None:
        assert lines == ("No item deltas detected.",)
    else:
        assert lines == (f'OWNER CHANGED: WI:{current.id} "{current.title}" ({expected})',)
        delta = deltas.owner_changes[0]
        assert _delta_row_detail(delta, category="owner_change") == expected.replace(" -> ", " \u2192 ")
        assert _format_draft_item_delta(delta) == f"Owner changed: {expected}"
        old_value = prior_owner or "unassigned"
        new_value = current_email or current_name or "unassigned"
        assert _describe_owner_change(delta, current) == (
            f"OWNER_CHANGE #{current.id} {current.title} moved from {old_value} to {new_value}."
        )
        hints = generate_delta_hints(
            deltas, {current.id: current}, 2,
            Program(schema_version="2.0", id="synthetic", name="Synthetic"),
        )
        owner_hint = next(hint for hint in hints if hint.hint_kind is HintKind.OWNER_CHANGED)
        assert (owner_hint.old_value, owner_hint.new_value) == (old_value, new_value)


def _seed_v2_report_layout(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    editions_root, programs_root = _write_demo_v2_layout(tmp_path)
    program_dir = programs_root / "acme"
    (programs_root / "demo").rename(program_dir)
    for path in program_dir.rglob("*.yaml"):
        path.write_text(path.read_text(encoding="utf-8").replace("demo", "acme"), encoding="utf-8")
    edition = yaml.safe_load((editions_root / "demo_weekly.yaml").read_text(encoding="utf-8"))
    edition.update(
        id="acme_weekly", program_id="acme", layout_mode="continuity",
        cadence_note={"detailed": "Weekly synthetic diff", "focused": "Focused synthetic diff"},
    )
    canonical_editions = program_dir / "editions"
    canonical_editions.mkdir()
    (canonical_editions / "acme_weekly.yaml").write_text(yaml.safe_dump(edition), encoding="utf-8")
    shutil.rmtree(editions_root)
    program_path = program_dir / "program.yaml"
    program = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program["kusto"] = {"enabled": False}
    program_path.write_text(yaml.safe_dump(program), encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "2.0",
            "workstreams": [{
                "id": "deployment_readiness", "name": "Deployment Readiness",
                "area_paths": ["One\\Adventure\\Acme\\Deployment"], "dri_email": "owner@example.com",
            }],
        }),
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "2.0",
            "scorecards": [{
                "name": "Acme Adventure/SYN-A 100% Ramp Readiness",
                "dimensions": [{
                    "name": "Deployment Velocity", "workstream_id": "deployment_readiness",
                    "ado_filter": "area_path contains 'Acme'",
                }],
            }],
        }),
        encoding="utf-8",
    )
    (program_dir / "chapter_contract.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "1.0",
            "chapters": [{
                "id": "deployment_readiness", "title": "Deployment Readiness",
                "priority": 1, "include_in": ["detailed"],
                "dimensions": [canonical_dimension_binding_id(
                    "Acme Adventure/SYN-A 100% Ramp Readiness", "Deployment Velocity", chapter_namespace="acme",
                )],
            }],
        }),
        encoding="utf-8",
    )
    return reports_root, archive_root, (tmp_path / "programs" / "acme" / "publications")


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=900001,
            type="Feature",
            title="Deployment velocity telemetry stabilization",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=["Safety"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900001,
                    rev_number=7,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Proposed", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=900002,
            type="Risk",
            title="Fleet pilot dependency on capacity allocation",
            state="At Risk",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Contoso\\Networking",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            tags=["EXGATE"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900002,
                    rev_number=3,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Active", "At Risk")},
                )
            ],
            comments=[
                Comment(
                    work_item_id=900002,
                    comment_id=1,
                    created_by="Vertex Maintainer",
                    created_by_email="maintainer@example.com",
                    created_date=as_of,
                    text="Capacity allocation follow-up is in progress.",
                )
            ],
            fetched_at=as_of,
        ),
        WorkItem(
            id=900003,
            type="Scenario",
            title="Pilot rollout path validation",
            state="Proposed",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900003,
                    rev_number=2,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"AreaPath": (None, "One\\Adventure\\Fabrikam\\Acme\\Scenarios")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _sample_items_with_diff(as_of: datetime) -> tuple[WorkItem, ...]:
    baseline = _sample_items(as_of)
    return (
        replace(
            baseline[0],
            target_date=date(2026, 5, 14),
            assigned_to="Next Maintainer",
            assigned_to_email="next-maintainer@example.com",
        ),
        replace(baseline[1], assigned_to="Second Owner", assigned_to_email=None),
        baseline[2],
        WorkItem(
            id=900004,
            type="Bug",
            title="New cache warmup safeguard",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=["Hotfix"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900004,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": (None, "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _append_approved_signal(*, programs_root: Path, signal: Signal) -> None:
    append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    append_review_decision(
        signal.program_id,
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=signal.timestamp,
            reviewed_by="test-author",
        ),
        programs_root=programs_root,
    )


def test_build_offline_diff_summary_detects_narrative_and_override_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    reports_root, archive_root, output_root = _seed_v2_report_layout(tmp_path, tmp_path)
    programs_root = tmp_path / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    section_id = build_anchor("Acme Adventure/SYN-A 100% Ramp Readiness-Deployment Velocity")
    narrative_path = get_narratives_dir("acme_weekly", 1, reports_root) / f"ws_{section_id}.md"
    narrative_path.write_text("Deployment velocity regressed after the rollout gate slipped.\n", encoding="utf-8")

    overrides_path = get_overrides_path("acme_weekly", reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Acme Adventure/SYN-A 100% Ramp Readiness"]["Deployment Velocity"]["risk"] = "high"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    summary_builder = Mock(side_effect=AssertionError("diff should not build AI reality context"))
    monkeypatch.setattr("src.commands.report_ai.build_reality_assessment_summary", summary_builder)
    summary = build_offline_diff_summary(
        edition_name="acme_weekly",
        issue_number=1,
        section=None,
        reports_root=reports_root,
        programs_root=programs_root,
    )

    assert "VERTEX DIFF - Issue 001 vs last dry-run" in summary
    assert "Deployment Velocity (narrative changed):" in summary
    assert "Deployment Velocity (risk level changed via override):" in summary
    summary_builder.assert_not_called()


def test_diff_cli_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    section_id = build_anchor("Acme Adventure/SYN-A 100% Ramp Readiness-Deployment Velocity")
    narrative_path = get_narratives_dir("acme_weekly", 1, reports_root) / f"ws_{section_id}.md"
    narrative_path.write_text("Deployment velocity regressed after the rollout gate slipped.\n", encoding="utf-8")

    monkeypatch.setattr("src.commands.diff.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.diff.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.diff.ARCHIVE_ROOT", archive_root)

    json_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition"] == "acme_weekly"
    assert payload["issue_number"] == 1
    assert payload["mode"] == "last-draft"
    assert payload["since"] == "last-draft"
    assert "VERTEX DIFF - Issue 001 vs last dry-run" in payload["summary"]

    csv_result = runner.invoke(app, ["diff", "--edition", "acme_weekly", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "edition,issue_number,since,section,mode,summary"
    assert "acme_weekly,1,last-draft,-,last-draft," in lines[1]
    assert "VERTEX DIFF - Issue 001 vs last dry-run" in lines[1]


def test_build_offline_diff_summary_surfaces_invalid_current_draft_state(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    draft_state_path = programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json"
    draft_state_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match=r"Cannot read draft state for edition 'acme_weekly', issue 001 \(JSONDecodeError\)\."):
        build_offline_diff_summary(
            edition_name="acme_weekly",
            issue_number=1,
            section=None,
            reports_root=reports_root,
            programs_root=programs_root,
        )
