"""specs/people.md Phase 1, PPL-W1.8: tests for manifest-consistent
registry backup/restore (src/core/people_registry_backup.py).

specs/people.md §9.1's own verification bar for PPL-W1.8: "generation-
consistent backup/restore"."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_registry_backup import (
    RegistryGenerationChangedDuringBackup,
    create_registry_backup_snapshot,
    restore_registry_backup_snapshot,
)
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_transaction import (
    RegistryFieldOperation,
    RegistryOperationKind,
    SyntheticRegistryRecord,
    commit_registry_transaction,
    prepare_registry_transaction,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _bootstrap(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)


def _commit_one_record(knowledge_root: Path, record_id: str = "rec-1") -> None:
    record = SyntheticRegistryRecord(record_id=record_id, value="v", source="graph", observed_at=_NOW, actor="tester")
    prepared = prepare_registry_transaction(
        knowledge_root, (RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=record),), owner="writer-1", as_of=_NOW
    )
    commit_registry_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def test_backup_requires_a_bootstrapped_registry(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    destination = tmp_path / "backup"

    with pytest.raises(ConfigError, match="has not been bootstrapped"):
        create_registry_backup_snapshot(knowledge_root, destination)


def test_backup_captures_bootstrap_only_registry(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    destination = tmp_path / "backup"

    result = create_registry_backup_snapshot(knowledge_root, destination)

    manifest = load_registry_manifest(knowledge_root)
    assert result.generation_id == manifest.generation_id
    assert (destination / "registry.yaml").exists()
    assert (destination / "registry_manifest.json").exists()
    assert (destination / "registry_backup_manifest.json").exists()


def test_backup_captures_committed_data_and_journal(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    _commit_one_record(knowledge_root)
    destination = tmp_path / "backup"

    result = create_registry_backup_snapshot(knowledge_root, destination)

    assert result.file_count >= 3  # registry.yaml, manifest, synthetic records at minimum.
    assert (destination / "registry_synthetic_records.yaml").exists()


def test_backup_destination_must_be_empty(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    destination = tmp_path / "backup"
    destination.mkdir()
    (destination / "existing_file.txt").write_text("not empty", encoding="utf-8")

    with pytest.raises(ConfigError, match="must be empty"):
        create_registry_backup_snapshot(knowledge_root, destination)


def test_backup_raises_and_cleans_up_when_generation_changes_during_copy(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    _commit_one_record(knowledge_root, record_id="rec-1")
    destination = tmp_path / "backup"

    import shutil as shutil_module

    import src.core.people_registry_backup as backup_module

    real_copy2 = shutil_module.copy2
    triggered = {"done": False}

    def _copy2_with_race(source, destination_path, *args, **kwargs):
        if not triggered["done"]:
            triggered["done"] = True
            _commit_one_record(knowledge_root, record_id="rec-2")  # A commit races in mid-backup.
        return real_copy2(source, destination_path, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, "copy2", _copy2_with_race)

    with pytest.raises(RegistryGenerationChangedDuringBackup):
        create_registry_backup_snapshot(knowledge_root, destination)

    assert not destination.exists() or not any(destination.iterdir())  # Cleaned up, not left half-written.


def test_restore_round_trips_and_verifies_generation(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    _commit_one_record(knowledge_root)
    destination = tmp_path / "backup"
    snapshot = create_registry_backup_snapshot(knowledge_root, destination)

    restore_target = tmp_path / "restored_knowledge"
    result = restore_registry_backup_snapshot(destination, restore_target)

    assert result.verified is True
    assert result.generation_id == snapshot.generation_id
    assert (restore_target / "registry_synthetic_records.yaml").read_text(encoding="utf-8") == (
        knowledge_root / "registry_synthetic_records.yaml"
    ).read_text(encoding="utf-8")
    restored_manifest = load_registry_manifest(restore_target)
    assert restored_manifest.generation_id == snapshot.generation_id


def test_restore_raises_on_tampered_backup_file(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    _commit_one_record(knowledge_root)
    destination = tmp_path / "backup"
    create_registry_backup_snapshot(knowledge_root, destination)
    (destination / "registry_synthetic_records.yaml").write_text("tampered: true\n", encoding="utf-8")

    restore_target = tmp_path / "restored_knowledge"
    with pytest.raises(ConfigError, match="hash mismatch"):
        restore_registry_backup_snapshot(destination, restore_target)
