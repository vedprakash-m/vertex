"""specs/people.md Phase 1, PPL-W1.3: tests for registry storage-class
qualification (src/core/people_registry_storage_class.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.core.people_registry_storage_class as storage_class_module
from src.core.exceptions import ConfigError
from src.core.people_registry_storage_class import (
    STORAGE_CLASS_LOCAL,
    STORAGE_CLASS_NETWORK_CONDITIONAL,
    STORAGE_CLASS_UNSUPPORTED_SYNC,
    ensure_storage_qualified_for_primary,
    load_registry_storage_status,
    qualify_registry_storage,
    refresh_registry_storage_status,
    registry_capability_status_path,
)


def test_local_path_qualifies_as_local_and_is_qualified_for_primary(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    qualification = qualify_registry_storage(knowledge_root)

    assert qualification.storage_class == STORAGE_CLASS_LOCAL
    assert qualification.qualified_for_primary is True


def test_network_path_qualifies_as_conditional_and_is_qualified_for_primary(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(storage_class_module, "_is_network_filesystem_path", lambda path: True)

    qualification = qualify_registry_storage(knowledge_root)

    assert qualification.storage_class == STORAGE_CLASS_NETWORK_CONDITIONAL
    assert qualification.qualified_for_primary is True


def test_cloud_sync_path_is_unsupported_and_not_qualified_for_primary(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(storage_class_module, "_is_cloud_sync_path", lambda path: True)

    qualification = qualify_registry_storage(knowledge_root)

    assert qualification.storage_class == STORAGE_CLASS_UNSUPPORTED_SYNC
    assert qualification.qualified_for_primary is False


def test_cloud_sync_detection_matches_paths_under_onedrive_env_root(monkeypatch, tmp_path: Path) -> None:
    onedrive_root = tmp_path / "OneDrive - Acme"
    knowledge_root = onedrive_root / "vertex" / "knowledge"
    monkeypatch.setenv("OneDriveCommercial", str(onedrive_root))
    monkeypatch.setattr(storage_class_module, "_is_network_filesystem_path", lambda path: False)

    qualification = qualify_registry_storage(knowledge_root)

    assert qualification.storage_class == STORAGE_CLASS_UNSUPPORTED_SYNC


def test_cloud_sync_detection_does_not_flag_unrelated_local_path(monkeypatch, tmp_path: Path) -> None:
    onedrive_root = tmp_path / "OneDrive - Acme"
    unrelated_root = tmp_path / "some_other_dir" / "knowledge"
    monkeypatch.setenv("OneDriveCommercial", str(onedrive_root))
    monkeypatch.setattr(storage_class_module, "_is_network_filesystem_path", lambda path: False)

    qualification = qualify_registry_storage(unrelated_root)

    assert qualification.storage_class == STORAGE_CLASS_LOCAL


def test_refresh_persists_status_and_load_reads_it_back(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    refreshed = refresh_registry_storage_status(knowledge_root)
    loaded = load_registry_storage_status(knowledge_root)

    assert registry_capability_status_path(knowledge_root).exists()
    assert loaded is not None
    assert loaded.storage_class == refreshed.storage_class
    assert loaded.qualified_for_primary == refreshed.qualified_for_primary
    assert loaded.detail == refreshed.detail


def test_load_registry_storage_status_before_refresh_returns_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    assert load_registry_storage_status(knowledge_root) is None


def test_ensure_storage_qualified_for_primary_passes_on_local_storage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    ensure_storage_qualified_for_primary(knowledge_root)  # Must not raise.


def test_ensure_storage_qualified_for_primary_blocks_on_mocked_unsupported_filesystem(monkeypatch, tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W1.3 verification: "Contract test
    # with a mocked unsupported filesystem asserts promotion is blocked."
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(storage_class_module, "_is_cloud_sync_path", lambda path: True)

    with pytest.raises(ConfigError, match="Cannot promote registry write_mode to 'primary'"):
        ensure_storage_qualified_for_primary(knowledge_root)

    # The blocked decision is still persisted, so doctor/status reflect the live truth.
    persisted = load_registry_storage_status(knowledge_root)
    assert persisted is not None
    assert persisted.qualified_for_primary is False


def test_ensure_storage_qualified_for_primary_always_recomputes_live_not_stale_cache(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    monkeypatch.setattr(storage_class_module, "_is_cloud_sync_path", lambda path: True)
    refresh_registry_storage_status(knowledge_root)  # Persist an "unsupported" snapshot.

    # Storage becomes qualified after the fact (e.g. the folder was moved) --
    # the gate must not trust the stale persisted "unsupported" snapshot.
    monkeypatch.setattr(storage_class_module, "_is_cloud_sync_path", lambda path: False)

    ensure_storage_qualified_for_primary(knowledge_root)  # Must not raise.

    refreshed = load_registry_storage_status(knowledge_root)
    assert refreshed is not None
    assert refreshed.qualified_for_primary is True


def test_load_registry_storage_status_rejects_invalid_storage_class(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    path = registry_capability_status_path(knowledge_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("storage_class: not_a_real_class\nqualified_for_primary: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="storage_class must be one of"):
        load_registry_storage_status(knowledge_root)
