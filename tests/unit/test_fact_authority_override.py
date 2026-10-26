from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core import program_fact_store, store_factory
from src.core.exceptions import ConfigError


@pytest.mark.parametrize("value", ["unexpected", "pimary", "disabled"])
def test_invalid_explicit_mode_never_selects_legacy_or_reads_persisted_state(
    monkeypatch, value,
) -> None:
    reader = Mock(side_effect=AssertionError("Invalid mode read persisted state"))
    monkeypatch.setattr(program_fact_store, "load_fact_sor_state", reader)
    with pytest.raises(ConfigError, match="VERTEX_FACT_SOR") as caught:
        program_fact_store.resolve_fact_sor_mode(
            program_id="fixture", environ={"VERTEX_FACT_SOR": value},
        )
    assert value not in str(caught.value)
    reader.assert_not_called()


def test_explicit_empty_environment_does_not_inherit_ambient_override(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    assert program_fact_store.resolve_fact_sor_mode(environ={}) == "legacy"


@pytest.mark.parametrize("mode", ["legacy", "shadow", "primary"])
def test_valid_mode_normalization_is_compatible(mode) -> None:
    assert program_fact_store.resolve_fact_sor_mode(
        environ={"VERTEX_FACT_SOR": f" {mode.upper()} "},
    ) == mode


def test_invalid_mode_blocks_live_fact_storage_before_open(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VERTEX_FACT_SOR", "invalid-authority")
    store = Mock(side_effect=AssertionError("Invalid authority opened fact store"))
    monkeypatch.setattr(program_fact_store, "ProgramFactStore", store)
    with pytest.raises(ConfigError, match="VERTEX_FACT_SOR"):
        program_fact_store.load_program_facts(
            "fixture", programs_root=tmp_path / "programs", db_root=tmp_path / "db",
        )
    store.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_invalid_mode_blocks_actual_backend_resolver_before_legacy_load(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VERTEX_FACT_SOR", "invalid-authority")
    reader = Mock(side_effect=AssertionError("Invalid authority selected legacy backend"))
    monkeypatch.setattr(store_factory, "load_program", reader)
    with pytest.raises(ConfigError, match="VERTEX_FACT_SOR"):
        store_factory.resolve_sor_aware_backend("fixture", programs_root=tmp_path / "programs")
    reader.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_override_retains_persisted_mode(monkeypatch, value, tmp_path) -> None:
    state = Mock(mode="shadow")
    reader = Mock(return_value=state)
    monkeypatch.setattr(program_fact_store, "load_fact_sor_state", reader)
    root = tmp_path / "programs"
    assert program_fact_store.resolve_fact_sor_mode(
        program_id="fixture", programs_root=root, environ={"VERTEX_FACT_SOR": value},
    ) == "shadow"
    reader.assert_called_once_with("fixture", programs_root=root)


def test_historical_snapshot_retains_direct_read_despite_invalid_live_override(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("VERTEX_FACT_SOR", "invalid-authority")
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = program_fact_store.ProgramFactSnapshot("fixture", instant, ())
    store = Mock()
    store.return_value.snapshot.return_value = snapshot
    monkeypatch.setattr(program_fact_store, "ProgramFactStore", store)
    reader = Mock(side_effect=AssertionError("Historical snapshot read live authority"))
    monkeypatch.setattr(program_fact_store, "load_fact_sor_state", reader)
    assert program_fact_store.load_program_facts(
        "fixture", programs_root=tmp_path / "programs", db_root=tmp_path / "db", as_of=instant,
    ) is snapshot
    reader.assert_not_called()
    store.return_value.snapshot.assert_called_once_with(as_of=instant, valid_at=None)
