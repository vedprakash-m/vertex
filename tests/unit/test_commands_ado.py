from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import ado
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, read_proposal_manifest, write_proposal_manifest
from src.core.ado_reconcile import ADOReconcileDiscrepancy, ADOReconcileReport
from src.core.ado_status import ADOStatusReport, AreaCoverageRow, GatherStatus, OrphanedItem
from src.core.analytics_store import load_autonomy_audit_records
from src.core.archive_store import write_confirmed_issue
from src.core.coverage_gap import CoverageGap
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence, ConfirmedDimension, EditionType, RiskLevel, RunManifest, Snapshot, SnapshotItem, WorkItem
from src.core.models_v2 import ADOConfig, Program, Signal, SignalReviewDecision, TrajectoryPoint, VitalityAggregate, VitalityScore, Workstream
from src.core.overrides_store import OverridesDocument
from src.core.program_fact_store import load_program_facts
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.trajectory import append_trajectory_point
from src.m365.ado_writer import ADOApplyArtifacts


runner = CliRunner()


class _FakeRepoDiscoveryClient:
    def __init__(self, repos: list[dict[str, object]], pr_counts: dict[str, int]) -> None:
        self._repos = repos
        self._pr_counts = pr_counts

    def list_repositories(self) -> list[dict[str, object]]:
        return list(self._repos)

    def list_pull_requests(self, repository_id: str, *, status: str = "active", top: int = 100) -> list[dict[str, object]]:
        return [{}] * self._pr_counts.get(repository_id, 0)


def test_generate_ado_status_reports_coverage_orphans_and_gaps(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    (program_dir / "narratives" / "issue_003").mkdir(parents=True)

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    gather_time = datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="approved-signal",
            timestamp=gather_time,
            source="ado/revision",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1001",),
            text="Covered item updated.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=gather_time,
    )
    append_review_decision(
        "demo",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=gather_time,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "demo",
        1001,
        TrajectoryPoint(
            date=gather_time.date(),
            state="Active",
            assigned_to="owner@example.com",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Demo\\WS",
        ),
        programs_root=programs_root,
    )

    program = Program(
        schema_version="2.0",
        id="demo",
        name="Demo Program",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo\\WS", "One\\Demo\\Orphan"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="ws_demo",
            name="Demo WS",
            area_paths=("One\\Demo\\WS",),
            ado_saved_query_ids=("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"),
            dri_email="owner@example.com",
        ),
    )
    items = (
        _work_item(1001, "Covered item", "One\\Demo\\WS", as_of),
        _work_item(1002, "Gap item", "One\\Demo\\WS", as_of),
        _work_item(1003, "Orphan item", "One\\Demo\\Orphan", as_of),
    )

    artifacts = ado.generate_ado_status(
        "demo",
        as_of=as_of,
        programs_root=programs_root,
        program_loader=lambda program_id, root: (program, workstreams),
        item_loader=lambda client, loaded_program, timestamp: (items, 1),
        area_scope_loader=lambda area_path: (f"{area_path}\\Child",) if area_path == "One\\Demo\\WS" else (),
    )

    assert artifacts.exit_code == 0
    assert artifacts.report.total_active_items == 3
    assert len(artifacts.report.area_coverage) == 1
    assert artifacts.report.area_coverage[0].area_path == "One\\Demo\\WS"


def test_discover_ado_repository_candidates_scores_repo_matches_by_workstream() -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme Program",
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
    workstreams = (
        Workstream(id="acme", name="Adventure on Northwind", aliases=("adventure-pf", "nmagent"), signal_sources=None),
        Workstream(id="dd_on_pf", name="Direct Drive on Northwind", aliases=("contoso",), signal_sources=None),
    )
    repos = [
        {"id": "repo-adventure", "name": "Storage-Adventure"},
        {"id": "repo-dd", "name": "Storage-DirectDrive"},
        {"id": "repo-nm", "name": "Networking-NMAgent"},
        {"id": "repo-random", "name": "Unrelated-Repo"},
    ]
    client = _FakeRepoDiscoveryClient(repos=repos, pr_counts={"repo-adventure": 4, "repo-dd": 3, "repo-nm": 2})

    candidates = ado.discover_ado_repository_candidates(
        "acme",
        program_loader=lambda program_id, root: (program, workstreams),
        client_factory=lambda loaded_program: client,
    )

    nova_candidates = [candidate for candidate in candidates if candidate.workstream_id == "acme"]
    dd_candidates = [candidate for candidate in candidates if candidate.workstream_id == "dd_on_pf"]

    assert any(candidate.repository_name == "Storage-Adventure" for candidate in nova_candidates)
    assert any(candidate.repository_name == "Networking-NMAgent" for candidate in nova_candidates)
    assert any(candidate.repository_name == "Storage-DirectDrive" for candidate in dd_candidates)
    assert all(candidate.repository_name != "Unrelated-Repo" for candidate in candidates)


def test_ado_discover_repos_command_supports_json_output(monkeypatch, tmp_path: Path) -> None:
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme Program",
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
    workstreams = (
        Workstream(id="acme", name="Adventure on Northwind", aliases=("adventure-pf",), signal_sources=None),
    )
    repos = [{"id": "repo-adventure", "name": "Storage-Adventure"}]
    client = _FakeRepoDiscoveryClient(repos=repos, pr_counts={"repo-adventure": 2})

    monkeypatch.setattr(
        ado,
        "discover_ado_repository_candidates",
        lambda program_id, workstream_id=None, programs_root=None, program_loader=None, client_factory=None: (
            ado.ADORepositoryCandidate(
                workstream_id="acme",
                repository_id="repo-adventure",
                repository_name="Storage-Adventure",
                score=25,
                matched_terms=("adventure",),
                active_pr_count=2,
            ),
        ),
    )

    result = runner.invoke(app, ["ado", "discover-repos", "--program", "acme", "--format", "json"])

    assert result.exit_code == 0
    assert '"repository_name": "Storage-Adventure"' in result.stdout
    assert '"workstream_id": "acme"' in result.stdout


def test_set_ado_repository_ids_updates_workstreams_yaml_from_repository_names(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Adventure on Northwind",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme Program",
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
    workstreams = (
        Workstream(id="acme", name="Adventure on Northwind", aliases=("adventure-pf",), signal_sources=None),
    )
    repos = [
        {"id": "repo-adventure", "name": "Storage-Adventure"},
        {"id": "repo-nmagent", "name": "Networking-NMAgent"},
    ]
    client = _FakeRepoDiscoveryClient(repos=repos, pr_counts={})

    updated_ids = ado.set_ado_repository_ids(
        "acme",
        workstream_id="acme",
        repository_names=("Storage-Adventure", "Networking-NMAgent"),
        programs_root=programs_root,
        program_loader=lambda program_id, root: (program, workstreams),
        client_factory=lambda loaded_program: client,
    )

    payload = yaml.safe_load((program_dir / "workstreams.yaml").read_text(encoding="utf-8"))

    assert updated_ids == ("repo-adventure", "repo-nmagent")
    assert payload["workstreams"][0]["ado_repository_ids"] == ["repo-adventure", "repo-nmagent"]


def test_ado_set_repos_command_updates_workstreams_yaml(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Adventure on Northwind",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme Program",
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
    workstreams = (
        Workstream(id="acme", name="Adventure on Northwind", aliases=("adventure-pf",), signal_sources=None),
    )
    repos = [{"id": "repo-adventure", "name": "Storage-Adventure"}]
    client = _FakeRepoDiscoveryClient(repos=repos, pr_counts={})

    monkeypatch.setattr("src.commands.ado.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        ado.gather_command_helpers,
        "_load_program_context",
        lambda program_id, root: (program, workstreams),
    )
    monkeypatch.setattr(ado, "_build_ado_client", lambda loaded_program: client)

    result = runner.invoke(
        app,
        [
            "ado",
            "set-repos",
            "--program",
            "acme",
            "--workstream",
            "acme",
            "--repository-name",
            "Storage-Adventure",
        ],
    )

    payload = yaml.safe_load((program_dir / "workstreams.yaml").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert payload["workstreams"][0]["ado_repository_ids"] == ["repo-adventure"]


def test_set_ado_repository_ids_writes_through_canonical_seam_with_fact_projection(
    tmp_path: Path,
) -> None:
    """rev. 324 — the inline workstreams.yaml mutation is now delegated to
    ``save_workstreams_document`` so the change also updates the
    ``workstream.entry`` fact projection.  This test pins that contract:
    calling ``set_ado_repository_ids`` lands the new repository IDs in
    the YAML AND in the Fact Store's ``workstream.entry`` revisions.

    Spec §11.3 Phase 7 D-24 P2 program-literals de-coupling: the previous
    direct write at ``commands/ado.py:524-545`` was the only remaining
    inline ``workstreams.yaml`` mutation, and skipping the canonical seam
    meant ``ado_repository_ids`` was invisible to fact-store readers
    (``load_current_workstreams`` returns a stale view).
    """
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Adventure on Northwind",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme Program",
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
    workstreams = (
        Workstream(id="acme", name="Adventure on Northwind", aliases=("adventure-pf",), signal_sources=None),
    )
    repos = [{"id": "repo-adventure", "name": "Storage-Adventure"}]
    client = _FakeRepoDiscoveryClient(repos=repos, pr_counts={})

    updated_ids = ado.set_ado_repository_ids(
        "acme",
        workstream_id="acme",
        repository_names=("Storage-Adventure",),
        programs_root=programs_root,
        program_loader=lambda program_id, root: (program, workstreams),
        client_factory=lambda loaded_program: client,
    )

    assert updated_ids == ("repo-adventure",)

    # Canonical seam: the YAML must be updated AND the fact-store projection
    # must reflect the new ado_repository_ids (this is the contract pinned by
    # this test — without the save_workstreams_document delegate, the fact
    # store would be stale and ``load_current_workstreams`` would return the
    # pre-call view).
    payload = yaml.safe_load((program_dir / "workstreams.yaml").read_text(encoding="utf-8"))
    assert payload["workstreams"][0]["ado_repository_ids"] == ["repo-adventure"]

    snapshot = load_program_facts(
        "acme",
        fact_types=("workstream.entry",),
        programs_root=programs_root,
    )
    workstream_facts = [fact for fact in snapshot.facts if fact.fact_type == "workstream.entry"]
    assert len(workstream_facts) == 1
    assert workstream_facts[0].payload["ado_repository_ids"] == ["repo-adventure"]


def test_generate_ado_status_reads_sqlite_backed_signals_and_trajectories(tmp_path: Path) -> None:
    editions_root, programs_root, _archive_root, _output_root = _seed_demo_program_layout(tmp_path)
    program_dir = programs_root / "demo"
    program_document = (program_dir / "program.yaml").read_text(encoding="utf-8")
    (program_dir / "program.yaml").write_text(program_document + "\nstorage_backend: sqlite\n", encoding="utf-8")
    (program_dir / "narratives" / "issue_003").mkdir(parents=True)

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    gather_time = datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc)
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    signal_store.append(
        Signal(
            id="approved-signal",
            timestamp=gather_time,
            source="ado/revision",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1001",),
            text="Covered item updated.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        )
    )
    signal_store.append_review(
        "demo",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=gather_time,
            reviewed_by="system",
            note=None,
        ),
    )
    SQLiteTrajectoryStore(programs_root=programs_root).append(
        "demo",
        1001,
        TrajectoryPoint(
            date=gather_time.date(),
            state="Active",
            assigned_to="owner@example.com",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Demo\\WS",
        ),
    )

    program = Program(
        schema_version="2.0",
        id="demo",
        name="Demo Program",
        storage_backend="sqlite",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo\\WS", "One\\Demo\\Orphan"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="ws_demo",
            name="Demo WS",
            area_paths=("One\\Demo\\WS",),
            ado_saved_query_ids=("11111111-1111-1111-1111-111111111111",),
            dri_email="owner@example.com",
        ),
    )
    items = (
        _work_item(1001, "Covered item", "One\\Demo\\WS", as_of),
        _work_item(1002, "Gap item", "One\\Demo\\WS", as_of),
    )

    artifacts = ado.generate_ado_status(
        "demo",
        as_of=as_of,
        programs_root=programs_root,
        program_loader=lambda program_id, root: (program, workstreams),
        item_loader=lambda client, loaded_program, timestamp: (items, 1),
        area_scope_loader=lambda area_path: (),
    )

    assert artifacts.report.coverage_gaps[0].work_item_id == 1002
    assert artifacts.report.coverage_gaps[0].confidence is Confidence.HIGH
    assert artifacts.report.last_gather is not None
    assert artifacts.report.last_gather.signal_count == 1
    assert artifacts.report.last_gather.trajectory_updates == 1


def test_load_approved_signals_reads_sqlite_backed_reviews(tmp_path: Path) -> None:
    _editions_root, programs_root, _archive_root, _ = _seed_demo_program_layout(tmp_path)
    program_dir = programs_root / "demo"
    program_document = (program_dir / "program.yaml").read_text(encoding="utf-8")
    (program_dir / "program.yaml").write_text(program_document + "\nstorage_backend: sqlite\n", encoding="utf-8")

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    approved_at = datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc)
    signal_store.append(
        Signal(
            id="approved-signal",
            timestamp=approved_at,
            source="ado/revision",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1001",),
            text="Approved signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        )
    )
    signal_store.append_review(
        "demo",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=approved_at,
            reviewed_by="system",
            note=None,
        ),
    )
    signal_store.append(
        Signal(
            id="pending-signal",
            timestamp=approved_at,
            source="ado/revision",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1002",),
            text="Pending signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        )
    )

    signals = ado._load_approved_signals(
        "demo",
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        window_days=14,
        programs_root=programs_root,
    )

    assert [signal.id for signal in signals] == ["approved-signal"]


def test_recent_ado_update_item_ids_reads_sqlite_backed_signals(tmp_path: Path) -> None:
    _editions_root, programs_root, _archive_root, _ = _seed_demo_program_layout(tmp_path)
    program_dir = programs_root / "demo"
    program_document = (program_dir / "program.yaml").read_text(encoding="utf-8")
    (program_dir / "program.yaml").write_text(program_document + "\nstorage_backend: sqlite\n", encoding="utf-8")

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    created_at = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    signal_store.append(
        Signal(
            id="ado-update-1",
            timestamp=created_at,
            source="vertex/ado_update",
            program_id="demo",
            workstream_id=None,
            entity_refs=("WI:1001",),
            text="Nudge posted.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata={"update_type": "vitality_nudge", "work_item_id": 1001},
            thread_id=None,
        )
    )
    signal_store.append(
        Signal(
            id="ado-update-2",
            timestamp=created_at,
            source="vertex/ado_update",
            program_id="demo",
            workstream_id=None,
            entity_refs=("WI:1002",),
            text="Different update type.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata={"update_type": "vitality_tag", "work_item_id": 1002},
            thread_id=None,
        )
    )

    item_ids = ado._recent_ado_update_item_ids(
        "demo",
        update_type="vitality_nudge",
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        lookback_days=7,
        programs_root=programs_root,
    )

    assert item_ids == {1001}


def test_ado_status_cli_renders_report(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "generate_ado_status",
        lambda program_id: ado.ADOStatusArtifacts(
            report=ADOStatusReport(
                program_id=program_id,
                organization="your-org",
                project="One",
                date_window_days=14,
                total_active_items=3,
                area_coverage=(
                    AreaCoverageRow(
                        area_path="One\\Demo\\WS",
                        workstream_id="ws_demo",
                        workstream_name="Demo WS",
                        active_item_count=2,
                        analytics_matches=("One\\Demo\\WS\\Child",),
                    ),
                ),
                unmapped_area_paths=("One\\Demo\\Orphan",),
                orphaned_items=(
                    OrphanedItem(
                        work_item_id=1003,
                        title="Orphan item",
                        state="Active",
                        area_path="One\\Demo\\Orphan",
                        assigned_to="owner@example.com",
                    ),
                ),
                coverage_gaps=(
                    CoverageGap(
                        work_item_id=1002,
                        title="Gap item",
                        state="Active",
                        assigned_to="owner@example.com",
                        confidence=Confidence.HIGH,
                    ),
                ),
                last_gather=GatherStatus(
                    captured_at=datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc),
                    signal_count=2,
                    trajectory_updates=1,
                ),
                saved_query_count=2,
            ),
            exit_code=0,
            ado_calls=1,
        ),
    )

    result = runner.invoke(app, ["ado", "status", "--program", "demo"])

    assert result.exit_code == 0
    assert "Program: demo" in result.stdout
    assert "Area Path Coverage:" in result.stdout
    assert "Orphaned Items:" in result.stdout
    assert "Coverage Gaps:" in result.stdout
    assert "WI:1002 - Gap item (Active; high confidence)" in result.stdout
    assert "Last Gather:" in result.stdout
    assert "Saved Queries: 2 configured" in result.stdout


def test_ado_status_cli_supports_json_and_csv(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "generate_ado_status",
        lambda program_id: ado.ADOStatusArtifacts(
            report=ADOStatusReport(
                program_id=program_id,
                organization="your-org",
                project="One",
                date_window_days=14,
                total_active_items=3,
                area_coverage=(
                    AreaCoverageRow(
                        area_path="One\\Demo\\WS",
                        workstream_id="ws_demo",
                        workstream_name="Demo WS",
                        active_item_count=2,
                        analytics_matches=("One\\Demo\\WS\\Child",),
                    ),
                ),
                unmapped_area_paths=("One\\Demo\\Orphan",),
                orphaned_items=(
                    OrphanedItem(
                        work_item_id=1003,
                        title="Orphan item",
                        state="Active",
                        area_path="One\\Demo\\Orphan",
                        assigned_to="owner@example.com",
                    ),
                ),
                coverage_gaps=(
                    CoverageGap(
                        work_item_id=1002,
                        title="Gap item",
                        state="Active",
                        assigned_to="owner@example.com",
                        confidence=Confidence.HIGH,
                    ),
                ),
                last_gather=GatherStatus(
                    captured_at=datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc),
                    signal_count=2,
                    trajectory_updates=1,
                ),
                saved_query_count=2,
            ),
            exit_code=0,
            ado_calls=1,
        ),
    )

    json_result = runner.invoke(app, ["ado", "status", "--program", "demo", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "demo"
    assert payload["ado_calls"] == 1
    assert payload["counts"]["total_active_items"] == 3
    assert payload["area_coverage"][0]["workstream_id"] == "ws_demo"
    assert payload["coverage_gaps"][0]["confidence"] == "high"
    assert payload["last_gather"]["signal_count"] == 2

    csv_result = runner.invoke(app, ["ado", "status", "--program", "demo", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "entry_type,program_id,ado_calls,workstream_id,ref_id,title,state,area_path,assigned_to,detail"
    assert any("summary,demo,1," in line for line in lines[1:])
    assert any("area_coverage,demo,1,ws_demo,,Demo WS,,One\\Demo\\WS,," in line for line in lines[1:])
    assert any(
        line.startswith("coverage_gap,demo,1,,1002,Gap item,Active,,owner@example.com,")
        and '""high""' in line
        for line in lines[1:]
    )


def test_ado_reconcile_cli_supports_json_and_csv(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "generate_ado_reconcile",
        lambda program_id: ado.ADOReconcileArtifacts(
            report=ADOReconcileReport(
                program_id=program_id,
                override_issue_number=7,
                discrepancies=(
                    ADOReconcileDiscrepancy(
                        kind="override_risk",
                        work_item_id=1001,
                        context="Demo Scorecard / Delivery",
                        vertex_value="high",
                        ado_value="medium",
                        note="stale override?",
                    ),
                ),
            ),
            ado_calls=1,
        ),
    )

    json_result = runner.invoke(app, ["ado", "reconcile", "--program", "demo", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "demo"
    assert payload["ado_calls"] == 1
    assert payload["override_issue_number"] == 7
    assert payload["discrepancies"][0]["kind"] == "override_risk"

    csv_result = runner.invoke(app, ["ado", "reconcile", "--program", "demo", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "program_id,ado_calls,override_issue_number,kind,work_item_id,context,vertex_value,ado_value,note"
    assert any("demo,1,7,override_risk,1001,Demo Scorecard / Delivery,high,medium,stale override?" == line for line in lines[1:])


def test_ado_propose_cli_writes_comment_manifest(tmp_path: Path, monkeypatch) -> None:
    editions_root, programs_root, archive_root, _ = _seed_demo_program_layout(tmp_path)
    _seed_confirmed_issue(archive_root)
    fake_client = _ProposalADOClient()

    monkeypatch.setattr(ado, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)

    result = runner.invoke(
        app,
        [
            "ado",
            "propose",
            "--program",
            "demo",
            "--edition",
            "demo_weekly",
            "--type",
            "comment",
            "--issue",
            "7",
        ],
    )

    manifest_paths = list((tmp_path / "programs" / "demo" / "publications").glob("demo_weekly/ado_proposals/*.json"))
    assert result.exit_code == 0
    assert len(manifest_paths) == 1
    proposal, proposal_status = read_proposal_manifest(manifest_paths[0])
    assert proposal_status == "pending"
    assert proposal.program_id == "demo"
    assert proposal.edition_id == "demo_weekly"
    assert len(proposal.entries) == 2
    assert proposal.expires_at == proposal.created_at + timedelta(hours=36)
    assert [entry.revision_id for entry in proposal.entries] == [11, 12]
    assert "Proposal" in result.stdout
    assert "Manifest:" in result.stdout


def test_ado_apply_cli_resolves_manifest_and_delegates_to_writer(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    proposal = ADOUpdateProposal(
        id="prop-demo",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=datetime(2026, 5, 13, 16, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 5, 16, 16, 0, tzinfo=timezone.utc),
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
    )
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    fake_client = _ProposalADOClient()
    fake_writer = _RecordingWriter()

    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)
    monkeypatch.setattr(ado.gather_command_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(ado, "ADOWriter", lambda client, programs_root: fake_writer)
    monkeypatch.setattr(ado.getpass, "getuser", lambda: "tester")

    result = runner.invoke(app, ["ado", "apply", "--proposal", "prop-demo", "--yes"])

    assert result.exit_code == 0
    assert fake_writer.applied_manifest_path == manifest_path
    assert "Applied proposal prop-demo: 1 applied, 0 skipped, 0 conflict, 0 failed." in result.stdout
    assert str(manifest_path) in result.stdout
    records = load_autonomy_audit_records("demo", programs_root=programs_root)
    assert len(records) == 1
    assert records[0].author_alias == "tester"
    assert records[0].action_type == "comment"
    assert records[0].policy_rule is None
    assert records[0].accepted is True
    assert records[0].evidence_refs == (
        "ado_proposal:prop-demo",
        "edition:demo_weekly",
        "confirmed_issue:7",
        "WI:1001",
    )


def test_ado_apply_cli_uses_promoted_batch_approval_rule_without_prompt(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    proposal = ADOUpdateProposal(
        id="prop-demo",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=datetime(2026, 5, 13, 16, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 5, 16, 16, 0, tzinfo=timezone.utc),
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
    )
    write_proposal_manifest(proposal, programs_root=programs_root)
    _seed_signal_approval_rules(programs_root, action_type="comment", rule_id="approval:comment")
    fake_client = _ProposalADOClient()
    fake_writer = _RecordingWriter()

    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)
    monkeypatch.setattr(ado.gather_command_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(ado, "ADOWriter", lambda client, programs_root: fake_writer)
    monkeypatch.setattr(ado.getpass, "getuser", lambda: "tester")

    result = runner.invoke(app, ["ado", "apply", "--proposal", "prop-demo"])

    assert result.exit_code == 0
    assert "Using promoted batch approval rule approval:comment for comment." in result.stdout
    assert "Apply 1 update(s) to ADO from proposal prop-demo?" not in result.stdout
    records = load_autonomy_audit_records("demo", programs_root=programs_root)
    assert len(records) == 1
    assert records[0].policy_rule == "approval:comment"
    assert records[0].action_type == "comment"
    assert records[0].level == "l3"


def test_ado_apply_cli_records_declined_review_without_writer_call(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    proposal = ADOUpdateProposal(
        id="prop-demo",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        update_type="comment",
        created_at=datetime(2026, 5, 13, 16, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 5, 16, 16, 0, tzinfo=timezone.utc),
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
    )
    write_proposal_manifest(proposal, programs_root=programs_root)
    fake_client = _ProposalADOClient()
    fake_writer = _RecordingWriter()

    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)
    monkeypatch.setattr(ado.gather_command_helpers, "_load_program_context", lambda program_id, root: (_demo_program(), ()))
    monkeypatch.setattr(ado, "ADOWriter", lambda client, programs_root: fake_writer)
    monkeypatch.setattr(ado.getpass, "getuser", lambda: "tester")

    result = runner.invoke(app, ["ado", "apply", "--proposal", "prop-demo"], input="n\n")

    assert result.exit_code == 1
    assert fake_writer.applied_manifest_path is None
    records = load_autonomy_audit_records("demo", programs_root=programs_root)
    assert len(records) == 1
    assert records[0].accepted is False
    assert records[0].action_type == "comment"
    assert records[0].level == "l2"
    assert records[0].rollback_mechanism == "No rollback needed; proposal was not applied."


def test_ado_reconcile_cli_renders_discrepancies(monkeypatch) -> None:
    monkeypatch.setattr(
        ado,
        "generate_ado_reconcile",
        lambda program_id: ado.ADOReconcileArtifacts(
            report=ADOReconcileReport(
                program_id=program_id,
                override_issue_number=7,
                discrepancies=(
                    ADOReconcileDiscrepancy(
                        kind="override_risk",
                        work_item_id=1001,
                        context="Demo Scorecard / Delivery",
                        vertex_value="high",
                        ado_value="medium",
                        note="stale override?",
                    ),
                ),
            ),
            ado_calls=1,
        ),
    )

    result = runner.invoke(app, ["ado", "reconcile", "--program", "demo"])

    assert result.exit_code == 0
    assert "Reconciliation: demo | 1 discrepancies found" in result.stdout
    assert "Overrides issue: 7" in result.stdout
    assert "Vertex override (Demo Scorecard / Delivery): high | ADO risk: medium" in result.stdout


def test_ado_propose_cli_writes_vitality_nudge_manifest(tmp_path: Path, monkeypatch) -> None:
    editions_root, programs_root, _archive_root, _ = _seed_demo_program_layout(tmp_path)
    fake_client = _ProposalADOClient()

    monkeypatch.setattr(ado, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)
    monkeypatch.setattr(
        ado,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root: _vitality_artifacts(as_of),
    )
    monkeypatch.setattr(
        ado,
        "_recent_ado_update_item_ids",
        lambda *args, **kwargs: set(),
    )

    result = runner.invoke(app, ["ado", "propose", "--program", "demo", "--type", "vitality_nudge"])

    manifest_paths = list((tmp_path / "programs" / "demo" / "publications").glob("demo_weekly/ado_proposals/*.json"))
    assert result.exit_code == 0
    assert len(manifest_paths) == 1
    proposal, proposal_status = read_proposal_manifest(manifest_paths[0])
    assert proposal_status == "pending"
    assert proposal.update_type == "vitality_nudge"
    assert proposal.expires_at == proposal.created_at + timedelta(hours=36)
    assert [entry.work_item_id for entry in proposal.entries] == [1001]
    assert "Recent non-ADO activity was detected for this item." in proposal.entries[0].proposed_value


def test_ado_propose_cli_skips_recent_vertex_ado_nudge_targets(tmp_path: Path, monkeypatch) -> None:
    editions_root, programs_root, _archive_root, _ = _seed_demo_program_layout(tmp_path)
    fake_client = _ProposalADOClient()

    monkeypatch.setattr(ado, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)
    monkeypatch.setattr(
        ado,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root: _vitality_artifacts(as_of),
    )
    monkeypatch.setattr(
        ado,
        "_recent_ado_update_item_ids",
        lambda *args, **kwargs: {1001},
    )

    result = runner.invoke(app, ["ado", "propose", "--program", "demo", "--type", "vitality_nudge"])

    manifest_paths = list((tmp_path / "programs" / "demo" / "publications").glob("demo_weekly/ado_proposals/*.json"))
    assert result.exit_code == 0
    assert len(manifest_paths) == 1
    proposal, proposal_status = read_proposal_manifest(manifest_paths[0])
    assert proposal_status == "pending"
    assert proposal.update_type == "vitality_nudge"
    assert proposal.entries == ()


def test_ado_propose_cli_writes_vitality_tag_manifest(tmp_path: Path, monkeypatch) -> None:
    editions_root, programs_root, _archive_root, _ = _seed_demo_program_layout(tmp_path)
    fake_client = _ProposalADOClient()

    monkeypatch.setattr(ado, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)
    monkeypatch.setattr(
        ado,
        "generate_vitality_report",
        lambda program_id, as_of, programs_root: _vitality_artifacts(as_of),
    )
    monkeypatch.setattr(ado, "_load_approved_signals", lambda *args, **kwargs: ())

    result = runner.invoke(app, ["ado", "propose", "--program", "demo", "--type", "vitality_tag"])

    manifest_paths = list((tmp_path / "programs" / "demo" / "publications").glob("demo_weekly/ado_proposals/*.json"))
    assert result.exit_code == 0
    assert len(manifest_paths) == 1
    proposal, proposal_status = read_proposal_manifest(manifest_paths[0])
    assert proposal_status == "pending"
    assert proposal.update_type == "vitality_tag"
    assert [(entry.work_item_id, entry.action) for entry in proposal.entries] == [(1001, "add_tag"), (1002, "remove_tag")]


def test_ado_propose_cli_writes_field_manifest(tmp_path: Path, monkeypatch) -> None:
    editions_root, programs_root, _archive_root, _ = _seed_demo_program_layout(tmp_path)
    fake_client = _ProposalADOClient()
    overrides_dir = programs_root / "demo" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "issue_008.yaml").write_text(
        "\n".join(
            [
                "issue_number: 8",
                "scorecards:",
                '  Demo Scorecard:',
                '    Execution:',
                '      risk: high',
            ]
        ),
        encoding="utf-8",
    )
    (programs_root / "demo" / "ado_field_map.yaml").write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "proposal_ttl_hours: 48",
                "mappings:",
                "  - vertex_field: risk_level",
                "    ado_field: Custom.RiskLevel",
                "    direction: vertex_to_ado",
                "    auto_propose: false",
                "  - vertex_field: workstream_id",
                "    ado_field: Custom.Workstream",
                "    direction: vertex_to_ado",
                "    auto_propose: false",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ado, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(ado, "ADOClient", lambda **_: fake_client)

    result = runner.invoke(app, ["ado", "propose", "--program", "demo", "--type", "field"])

    manifest_paths = list((tmp_path / "programs" / "demo" / "publications").glob("demo_weekly/ado_proposals/*.json"))
    assert result.exit_code == 0
    assert len(manifest_paths) == 1
    proposal, proposal_status = read_proposal_manifest(manifest_paths[0])
    assert proposal_status == "pending"
    assert proposal.update_type == "field"
    assert proposal.expires_at == proposal.created_at + timedelta(hours=48)
    assert [(entry.work_item_id, entry.field_or_tag, entry.proposed_value) for entry in proposal.entries] == [
        (1001, "Custom.RiskLevel", "high"),
        (1001, "Custom.Workstream", "ws_demo"),
    ]
    assert fake_client.calls[0]["kind"] == "query_all"
    assert fake_client.calls[1]["kind"] == "query_work_items_batch"
    assert fake_client.calls[2]["kind"] == "query_work_items_batch"


def _work_item(work_item_id: int, title: str, area_path: str, as_of: datetime) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path=area_path,
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={"changed_date": "2026-04-01T00:00:00+00:00"},
        revisions=[],
        comments=[],
        fetched_at=as_of,
    )


def _seed_demo_program_layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    program_dir = programs_root / "demo"
    editions_root.mkdir(parents=True)
    (program_dir / "editions").mkdir(parents=True)
    (program_dir / "editions" / "demo_weekly.yaml").write_text(
        "\n".join(
            [
                'schema_version: "2.0"',
                "id: demo_weekly",
                "program_id: demo",
                'name: "Demo Weekly"',
                "type: detailed",
                "altitude: helicopter",
                "cadence: weekly",
            ]
        ),
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                'schema_version: "2.0"',
                "id: demo",
                'name: "Demo Program"',
                "ado:",
                "  organization: your-org",
                "  project: One",
                "  area_paths:",
                "    - 'One\\Demo\\WS'",
                "  work_item_types:",
                '    - "Feature"',
                "  excluded_states:",
                '    - "Removed"',
                "  date_window_days: 14",
                "  api_timeout_seconds: 30",
                "  proposal_ttl_hours: 36",
                "vitality:",
                "  surfaces:",
                "    ado_nudge_comments: true",
                "    ado_tags: true",
                "  nudge_composite_threshold: 40",
                "  nudge_stale_days: 14",
                "  nudge_cooldown_days: 14",
                "  tag_consecutive_gaps: 2",
                '  vitality_tag_name: "Needs-PM-Review"',
            ]
        ),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        "\n".join(
            [
                "workstreams:",
                "  - id: ws_demo",
                '    name: "Demo Workstream"',
                "    area_paths:",
                "      - 'One\\Demo\\WS'",
            ]
        ),
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text(
        "\n".join(
            [
                "scorecards:",
                '  - name: "Demo Scorecard"',
                "    dimensions:",
                '      - name: "Execution"',
                "        workstream_id: ws_demo",
            ]
        ),
        encoding="utf-8",
    )
    return editions_root, programs_root, archive_root, (tmp_path / "programs" / "demo" / "publications")


def _seed_confirmed_issue(archive_root: Path) -> None:
    generated_at = datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)
    snapshot = Snapshot(
        issue_number=7,
        generated_at=generated_at,
        ado_data_as_of=generated_at,
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1001,
                type="Feature",
                title="Tracked item 1",
                state="Active",
                assigned_to="owner@example.com",
                area_path="One\\Demo\\WS",
                target_date=date(2026, 6, 1),
                risk_level=RiskLevel.HIGH,
                tags=["demo"],
            ),
            SnapshotItem(
                id=1002,
                type="Feature",
                title="Tracked item 2",
                state="Active",
                assigned_to="owner@example.com",
                area_path="One\\Demo\\WS",
                target_date=date(2026, 6, 2),
                risk_level=RiskLevel.MEDIUM,
                tags=["demo"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo Scorecard",
                name="Execution",
                risk=RiskLevel.HIGH,
                prior_risk=None,
                item_count=2,
                ado_query_url="https://dev.azure.com/query",
            ),
        ),
    )
    manifest = RunManifest(
        manifest_id="manifest-7",
        issue_number=7,
        edition="demo_weekly",
        started_at=generated_at,
        ended_at=generated_at,
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=0,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={},
        git_sha="abc1234",
    )
    write_confirmed_issue(
        edition="demo_weekly",
        issue_number=7,
        snapshot=snapshot,
        html_body="<html><body>Issue 007</body></html>",
        markdown_body="# Issue 007",
        manifest=manifest,
        archive_root=archive_root,
    )


def _seed_signal_approval_rules(programs_root: Path, *, action_type: str, rule_id: str) -> None:
    path = programs_root / "demo" / "_feedback" / "signal_approval_rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                'updated_at: "2026-05-20T00:00:00+00:00"',
                "proposals: []",
                "rules:",
                f"  - rule_id: {rule_id}",
                f"    action_type: {action_type}",
                "    label: Comment",
                "    sample_count: 12",
                "    accepted_count: 11",
                "    acceptance_rate: 0.9167",
                "    average_prior_acceptance_rate: 0.9",
                "    bootstrap: false",
                "    recommended_level: l2",
                "    recommended_mode: batch_approval",
                '    rationale: "Eligible for batch approval."',
                "    required_acceptance_rate: 0.7",
                '    promoted_at: "2026-05-20T00:00:00+00:00"',
                "    promoted_by: tester",
            ]
        )
        + "\n",
        encoding="utf-8",
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


class _ProposalADOClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def query_all(self, *, filter_expression: str, select_fields: tuple[str, ...], top: int) -> list[dict[str, object]]:
        self.calls.append(
            {
                "kind": "query_all",
                "filter_expression": filter_expression,
                "fields": tuple(select_fields),
                "top": top,
            }
        )
        return [
            {
                "WorkItemId": 1001,
                "WorkItemType": "Feature",
                "Title": "Tracked item 1",
                "State": "Active",
                "ChangedDate": "2026-05-12T00:00:00+00:00",
                "Area": {"AreaPath": "One\\Demo\\WS"},
                "IterationPath": "Sprint 1",
                "TargetDate": "2026-06-01",
                "Tags": "demo",
                "AssignedTo": "owner@example.com",
                "AssignedToEmail": "owner@example.com",
            },
            {
                "WorkItemId": 1002,
                "WorkItemType": "Feature",
                "Title": "Tracked item 2",
                "State": "Active",
                "ChangedDate": "2026-05-12T00:00:00+00:00",
                "Area": {"AreaPath": "One\\Demo\\WS"},
                "IterationPath": "Sprint 1",
                "TargetDate": "2026-06-02",
                "Tags": "demo",
                "AssignedTo": "owner@example.com",
                "AssignedToEmail": "owner@example.com",
            },
        ]

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        self.calls.append({"kind": "query_work_items_batch", "ids": list(work_item_ids), "fields": tuple(fields)})
        rows = {
            1001: {
                "System.Id": 1001,
                "System.Rev": 11,
                "Custom.RiskLevel": "low",
                "Custom.Workstream": "legacy_ws",
            },
            1002: {
                "System.Id": 1002,
                "System.Rev": 12,
                "Custom.RiskLevel": "high",
                "Custom.Workstream": "ws_demo",
            },
        }
        return [
            {"id": work_item_id, "fields": {field: value for field, value in rows[work_item_id].items() if field in set(fields)}}
            for work_item_id in work_item_ids
        ]


class _RecordingWriter:
    def __init__(self) -> None:
        self.applied_manifest_path: Path | None = None

    def apply_manifest(self, manifest_path: Path, *, applied_at: datetime | None = None) -> ADOApplyArtifacts:
        self.applied_manifest_path = manifest_path
        proposal, _ = read_proposal_manifest(manifest_path)
        return ADOApplyArtifacts(
            manifest_path=manifest_path,
            proposal=proposal,
            proposal_status="applied",
            applied_count=1,
            skipped_count=0,
            conflict_count=0,
            failed_count=0,
        )


def _vitality_artifacts(as_of: datetime):
    items = (
        WorkItem(
            id=1001,
            type="Feature",
            title="Stale uncovered item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo\\WS",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": "2026-04-10T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=1002,
            type="Feature",
            title="Fresh tagged item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Demo\\WS",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 5),
            risk_level=RiskLevel.LOW,
            tags=["Needs-PM-Review"],
            custom_fields={"changed_date": "2026-05-14T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )

    return type(
        "_Artifacts",
        (),
        {
            "program_id": "demo",
            "items": items,
            "scored_items": (
                VitalityScore(1001, "owner", "ws_demo", 35, "red", 25, ("recent_comment",), 1, 2, 29, "Add an owner comment"),
                VitalityScore(1002, "owner", "ws_demo", 1, "green", 80, (), 0, 0, 92, None),
            ),
            "owner_aggregates": (
                VitalityAggregate("owner", "owner", 2, 1, 52.5, 1, 2, 0.5, 61, None),
            ),
            "workstream_aggregates": (),
            "ado_calls": 4,
        },
    )()
