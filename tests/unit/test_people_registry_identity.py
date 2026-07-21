"""specs/people.md Phase 1, PPL-W1.1: tests for the workspace registry
identity bootstrap (src/core/people_registry_identity.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_registry_identity import (
    RegistryConfig,
    bootstrap_registry_identity,
    load_registry_config,
    load_registry_manifest,
    registry_config_path,
    registry_manifest_path,
)

_AS_OF = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def test_bootstrap_preview_without_apply_writes_nothing(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    result = bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=False, as_of=_AS_OF)

    assert result.created is False
    assert not registry_config_path(knowledge_root).exists()
    assert not registry_manifest_path(knowledge_root).exists()


def test_bootstrap_apply_requires_customer_boundary_id(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    with pytest.raises(ConfigError, match="customer-boundary-id"):
        bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id=None, apply=True, as_of=_AS_OF)


def test_bootstrap_apply_mints_and_persists_identity(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    result = bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True, as_of=_AS_OF)

    assert result.created is True
    assert result.identity.workspace_id.startswith("workspace:")
    assert result.identity.customer_boundary_id == "acme-corp"
    assert registry_config_path(knowledge_root).exists()
    assert registry_manifest_path(knowledge_root).exists()

    reloaded_config = load_registry_config(knowledge_root)
    reloaded_manifest = load_registry_manifest(knowledge_root)
    assert reloaded_config is not None and reloaded_manifest is not None
    assert reloaded_config.workspace_id == result.identity.workspace_id
    assert reloaded_config.write_mode == "legacy"
    assert reloaded_manifest.workspace_id == result.identity.workspace_id
    assert reloaded_manifest.fencing_token == 0
    assert reloaded_manifest.transaction_id is None


def test_bootstrap_is_idempotent_never_remints(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    first = bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True, as_of=_AS_OF)
    # Repeated calls -- even with a DIFFERENT customer_boundary_id argument or --apply toggled --
    # must observe the same immutable identity, never mint a second one.
    second = bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="different-tenant", apply=True, as_of=_AS_OF)
    third = bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id=None, apply=False, as_of=_AS_OF)

    assert second.created is False
    assert third.created is False
    assert first.identity.workspace_id == second.identity.workspace_id == third.identity.workspace_id
    assert second.identity.customer_boundary_id == "acme-corp"  # Not overwritten by the second call's argument.


def test_bootstrap_detects_inconsistent_partial_state(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    registry_config_path(knowledge_root).write_text(
        'schema_version: "1.0"\nworkspace_id: "workspace:X"\ncustomer_boundary_id: "acme"\nwrite_mode: legacy\n',
        encoding="utf-8",
    )
    # registry_manifest.json deliberately absent -- simulates a crash between the two writes.

    with pytest.raises(ConfigError, match="inconsistent state"):
        bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme", apply=True, as_of=_AS_OF)


def test_load_registry_config_rejects_invalid_write_mode(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    registry_config_path(knowledge_root).write_text(
        'schema_version: "1.0"\nworkspace_id: "workspace:X"\ncustomer_boundary_id: "acme"\nwrite_mode: bogus\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="write_mode"):
        load_registry_config(knowledge_root)


def test_registry_config_program_mode_defaults_to_legacy_for_unlisted_program() -> None:
    config = RegistryConfig(
        schema_version="1.0",
        workspace_id="workspace:X",
        customer_boundary_id="acme",
        write_mode="legacy",
        program_modes=(("acme", "primary"),),
    )

    assert config.program_mode("acme") == "primary"
    assert config.program_mode("unlisted_program") == "legacy"


def test_load_registry_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_registry_config(tmp_path / "knowledge") is None
    assert load_registry_manifest(tmp_path / "knowledge") is None
