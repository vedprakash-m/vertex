from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import portalocker
import re
import sqlite3
import subprocess
import shutil
from typing import Any, cast
from uuid import uuid4

import typer

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.metric_models import MetricDefinition, MetricQualityState
from src.core.metric_registry import load_metric_definition_map
from src.core.reality_store import get_program_reality_db_path


app = typer.Typer(help="Inspect and validate the L1 reality database.")

_SCHEMA_MIGRATIONS = (
    {
        "migration_id": "2026_05_20_001_l1_foundation",
        "description": "Record the current implemented L1 schema as the baseline migration.",
    },
    {
        "migration_id": "2026_05_27_001_drop_incidents",
        "description": "Record the v0.5/v0.6 removal of L1 incident tables for current-schema-compatible stores.",
    },
    {
        "migration_id": "2026_05_27_002_drop_docs",
        "description": "Record the v0.5/v0.6 removal of document-index tables from the L1 SQLite store.",
    },
    {
        "migration_id": "2026_05_27_003_drop_assertion_violation_state",
        "description": "Record the append-only assertion-evaluations transition on current-schema-compatible stores.",
    },
    {
        "migration_id": "2026_05_27_004_drop_observation_corrections",
        "description": "Record the identity-preserving late-correction layout on metric observations.",
    },
    {
        "migration_id": "2026_05_27_005_drop_source_health",
        "description": "Record the binding-health consolidation that removes the old source-health table.",
    },
    {
        "migration_id": "2026_05_27_006_drop_composite_assertions",
        "description": "Record the deferral of composite assertions from the L1 schema.",
    },
    {
        "migration_id": "2026_05_27_007_drop_challenge_events",
        "description": "Record the M0 entity-only challenge audit shape with inline state metadata.",
    },
    {
        "migration_id": "2026_05_27_008_drop_hypothesis_events_for_m0",
        "description": "Record the M0 entity-only hypothesis audit shape with inline state metadata.",
    },
    {
        "migration_id": "2026_05_27_009_drop_max_dimensions",
        "description": "Record the removal of legacy max-dimensions binding schema from L1.",
    },
    {
        "migration_id": "2026_05_27_010_drop_captured_bucket",
        "description": "Record the simplified observation identity key without captured_bucket.",
    },
    {
        "migration_id": "2026_05_27_011_drop_lineage_hashes",
        "description": "Record the final observation schema that keeps binding_version but drops legacy lineage hashes.",
    },
    {
        "migration_id": "2026_05_27_012_drop_recovery_buffer_minutes",
        "description": "Record the simplified maintenance-window schema without recovery buffers.",
    },
    {
        "migration_id": "2026_05_27_013_drop_threshold_upper",
        "description": "Record the historical deferral that removed threshold_upper from an earlier draft of assertion policy state.",
    },
    {
        "migration_id": "2026_05_27_014_add_v05_v06_columns",
        "description": "Record the current v0.5/v0.6 inline audit, evidence-url, and snooze columns.",
    },
    {
        "migration_id": "2026_05_27_015_drop_expected_value_floor",
        "description": "Record the removal of expected_value_floor in favor of regular assertions.",
    },
    {
        "migration_id": "2026_05_27_016_retain_last_validated_kql_hash",
        "description": "Record retention of last_validated_kql_hash for doctor-driven drift checks.",
    },
    {
        "migration_id": "2026_05_27_017_drop_security_yaml",
        "description": "Record the removal of principal-gating security.yaml from the L1 contract.",
    },
    {
        "migration_id": "2026_05_28_018_restore_threshold_upper",
        "description": "Record the restored threshold_upper column that enables BETWEEN assertion policy state.",
    },
    {
        "migration_id": "2026_05_29_019_restore_composite_assertions",
        "description": "Record the reintroduction of composite assertions into the supported L1 schema.",
    },
)

_COMPACT_EVALUATION_RETENTION_DAYS = 30
_AUTO_COMPACT_INTERVAL_DAYS = 7
_COMPACTED_EVALUATION_NOTE = re.compile(r"^compacted_(\d+)_evaluations_from_")

_EXPECTED_TABLES = frozenset(
    {
        "schema_versions",
        "reality_metric_source_bindings",
        "reality_metric_observations",
        "reality_telemetry_assertions",
        "reality_hypotheses",
        "reality_challenges",
        "reality_ingestion_runs",
        "reality_metric_binding_health",
        "reality_assertion_evaluations",
        "reality_maintenance_windows",
        "reality_suppression_events",
        "reality_composite_assertions",
        "digest_cache",
    }
)

_LEGACY_TABLES = frozenset(
    {
        "reality_incidents",
        "doc_revisions",
        "doc_sections",
        "doc_sections_fts",
        "doc_section_aliases",
        "doc_assertions",
        "doc_quarantine",
        "concept_to_ref",
        "reality_assertion_violation_state",
        "reality_observation_corrections",
        "reality_source_health",
        "reality_challenge_events",
        "reality_hypothesis_events",
    }
)

_REQUIRED_SCHEMA_COLUMNS = {
    "reality_metric_source_bindings": frozenset({
        "binding_id",
        "metric_id",
        "program_id",
        "binding_version",
        "last_validated_kql_hash",
        "evidence_url_template",
        "valid_from",
        "valid_until",
    }),
    "reality_metric_observations": frozenset({
        "observation_id",
        "binding_version",
        "corrected_at",
        "corrected_reason",
        "inserted_at",
        "is_pinned",
        "pinned_at",
        "pin_reason",
    }),
    "reality_telemetry_assertions": frozenset({
        "id",
        "metric_id",
        "threshold_upper",
        "policy_version",
        "valid_from",
        "valid_until",
        "created_by",
    }),
    "reality_composite_assertions": frozenset({
        "id",
        "program_id",
        "operator",
        "child_assertion_ids_json",
        "policy_version",
        "valid_from",
        "valid_until",
        "created_by",
    }),
    "reality_hypotheses": frozenset({
        "id",
        "expected_value_frozen_at",
        "composite_assertion_id",
        "policy_version",
        "state_actor",
        "state_reason",
        "state_changed_at",
    }),
    "reality_challenges": frozenset({
        "id",
        "current_state",
        "state_actor",
        "state_reason",
        "state_changed_at",
        "evidence_url",
        "ado_current_target",
        "snoozed_until",
        "snooze_reason",
    }),
    "reality_metric_binding_health": frozenset({
        "program_id",
        "binding_id",
        "last_successful_observation_at",
        "last_failure_at",
        "last_validation_error",
    }),
    "reality_maintenance_windows": frozenset({
        "id",
        "scope_kind",
        "scope_value",
        "reference",
    }),
    "reality_assertion_evaluations": frozenset({
        "id",
        "hypothesis_id",
        "assertion_id",
        "evaluated_at",
        "note",
    }),
}

_UNSUPPORTED_LEGACY_COLUMNS = {
    "reality_metric_source_bindings": frozenset({"max_dimensions", "expected_value_floor"}),
    "reality_metric_observations": frozenset({"captured_bucket", "source_query_hash", "kql_template_hash"}),
    "reality_telemetry_assertions": frozenset(),
    "reality_composite_assertions": frozenset(),
    "reality_hypotheses": frozenset(),
    "reality_maintenance_windows": frozenset({"recovery_buffer_minutes"}),
}


@app.command("verify")
def verify_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
) -> None:
    normalized_format = _normalize_format(format)
    db_path = get_program_reality_db_path(program, db_root=db_root)

    if not db_path.exists():
        _emit(
            {
                "program_id": program,
                "db_path": str(db_path),
                "is_valid": False,
                "error": "database_missing",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    try:
        payload = _verify_database(program, db_path)
    except sqlite3.OperationalError as exc:
        _emit(
            {
                "program_id": program,
                "db_path": str(db_path),
                "is_valid": False,
                "error": f"cannot_open:{exc}",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    _emit(payload, format=normalized_format)
    raise typer.Exit(code=0 if payload["is_valid"] else 1)


@app.command("backup")
def backup_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dest: Path | None = typer.Option(None, "--dest", help="Destination directory or sqlite file path for the backup."),
    accept_unencrypted: bool = typer.Option(False, "--accept-unencrypted", help="Allow writing the backup even when the destination volume reports encryption disabled."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
) -> None:
    normalized_format = _normalize_format(format)
    db_path = get_program_reality_db_path(program, db_root=db_root)
    if not db_path.exists():
        _emit_backup(
            {
                "program_id": program,
                "source_path": str(db_path),
                "is_valid": False,
                "error": "database_missing",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    destination_path = _resolve_backup_destination(db_path, dest)
    destination_encrypted = _get_destination_encryption_status(destination_path.parent)
    if destination_encrypted is False and not accept_unencrypted:
        _emit_backup(
            {
                "program_id": program,
                "source_path": str(db_path),
                "destination_path": str(destination_path),
                "is_valid": False,
                "error": "unencrypted_destination",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    payload = _backup_database(
        program,
        db_path,
        destination_path,
        destination_encrypted=destination_encrypted,
        accept_unencrypted=accept_unencrypted,
    )
    _emit_backup(payload, format=normalized_format)
    raise typer.Exit(code=0)


@app.command("migrate")
def migrate_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List pending schema-version records without writing them."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
) -> None:
    normalized_format = _normalize_format(format)
    db_path = get_program_reality_db_path(program, db_root=db_root)
    if not db_path.exists():
        _emit_migrate(
            {
                "program_id": program,
                "db_path": str(db_path),
                "is_valid": False,
                "error": "database_missing",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    payload = _migrate_database(program, db_path, dry_run=dry_run)
    _emit_migrate(payload, format=normalized_format)
    raise typer.Exit(code=0 if payload.get("is_valid", False) else 2)


@app.command("compact")
def compact_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview compaction without writing any database changes."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
) -> None:
    normalized_format = _normalize_format(format)
    db_path = get_program_reality_db_path(program, db_root=db_root)
    if not db_path.exists():
        _emit_compact(
            {
                "program_id": program,
                "db_path": str(db_path),
                "is_valid": False,
                "error": "database_missing",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    payload = _compact_database(program, db_path, dry_run=dry_run)
    _emit_compact(payload, format=normalized_format)
    raise typer.Exit(code=0)


@app.command("relocate")
def relocate_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path | None = typer.Option(None, "--programs-root", hidden=True),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
) -> None:
    normalized_format = _normalize_format(format)
    legacy_path = (programs_root or PROGRAMS_ROOT) / program / "vertex.sqlite3"
    target_path = get_program_reality_db_path(program, db_root=db_root)

    if not legacy_path.exists():
        _emit_relocate(
            {
                "program_id": program,
                "legacy_path": str(legacy_path),
                "target_path": str(target_path),
                "is_valid": False,
                "error": "legacy_database_missing",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)
    if target_path.exists():
        _emit_relocate(
            {
                "program_id": program,
                "legacy_path": str(legacy_path),
                "target_path": str(target_path),
                "is_valid": False,
                "error": "target_database_exists",
            },
            format=normalized_format,
        )
        raise typer.Exit(code=2)

    payload = _relocate_database(program, legacy_path, target_path)
    _emit_relocate(payload, format=normalized_format)
    raise typer.Exit(code=0)


def _verify_database(program: str, db_path: Path) -> dict[str, object]:
    with _open_readonly(db_path) as connection:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        present_tables = {str(row[0]) for row in table_rows}
        missing_tables = sorted(_EXPECTED_TABLES - present_tables)

        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        integrity_messages = tuple(str(row[0]) for row in integrity_rows)

        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        quick_messages = tuple(str(row[0]) for row in quick_rows)

        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        foreign_key_issues = tuple(
            {
                "table": str(row[0]),
                "rowid": int(row[1]),
                "parent": str(row[2]),
                "fk_index": int(row[3]),
            }
            for row in foreign_key_rows
        )

        journal_row = connection.execute("PRAGMA journal_mode").fetchone()
        journal_mode = str(journal_row[0]).lower() if journal_row is not None else "unknown"

        schema_versions: tuple[str, ...]
        if "schema_versions" in present_tables:
            rows = connection.execute(
                "SELECT migration_id FROM schema_versions ORDER BY migration_id ASC"
            ).fetchall()
            schema_versions = tuple(str(row[0]) for row in rows)
        else:
            schema_versions = ()

    integrity_ok = len(integrity_messages) == 1 and integrity_messages[0].lower() == "ok"
    quick_ok = len(quick_messages) == 1 and quick_messages[0].lower() == "ok"
    wal_ok = journal_mode == "wal"
    is_valid = integrity_ok and quick_ok and wal_ok and not missing_tables and not foreign_key_issues

    return {
        "program_id": program,
        "db_path": str(db_path),
        "is_valid": is_valid,
        "journal_mode": journal_mode,
        "integrity_messages": list(integrity_messages),
        "quick_check_messages": list(quick_messages),
        "foreign_key_issues": list(foreign_key_issues),
        "missing_tables": missing_tables,
        "schema_versions": list(schema_versions),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _backup_database(
    program: str,
    db_path: Path,
    destination_path: Path,
    *,
    destination_encrypted: bool | None,
    accept_unencrypted: bool,
) -> dict[str, object]:
    _prepare_backup_destination(destination_path)

    with _open_readonly(db_path) as source_connection, sqlite3.connect(destination_path) as destination_connection:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA journal_mode = WAL")

    verification = _verify_database(program, destination_path)
    audit_log_path: Path | None = None
    if destination_encrypted is False and accept_unencrypted:
        audit_log_path = _append_backup_override_audit_log(program, destination_path)

    return {
        "program_id": program,
        "source_path": str(db_path),
        "destination_path": str(destination_path),
        "destination_encrypted": destination_encrypted,
        "override_used": destination_encrypted is False and accept_unencrypted,
        "audit_log_path": str(audit_log_path) if audit_log_path is not None else None,
        "schema_versions": verification["schema_versions"],
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "is_valid": True,
    }


def _migrate_database(program: str, db_path: Path, *, dry_run: bool) -> dict[str, object]:
    schema_state = _read_schema_state(db_path)
    schema_issues = _collect_schema_compatibility_issues(schema_state)
    if schema_state["missing_tables"] or schema_issues:
        return {
            "program_id": program,
            "db_path": str(db_path),
            "is_valid": False,
            "error": "unsupported_schema_state",
            "missing_tables": list(cast(list[str], schema_state["missing_tables"])),
            "schema_issues": schema_issues,
        }

    recorded_versions = tuple(cast(tuple[str, ...], schema_state["schema_versions"]))
    ordered_migration_ids = tuple(str(migration["migration_id"]) for migration in _SCHEMA_MIGRATIONS)
    unknown_versions = [migration_id for migration_id in recorded_versions if migration_id not in ordered_migration_ids]
    if unknown_versions:
        return {
            "program_id": program,
            "db_path": str(db_path),
            "is_valid": False,
            "error": "unknown_schema_versions",
            "unknown_schema_versions": unknown_versions,
        }

    expected_prefix = ordered_migration_ids[: len(recorded_versions)]
    if recorded_versions != expected_prefix:
        return {
            "program_id": program,
            "db_path": str(db_path),
            "is_valid": False,
            "error": "out_of_order_schema_versions",
            "schema_versions": list(recorded_versions),
            "expected_prefix": list(expected_prefix),
        }

    applied_versions = set(recorded_versions)
    pending = [
        migration
        for migration in _SCHEMA_MIGRATIONS
        if migration["migration_id"] not in applied_versions
    ]
    applied_now: list[str] = []
    if not dry_run and pending:
        for migration in pending:
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "INSERT INTO schema_versions (migration_id, applied_at, applied_by) VALUES (?, ?, ?)",
                    (
                        migration["migration_id"],
                        datetime.now(timezone.utc).isoformat(),
                        "vertex admin db migrate",
                    ),
                )
                applied_now.append(str(migration["migration_id"]))
        schema_versions = list(ordered_migration_ids[: len(recorded_versions) + len(applied_now)])
    else:
        schema_versions = list(recorded_versions)

    return {
        "program_id": program,
        "db_path": str(db_path),
        "is_valid": True,
        "dry_run": dry_run,
        "pending_migrations": pending,
        "applied_migrations": applied_now,
        "schema_versions": schema_versions,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _compact_database(program: str, db_path: Path, *, dry_run: bool) -> dict[str, object]:
    compacted_at = datetime.now(timezone.utc)
    evaluation_cutoff = compacted_at - timedelta(days=_COMPACT_EVALUATION_RETENTION_DAYS)
    metric_definitions = load_metric_definition_map(as_of=compacted_at)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        evaluation_plan = _plan_evaluation_compaction(connection, program, cutoff=evaluation_cutoff)
        excluded_evaluation_ids = {row_id for plan in evaluation_plan for row_id in cast(list[str], plan["deleted_row_ids"])}
        if not dry_run:
            _apply_evaluation_compaction(connection, evaluation_plan)
            excluded_evaluation_ids = set()

        observation_plan = _plan_observation_compaction(
            connection,
            program,
            as_of=compacted_at,
            metric_definitions=metric_definitions,
            excluded_evaluation_ids=excluded_evaluation_ids,
        )
        if not dry_run:
            _apply_observation_compaction(connection, observation_plan, inserted_at=compacted_at)

    sentinel_path = db_path.parent / ".compact_last_run"
    if not dry_run:
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text(compacted_at.isoformat(), encoding="utf-8")

    evaluation_rows_deleted = sum(len(cast(list[str], plan["deleted_row_ids"])) for plan in evaluation_plan)
    evaluation_logical_count = sum(cast(int, plan["logical_evaluation_count"]) for plan in evaluation_plan)
    observation_rows_deleted = len(cast(list[object], observation_plan["deleted_row_ids"]))
    rollup_rows_inserted = len(cast(list[object], observation_plan["insertions"]))
    rollup_rows_updated = len(cast(list[object], observation_plan["updates"]))

    return {
        "program_id": program,
        "db_path": str(db_path),
        "is_valid": True,
        "dry_run": dry_run,
        "evaluation_compaction": {
            "pairs_compacted": len(evaluation_plan),
            "rows_deleted": evaluation_rows_deleted,
            "logical_evaluations_collapsed": evaluation_logical_count,
            "summary_rows_written": len(evaluation_plan),
            "cutoff": evaluation_cutoff.isoformat(),
        },
        "observation_compaction": {
            "rows_deleted": observation_rows_deleted,
            "rollup_rows_inserted": rollup_rows_inserted,
            "rollup_rows_updated": rollup_rows_updated,
            "rows_skipped_referenced": observation_plan["rows_skipped_referenced"],
            "unknown_metric_ids": sorted(cast(set[str], observation_plan["unknown_metric_ids"])),
        },
        "sentinel_path": str(sentinel_path),
        "compacted_at": compacted_at.isoformat(),
    }


def maybe_run_scheduled_compaction(
    program: str,
    *,
    db_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object] | None:
    reference_time = now or datetime.now(timezone.utc)
    db_path = get_program_reality_db_path(program, db_root=db_root)
    if not db_path.exists():
        return None
    sentinel_path = db_path.parent / ".compact_last_run"
    if not _scheduled_compaction_due(sentinel_path, now=reference_time):
        return None
    return _compact_database(program, db_path, dry_run=False)


def _scheduled_compaction_due(sentinel_path: Path, *, now: datetime) -> bool:
    if not sentinel_path.exists():
        return True
    try:
        last_run = _parse_iso_datetime(sentinel_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return now - last_run >= timedelta(days=_AUTO_COMPACT_INTERVAL_DAYS)


def _plan_evaluation_compaction(
    connection: sqlite3.Connection,
    program: str,
    *,
    cutoff: datetime,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT id, hypothesis_id, assertion_id, evaluated_at, violated, expected_value, quality_state, note
        FROM reality_assertion_evaluations
        WHERE program_id = ? AND evaluated_at < ?
        ORDER BY hypothesis_id ASC, assertion_id ASC, evaluated_at ASC, id ASC
        """,
        (program, cutoff.isoformat()),
    ).fetchall()

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (str(row["hypothesis_id"]), str(row["assertion_id"]))
        grouped.setdefault(key, []).append(row)

    plans: list[dict[str, object]] = []
    for (hypothesis_id, assertion_id), pair_rows in grouped.items():
        if len(pair_rows) == 1 and _COMPACTED_EVALUATION_NOTE.match(_optional_string(pair_rows[0]["note"]) or ""):
            continue
        latest_row = pair_rows[-1]
        earliest_at = str(pair_rows[0]["evaluated_at"])
        logical_count = sum(_logical_evaluation_count(row) for row in pair_rows)
        plans.append(
            {
                "deleted_row_ids": [str(row["id"]) for row in pair_rows],
                "logical_evaluation_count": logical_count,
                "summary_row": {
                    "id": uuid4().hex,
                    "program_id": program,
                    "hypothesis_id": hypothesis_id,
                    "assertion_id": assertion_id,
                    "observation_id": None,
                    "evaluated_at": cutoff.isoformat(),
                    "violated": int(latest_row["violated"]),
                    "value_num": None,
                    "expected_value": latest_row["expected_value"],
                    "quality_state": latest_row["quality_state"],
                    "note": f"compacted_{logical_count}_evaluations_from_{earliest_at}_to_{cutoff.isoformat()}",
                },
            }
        )
    return plans


def _apply_evaluation_compaction(connection: sqlite3.Connection, plans: list[dict[str, object]]) -> None:
    for plan in plans:
        connection.executemany(
            "DELETE FROM reality_assertion_evaluations WHERE id = ?",
            [(row_id,) for row_id in cast(list[str], plan["deleted_row_ids"])],
        )
        summary_row = cast(dict[str, object], plan["summary_row"])
        connection.execute(
            """
            INSERT INTO reality_assertion_evaluations (
                id, program_id, hypothesis_id, assertion_id, observation_id, evaluated_at,
                violated, value_num, expected_value, quality_state, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary_row["id"],
                summary_row["program_id"],
                summary_row["hypothesis_id"],
                summary_row["assertion_id"],
                summary_row["observation_id"],
                summary_row["evaluated_at"],
                summary_row["violated"],
                summary_row["value_num"],
                summary_row["expected_value"],
                summary_row["quality_state"],
                summary_row["note"],
            ),
        )


def _plan_observation_compaction(
    connection: sqlite3.Connection,
    program: str,
    *,
    as_of: datetime,
    metric_definitions: Mapping[str, MetricDefinition],
    excluded_evaluation_ids: set[str],
) -> dict[str, object]:
    referenced_observation_ids = _load_referenced_observation_ids(
        connection,
        excluded_evaluation_ids=excluded_evaluation_ids,
    )
    partial_rows = connection.execute(
        """
        SELECT * FROM reality_metric_observations
        WHERE program_id = ? AND quality_state = ?
        ORDER BY observed_at DESC, observation_id DESC
        """,
        (program, MetricQualityState.PARTIAL.value),
    ).fetchall()
    existing_rollups: dict[tuple[str, str, str | None, str], sqlite3.Row] = {}
    for row in partial_rows:
        key = _observation_rollup_key(row)
        existing_rollups.setdefault(key, row)

    rows = connection.execute(
        """
        SELECT * FROM reality_metric_observations
        WHERE program_id = ? AND quality_state != ?
        ORDER BY metric_id ASC, dimensions_json ASC, source_binding_id ASC, measurement_period_end ASC, observed_at ASC, inserted_at ASC, observation_id ASC
        """,
        (program, MetricQualityState.PARTIAL.value),
    ).fetchall()

    grouped: dict[tuple[str, str, str | None, str], list[sqlite3.Row]] = {}
    rows_skipped_referenced = 0
    unknown_metric_ids: set[str] = set()
    for row in rows:
        metric_id = str(row["metric_id"])
        definition = metric_definitions.get(metric_id)
        if definition is None:
            unknown_metric_ids.add(metric_id)
            continue
        cutoff = as_of - timedelta(days=int(definition.retention_days))
        observed_at = _parse_iso_datetime(row["observed_at"])
        if observed_at >= cutoff:
            continue
        observation_id = str(row["observation_id"])
        if observation_id in referenced_observation_ids:
            rows_skipped_referenced += 1
            continue
        grouped.setdefault(_observation_rollup_key(row), []).append(row)

    deleted_row_ids: list[str] = []
    insertions: list[dict[str, object]] = []
    updates: list[dict[str, object]] = []
    for key, group_rows in grouped.items():
        deleted_row_ids.extend(str(row["observation_id"]) for row in group_rows)
        latest_row = group_rows[-1]
        earliest_start = min(str(row["measurement_period_start"]) for row in group_rows)
        latest_end = max(str(row["measurement_period_end"]) for row in group_rows)
        compacted_count = len(group_rows)
        existing_rollup = existing_rollups.get(key)
        if existing_rollup is None:
            insertions.append(
                {
                    "observation_id": uuid4().hex,
                    "program_id": program,
                    "metric_id": str(latest_row["metric_id"]),
                    "dimensions_json": str(latest_row["dimensions_json"]),
                    "measurement_period_start": earliest_start,
                    "measurement_period_end": latest_end,
                    "observed_at": str(latest_row["observed_at"]),
                    "value_num": latest_row["value_num"],
                    "value_text": latest_row["value_text"],
                    "sample_count": compacted_count,
                    "quality_state": MetricQualityState.PARTIAL.value,
                    "source_binding_id": latest_row["source_binding_id"],
                    "binding_version": latest_row["binding_version"],
                }
            )
            continue

        existing_count = int(existing_rollup["sample_count"] or 0)
        existing_observed_at = _parse_iso_datetime(existing_rollup["observed_at"])
        latest_observed_at = _parse_iso_datetime(latest_row["observed_at"])
        representative_row = latest_row if latest_observed_at >= existing_observed_at else existing_rollup
        updates.append(
            {
                "observation_id": str(existing_rollup["observation_id"]),
                "measurement_period_start": min(str(existing_rollup["measurement_period_start"]), earliest_start),
                "measurement_period_end": max(str(existing_rollup["measurement_period_end"]), latest_end),
                "observed_at": str(representative_row["observed_at"]),
                "value_num": representative_row["value_num"],
                "value_text": representative_row["value_text"],
                "sample_count": existing_count + compacted_count,
                "binding_version": representative_row["binding_version"],
            }
        )

    return {
        "deleted_row_ids": deleted_row_ids,
        "insertions": insertions,
        "updates": updates,
        "rows_skipped_referenced": rows_skipped_referenced,
        "unknown_metric_ids": unknown_metric_ids,
    }


def _apply_observation_compaction(
    connection: sqlite3.Connection,
    plan: dict[str, object],
    *,
    inserted_at: datetime,
) -> None:
    deleted_row_ids = cast(list[str], plan["deleted_row_ids"])
    if deleted_row_ids:
        connection.executemany(
            "DELETE FROM reality_metric_observations WHERE observation_id = ?",
            [(row_id,) for row_id in deleted_row_ids],
        )

    for update in cast(list[dict[str, object]], plan["updates"]):
        connection.execute(
            """
            UPDATE reality_metric_observations
            SET measurement_period_start = ?,
                measurement_period_end = ?,
                observed_at = ?,
                value_num = ?,
                value_text = ?,
                sample_count = ?,
                quality_state = ?,
                binding_version = ?,
                ingestion_run_id = NULL,
                corrected_at = NULL,
                corrected_reason = NULL
            WHERE observation_id = ?
            """,
            (
                update["measurement_period_start"],
                update["measurement_period_end"],
                update["observed_at"],
                update["value_num"],
                update["value_text"],
                update["sample_count"],
                MetricQualityState.PARTIAL.value,
                update["binding_version"],
                update["observation_id"],
            ),
        )

    for insertion in cast(list[dict[str, object]], plan["insertions"]):
        connection.execute(
            """
            INSERT INTO reality_metric_observations (
                observation_id, program_id, metric_id, dimensions_json, measurement_period_start,
                measurement_period_end, observed_at, value_num, value_text, sample_count,
                quality_state, source_binding_id, binding_version, ingestion_run_id, corrected_at,
                corrected_reason, inserted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                insertion["observation_id"],
                insertion["program_id"],
                insertion["metric_id"],
                insertion["dimensions_json"],
                insertion["measurement_period_start"],
                insertion["measurement_period_end"],
                insertion["observed_at"],
                insertion["value_num"],
                insertion["value_text"],
                insertion["sample_count"],
                insertion["quality_state"],
                insertion["source_binding_id"],
                insertion["binding_version"],
                inserted_at.isoformat(),
            ),
        )


def _load_referenced_observation_ids(
    connection: sqlite3.Connection,
    *,
    excluded_evaluation_ids: set[str],
) -> set[str]:
    referenced_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT observation_id FROM reality_challenges WHERE observation_id IS NOT NULL"
        ).fetchall()
    }
    if excluded_evaluation_ids:
        placeholders = ", ".join("?" for _ in excluded_evaluation_ids)
        query = (
            "SELECT DISTINCT observation_id FROM reality_assertion_evaluations "
            f"WHERE observation_id IS NOT NULL AND id NOT IN ({placeholders})"
        )
        rows = connection.execute(query, tuple(sorted(excluded_evaluation_ids))).fetchall()
    else:
        rows = connection.execute(
            "SELECT DISTINCT observation_id FROM reality_assertion_evaluations WHERE observation_id IS NOT NULL"
        ).fetchall()
    referenced_ids.update(str(row[0]) for row in rows)
    return referenced_ids


def _observation_rollup_key(row: sqlite3.Row) -> tuple[str, str, str | None, str]:
    return (
        str(row["metric_id"]),
        str(row["dimensions_json"]),
        _optional_string(row["source_binding_id"]),
        str(row["measurement_period_end"])[:7],
    )


def _logical_evaluation_count(row: sqlite3.Row) -> int:
    note = _optional_string(row["note"])
    if note is None:
        return 1
    match = _COMPACTED_EVALUATION_NOTE.match(note)
    if match is None:
        return 1
    return int(match.group(1))


def _parse_iso_datetime(value: str | None) -> datetime:
    if value is None:
        raise ValueError("Expected an ISO datetime value.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_schema_state(db_path: Path) -> dict[str, object]:
    with _open_readonly(db_path) as connection:
        table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        present_tables = {str(row[0]) for row in table_rows}
        missing_tables = sorted(_EXPECTED_TABLES - present_tables)
        table_columns = {
            table_name: frozenset(
                str(column_row[1])
                for column_row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            )
            for table_name in present_tables
        }
        schema_rows: list[Any]
        if "schema_versions" in present_tables:
            schema_rows = connection.execute(
                "SELECT migration_id FROM schema_versions ORDER BY migration_id ASC"
            ).fetchall()
        else:
            schema_rows = []
    return {
        "present_tables": present_tables,
        "missing_tables": missing_tables,
        "table_columns": table_columns,
        "schema_versions": tuple(str(row[0]) for row in schema_rows),
    }


def _collect_schema_compatibility_issues(schema_state: dict[str, object]) -> list[str]:
    issues: list[str] = []
    present_tables = cast(set[str], schema_state["present_tables"])
    legacy_tables = sorted(_LEGACY_TABLES & present_tables)
    if legacy_tables:
        issues.append("legacy tables present: " + ", ".join(legacy_tables))

    table_columns = cast(dict[str, frozenset[str]], schema_state["table_columns"])
    for table_name, required_columns in _REQUIRED_SCHEMA_COLUMNS.items():
        present_columns = set(table_columns.get(table_name, frozenset()))
        missing_columns = sorted(required_columns - present_columns)
        if missing_columns:
            issues.append(f"{table_name} missing columns: " + ", ".join(missing_columns))

    for table_name, unsupported_columns in _UNSUPPORTED_LEGACY_COLUMNS.items():
        present_columns = set(table_columns.get(table_name, frozenset()))
        legacy_columns = sorted(unsupported_columns & present_columns)
        if legacy_columns:
            issues.append(f"{table_name} has legacy columns: " + ", ".join(legacy_columns))

    return issues


def _resolve_backup_destination(db_path: Path, dest: Path | None) -> Path:
    if dest is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return db_path.parent / "_backup" / timestamp / db_path.name
    if dest.suffix.lower() == ".sqlite3":
        return dest
    return dest / db_path.name


def _prepare_backup_destination(destination_path: Path) -> None:
    destination_root = destination_path.parent
    if destination_path.exists():
        raise typer.BadParameter(f"Backup destination already exists: {destination_path}")
    if destination_root.exists():
        if not destination_root.is_dir():
            raise typer.BadParameter(f"Backup destination must be a directory: {destination_root}")
        if any(destination_root.iterdir()):
            raise typer.BadParameter(f"Backup destination must be empty: {destination_root}")
    else:
        destination_root.mkdir(parents=True, exist_ok=False)


def _get_destination_encryption_status(destination_root: Path) -> bool | None:
    if os.name != "nt":
        return None
    mount_point = destination_root.resolve().anchor.rstrip("\\/")
    if not mount_point:
        return None
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$ErrorActionPreference='Stop'; "
                f"$volume = Get-BitLockerVolume -MountPoint '{mount_point}'; "
                "[Console]::Out.Write($volume.ProtectionStatus)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip().lower()
    if output in {"1", "on"}:
        return True
    if output in {"0", "off"}:
        return False
    return None


def _append_backup_override_audit_log(program: str, destination_path: Path) -> Path:
    audit_log_path = destination_path.parent.parent / "audit.log"
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    actor = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    with audit_log_path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "program_id": program,
                        "actor": actor,
                        "action": "db_backup_accept_unencrypted",
                        "destination_path": str(destination_path),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)
    return audit_log_path


def _relocate_database(program: str, legacy_path: Path, target_path: Path) -> dict[str, object]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_path), str(target_path))
    legacy_pointer_created = _try_create_legacy_pointer(legacy_path, target_path)

    verification = _verify_database(program, target_path)
    return {
        "program_id": program,
        "legacy_path": str(legacy_path),
        "target_path": str(target_path),
        "legacy_pointer_created": legacy_pointer_created,
        "schema_versions": verification["schema_versions"],
        "relocated_at": datetime.now(timezone.utc).isoformat(),
        "is_valid": True,
    }


def _try_create_legacy_pointer(legacy_path: Path, target_path: Path) -> bool:
    try:
        os.symlink(target_path, legacy_path)
    except OSError:
        return False
    return True


def _normalize_format(raw: str) -> str:
    normalized = raw.strip().lower()
    if normalized not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")
    return normalized


def _emit(payload: dict[str, object], *, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if payload.get("error") == "database_missing":
        typer.echo(f"Reality DB not found: {payload['db_path']}", err=True)
        return
    error = payload.get("error")
    if isinstance(error, str) and error.startswith("cannot_open:"):
        typer.echo(f"Cannot open reality DB: {error.split(':', 1)[1]}", err=True)
        return

    typer.echo(f"Reality DB verify {'OK' if payload['is_valid'] else 'FAILED'}: {payload['db_path']}")
    typer.echo(f"Program: {payload['program_id']}")
    typer.echo(f"Journal mode: {payload['journal_mode']}")
    schema_versions: list[Any] = cast(list[Any], payload.get("schema_versions", []))
    typer.echo(
        "Schema versions: " + ", ".join(str(entry) for entry in schema_versions)
        if schema_versions
        else "Schema versions: none recorded"
    )
    missing_tables: list[Any] = cast(list[Any], payload.get("missing_tables", []))
    if missing_tables:
        typer.echo("Missing tables: " + ", ".join(str(entry) for entry in missing_tables), err=True)
    integrity_messages: list[Any] = cast(list[Any], payload.get("integrity_messages", []))
    if integrity_messages and integrity_messages != ["ok"]:
        typer.echo("integrity_check: " + "; ".join(str(entry) for entry in integrity_messages), err=True)
    quick_messages: list[Any] = cast(list[Any], payload.get("quick_check_messages", []))
    if quick_messages and quick_messages != ["ok"]:
        typer.echo("quick_check: " + "; ".join(str(entry) for entry in quick_messages), err=True)
    foreign_key_issues: list[Any] = cast(list[Any], payload.get("foreign_key_issues", []))
    if foreign_key_issues:
        typer.echo(f"Foreign key issues: {len(foreign_key_issues)}", err=True)


def _emit_backup(payload: dict[str, object], *, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if payload.get("error") == "database_missing":
        typer.echo(f"Reality DB not found: {payload['source_path']}", err=True)
        return
    if payload.get("error") == "unencrypted_destination":
        typer.echo(
            f"Backup destination is not encrypted: {payload['destination_path']}. Re-run with --accept-unencrypted to override.",
            err=True,
        )
        return

    typer.echo(f"Reality DB backup complete: {payload['destination_path']}")
    typer.echo(f"Program: {payload['program_id']}")
    typer.echo(f"Source: {payload['source_path']}")
    encryption_state = payload.get("destination_encrypted")
    if encryption_state is True:
        typer.echo("Destination encryption: enabled")
    elif encryption_state is False:
        typer.echo("Destination encryption: disabled (override accepted)")
    else:
        typer.echo("Destination encryption: unknown")
    schema_versions_bak: list[Any] = cast(list[Any], payload.get("schema_versions", []))
    typer.echo(
        "Schema versions: " + ", ".join(str(entry) for entry in schema_versions_bak)
        if schema_versions_bak
        else "Schema versions: none recorded"
    )
    audit_log_path = payload.get("audit_log_path")
    if audit_log_path:
        typer.echo(f"Audit log: {audit_log_path}")


def _emit_migrate(payload: dict[str, object], *, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if payload.get("error") == "database_missing":
        typer.echo(f"Reality DB not found: {payload['db_path']}", err=True)
        return
    if payload.get("error") == "unsupported_schema_state":
        typer.echo(
            "Reality DB schema is not in a supported state for ordered migration recording.",
            err=True,
        )
        missing_tables_m: list[Any] = cast(list[Any], payload.get("missing_tables", []))
        if missing_tables_m:
            typer.echo("Missing tables: " + ", ".join(str(entry) for entry in missing_tables_m), err=True)
        schema_issues: list[Any] = cast(list[Any], payload.get("schema_issues", []))
        if schema_issues:
            for issue in schema_issues:
                typer.echo(f"- {issue}", err=True)
        return
    if payload.get("error") == "unknown_schema_versions":
        typer.echo(
            "Reality DB schema_versions contains unknown migration ids: "
            + ", ".join(str(entry) for entry in cast(list[Any], payload.get("unknown_schema_versions", []))),
            err=True,
        )
        return
    if payload.get("error") == "out_of_order_schema_versions":
        typer.echo(
            "Reality DB schema_versions is not a contiguous applied prefix of the supported migration ladder.",
            err=True,
        )
        return

    pending: list[Any] = cast(list[Any], payload.get("pending_migrations", []))
    applied: list[Any] = cast(list[Any], payload.get("applied_migrations", []))
    typer.echo(f"Reality DB migrate complete: {payload['db_path']}")
    typer.echo(f"Program: {payload['program_id']}")
    typer.echo(f"Pending migrations: {len(pending)}")
    if pending:
        for migration in cast(list[dict[str, Any]], pending):
            typer.echo(f"- {migration['migration_id']}: {migration['description']}")
    if payload.get("dry_run"):
        typer.echo("Dry-run: no schema_versions rows were written.")
    elif applied:
        typer.echo("Applied migrations: " + ", ".join(str(entry) for entry in applied))
    else:
        typer.echo("Applied migrations: none")


def _emit_compact(payload: dict[str, object], *, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if payload.get("error") == "database_missing":
        typer.echo(f"Reality DB not found: {payload['db_path']}", err=True)
        return

    evaluation = cast(dict[str, Any], payload["evaluation_compaction"])
    observations = cast(dict[str, Any], payload["observation_compaction"])
    operation = "preview" if payload.get("dry_run") else "complete"
    typer.echo(f"Reality DB compact {operation}: {payload['db_path']}")
    typer.echo(f"Program: {payload['program_id']}")
    typer.echo(
        "Assertion evaluations: "
        f"{evaluation['rows_deleted']} row(s) -> {evaluation['summary_rows_written']} summary row(s) across {evaluation['pairs_compacted']} pair(s)"
    )
    typer.echo(
        "Metric observations: "
        f"{observations['rows_deleted']} row(s) compacted; {observations['rollup_rows_inserted']} rollup row(s) inserted; "
        f"{observations['rollup_rows_updated']} rollup row(s) updated"
    )
    typer.echo(f"Referenced observation rows skipped: {observations['rows_skipped_referenced']}")
    unknown_metric_ids: list[Any] = cast(list[Any], observations.get("unknown_metric_ids", []))
    if unknown_metric_ids:
        typer.echo("Skipped metrics without retention definitions: " + ", ".join(str(metric_id) for metric_id in unknown_metric_ids))
    if payload.get("dry_run"):
        typer.echo("Dry-run: no database rows were written.")
    else:
        typer.echo(f"Sentinel updated: {payload['sentinel_path']}")


def _emit_relocate(payload: dict[str, object], *, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    error = payload.get("error")
    if error == "legacy_database_missing":
        typer.echo(f"Legacy workspace DB not found: {payload['legacy_path']}", err=True)
        return
    if error == "target_database_exists":
        typer.echo(f"Target reality DB already exists: {payload['target_path']}", err=True)
        return

    typer.echo(f"Reality DB relocated: {payload['legacy_path']} -> {payload['target_path']}")
    typer.echo(f"Program: {payload['program_id']}")
    typer.echo(
        "Legacy pointer: created" if payload.get("legacy_pointer_created") else "Legacy pointer: not created"
    )
    schema_versions_rsync: list[Any] = cast(list[Any], payload.get("schema_versions", []))
    typer.echo(
        "Schema versions: " + ", ".join(str(entry) for entry in schema_versions_rsync)
        if schema_versions_rsync
        else "Schema versions: none recorded"
    )
