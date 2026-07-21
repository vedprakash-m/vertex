"""specs/people.md Phase 1, PPL-W1.9: tests for per-program modes and
kill switches (src/core/people_registry_modes.py).

specs/people.md §9.1's own verification bar: "Flipping any mode/flag
returns to legacy behavior without rewriting customer data" (§11.1
"Feature rollback")."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config
from src.core.people_registry_lease import read_registry_lease_state
from src.core.people_registry_modes import (
    ENV_DISABLE_AUDIENCE_EXPANSION,
    ENV_DISABLE_PROVIDER_REFRESH,
    ENV_FORCE_LEGACY,
    load_effective_registry_config,
    program_shadow_status,
    set_program_mode,
    set_registry_flag,
    set_workspace_write_mode,
)


def _bootstrap(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)


def _synthetic_records_path(knowledge_root: Path) -> Path:
    from src.core.people_registry_transaction import synthetic_records_path

    return synthetic_records_path(knowledge_root)


def test_set_workspace_write_mode_requires_bootstrap(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    with pytest.raises(ConfigError, match="has not been bootstrapped"):
        set_workspace_write_mode(knowledge_root, "shadow", actor="operator")


def test_set_workspace_write_mode_to_shadow_and_back_to_legacy_touches_no_data_file(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    set_workspace_write_mode(knowledge_root, "shadow", actor="operator")
    updated = set_workspace_write_mode(knowledge_root, "legacy", actor="operator")

    assert updated.write_mode == "legacy"
    # "Flipping any mode/flag returns to legacy behavior without rewriting customer data."
    assert not _synthetic_records_path(knowledge_root).exists()
    assert read_registry_lease_state(knowledge_root=knowledge_root) is None  # Lease released each time.


def test_set_workspace_write_mode_rejects_invalid_mode(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    with pytest.raises(ConfigError, match="write_mode must be one of"):
        set_workspace_write_mode(knowledge_root, "bogus", actor="operator")


def test_set_workspace_write_mode_to_primary_blocked_on_unsupported_storage(monkeypatch, tmp_path: Path) -> None:
    import src.core.people_registry_storage_class as storage_class_module

    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    monkeypatch.setattr(storage_class_module, "_is_cloud_sync_path", lambda path: True)

    with pytest.raises(ConfigError, match="Cannot promote registry write_mode to 'primary'"):
        set_workspace_write_mode(knowledge_root, "primary", actor="operator")

    assert load_registry_config(knowledge_root).write_mode == "legacy"  # Unchanged.
    assert read_registry_lease_state(knowledge_root=knowledge_root) is None  # Never acquired -- gate checked first.


def test_set_workspace_write_mode_to_primary_succeeds_on_qualified_storage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    updated = set_workspace_write_mode(knowledge_root, "primary", actor="operator")

    assert updated.write_mode == "primary"


def test_set_program_mode_legacy_and_shadow_round_trip(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    set_program_mode(knowledge_root, "acme", "shadow", actor="operator")
    updated = set_program_mode(knowledge_root, "acme", "legacy", actor="operator")

    assert updated.program_mode("acme") == "legacy"
    assert not _synthetic_records_path(knowledge_root).exists()


def test_set_program_mode_to_primary_rejects_a_direct_legacy_jump(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    with pytest.raises(ConfigError, match="must be in 'shadow'"):
        set_program_mode(knowledge_root, "acme", "primary", actor="operator")

    assert load_registry_config(knowledge_root).program_mode("acme") == "legacy"


def test_set_program_mode_only_touches_the_named_program(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    set_program_mode(knowledge_root, "acme", "shadow", actor="operator")

    updated = set_program_mode(knowledge_root, "fabrikam", "shadow", actor="operator")

    assert updated.program_mode("acme") == "shadow"
    assert updated.program_mode("fabrikam") == "shadow"
    assert updated.program_mode("unlisted-program") == "legacy"  # Default per §6.6.


def test_set_registry_flag_round_trips(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="operator")
    updated = set_registry_flag(knowledge_root, "provider_refresh_enabled", False, actor="operator")

    assert updated.provider_refresh_enabled is False
    assert not _synthetic_records_path(knowledge_root).exists()


def test_set_registry_flag_rejects_unknown_flag_name(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    with pytest.raises(ConfigError, match="flag_name must be one of"):
        set_registry_flag(knowledge_root, "not_a_real_flag", True, actor="operator")


def test_effective_config_force_legacy_kill_switch_masks_persisted_primary(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    set_workspace_write_mode(knowledge_root, "primary", actor="operator")
    monkeypatch.setenv(ENV_FORCE_LEGACY, "1")

    effective = load_effective_registry_config(knowledge_root)

    assert effective.persisted.write_mode == "primary"  # On-disk value untouched.
    assert effective.effective_write_mode == "legacy"  # Kill switch masks it at read time.
    assert effective.force_legacy_active is True
    assert effective.effective_program_mode("acme") == "legacy"


def test_effective_config_disables_provider_refresh_via_env(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="operator")
    monkeypatch.setenv(ENV_DISABLE_PROVIDER_REFRESH, "1")

    effective = load_effective_registry_config(knowledge_root)

    assert effective.persisted.provider_refresh_enabled is True
    assert effective.effective_provider_refresh_enabled is False
    assert effective.provider_refresh_disabled_by_env is True


def test_effective_config_disables_audience_expansion_via_env(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    set_registry_flag(knowledge_root, "audience_scopes_enabled", True, actor="operator")
    monkeypatch.setenv(ENV_DISABLE_AUDIENCE_EXPANSION, "1")

    effective = load_effective_registry_config(knowledge_root)

    assert effective.effective_audience_scopes_enabled is False


def test_effective_config_with_no_kill_switches_matches_persisted(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    effective = load_effective_registry_config(knowledge_root)

    assert effective.effective_write_mode == effective.persisted.write_mode == "legacy"
    assert effective.force_legacy_active is False


def test_effective_config_returns_none_before_bootstrap(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    assert load_effective_registry_config(knowledge_root) is None


def test_program_shadow_status_reports_current_mode(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    set_program_mode(knowledge_root, "acme", "shadow", actor="operator")

    status = program_shadow_status(knowledge_root, "acme")

    assert status is not None
    assert status.mode == "shadow"
    assert status.divergence_tracking_available is True


def test_program_shadow_status_returns_none_before_bootstrap(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    assert program_shadow_status(knowledge_root, "acme") is None
