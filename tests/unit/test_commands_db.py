from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands.db import _SCHEMA_MIGRATIONS, _plan_observation_compaction, maybe_run_scheduled_compaction
from src.core.hypothesis_models import AssertionEvaluation, AssertionOperator, ChallengeKind, ChallengeSeverity, Hypothesis, HypothesisKind, HypothesisStatus, RealityChallenge, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.program_fact_store import ProgramFactStore
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


def test_admin_db_backup_command_writes_backup_copy(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    backup_root = tmp_path / "backup"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    store.record_schema_version(
        "2026_05_20_001_l1_foundation",
        "vertex-test",
        applied_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.db._get_destination_encryption_status", lambda _path: True)

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


@pytest.mark.parametrize("fact_schema", [False, True])
def test_scheduled_compaction_without_reality_tables_skips_without_writes(caplog, tmp_path: Path, fact_schema: bool) -> None:
    db_root = tmp_path / "db"
    store = ProgramFactStore("acme", db_root=db_root)
    if fact_schema:
        store.initialize()
    else:
        store.db_path.parent.mkdir(parents=True)
        sqlite3.connect(store.db_path).close()
    before = store.db_path.read_bytes()
    sentinel_path = store.db_path.parent / ".compact_last_run"

    with caplog.at_level("INFO", logger="src.commands.db"):
        for _ in range(2):
            assert maybe_run_scheduled_compaction("acme", db_root=db_root) is None

    assert store.db_path.read_bytes() == before
    assert not sentinel_path.exists()
    assert len(caplog.records) == 2
    assert all("not applicable" in record.message for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)


def test_scheduled_compaction_runs_after_reality_schema_is_initialized(monkeypatch, tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    fact_store = ProgramFactStore("acme", db_root=db_root)
    fact_store.initialize()
    assert maybe_run_scheduled_compaction("acme", db_root=db_root) is None

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

    payload = maybe_run_scheduled_compaction("acme", db_root=db_root)

    assert payload is not None
    assert payload["evaluation_compaction"]["rows_deleted"] == 2
    assert payload["observation_compaction"]["rows_deleted"] == 2
    assert Path(payload["sentinel_path"]).exists()
    assert len(store.list_metric_observations(metric_id)) == 2
    assert len(store.list_assertion_evaluations()) == 1
    assert maybe_run_scheduled_compaction("acme", db_root=db_root) is None


@pytest.mark.parametrize("schema", ["partial", "missing_column", "invalid_database"])
def test_scheduled_compaction_keeps_schema_failures_visible(tmp_path: Path, schema: str) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.db_path.parent.mkdir(parents=True)
    if schema == "invalid_database":
        store.db_path.write_bytes(b"not a SQLite database")
    elif schema == "partial":
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("CREATE TABLE reality_metric_observations (observation_id TEXT)")
    else:
        store.initialize()
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("ALTER TABLE reality_assertion_evaluations DROP COLUMN evaluated_at")
    sentinel_path = store.db_path.parent / ".compact_last_run"
    sentinel_path.write_text("2020-01-01T00:00:00+00:00", encoding="utf-8")
    before = store.db_path.read_bytes()

    with pytest.raises(sqlite3.DatabaseError):
        maybe_run_scheduled_compaction("acme", db_root=db_root)

    assert store.db_path.read_bytes() == before
    assert sentinel_path.read_text(encoding="utf-8") == "2020-01-01T00:00:00+00:00"


def test_scheduled_compaction_rolls_back_when_later_table_is_missing(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    _seed_db_compaction_fixture(store)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE reality_challenges")
        before = tuple(connection.iterdump())

    with pytest.raises(sqlite3.OperationalError, match="reality_challenges"):
        maybe_run_scheduled_compaction("acme", db_root=db_root)

    with sqlite3.connect(store.db_path) as connection:
        assert tuple(connection.iterdump()) == before
    assert not (store.db_path.parent / ".compact_last_run").exists()


def test_startup_maintenance_without_reality_schema_does_not_log_failure(monkeypatch, caplog, tmp_path: Path) -> None:
    import cli

    db_root = tmp_path / "db"
    ProgramFactStore("acme", db_root=db_root).initialize()
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    monkeypatch.setattr(cli, "_should_skip_scheduled_db_maintenance", lambda *_args: False)
    monkeypatch.setattr(cli, "_program_for_startup", lambda _ctx: "acme")

    with caplog.at_level("INFO"):
        for _ in range(2):
            cli._maybe_run_scheduled_db_maintenance(None)

    assert not any("scheduled db maintenance failed" in record.message for record in caplog.records)
    assert sum("not applicable" in record.message for record in caplog.records) == 2
    assert not (db_root / "acme" / ".compact_last_run").exists()


def test_startup_maintenance_keeps_partial_schema_failure_nonblocking(monkeypatch, caplog, tmp_path: Path) -> None:
    import cli

    db_root = tmp_path / "db"
    store = ProgramFactStore("acme", db_root=db_root)
    store.initialize()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("CREATE TABLE reality_metric_observations (observation_id TEXT)")
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    monkeypatch.setattr(cli, "_should_skip_scheduled_db_maintenance", lambda *_args: False)
    monkeypatch.setattr(cli, "_program_for_startup", lambda _ctx: "acme")

    cli._maybe_run_scheduled_db_maintenance(None)

    failures = [record for record in caplog.records if "scheduled db maintenance failed" in record.message]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert not (db_root / "acme" / ".compact_last_run").exists()


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


def test_observation_compaction_separates_unit_and_definition_lineage(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(MetricSourceBinding(
        binding_id="binding-001",
        metric_id="acme.latency",
        program_id="acme",
        source_kind="kusto",
    ))
    for observation_id, value, unit, fingerprint, binding_version, day in (
        ("obs-ms", 5000.0, "ms", "sha256:latency-ms", 1, 5),
        ("obs-s", 5.0, "s", "sha256:latency-s", 1, 6),
        ("obs-ms-v2", 5100.0, "ms", "sha256:latency-ms", 2, 7),
        ("obs-padded-unit", 5200.0, " ms ", "sha256:latency-ms", 1, 8),
    ):
        observed_at = datetime(2024, 3, day, tzinfo=timezone.utc)
        store.write_metric_observation(MetricObservation(
            observation_id=observation_id,
            program_id="acme",
            metric_id="acme.latency",
            dimensions_json="{}",
            measurement_period_start=observed_at,
            measurement_period_end=observed_at,
            observed_at=observed_at,
            value_num=value,
            value_text=None,
            sample_count=1,
            source_binding_id="binding-001",
            binding_version=binding_version,
            observed_unit=unit,
            definition_fingerprint=fingerprint,
        ))

    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        plan = _plan_observation_compaction(
            connection,
            "acme",
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
            metric_definitions={"acme.latency": MetricDefinition(
                id="acme.latency",
                title="Latency",
                unit="ms",
                aggregation=MetricAggregation.LAST,
                retention_days=30,
            )},
            excluded_evaluation_ids=set(),
        )

    insertions = plan["insertions"]
    assert len(insertions) == 4
    assert {
        (item["observed_unit"], item["definition_fingerprint"], item["binding_version"])
        for item in insertions
    } == {
        ("ms", "sha256:latency-ms", 1),
        ("s", "sha256:latency-s", 1),
        ("ms", "sha256:latency-ms", 2),
        (" ms ", "sha256:latency-ms", 1),
    }


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


def _write_workspace_metric_registry(workspace: Path, *, retention_days: int) -> None:
    metrics_dir = workspace / "knowledge" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "acme.yaml").write_text(
        (
            "metrics:\n"
            "  - id: acme.cluster_count\n"
            "    title: Cluster Count\n"
            "    unit: count\n"
            "    aggregation: last\n"
            f"    retention_days: {retention_days}\n"
        ),
        encoding="utf-8",
    )


def _seed_external_workspace_compaction_db(workspace: Path) -> Path:
    db_root = workspace / "vertex-db"
    store = RealityStore("acme", db_root=db_root)
    store.initialize()
    _seed_db_compaction_fixture(store)
    return store.db_path


def test_compact_resolves_workspace_metrics_registry_independently(tmp_path: Path) -> None:
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    (ws_a / "programs").mkdir(parents=True)
    (ws_b / "programs").mkdir(parents=True)
    _write_workspace_metric_registry(ws_a, retention_days=30)
    _write_workspace_metric_registry(ws_b, retention_days=3650)
    db_a = _seed_external_workspace_compaction_db(ws_a)
    db_b = _seed_external_workspace_compaction_db(ws_b)

    result_a = runner.invoke(
        app,
        [
            "admin",
            "db",
            "compact",
            "--program",
            "acme",
            "--programs-root",
            str(ws_a / "programs"),
            "--format",
            "json",
        ],
    )
    assert result_a.exit_code == 0, result_a.output
    payload_a = json.loads(result_a.stdout)
    assert payload_a["observation_compaction"]["rows_deleted"] == 2
    assert payload_a["observation_compaction"]["rollup_rows_inserted"] == 1

    scheduled_b = maybe_run_scheduled_compaction(
        "acme",
        programs_root=ws_b / "programs",
        db_root=ws_b / "vertex-db",
    )
    assert scheduled_b is not None
    assert scheduled_b["observation_compaction"]["rows_deleted"] == 0

    with sqlite3.connect(db_a) as conn_a, sqlite3.connect(db_b) as conn_b:
        count_a = conn_a.execute("SELECT COUNT(*) FROM reality_metric_observations").fetchone()[0]
        count_b = conn_b.execute("SELECT COUNT(*) FROM reality_metric_observations").fetchone()[0]
    assert count_a == 2
    assert count_b == 3


def test_compact_fails_closed_when_external_workspace_metrics_missing_or_invalid(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.core.exceptions import ConfigError

    ws_missing = tmp_path / "ws_missing"
    (ws_missing / "programs").mkdir(parents=True)
    db_missing = _seed_external_workspace_compaction_db(ws_missing)
    before_bytes = db_missing.read_bytes()

    manual_result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "compact",
            "--program",
            "acme",
            "--programs-root",
            str(ws_missing / "programs"),
            "--format",
            "json",
        ],
    )
    assert manual_result.exit_code == 2
    assert json.loads(manual_result.stdout)["error"] == "metrics_registry_missing"
    assert db_missing.read_bytes() == before_bytes
    assert not (db_missing.parent / ".compact_last_run").exists()

    with pytest.raises(ConfigError):
        maybe_run_scheduled_compaction(
            "acme",
            programs_root=ws_missing / "programs",
            db_root=ws_missing / "vertex-db",
        )
    assert db_missing.read_bytes() == before_bytes
    assert not (db_missing.parent / ".compact_last_run").exists()

    ws_invalid = tmp_path / "ws_invalid"
    (ws_invalid / "programs").mkdir(parents=True)
    invalid_metrics = ws_invalid / "knowledge" / "metrics"
    invalid_metrics.mkdir(parents=True)
    (invalid_metrics / "broken.yaml").write_text("metrics: [\n", encoding="utf-8")
    db_invalid = _seed_external_workspace_compaction_db(ws_invalid)
    before_invalid_bytes = db_invalid.read_bytes()

    invalid_result = runner.invoke(
        app,
        [
            "admin",
            "db",
            "compact",
            "--program",
            "acme",
            "--programs-root",
            str(ws_invalid / "programs"),
            "--format",
            "json",
        ],
    )
    assert invalid_result.exit_code == 2
    assert json.loads(invalid_result.stdout)["error"] == "metrics_registry_invalid"
    assert db_invalid.read_bytes() == before_invalid_bytes
    assert not (db_invalid.parent / ".compact_last_run").exists()


def test_storage_doctor_warns_when_external_workspace_lacks_compaction_metrics_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.commands.doctor_checks.storage_checks import run_storage_doctor
    from tests.support.synthetic_report_source import EDITION_NAME, build_synthetic_report_source

    ws = tmp_path / "ws"
    monkeypatch.setenv("USERPROFILE", str(ws))
    monkeypatch.setenv("HOME", str(ws))
    build_synthetic_report_source(ws)
    programs_root = ws / "programs"
    editions_root = programs_root / "acme" / "editions"
    archive_root = ws / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    _seed_external_workspace_compaction_db(ws)

    report = run_storage_doctor(
        edition_name=EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=ws / "vertex-db",
    )
    check = next((item for item in report.checks if item.label == "Compaction Authority"), None)
    assert check is not None
    assert check.status == "warn"
    assert "knowledge/metrics" in check.detail.replace("\\", "/")

    # Malformed YAML -> warn; valid YAML -> pass; absent DB -> pass
    bad_metrics = ws / "knowledge" / "metrics"
    bad_metrics.mkdir(parents=True, exist_ok=True)
    bad_yaml = bad_metrics / "broken.yaml"
    bad_yaml.write_text("metrics: [\n", encoding="utf-8")
    malformed_report = run_storage_doctor(
        edition_name=EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=ws / "vertex-db",
    )
    malformed_check = next(item for item in malformed_report.checks if item.label == "Compaction Authority")
    assert malformed_check.status == "warn"
    assert "invalid" in malformed_check.detail

    bad_yaml.unlink()
    _write_workspace_metric_registry(ws, retention_days=90)
    valid_report = run_storage_doctor(
        edition_name=EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=ws / "vertex-db",
    )
    valid_check = next(item for item in valid_report.checks if item.label == "Compaction Authority")
    assert valid_check.status == "pass"

    no_db_report = run_storage_doctor(
        edition_name=EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=ws / "empty-vertex-db",
    )
    no_db_check = next(item for item in no_db_report.checks if item.label == "Compaction Authority")
    assert no_db_check.status == "pass"

