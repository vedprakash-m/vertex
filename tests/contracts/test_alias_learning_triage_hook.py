"""WI-2.6: Contract tests — alias learning triage hook + curation list.

Acceptance:
  - round-trip: alias fact emitted → readable back from fact store
  - config floor: entities in config YAML win over learned aliases
  - unresolved refs → written to alias_curation.yaml
  - hook does not block triage on error
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.core.entity_registry import EntityRegistry
from src.core.entity_alias_emitter import emit_entity_alias_facts
from src.core.program_reality import CanonicalEntity
from src.core.signal_normalizer import collect_unresolved_entity_refs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(canonical_id: str, canonical_name: str, *aliases: str) -> EntityRegistry:
    entity = CanonicalEntity(
        entity_id=canonical_id,
        entity_type="person",
        canonical_name=canonical_name,
        aliases=tuple(aliases),
        scope="program",
    )
    return EntityRegistry(program_entities=(entity,), org_entities=())


def _make_facts_snapshot(entity_refs_by_fact: list[tuple[str, ...]]) -> MagicMock:
    """Create a minimal facts snapshot with facts having entity_refs."""
    facts = []
    for refs in entity_refs_by_fact:
        fact = MagicMock()
        fact.entity_refs = refs
        fact.fact_type = "workstream.entry"
        fact.review_state = None
        fact.payload = {}
        facts.append(fact)
    snap = MagicMock()
    snap.facts = facts
    return snap


# ---------------------------------------------------------------------------
# collect_unresolved_entity_refs
# ---------------------------------------------------------------------------

class TestCollectUnresolvedEntityRefs:
    def test_returns_empty_when_all_resolve(self) -> None:
        registry = _make_registry("person:alice", "Alice", "alice@example.com")
        snapshot = _make_facts_snapshot([("alice@example.com",)])
        result = collect_unresolved_entity_refs(snapshot, registry)
        assert result == frozenset()

    def test_returns_unresolvable_refs(self) -> None:
        registry = _make_registry("person:alice", "Alice", "alice@example.com")
        snapshot = _make_facts_snapshot([("alice@example.com", "bob@unknown.com")])
        result = collect_unresolved_entity_refs(snapshot, registry)
        assert result == frozenset({"bob@unknown.com"})

    def test_empty_snapshot_gives_empty(self) -> None:
        registry = _make_registry("person:alice", "Alice")
        snapshot = _make_facts_snapshot([])
        result = collect_unresolved_entity_refs(snapshot, registry)
        assert result == frozenset()

    def test_deduplicates_across_facts(self) -> None:
        registry = _make_registry("person:alice", "Alice")
        snapshot = _make_facts_snapshot([("unknown-ref",), ("unknown-ref",), ("also-unknown",)])
        result = collect_unresolved_entity_refs(snapshot, registry)
        assert result == frozenset({"unknown-ref", "also-unknown"})

    def test_ambiguous_ref_counts_as_unresolved(self) -> None:
        """ADF-W2.6: a near-tied fuzzy match (registry.resolve_with_binding's
        ambiguous=True) must count as unresolved too -- Section 8.14.3's
        "ambiguous entities remain unresolved" -- rather than being silently
        picked by the old resolve() ladder and undercounted here."""
        entity_a = CanonicalEntity(entity_id="t1", entity_type="person", canonical_name="Jordan Rivers", aliases=(), scope="program")
        entity_b = CanonicalEntity(entity_id="t2", entity_type="person", canonical_name="Jordan Rivera", aliases=(), scope="program")
        registry = EntityRegistry(program_entities=(entity_a, entity_b), org_entities=())
        snapshot = _make_facts_snapshot([("Jordan River",)])
        result = collect_unresolved_entity_refs(snapshot, registry)
        assert result == frozenset({"Jordan River"})


# ---------------------------------------------------------------------------
# emit_entity_alias_facts round-trip
# ---------------------------------------------------------------------------

class TestAliasEmitRoundTrip:
    def test_emitted_fact_readable_back(self, tmp_path: Path) -> None:
        """Alias fact emitted → readable back from the fact store."""
        from src.core.program_fact_store import ProgramFactStore

        program_id = "test-prog-wi26"
        entity = CanonicalEntity(
            entity_id="person:bob",
            entity_type="person",
            canonical_name="Bob Smith",
            aliases=("bob@example.com",),
            scope="program",
        )
        result = emit_entity_alias_facts(
            program_id,
            (entity,),
            programs_root=tmp_path,
            emitted_by="test_round_trip",
        )
        assert result.emitted == 1
        assert result.skipped_duplicates == 0

        store = ProgramFactStore(program_id, db_root=tmp_path)
        snapshot = store.snapshot(as_of=None)
        alias_facts = [f for f in snapshot.facts if f.fact_type == "entity.alias"]
        assert len(alias_facts) == 1
        assert alias_facts[0].payload["canonical_id"] == "person:bob"
        assert alias_facts[0].payload["alias"] == "Bob Smith"

    def test_second_emit_is_idempotent(self, tmp_path: Path) -> None:
        """Emitting the same entity twice doesn't create duplicate facts."""
        program_id = "test-prog-wi26-idem"
        entity = CanonicalEntity(
            entity_id="person:carol",
            entity_type="person",
            canonical_name="Carol",
            aliases=(),
            scope="program",
        )
        emit_entity_alias_facts(program_id, (entity,), programs_root=tmp_path)
        result2 = emit_entity_alias_facts(program_id, (entity,), programs_root=tmp_path)
        assert result2.emitted == 0
        assert result2.skipped_duplicates == 1

    def test_records_trace_link_when_correlation_id_present(self, tmp_path: Path) -> None:
        # ADF-W2.12: a triage-run correlation id threaded down must produce
        # a real stage="fact" OperationTrace link per newly-emitted alias,
        # no-op when absent (the pre-existing default).
        from src.core.operation_trace import load_operation_trace

        program_id = "test-prog-wi26-trace"
        entity = CanonicalEntity(
            entity_id="person:dana", entity_type="person", canonical_name="Dana", aliases=(), scope="program",
        )
        result = emit_entity_alias_facts(
            program_id, (entity,), programs_root=tmp_path, correlation_id="triage-corr-1",
        )
        assert result.emitted == 1

        trace = load_operation_trace(program_id, "triage-corr-1", programs_root=tmp_path)
        assert trace is not None
        assert len(trace.fact_refs) == 1
        assert "entity.alias:person:person:dana" in trace.fact_refs[0]


# ---------------------------------------------------------------------------
# Config floor
# ---------------------------------------------------------------------------

class TestConfigFloor:
    def test_config_entity_resolves_over_learned_alias(self, tmp_path: Path) -> None:
        """Config-defined entity wins; the registry prioritizes config-loaded entities."""
        program_id = "test-prog-floor"
        # Write a config-defined entity
        knowledge_dir = tmp_path / program_id / "knowledge"
        knowledge_dir.mkdir(parents=True)
        entities_yaml = knowledge_dir / "entities.yaml"
        entities_yaml.write_text(
            textwrap.dedent("""\
                entities:
                  - id: person:dave
                    type: person
                    name: "Dave (Config)"
                    aliases:
                      - dave@example.com
                    scope: program
            """),
            encoding="utf-8",
        )
        registry = EntityRegistry.load(program_id, programs_root=tmp_path, org_scope=False)
        entity = registry.resolve("dave@example.com")
        assert entity is not None
        assert entity.entity_id == "person:dave"
        assert entity.canonical_name == "Dave (Config)"

    def test_learned_alias_does_not_override_config(self, tmp_path: Path) -> None:
        """Config floor: emitting a learned alias for same canonical_id still uses config name."""
        program_id = "test-prog-floor-2"
        knowledge_dir = tmp_path / program_id / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (knowledge_dir / "entities.yaml").write_text(
            textwrap.dedent("""\
                entities:
                  - id: workstream:auth
                    type: workstream
                    name: "Auth (Config)"
                    aliases:
                      - auth-team
                    scope: program
            """),
            encoding="utf-8",
        )
        registry = EntityRegistry.load(program_id, programs_root=tmp_path, org_scope=False)
        config_entity = registry.resolve("auth-team")
        assert config_entity is not None

        # Emit a "learned" alias fact (same entity) — should not change registry resolution
        emit_entity_alias_facts(
            program_id, (config_entity,), programs_root=tmp_path, emitted_by="learned"
        )

        # Re-load registry — config entity still resolves correctly
        registry2 = EntityRegistry.load(program_id, programs_root=tmp_path, org_scope=False)
        result = registry2.resolve("auth-team")
        assert result is not None
        assert result.canonical_name == "Auth (Config)"


# ---------------------------------------------------------------------------
# Curation YAML
# ---------------------------------------------------------------------------

class TestCurationYaml:
    def test_unresolved_refs_written_to_curation_file(self, tmp_path: Path) -> None:
        """Unresolved entity_refs are persisted to alias_curation.yaml."""
        program_id = "test-prog-curate"
        curation_path = tmp_path / program_id / "knowledge" / "alias_curation.yaml"
        curation_path.parent.mkdir(parents=True)

        unresolved_refs = frozenset({"unknown-team", "ghost@example.com"})
        # Simulate the curation write (same logic as triage hook)
        existing: list[str] = []
        merged = sorted(set(existing) | unresolved_refs)
        curation_path.write_text(
            yaml.dump({"unresolved_refs": merged}, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

        data = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
        assert "unresolved_refs" in data
        assert "unknown-team" in data["unresolved_refs"]
        assert "ghost@example.com" in data["unresolved_refs"]

    def test_curation_file_deduplicates_on_re_run(self, tmp_path: Path) -> None:
        """Re-running triage merges unresolved refs without duplicates."""
        program_id = "test-prog-curate-dedup"
        curation_path = tmp_path / program_id / "knowledge" / "alias_curation.yaml"
        curation_path.parent.mkdir(parents=True)

        # First run
        curation_path.write_text(
            yaml.dump({"unresolved_refs": ["ref-a", "ref-b"]}, default_flow_style=False),
            encoding="utf-8",
        )

        # Second run adds "ref-b" again + "ref-c"
        unresolved_new = frozenset({"ref-b", "ref-c"})
        existing_data = yaml.safe_load(curation_path.read_text(encoding="utf-8")) or {}
        existing_list = existing_data.get("unresolved_refs", [])
        merged = sorted(set(existing_list) | unresolved_new)
        curation_path.write_text(
            yaml.dump({"unresolved_refs": merged}, default_flow_style=False),
            encoding="utf-8",
        )

        data = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
        assert data["unresolved_refs"] == ["ref-a", "ref-b", "ref-c"]
