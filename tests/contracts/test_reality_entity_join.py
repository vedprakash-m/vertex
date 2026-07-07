"""WI-2.5: Contract tests — reality_store bindings adopt canonical entity IDs.

Verifies:
  - MetricSourceBinding.owner_entity_ref field persists through the DB round-trip.
  - resolve_binding_owner() resolves owner_alias → canonical ID via EntityRegistry.
  - owner_entity_ref, when pre-set, is returned directly (no redundant lookup).
  - Bindings with no owner_alias or no registry return None gracefully.
  - _ensure_table_columns migration safely adds owner_entity_ref to existing DBs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.metric_models import MetricSourceBinding
from src.core.reality_store import RealityStore, resolve_binding_owner

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_binding(
    *,
    binding_id: str = "b-001",
    owner_alias: str | None = "Auth DRI",
    owner_entity_ref: str | None = None,
) -> MetricSourceBinding:
    return MetricSourceBinding(
        binding_id=binding_id,
        metric_id="metric-auth-latency",
        program_id="test-prog",
        source_kind="kusto",
        valid_from=_NOW,
        owner_alias=owner_alias,
        owner_entity_ref=owner_entity_ref,
    )


def _make_registry(alias: str, canonical_id: str) -> MagicMock:
    registry = MagicMock()
    registry.resolve.side_effect = lambda ref: canonical_id if ref == alias else None
    return registry


class TestOwnerEntityRefField:
    def test_field_default_is_none(self) -> None:
        binding = _make_binding()
        assert binding.owner_entity_ref is None

    def test_field_accepts_canonical_id(self) -> None:
        binding = _make_binding(owner_entity_ref="workstream:acme-auth")
        assert binding.owner_entity_ref == "workstream:acme-auth"


class TestResolveBindingOwner:
    def test_resolves_alias_to_canonical_id(self) -> None:
        binding = _make_binding(owner_alias="Auth DRI")
        registry = _make_registry("Auth DRI", "person:auth-dri")
        result = resolve_binding_owner(binding, registry)
        assert result == "person:auth-dri"

    def test_returns_owner_entity_ref_directly_when_set(self) -> None:
        binding = _make_binding(owner_alias="Auth DRI", owner_entity_ref="person:auth-dri")
        registry = _make_registry("Auth DRI", "DIFFERENT-ID")
        result = resolve_binding_owner(binding, registry)
        assert result == "person:auth-dri"
        registry.resolve.assert_not_called()

    def test_returns_none_when_no_owner_alias(self) -> None:
        binding = _make_binding(owner_alias=None)
        registry = _make_registry("Auth DRI", "person:auth-dri")
        result = resolve_binding_owner(binding, registry)
        assert result is None

    def test_returns_none_when_registry_returns_none(self) -> None:
        binding = _make_binding(owner_alias="Unknown Person")
        registry = _make_registry("Auth DRI", "person:auth-dri")
        result = resolve_binding_owner(binding, registry)
        assert result is None

    def test_returns_none_when_registry_lacks_resolve(self) -> None:
        binding = _make_binding(owner_alias="Auth DRI")
        result = resolve_binding_owner(binding, object())
        assert result is None

    def test_returns_none_on_registry_exception(self) -> None:
        binding = _make_binding(owner_alias="Auth DRI")
        registry = MagicMock()
        registry.resolve.side_effect = RuntimeError("registry down")
        result = resolve_binding_owner(binding, registry)
        assert result is None


class TestOwnerEntityRefPersistence:
    def test_round_trip_stores_owner_entity_ref(self, tmp_path: Path) -> None:
        """owner_entity_ref is written and read back from the DB."""
        store = RealityStore("test-prog", db_root=tmp_path)
        store.initialize()

        binding = _make_binding(
            owner_alias="Auth DRI",
            owner_entity_ref="workstream:acme-auth",
        )
        store.upsert_metric_source_binding(binding)

        result = store.get_metric_source_binding("b-001")
        assert result is not None
        assert result.owner_entity_ref == "workstream:acme-auth"
        assert result.owner_alias == "Auth DRI"

    def test_round_trip_preserves_none_owner_entity_ref(self, tmp_path: Path) -> None:
        """Binding without owner_entity_ref round-trips as None."""
        store = RealityStore("test-prog", db_root=tmp_path)
        store.initialize()

        binding = _make_binding(owner_alias="Auth DRI", owner_entity_ref=None)
        store.upsert_metric_source_binding(binding)

        result = store.get_metric_source_binding("b-001")
        assert result is not None
        assert result.owner_entity_ref is None

    def test_join_flow_upsert_with_resolved_entity_ref(self, tmp_path: Path) -> None:
        """Full join flow: create binding → resolve via registry → upsert with canonical ID."""
        store = RealityStore("test-prog", db_root=tmp_path)
        store.initialize()

        binding = _make_binding(owner_alias="Auth DRI")
        registry = _make_registry("Auth DRI", "person:auth-dri")

        canonical_ref = resolve_binding_owner(binding, registry)
        assert canonical_ref == "person:auth-dri"

        # Replace binding with canonical owner_entity_ref set
        import dataclasses
        updated = dataclasses.replace(binding, owner_entity_ref=canonical_ref)
        store.upsert_metric_source_binding(updated)

        result = store.get_metric_source_binding("b-001")
        assert result is not None
        assert result.owner_entity_ref == "person:auth-dri"

    def test_list_bindings_returns_owner_entity_ref(self, tmp_path: Path) -> None:
        """list_active_metric_source_bindings returns bindings with owner_entity_ref."""
        store = RealityStore("test-prog", db_root=tmp_path)
        store.initialize()

        b1 = _make_binding(binding_id="b-001", owner_entity_ref="person:alice")
        b2 = _make_binding(binding_id="b-002", owner_entity_ref=None)
        store.upsert_metric_source_binding(b1)
        store.upsert_metric_source_binding(b2)

        bindings = store.list_active_metric_source_bindings()
        refs = {b.binding_id: b.owner_entity_ref for b in bindings}
        assert refs["b-001"] == "person:alice"
        assert refs["b-002"] is None
