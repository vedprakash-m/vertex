"""S-2c: Entity resolution before fact shaping — contract tests.

Verifies that bridge-authority facts get their entity_refs resolved
against known natural keys before being written to the fact store.

Key invariants:
  - Exact match → canonical ref retained.
  - Numeric short-form → resolved to canonical ``namespace:NNN`` key.
  - No match → prefixed ``UNRESOLVED:<original>`` (never silent drop).
  - Non-bridge (human, system) → passthrough, unchanged.
  - Empty known_natural_keys → all refs are UNRESOLVED.
"""
from __future__ import annotations

import pytest

from src.core.entity_resolution import (
    FactEntityResolutionResult,
    resolve_fact_entity_refs_for_store,
    _UNRESOLVED_PREFIX,
    _BRIDGE_WRITE_AUTHORITY,
)


class TestPassthrough:

    def test_human_write_authority_is_passthrough(self) -> None:
        refs = ("workitem:1234", "workitem:5678")
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset({"workitem:9999"}),
            write_authority="human",
        )
        assert result.resolution_strategy == "passthrough"
        assert result.resolved_refs == refs
        assert result.unresolved_count == 0

    def test_system_write_authority_is_passthrough(self) -> None:
        refs = ("workitem:1111",)
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset(),
            write_authority="system",
        )
        assert result.resolution_strategy == "passthrough"
        assert result.resolved_refs == refs

    def test_empty_refs_passthrough(self) -> None:
        result = resolve_fact_entity_refs_for_store(
            (),
            known_natural_keys=frozenset({"workitem:1"}),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert result.resolution_strategy == "passthrough"
        assert result.resolved_refs == ()
        assert result.unresolved_count == 0


class TestExactMatch:

    def test_exact_match_retains_canonical_ref(self) -> None:
        refs = ("workitem:42",)
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset({"workitem:42"}),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert result.resolved_refs == ("workitem:42",)
        assert result.resolution_strategy == "direct_match"
        assert result.unresolved_count == 0

    def test_multiple_exact_matches(self) -> None:
        refs = ("workitem:1", "workitem:2", "workitem:3")
        known = frozenset({"workitem:1", "workitem:2", "workitem:3"})
        result = resolve_fact_entity_refs_for_store(
            refs, known_natural_keys=known, write_authority=_BRIDGE_WRITE_AUTHORITY
        )
        assert result.resolved_refs == refs
        assert result.resolution_strategy == "direct_match"


class TestPartialMatch:

    def test_numeric_ref_resolves_to_namespaced_key(self) -> None:
        """Short integer '12345' → canonical 'workitem:12345'."""
        refs = ("12345",)
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset({"workitem:12345"}),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert result.resolved_refs == ("workitem:12345",)
        assert result.resolution_strategy == "partial_match"
        assert result.unresolved_count == 0

    def test_ambiguous_numeric_ref_is_unresolved(self) -> None:
        """Integer ref matching multiple canonical keys → UNRESOLVED (ambiguous)."""
        refs = ("100",)
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset({"workitem:100", "milestone:100"}),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert result.resolved_refs == (f"{_UNRESOLVED_PREFIX}100",)
        assert result.unresolved_count == 1


class TestUnresolved:

    def test_unknown_ref_marked_unresolved(self) -> None:
        refs = ("workitem:999",)
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset({"workitem:1"}),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert result.resolved_refs == (f"{_UNRESOLVED_PREFIX}workitem:999",)
        assert result.unresolved_count == 1
        assert result.resolution_strategy == "unresolved"

    def test_empty_known_keys_all_unresolved(self) -> None:
        refs = ("workitem:1", "workitem:2")
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset(),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert all(r.startswith(_UNRESOLVED_PREFIX) for r in result.resolved_refs)
        assert result.unresolved_count == 2
        assert result.resolution_strategy == "unresolved"

    def test_mixed_resolved_and_unresolved(self) -> None:
        """Some refs resolve, some don't → partial_match strategy."""
        refs = ("workitem:1", "workitem:999")
        known = frozenset({"workitem:1"})
        result = resolve_fact_entity_refs_for_store(
            refs, known_natural_keys=known, write_authority=_BRIDGE_WRITE_AUTHORITY
        )
        assert result.resolved_refs[0] == "workitem:1"
        assert result.resolved_refs[1] == f"{_UNRESOLVED_PREFIX}workitem:999"
        assert result.unresolved_count == 1
        assert result.resolution_strategy == "partial_match"

    def test_unresolved_prefix_is_never_dropped(self) -> None:
        """Verifies the UNRESOLVED: prefix marks unknown refs — not silent omission."""
        refs = ("totally-unknown-ref",)
        result = resolve_fact_entity_refs_for_store(
            refs,
            known_natural_keys=frozenset(),
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert len(result.resolved_refs) == 1
        assert result.resolved_refs[0].startswith(_UNRESOLVED_PREFIX)
        assert "totally-unknown-ref" in result.resolved_refs[0]
