"""WI-2.5 contract: entity_fact_index keyed by canonical entity_id.

Acceptance: join test — a fact stored with an alias entity_ref is indexed
by its canonical entity_id when an EntityRegistry is supplied to load().
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from unittest.mock import MagicMock, patch

import pytest

from src.core.entity_registry import EntityRegistry
from src.core.program_reality import CanonicalEntity, ProgramReality

_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_fact_revision(fact_id: str, fact_type: str, entity_refs: tuple[str, ...]) -> MagicMock:
    """Build a minimal ProgramFactRevision mock."""
    fact = MagicMock()
    fact.fact_id = fact_id
    fact.fact_type = fact_type
    fact.entity_refs = entity_refs
    fact.lifecycle_state = "active"
    fact.recorded_at = _NOW
    return fact


def _make_snapshot(facts: list) -> MagicMock:
    snapshot = MagicMock()
    snapshot.program_id = "test_program"
    snapshot.facts = facts
    return snapshot


def _make_registry_with_alias(
    canonical_id: str,
    canonical_name: str,
    alias: str,
    entity_type: str = "workstream",
) -> EntityRegistry:
    entity = CanonicalEntity(
        entity_id=canonical_id,
        entity_type=entity_type,
        canonical_name=canonical_name,
        aliases=(alias,),
        scope="program",
    )
    return EntityRegistry(
        program_entities=(entity,),
        org_entities=(),
    )


_MOCK_PATCHES = [
    "src.core.program_reality.project_action_items",
    "src.core.program_reality.project_risk_entries",
    "src.core.program_reality.project_decision_entries",
    "src.core.program_reality.project_dependencies",
    "src.core.program_reality.project_milestones",
    "src.core.program_reality.project_assumptions",
    "src.core.program_reality.project_claim_entries",
    "src.core.program_fact_store.resolve_fact_sor_mode",
]


def _apply_base_patches(wstreams: list, snapshot: MagicMock):
    """Return a context manager that patches projectors and loads a synthetic snapshot."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("src.core.program_reality.load_program_facts", return_value=snapshot))
    stack.enter_context(patch("src.core.program_reality.project_workstreams", return_value=wstreams))
    for path in _MOCK_PATCHES:
        if "resolve_fact_sor_mode" in path:
            stack.enter_context(patch(path, return_value="legacy"))
        else:
            stack.enter_context(patch(path, return_value=[]))
    return stack


class TestEntityFactIndexJoin:
    """WI-2.5: entity_fact_index uses canonical entity_id keys when registry is provided."""

    def test_join_via_canonical_id_when_alias_in_entity_refs(self, tmp_path: Path) -> None:
        """Fact stored with alias entity_ref → indexed by canonical entity_id."""
        canonical_id = "entity_risk_001"
        alias = "proj-risk-alias"
        fact_id = "pf_abc123"

        registry = _make_registry_with_alias(
            canonical_id=canonical_id,
            canonical_name="Project Risk Alpha",
            alias=alias,
        )
        fact = _make_fact_revision(fact_id, "workstream.entry", (alias,))
        snapshot = _make_snapshot([fact])

        with _apply_base_patches([MagicMock(id=alias, fact_id=fact_id, name="WS Alpha")], snapshot):
            reality = ProgramReality.load(
                "test_program",
                programs_root=tmp_path,
                entity_registry=registry,
            )

        index = reality._entity_fact_index
        assert canonical_id in index, (
            f"Expected canonical_id '{canonical_id}' in entity_fact_index, got keys: {list(index.keys())}"
        )
        assert alias not in index, (
            f"Alias '{alias}' should not be a key after WI-2.5 resolution"
        )

    def test_fallback_to_fact_id_when_no_registry(self, tmp_path: Path) -> None:
        """Without a registry, entity_fact_index keys fall back to fact_id UUIDs."""
        fact_id = "pf_fallback01"
        alias = "some-alias"

        fact = _make_fact_revision(fact_id, "workstream.entry", (alias,))
        snapshot = _make_snapshot([fact])

        with _apply_base_patches([MagicMock(id=alias, fact_id=fact_id, name="WS Beta")], snapshot):
            reality = ProgramReality.load("test_program", programs_root=tmp_path)

        index = reality._entity_fact_index
        assert fact_id in index, f"Expected fact_id '{fact_id}' in index without registry"
        assert alias not in index

    def test_fallback_to_fact_id_when_ref_unresolvable(self, tmp_path: Path) -> None:
        """When entity_ref doesn't resolve in registry, fall back to fact_id."""
        fact_id = "pf_unresolvable01"
        unresolvable_alias = "xyzzy-unknown-entity"

        registry = _make_registry_with_alias(
            canonical_id="entity_known_001",
            canonical_name="Known Entity",
            alias="known-alias",
        )
        fact = _make_fact_revision(fact_id, "workstream.entry", (unresolvable_alias,))
        snapshot = _make_snapshot([fact])

        with _apply_base_patches([MagicMock(id=unresolvable_alias, fact_id=fact_id, name="WS Gamma")], snapshot):
            reality = ProgramReality.load(
                "test_program",
                programs_root=tmp_path,
                entity_registry=registry,
            )

        index = reality._entity_fact_index
        assert fact_id in index, f"Expected fallback fact_id '{fact_id}' in index for unresolvable alias"
        assert unresolvable_alias not in index

    def test_empty_program_no_facts_gives_empty_index(self, tmp_path: Path) -> None:
        """ProgramReality with no facts gives empty entity_fact_index."""
        registry = _make_registry_with_alias(
            canonical_id="entity_xyz",
            canonical_name="Entity XYZ",
            alias="xyz",
        )
        snapshot = _make_snapshot([])

        with _apply_base_patches([], snapshot):
            reality = ProgramReality.load(
                "test_program",
                programs_root=tmp_path,
                entity_registry=registry,
            )
        assert reality._entity_fact_index == {}

    def test_fallback_to_fact_id_when_ref_ambiguous(self, tmp_path: Path) -> None:
        """ADF-W2.6: when entity_ref matches two near-tied fuzzy candidates,
        the index falls back to fact_id -- the same as an unresolvable ref --
        rather than silently joining under whichever candidate scored
        marginally higher."""
        fact_id = "pf_ambiguous01"
        ambiguous_ref = "Jordan River"

        entity_a = CanonicalEntity(entity_id="t1", entity_type="person", canonical_name="Jordan Rivers", aliases=(), scope="program")
        entity_b = CanonicalEntity(entity_id="t2", entity_type="person", canonical_name="Jordan Rivera", aliases=(), scope="program")
        registry = EntityRegistry(program_entities=(entity_a, entity_b), org_entities=())
        fact = _make_fact_revision(fact_id, "workstream.entry", (ambiguous_ref,))
        snapshot = _make_snapshot([fact])

        with _apply_base_patches([MagicMock(id=ambiguous_ref, fact_id=fact_id, name="WS Ambiguous")], snapshot):
            reality = ProgramReality.load(
                "test_program",
                programs_root=tmp_path,
                entity_registry=registry,
            )

        index = reality._entity_fact_index
        assert fact_id in index, f"Expected fallback fact_id '{fact_id}' in index for ambiguous ref"
        assert "t1" not in index
        assert "t2" not in index

    def test_two_facts_same_entity_grouped_in_index(self, tmp_path: Path) -> None:
        """Two facts with different aliases for the same entity are grouped in the index."""
        canonical_id = "entity_group_001"
        alias1 = "team-alpha-v1"
        alias2 = "team-alpha-v2"
        fact_id_1 = "pf_group_001"
        fact_id_2 = "pf_group_002"

        # Registry with BOTH aliases pointing to the same canonical entity
        entity = CanonicalEntity(
            entity_id=canonical_id,
            entity_type="workstream",
            canonical_name="Team Alpha",
            aliases=(alias1, alias2),
            scope="program",
        )
        registry = EntityRegistry(program_entities=(entity,), org_entities=())
        fact1 = _make_fact_revision(fact_id_1, "workstream.entry", (alias1,))
        fact2 = _make_fact_revision(fact_id_2, "workstream.entry", (alias2,))
        snapshot = _make_snapshot([fact1, fact2])

        with _apply_base_patches(
            [MagicMock(id=alias1, fact_id=fact_id_1, name="WS One"), MagicMock(id=alias2, fact_id=fact_id_2, name="WS Two")],
            snapshot,
        ):
            reality = ProgramReality.load(
                "test_program",
                programs_root=tmp_path,
                entity_registry=registry,
            )

        index = reality._entity_fact_index
        assert canonical_id in index
        assert len(index[canonical_id]) == 2, (
            f"Expected 2 assessments grouped under canonical_id, got {len(index[canonical_id])}"
        )

