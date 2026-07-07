from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import vitality
from src.core.leakage_detector import LeakageReport
from src.core.models import Comment, Confidence, Revision, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, Program, Signal, TrajectoryPoint, VitalityScore, Workstream
from src.core.sqlite_stores import SQLiteTrajectoryStore


runner = CliRunner()


def test_generate_vitality_report_groups_owner_scores(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(vitality, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="deployment_readiness", name="Deployment", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(vitality.gather_helpers, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(vitality, "load_approved_workiq_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        vitality,
        "detect_leakage",
        lambda *args, **kwargs: LeakageReport(events=(), signal_counts_by_item={}, leakage_counts_by_item={}, owner_leakage_ratios={}),
    )

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    artifacts = vitality.generate_vitality_report(
        "acme",
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of: (_sample_items(as_of), 0),
    )

    assert len(artifacts.scored_items) == 2
    assert artifacts.owner_aggregates[0].scope_type == "owner"
    assert artifacts.workstream_aggregates[0].scope_id == "deployment_readiness"


def test_vitality_cli_prints_owner_aggregates(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(vitality, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="deployment_readiness", name="Deployment", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(vitality.gather_helpers, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(vitality, "_load_vitality_items", lambda program, workstreams, as_of: (_sample_items(as_of), 0))
    monkeypatch.setattr(vitality, "load_approved_workiq_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        vitality,
        "detect_leakage",
        lambda *args, **kwargs: LeakageReport(events=(), signal_counts_by_item={}, leakage_counts_by_item={}, owner_leakage_ratios={}),
    )

    result = runner.invoke(app, ["vitality", "--program", "acme"])

    assert result.exit_code == 0
    assert "ADO Vitality: acme" in result.stdout
    assert "OWNER AGGREGATES:" in result.stdout


def test_vitality_cli_supports_json_and_csv(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(vitality, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="deployment_readiness", name="Deployment", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(vitality.gather_helpers, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(vitality, "_load_vitality_items", lambda program, workstreams, as_of: (_sample_items(as_of), 0))
    monkeypatch.setattr(vitality, "load_approved_workiq_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        vitality,
        "detect_leakage",
        lambda *args, **kwargs: LeakageReport(events=(), signal_counts_by_item={}, leakage_counts_by_item={}, owner_leakage_ratios={}),
    )

    json_result = runner.invoke(app, ["vitality", "--program", "acme", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["summary"]["total_items"] == 2
    assert payload["owner_aggregates"][0]["scope_type"] == "owner"
    assert payload["workstream_aggregates"][0]["scope_id"] == "deployment_readiness"
    assert payload["item_scores"][0]["work_item_id"] == 10

    csv_result = runner.invoke(app, ["vitality", "--program", "acme", "--format", "csv"])

    assert csv_result.exit_code == 0
    csv_lines = csv_result.stdout.strip().splitlines()
    assert csv_lines[0].startswith("entry_type,program_id")
    assert csv_lines[1].startswith("summary,acme")
    assert any(line.startswith("owner_aggregate,acme,operator,owner") for line in csv_lines[2:])
    assert any(line.startswith("workstream_aggregate,acme,deployment_readiness,workstream") for line in csv_lines[2:])
    assert any(line.startswith("item_score,acme,,,10,operator,deployment_readiness") for line in csv_lines[2:])


def test_generate_vitality_report_excludes_people_directory_vitality_exemptions(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    (knowledge_root / "people_directory.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'people:\n'
            '  - alias: operator\n'
            '    exempt_from_vitality: true\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vitality, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="deployment_readiness", name="Deployment", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(vitality.gather_helpers, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(vitality, "load_approved_workiq_signals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        vitality,
        "detect_leakage",
        lambda *args, **kwargs: LeakageReport(events=(), signal_counts_by_item={}, leakage_counts_by_item={}, owner_leakage_ratios={}),
    )

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    artifacts = vitality.generate_vitality_report(
        "acme",
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of: (_sample_items(as_of), 0),
    )

    assert artifacts.items == ()
    assert artifacts.scored_items == ()
    assert artifacts.owner_aggregates == ()


def test_generate_vitality_report_reads_sqlite_backed_trajectory_history(monkeypatch, tmp_path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
                "storage_backend": "sqlite",
                "vitality": {
                    "sparse_workiq_threshold": 1,
                    "surfaces": {},
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vitality, "PROGRAMS_ROOT", programs_root)

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (Workstream(id="deployment_readiness", name="Deployment", area_paths=("One\\Adventure\\Acme",)),)
    monkeypatch.setattr(vitality.gather_helpers, "_load_program_context", lambda program_id, programs_root: (program, workstreams))

    SQLiteTrajectoryStore(programs_root=programs_root).append(
        "acme",
        10,
        TrajectoryPoint(
            date=datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc).date(),
            state="Active",
            assigned_to="operator@example.com",
            target_date=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc).date(),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme",
        ),
    )

    monkeypatch.setattr(
        vitality,
        "load_approved_workiq_signals",
        lambda *args, **kwargs: (
            Signal(
                id="workiq-1",
                timestamp=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
                source="workiq/meeting",
                program_id="acme",
                workstream_id="deployment_readiness",
                entity_refs=("WI:10",),
                text="Need to resolve blocker with owner by 2026-05-12.",
                raw_ref="workiq:1",
                confidence=Confidence.HIGH,
                metadata={"entity_link_confidence": "high"},
                thread_id="thread-1",
            ),
        ),
    )

    captured = {}

    def _capture_score_vitality(items, *, as_of, workstream_resolver, leakage, leakage_signal_threshold):
        captured["leakage"] = leakage
        captured["threshold"] = leakage_signal_threshold
        return (
            VitalityScore(
                work_item_id=10,
                owner_alias="operator",
                workstream_id="deployment_readiness",
                freshness_days=28,
                freshness_grade="red",
                richness_score=75,
                richness_missing=("recent_comment",),
                leakage_events=leakage.leakage_counts_by_item.get(10, 0),
                workiq_signal_count=leakage.signal_counts_by_item.get(10, 0),
                composite_score=61,
                suggested_update="Add an owner comment",
            ),
        )

    monkeypatch.setattr(vitality, "score_vitality", _capture_score_vitality)
    monkeypatch.setattr(
        vitality,
        "aggregate_vitality",
        lambda scores, *, scope_type, leakage_signal_threshold: (
            SimpleNamespace(scope_id=("operator" if scope_type == "owner" else "deployment_readiness"), scope_type=scope_type),
        ),
    )

    artifacts = vitality.generate_vitality_report(
        "acme",
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of: (
            (
                WorkItem(
                    id=10,
                    type="Feature",
                    title="Healthy item",
                    state="Active",
                    assigned_to="operator@example.com",
                    assigned_to_email="operator@example.com",
                    area_path="One\\Adventure\\Acme",
                    iteration_path="Sprint 1",
                    target_date=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc).date(),
                    risk_level=RiskLevel.HIGH,
                    tags=["Safety"],
                    custom_fields={"changed_date": (datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc) - timedelta(days=28)).isoformat()},
                    revisions=[],
                    comments=[],
                    fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                ),
            ),
            0,
        ),
    )

    assert len(artifacts.scored_items) == 1
    assert captured["threshold"] == 1
    assert captured["leakage"].signal_counts_by_item == {10: 1}
    assert captured["leakage"].leakage_counts_by_item == {}


def test_load_vitality_items_avoids_raw_area_path_fields(monkeypatch, tmp_path) -> None:
    """_load_vitality_items uses query_work_items_batch (not query_all) so
    area_path comes from the batch API's System.AreaPath field, not from an
    OData projection that could return truncated paths."""
    from types import SimpleNamespace

    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    registry_path = programs_root / "acme" / "channel_registry.sqlite3"
    registry_path.parent.mkdir()
    # Create an empty file so the existence check passes
    registry_path.touch()

    class FakeRegistration:
        ref_id = "10"

    class FakeStore:
        def __init__(self, *args, **kwargs):
            pass

        def pullable_registrations(self, channel, **kwargs):
            return (FakeRegistration(),)

    class FakeClient:
        def __init__(self, organization: str, project: str, timeout: int) -> None:
            self.organization = organization
            self.project = project
            self.timeout = timeout

        def query_work_items_batch(self, ids, fields):
            del fields
            return [{
                "id": ids[0],
                "fields": {
                    "System.Id": ids[0],
                    "System.AreaPath": "One\\Adventure\\Acme",
                    "System.IterationPath": "Sprint 1",
                    "Microsoft.VSTS.Scheduling.TargetDate": "2026-05-20",
                    "System.AssignedTo": {"displayName": "Vertex Maintainer", "uniqueName": "operator@example.com"},
                    "System.CreatedDate": datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat(),
                    "System.ChangedDate": datetime(2026, 5, 8, tzinfo=timezone.utc).isoformat(),
                    "System.Description": "Detailed description",
                },
            }]

        def list_work_item_comments(self, work_item_id: int):
            del work_item_id
            return []

        def list_work_item_revisions(self, work_item_id: int):
            del work_item_id
            return []

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        ado=ADOConfig(
            organization="contoso",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )

    monkeypatch.setattr(vitality, "ADOClient", FakeClient)
    monkeypatch.setattr(vitality, "ChannelRegistryStore", FakeStore)
    monkeypatch.setattr(vitality, "PROGRAMS_ROOT", programs_root)

    items, ado_calls = vitality._load_vitality_items(program, (), datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc))

    assert len(items) == 1
    assert items[0].area_path == "One\\Adventure\\Acme"
    assert items[0].iteration_path == "Sprint 1"
    assert str(items[0].target_date) == "2026-05-20"
    assert ado_calls == 3  # 1 batch + 2 per-item (comments + revisions)


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=10,
            type="Feature",
            title="Healthy item",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="operator@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=(as_of + timedelta(days=10)).date(),
            risk_level=RiskLevel.HIGH,
            tags=["Safety"],
            custom_fields={"changed_date": (as_of - timedelta(days=2)).isoformat(), "description": "Update owner and confirm mitigation by 2026-05-20 with the team blocker cleared."},
            revisions=[
                Revision(
                    work_item_id=10,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="operator@example.com",
                    changed_date=as_of - timedelta(days=2),
                    fields_changed={"System.State": ("Proposed", "Active")},
                )
            ],
            comments=[
                Comment(
                    work_item_id=10,
                    comment_id=1,
                    created_by="Vertex Maintainer",
                    created_by_email="operator@example.com",
                    created_date=as_of - timedelta(days=1),
                    text="Update owner status and confirm follow up by 2026-05-20.",
                )
            ],
            fetched_at=as_of,
        ),
        WorkItem(
            id=11,
            type="Feature",
            title="Stale item",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="operator@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=(as_of + timedelta(days=4)).date(),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": (as_of - timedelta(days=18)).isoformat(), "description": "short"},
            revisions=[
                Revision(
                    work_item_id=11,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="operator@example.com",
                    changed_date=as_of - timedelta(days=18),
                    fields_changed={"System.State": ("Proposed", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )