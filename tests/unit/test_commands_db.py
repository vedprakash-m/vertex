from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from typer.testing import CliRunner

from cli import app
from src.commands.db import _SCHEMA_MIGRATIONS
from src.core.hypothesis_models import AssertionEvaluation, AssertionOperator, ChallengeKind, ChallengeSeverity, Hypothesis, HypothesisKind, HypothesisStatus, RealityChallenge, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricQualityState, ObservationWindow
from src.core.reality_store import RealityStore


runner = CliRunner()
_MIGRATION_ROWS = [
    {"migration_id": migration["migration_id"], "description": migration["description"]}
    for migration in _SCHEMA_MIGRATIONS
]
_MIGRATION_IDS = [migration["migration_id"] for migration in _SCHEMA_MIGRATIONS]


def test_admin_db_verify_command_reports_healthy_reality_store(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.record_schema_version(
        "2026_05_20_001_l1_foundation",
        "vertex-test",
        applied_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "verify",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["is_valid"] is True
    assert payload["journal_mode"] == "wal"
    assert payload["missing_tables"] == []
    assert payload["foreign_key_issues"] == []
    assert payload["schema_versions"] == ["2026_05_20_001_l1_foundation"]


def test_admin_db_verify_command_reports_missing_reality_db(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "verify",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["is_valid"] is False
    assert payload["error"] == "database_missing"


def test_admin_db_migrate_command_lists_pending_baseline_migration_in_dry_run(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "migrate",
            "--program",
            "acme",
            "--dry-run",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["pending_migrations"] == _MIGRATION_ROWS
    assert payload["applied_migrations"] == []
    assert store.read_schema_versions() == ()


def test_admin_db_migrate_command_records_baseline_schema_version(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "migrate",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["pending_migrations"] == _MIGRATION_ROWS
    assert payload["applied_migrations"] == _MIGRATION_IDS
    assert store.read_schema_versions() == tuple(_MIGRATION_IDS)


def test_admin_db_migrate_command_rejects_legacy_schema_tables(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("CREATE TABLE reality_incidents (id TEXT PRIMARY KEY)")

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "migrate",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["is_valid"] is False
    assert payload["error"] == "unsupported_schema_state"
    assert payload["schema_issues"] == ["legacy tables present: reality_incidents"]


def test_admin_db_backup_command_writes_backup_copy(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    backup_root = tmp_path / "backup"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.record_schema_version(
        "2026_05_20_001_l1_foundation",
        "vertex-test",
        applied_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "backup",
            "--program",
            "acme",
            "--dest",
            str(backup_root),
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["is_valid"] is True
    backed_up_db = Path(payload["destination_path"])
    assert backed_up_db.exists()
    connection = sqlite3.connect(backed_up_db)
    try:
        rows = connection.execute("SELECT migration_id FROM schema_versions ORDER BY migration_id ASC").fetchall()
    finally:
        connection.close()
    assert [row[0] for row in rows] == ["2026_05_20_001_l1_foundation"]


def test_admin_db_backup_command_refuses_unencrypted_destination(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    monkeypatch.setattr("src.commands.db._get_destination_encryption_status", lambda _path: False)

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "backup",
            "--program",
            "acme",
            "--dest",
            str(tmp_path / "backup"),
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["is_valid"] is False
    assert payload["error"] == "unencrypted_destination"


def test_admin_db_backup_command_allows_unencrypted_destination_with_override(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    backup_root = tmp_path / "backup"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    monkeypatch.setattr("src.commands.db._get_destination_encryption_status", lambda _path: False)

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "backup",
            "--program",
            "acme",
            "--dest",
            str(backup_root),
            "--accept-unencrypted",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["override_used"] is True
    assert Path(payload["audit_log_path"]).exists()
    audit_lines = Path(payload["audit_log_path"]).read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) == 1
    audit_payload = json.loads(audit_lines[0])
    assert audit_payload["action"] == "db_backup_accept_unencrypted"
    assert audit_payload["program_id"] == "acme"


def test_admin_db_relocate_moves_legacy_workspace_db(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    legacy_db_path = programs_root / "acme" / "vertex.sqlite3"
    store = RealityStore("acme", db_root=legacy_db_path)
    store.initialize()
    store.record_schema_version(
        "2026_05_20_001_l1_foundation",
        "vertex-test",
        applied_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.db._try_create_legacy_pointer", lambda _legacy, _target: False)

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "relocate",
            "--program",
            "acme",
            "--format",
            "json",
            "--programs-root",
            str(programs_root),
            "--db-root",
            str(tmp_path / "db-home"),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    target_path = Path(payload["target_path"])
    assert target_path.exists()
    assert not legacy_db_path.exists()
    connection = sqlite3.connect(target_path)
    try:
        rows = connection.execute("SELECT migration_id FROM schema_versions ORDER BY migration_id ASC").fetchall()
    finally:
        connection.close()
    assert [row[0] for row in rows] == ["2026_05_20_001_l1_foundation"]


def test_admin_db_relocate_reports_missing_legacy_workspace_db(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "relocate",
            "--program",
            "acme",
            "--format",
            "json",
            "--programs-root",
            str(tmp_path / "programs"),
            "--db-root",
            str(tmp_path / "db-home"),
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"] == "legacy_database_missing"


def test_admin_db_compact_command_dry_run_previews_pending_compaction(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    metric_id = _seed_db_compaction_fixture(store)
    monkeypatch.setattr(
        "src.commands.db.load_metric_definition_map",
        lambda **_kwargs: {
            metric_id: MetricDefinition(
                id=metric_id,
                title=metric_id,
                unit="count",
                aggregation=MetricAggregation.LAST,
                retention_days=30,
            )
        },
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "compact",
            "--program",
            "acme",
            "--dry-run",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["evaluation_compaction"]["rows_deleted"] == 2
    assert payload["evaluation_compaction"]["summary_rows_written"] == 1
    assert payload["observation_compaction"]["rows_deleted"] == 2
    assert payload["observation_compaction"]["rollup_rows_inserted"] == 1
    assert payload["observation_compaction"]["rows_skipped_referenced"] == 1
    observations = store.list_metric_observations(metric_id)
    assert len(observations) == 3
    evaluations = store.list_assertion_evaluations()
    assert len(evaluations) == 2
    assert not (db_root / "acme" / ".compact_last_run").exists()


def test_admin_db_compact_command_compacts_old_rows_and_preserves_referenced_observations(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    metric_id = _seed_db_compaction_fixture(store)
    monkeypatch.setattr(
        "src.commands.db.load_metric_definition_map",
        lambda **_kwargs: {
            metric_id: MetricDefinition(
                id=metric_id,
                title=metric_id,
                unit="count",
                aggregation=MetricAggregation.LAST,
                retention_days=30,
            )
        },
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "compact",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["evaluation_compaction"]["rows_deleted"] == 2
    assert payload["evaluation_compaction"]["summary_rows_written"] == 1
    assert payload["observation_compaction"]["rows_deleted"] == 2
    assert payload["observation_compaction"]["rollup_rows_inserted"] == 1
    assert payload["observation_compaction"]["rows_skipped_referenced"] == 1
    assert Path(payload["sentinel_path"]).exists()

    observations = store.list_metric_observations(metric_id)
    assert len(observations) == 2
    raw_observation = next(observation for observation in observations if observation.quality_state == MetricQualityState.OK)
    assert raw_observation.observation_id == "obs-001"
    rollup_observation = next(observation for observation in observations if observation.quality_state == MetricQualityState.PARTIAL)
    assert rollup_observation.sample_count == 2

    evaluations = store.list_assertion_evaluations()
    assert len(evaluations) == 1
    assert evaluations[0].observation_id is None
    assert evaluations[0].note is not None
    assert evaluations[0].note.startswith("compacted_2_evaluations_from_")

    challenge = store.get_challenge("rc-001")
    assert challenge is not None
    assert challenge.observation_id == "obs-001"

    second_result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "compact",
            "--program",
            "acme",
            "--format",
            "json",
            "--db-root",
            str(db_root),
        ],
    )

    assert second_result.exit_code == 0
    second_payload = json.loads(second_result.output)
    assert second_payload["evaluation_compaction"]["rows_deleted"] == 0
    assert second_payload["evaluation_compaction"]["summary_rows_written"] == 0
    assert second_payload["observation_compaction"]["rows_deleted"] == 0
    assert second_payload["observation_compaction"]["rows_skipped_referenced"] == 1


def test_maybe_run_scheduled_compaction_runs_when_sentinel_is_missing(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()

    calls: list[str] = []
    monkeypatch.setattr(
        "src.commands.db._compact_database",
        lambda program, db_path, *, dry_run: calls.append(f"{program}:{db_path.name}:{dry_run}") or {"program_id": program},
    )

    payload = __import__("src.commands.db", fromlist=["maybe_run_scheduled_compaction"]).maybe_run_scheduled_compaction(
        "acme",
        db_root=db_root,
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert payload == {"program_id": "acme"}
    assert calls == ["acme:vertex.sqlite3:False"]


def test_maybe_run_scheduled_compaction_skips_recent_sentinel(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    sentinel_path = db_root / "acme" / ".compact_last_run"
    sentinel_path.write_text("2026-05-20T12:00:00+00:00", encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(
        "src.commands.db._compact_database",
        lambda program, db_path, *, dry_run: calls.append(program) or {"program_id": program},
    )

    payload = __import__("src.commands.db", fromlist=["maybe_run_scheduled_compaction"]).maybe_run_scheduled_compaction(
        "acme",
        db_root=db_root,
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert payload is None
    assert calls == []


def _seed_db_compaction_fixture(store: RealityStore) -> str:
    metric_id = "acme.cluster_count"
    # Fixed reference date: ensures obs-002 and obs-003 always land in the
    # same calendar month regardless of when the test runs (avoids a
    # date-sensitive rollup-key failure where month boundaries split the two
    # deletable observations into 2 rollup rows instead of 1).
    now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    observation_1_at = now - timedelta(days=90)
    observation_2_at = now - timedelta(days=80)
    observation_3_at = now - timedelta(days=70)

    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id=metric_id,
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.LTE,
        threshold=10.0,
    )
    store.upsert_telemetry_assertion(assertion)
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays at or below 10.",
            expected_value=10.0,
            as_of_date=date(2026, 5, 21),
            telemetry_assertion_id=assertion.id,
            proposed_by="pm",
            proposed_at=now,
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=now,
        )
    )

    for observation_id, observed_at, value in (
        ("obs-001", observation_1_at, 12.0),
        ("obs-002", observation_2_at, 14.0),
        ("obs-003", observation_3_at, 16.0),
    ):
        store.write_metric_observation(
            MetricObservation(
                observation_id=observation_id,
                program_id="acme",
                metric_id=metric_id,
                dimensions_json="{}",
                measurement_period_start=observed_at - timedelta(hours=1),
                measurement_period_end=observed_at,
                observed_at=observed_at,
                value_num=value,
                value_text=None,
                sample_count=1,
                quality_state=MetricQualityState.OK,
            )
        )

    store.append_assertion_evaluation(
        AssertionEvaluation(
            id="eval-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id="obs-002",
            evaluated_at=observation_2_at + timedelta(minutes=5),
            violated=True,
            value_num=14.0,
            expected_value=10.0,
            quality_state=MetricQualityState.OK,
        )
    )
    store.append_assertion_evaluation(
        AssertionEvaluation(
            id="eval-002",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id="obs-003",
            evaluated_at=observation_3_at + timedelta(minutes=5),
            violated=True,
            value_num=16.0,
            expected_value=10.0,
            quality_state=MetricQualityState.OK,
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="rc-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id="obs-001",
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
            observed_value=12.0,
            expected_value=10.0,
            delta_magnitude=2.0,
            severity=ChallengeSeverity.ALERT,
            source="kusto:test",
            detected_at=observation_1_at + timedelta(minutes=5),
        )
    )
    return metric_id
