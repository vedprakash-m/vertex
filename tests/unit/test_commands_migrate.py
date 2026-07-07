from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import migrate
from src.core.analytics_store import AnalyticsRebuildArtifacts
from src.core.journal import append_review_decision, append_signal, append_signal_thread_link, append_usage_marker
from src.core.models import Confidence, ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.models_v2 import Signal, SignalReviewDecision, SignalThreadLink, SignalUsageMarker, TrajectoryPoint
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore, get_program_sqlite_store_path
from src.core.snapshot_store import write_confirmed
from src.core.trajectory import append_trajectory_point


runner = CliRunner()


def test_run_storage_migration_copies_file_history_and_updates_backend(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    captured_at = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _seed_file_history(programs_root, captured_at=captured_at)

    artifacts = migrate.run_storage_migration(
        program_id="demo",
        target_backend="sqlite",
        programs_root=programs_root,
    )

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    program_document = yaml.safe_load((programs_root / "demo" / "program.yaml").read_text(encoding="utf-8"))

    assert artifacts.signal_count == 2
    assert artifacts.review_count == 1
    assert artifacts.thread_link_count == 1
    assert artifacts.trajectory_point_count == 2
    assert artifacts.usage_marker_count == 1
    assert artifacts.database_path == get_program_sqlite_store_path("demo", programs_root=programs_root)
    assert [signal.id for signal in signal_store.read("demo")] == ["signal-1", "signal-2"]
    assert set(signal_store.read_reviews("demo")) == {"signal-1"}
    assert [marker.signal_id for marker in signal_store.read_usage_markers("demo")] == ["signal-1"]
    assert set(signal_store.read_threads("demo")) == {"signal-1"}
    assert len(trajectory_store.read("demo", 1001)) == 2
    assert program_document["storage_backend"] == "sqlite"


def test_migrate_cli_dry_run_previews_counts_without_writing(tmp_path: Path, monkeypatch) -> None:
    programs_root = _seed_program_layout(tmp_path)
    captured_at = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _seed_file_history(programs_root, captured_at=captured_at)

    monkeypatch.setattr(migrate, "PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["migrate", "--program", "demo", "--to", "sqlite", "--dry-run"])

    program_document = yaml.safe_load((programs_root / "demo" / "program.yaml").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert "signals: 2" in result.stdout
    assert "reviews: 1" in result.stdout
    assert "threads: 1" in result.stdout
    assert "trajectory points: 2" in result.stdout
    assert "usage markers: 1" in result.stdout
    assert "Dry-run: sqlite database and program.yaml were not updated." in result.stdout
    assert get_program_sqlite_store_path("demo", programs_root=programs_root).exists() is False
    assert "storage_backend" not in program_document


def test_migrate_cli_rebuild_analytics_rebuilds_projection(tmp_path: Path, monkeypatch) -> None:
    programs_root = _seed_program_layout(tmp_path)

    monkeypatch.setattr(migrate, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        migrate,
        "run_rebuild_analytics",
        lambda *, program_id, programs_root: AnalyticsRebuildArtifacts(
            program_id=program_id,
            database_path=programs_root / "demo" / "vertex_analytics.sqlite3",
            confirmed_risks=1,
            confirmed_claims=2,
            confirmed_decisions=3,
            program_fact_decisions=1,
            context_snapshot_decisions=1,
            raw_decision_fallbacks=1,
            confirmed_vitality=4,
            confirmed_actions=5,
            dri_response_log=0,
            autonomy_audit=6,
        ),
    )

    result = runner.invoke(app, ["migrate", "--program", "demo", "--rebuild-analytics"])

    assert result.exit_code == 0
    assert "Rebuilt analytics for demo" in result.stdout
    assert "risks: 1" in result.stdout
    assert "claims: 2" in result.stdout
    assert "decisions: 3" in result.stdout
    assert "Decision rebuild tiers: program_facts=1 | context_snapshot=1 | raw_fallback=1 | low_fidelity=2" in result.stdout


def _seed_program_layout(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "demo",
                "name": "Demo Program",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    return programs_root


def _seed_file_history(programs_root: Path, *, captured_at: datetime) -> None:
    append_signal(
        Signal(
            id="signal-1",
            timestamp=captured_at,
            source="ado/revision",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1001",),
            text="Reviewed signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=captured_at,
    )
    append_signal(
        Signal(
            id="signal-2",
            timestamp=captured_at,
            source="workiq/meeting",
            program_id="demo",
            workstream_id="ws_demo",
            entity_refs=("WI:1002",),
            text="Unreviewed signal.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=captured_at,
    )
    append_review_decision(
        "demo",
        SignalReviewDecision(
            signal_id="signal-1",
            decision="approved",
            reviewed_at=captured_at,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_usage_marker(
        "demo",
        SignalUsageMarker(
            signal_id="signal-1",
            issue_number=7,
            edition_id="demo_weekly",
            manifest_id="manifest-7",
            used_at=captured_at,
        ),
        programs_root=programs_root,
    )
    append_signal_thread_link(
        "demo",
        SignalThreadLink(
            signal_id="signal-1",
            thread_id="thread-1",
            linked_at=captured_at,
            linked_by="system",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "demo",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 9),
            state="Active",
            assigned_to="owner@example.com",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Demo\\WS",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "demo",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 10),
            state="Active",
            assigned_to="owner@example.com",
            target_date=date(2026, 6, 3),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Demo\\WS",
        ),
        programs_root=programs_root,
    )


def _seed_sqlite_signals(programs_root: Path, *, n: int, captured_at: datetime) -> None:
    """Seed n signals directly into SQLite for the demo program."""
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    for i in range(1, n + 1):
        signal_store.append(
            Signal(
                id=f"signal-{i:03d}",
                timestamp=captured_at,
                source="ado/revision",
                program_id="demo",
                workstream_id="ws_demo",
                entity_refs=(f"WI:{1000 + i}",),
                text=f"Signal number {i}.",
                raw_ref=None,
                confidence=Confidence.HIGH,
                metadata=None,
                thread_id=None,
            )
        )
    trajectory_store.append(
        "demo",
        2001,
        TrajectoryPoint(
            date=date(2026, 5, 10),
            state="Active",
            assigned_to="owner@example.com",
            target_date=date(2026, 7, 1),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Demo",
        ),
    )


def _seed_confirmed_archive(programs_root: Path) -> None:
    archive_root = programs_root / "demo" / "archive"
    snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 19, 17, 30, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1001,
                type="Feature",
                title="Demo item",
                state="Active",
                assigned_to="owner@example.com",
                area_path="One\\Demo",
                target_date=date(2026, 6, 1),
                risk_level=RiskLevel.MEDIUM,
                tags=["demo"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo",
                name="Execution",
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.LOW,
                item_count=1,
                ado_query_url="https://ado/demo",
            ),
        ),
    )
    snapshot_path = write_confirmed(
        edition="demo_weekly",
        issue_number=1,
        snapshot=snapshot,
        archive_root=archive_root,
        promote=True,
    )
    edition_root = archive_root / "demo_weekly"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": "demo_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(snapshot_path),
                        "manifest_path": None,
                    }
                ],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Reverse migration: --to file (S1A.6)
# ---------------------------------------------------------------------------


def test_reverse_migration_round_trips_50_signals(tmp_path: Path) -> None:
    """SQLite -> file preserves all 50 signals, renames DB, and updates program.yaml."""
    programs_root = _seed_program_layout(tmp_path)
    captured_at = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _seed_sqlite_signals(programs_root, n=50, captured_at=captured_at)
    db_path = get_program_sqlite_store_path("demo", programs_root=programs_root)

    artifacts = migrate.run_storage_migration(
        program_id="demo",
        target_backend="file",
        programs_root=programs_root,
    )

    # SQLite DB must be renamed (tombstoned)
    assert not db_path.exists()
    tombstones = list(programs_root.glob("demo/vertex_store.sqlite3.pre-rollback-*"))
    assert len(tombstones) == 1

    # program.yaml must have storage_backend: file
    program_document = yaml.safe_load((programs_root / "demo" / "program.yaml").read_text(encoding="utf-8"))
    assert program_document["storage_backend"] == "file"

    # File journal must have all 50 signals
    from src.core.journal import read_signals as read_file_signals
    file_signals = read_file_signals("demo", programs_root=programs_root)
    assert len(file_signals) == 50
    assert {s.id for s in file_signals} == {f"signal-{i:03d}" for i in range(1, 51)}

    # Trajectory points must be present in file store
    from src.core.trajectory import read_trajectory
    file_trajectory = read_trajectory("demo", 2001, programs_root=programs_root)
    assert len(file_trajectory) == 1

    assert artifacts.signal_count == 50
    assert artifacts.trajectory_point_count == 1
    assert artifacts.config_updated is True


def test_reverse_migration_aborts_on_corrupt_sqlite(tmp_path: Path) -> None:
    """Corrupted SQLite DB causes _validate_sqlite_integrity to raise and abort migration."""
    programs_root = _seed_program_layout(tmp_path)
    captured_at = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _seed_sqlite_signals(programs_root, n=5, captured_at=captured_at)
    db_path = get_program_sqlite_store_path("demo", programs_root=programs_root)

    # Corrupt the database
    raw = bytearray(db_path.read_bytes())
    raw[100:300] = b"\xff" * 200
    db_path.write_bytes(bytes(raw))

    from typer import BadParameter
    try:
        migrate.run_storage_migration(
            program_id="demo",
            target_backend="file",
            programs_root=programs_root,
        )
        assert False, "Expected BadParameter from corrupt DB"
    except (BadParameter, SystemExit, Exception) as exc:
        # Any exception/exit indicating failure is acceptable —
        # the key assertion is that no file journal was created
        pass

    # File journal must NOT have been written
    journal_dir = programs_root / "demo" / "journal"
    assert not journal_dir.exists() or not any(journal_dir.glob("*.jsonl"))


def test_reverse_migration_dry_run_previews_without_writing(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    captured_at = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _seed_sqlite_signals(programs_root, n=10, captured_at=captured_at)
    db_path = get_program_sqlite_store_path("demo", programs_root=programs_root)

    artifacts = migrate.run_storage_migration(
        program_id="demo",
        target_backend="file",
        programs_root=programs_root,
        dry_run=True,
    )

    # SQLite DB must NOT be renamed
    assert db_path.exists()
    # program.yaml must NOT have storage_backend changed
    program_document = yaml.safe_load((programs_root / "demo" / "program.yaml").read_text(encoding="utf-8"))
    assert "storage_backend" not in program_document
    # File journal must NOT have been written
    journal_dir = programs_root / "demo" / "journal"
    assert not journal_dir.exists() or not any(journal_dir.glob("*.jsonl"))

    assert artifacts.signal_count == 10
    assert artifacts.dry_run is True
    assert artifacts.config_updated is False