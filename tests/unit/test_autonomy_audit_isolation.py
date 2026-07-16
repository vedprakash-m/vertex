"""ADF-W1.8: autonomy_audit.jsonl must never crash on a foreign/mixed-schema row.

Reproduces the real defect ("single foreign migration_audit row" contaminating
programs/xpf/journal/autonomy_audit.jsonl, verified 2026-07-11): a row shaped
like a different sidecar's schema (e.g. migration_log-style fields, missing
the required autonomy-audit fields) used to raise KeyError and crash every
reader, including `vertex maturity-check`.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.analytics_store import (
    get_program_autonomy_audit_path,
    load_autonomy_audit_records,
)
from src.core.migration_log import read_migration_log
from scripts.migrate_autonomy_audit import migrate_autonomy_audit

_VALID_ROW_1 = {
    "schema_version": "1.0",
    "action_id": "action-001",
    "level": "l2",
    "author_alias": "operator@example.com",
    "evidence_refs": ["event:abc"],
    "policy_rule": "autonomy_ceiling.ado_create_task",
    "accepted": True,
    "applied_at": "2026-07-01T00:00:00+00:00",
}
_VALID_ROW_2 = {
    "schema_version": "1.0",
    "action_id": "action-002",
    "level": "l1",
    "author_alias": "operator@example.com",
    "evidence_refs": [],
    "policy_rule": None,
    "accepted": False,
    "applied_at": "2026-07-02T00:00:00+00:00",
}
# Real-shape foreign row: a migration_log.jsonl-style entry that was
# accidentally appended to autonomy_audit.jsonl (plausible mixup given the
# similar filenames/schemas). Missing action_id/level/author_alias/applied_at.
_FOREIGN_ROW = {
    "id": "8f3c1e2a-0000-0000-0000-000000000000",
    "timestamp": "2026-06-15T00:00:00+00:00",
    "kind": "chart_id_alias",
    "source_id": "xpf::deployment_velocity",
    "target_id": "core::deployment_velocity",
    "files_touched": ["programs/xpf/knowledge/dashboards/foo.yaml"],
    "dry_run": False,
    "operator": "vertex migrate",
}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_all_valid_rows_load_normally(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    _write_jsonl(path, [_VALID_ROW_1, _VALID_ROW_2])

    records = load_autonomy_audit_records("fixture_prog", programs_root=programs_root)

    assert len(records) == 2
    assert {r.action_id for r in records} == {"action-001", "action-002"}


def test_foreign_row_is_quarantined_not_crashed(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    _write_jsonl(path, [_VALID_ROW_1, _FOREIGN_ROW, _VALID_ROW_2])

    records = load_autonomy_audit_records("fixture_prog", programs_root=programs_root)  # must not raise

    assert len(records) == 2
    assert {r.action_id for r in records} == {"action-001", "action-002"}

    # The live file now contains only the valid rows.
    remaining_lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(remaining_lines) == 2

    # The original (all three rows) is preserved verbatim in quarantine.
    quarantine_dir = path.parent / "quarantine"
    quarantine_files = list(quarantine_dir.glob("autonomy_audit.*.jsonl"))
    assert len(quarantine_files) == 1
    quarantined_lines = [line for line in quarantine_files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(quarantined_lines) == 3
    quarantined_payloads = [json.loads(line) for line in quarantined_lines]
    assert _FOREIGN_ROW in quarantined_payloads


def test_second_read_after_quarantine_is_clean_and_idempotent(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    _write_jsonl(path, [_VALID_ROW_1, _FOREIGN_ROW])

    first = load_autonomy_audit_records("fixture_prog", programs_root=programs_root)
    second = load_autonomy_audit_records("fixture_prog", programs_root=programs_root)

    assert len(first) == 1
    assert len(second) == 1
    # No new quarantine file on the second (already-clean) read.
    quarantine_dir = path.parent / "quarantine"
    assert len(list(quarantine_dir.glob("autonomy_audit.*.jsonl"))) == 1


def test_absent_file_returns_empty_without_error(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_autonomy_audit_records("fixture_prog", programs_root=programs_root) == ()


def test_migrate_script_records_migration_log_entry_when_foreign_rows_found(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    _write_jsonl(path, [_VALID_ROW_1, _FOREIGN_ROW])

    quarantined = migrate_autonomy_audit("fixture_prog", programs_root=programs_root)

    assert quarantined == 1
    log_entries = read_migration_log("fixture_prog", programs_root)
    assert len(log_entries) == 1
    assert log_entries[0].kind == "autonomy_audit_foreign_row_quarantine"


def test_migrate_script_is_noop_on_already_clean_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_program_autonomy_audit_path("fixture_prog", programs_root=programs_root)
    _write_jsonl(path, [_VALID_ROW_1, _VALID_ROW_2])

    quarantined = migrate_autonomy_audit("fixture_prog", programs_root=programs_root)

    assert quarantined == 0
    assert read_migration_log("fixture_prog", programs_root) == ()


def test_migrate_script_on_absent_program_is_noop(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert migrate_autonomy_audit("nonexistent_prog", programs_root=programs_root) == 0
