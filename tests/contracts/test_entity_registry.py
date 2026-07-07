"""WI-2.0 / WI-2.1: Tests for EntityRegistry (exact, casefold, fuzzy ladder, per-scope thresholds).

Covers:
- WI-2.0: exact + casefold resolution; program + org scope loading
- WI-2.1: fuzzy ladder with per-scope thresholds; literals guard (below threshold → None)
"""
from __future__ import annotations

from datetime import datetime, timezone
import textwrap
import tempfile
from pathlib import Path

import pytest

from src.core.entity_registry import (
    EntityRegistry,
    ResolutionRateBlock,
    _DEFAULT_FUZZY_THRESHOLDS,
)
from src.core.program_reality import CanonicalEntity
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity as Schema2CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityRedirect,
    EntityStatus,
    write_entities_document,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_entity(entity_id: str, canonical_name: str, aliases: tuple[str, ...] = (), scope: str = "program") -> CanonicalEntity:
    return CanonicalEntity(
        entity_id=entity_id,
        entity_type="person",
        canonical_name=canonical_name,
        aliases=aliases,
        scope=scope,
    )


@pytest.fixture
def small_registry():
    e1 = _make_entity("p1", "Alice Wonderland", ("alice", "alice.wonderland"), scope="program")
    e2 = _make_entity("p2", "Bob Builder", ("bbuilder", "bob.builder"), scope="program")
    e3 = _make_entity("o1", "Carol Danvers", ("cdanvers",), scope="org")
    return EntityRegistry(program_entities=(e1, e2), org_entities=(e3,))


# ---------------------------------------------------------------------------
# WI-2.0: exact match
# ---------------------------------------------------------------------------

class TestExactMatch:
    def test_exact_entity_id(self, small_registry):
        r = small_registry.resolve("p1")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_exact_canonical_name(self, small_registry):
        r = small_registry.resolve("Alice Wonderland")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_exact_alias(self, small_registry):
        r = small_registry.resolve("alice")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_exact_org_entity(self, small_registry):
        r = small_registry.resolve("Carol Danvers")
        assert r is not None
        assert r.canonical_name == "Carol Danvers"

    def test_exact_no_match(self, small_registry):
        assert small_registry.resolve("Zzz Unknown") is None

    def test_empty_string_returns_none(self, small_registry):
        assert small_registry.resolve("") is None


# ---------------------------------------------------------------------------
# WI-2.0: casefold match
# ---------------------------------------------------------------------------

class TestCasefoldMatch:
    def test_lowercase_canonical(self, small_registry):
        r = small_registry.resolve("alice wonderland")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_uppercase_canonical(self, small_registry):
        r = small_registry.resolve("ALICE WONDERLAND")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_casefold_alias(self, small_registry):
        r = small_registry.resolve("ALICE")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_mixed_case_canonical(self, small_registry):
        r = small_registry.resolve("alice.wonderland")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"


# ---------------------------------------------------------------------------
# WI-2.0: entity_type filter
# ---------------------------------------------------------------------------

def test_entity_type_filter_match(small_registry):
    r = small_registry.resolve("Alice Wonderland", entity_type="person")
    assert r is not None
    assert r.entity_type == "person"


def test_entity_type_filter_no_match(small_registry):
    # "team" doesn't match the "person" entity
    r = small_registry.resolve("Alice Wonderland", entity_type="team")
    assert r is None


# ---------------------------------------------------------------------------
# WI-2.1: fuzzy match — per-scope thresholds
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    def test_fuzzy_close_match(self, small_registry):
        # 'Alice Wonderlan' is close enough to 'Alice Wonderland'
        r = small_registry.resolve("Alice Wonderlan")
        assert r is not None
        assert r.canonical_name == "Alice Wonderland"

    def test_fuzzy_single_typo(self, small_registry):
        # 'Bobb Builder' should still resolve to 'Bob Builder'
        r = small_registry.resolve("Bobb Builder")
        assert r is not None
        assert r.canonical_name == "Bob Builder"

    def test_fuzzy_threshold_guard_below_threshold(self, small_registry):
        # Completely different name should NOT resolve
        r = small_registry.resolve("Zzz Unknown Person Xyz")
        assert r is None

    def test_fuzzy_threshold_guard_very_different(self, small_registry):
        # Short string that doesn't match well
        r = small_registry.resolve("xy")
        assert r is None


# ---------------------------------------------------------------------------
# WI-2.1: per-scope threshold configuration
# ---------------------------------------------------------------------------

def test_per_scope_thresholds_configured():
    """Default thresholds for program and org scopes must be configured."""
    assert "program" in _DEFAULT_FUZZY_THRESHOLDS
    assert "org" in _DEFAULT_FUZZY_THRESHOLDS
    assert _DEFAULT_FUZZY_THRESHOLDS["program"] >= 80.0
    assert _DEFAULT_FUZZY_THRESHOLDS["org"] >= 80.0


def test_program_scope_threshold_tighter_than_org():
    """Program-scope threshold must be >= org-scope (tighter or equal)."""
    prog = _DEFAULT_FUZZY_THRESHOLDS["program"]
    org = _DEFAULT_FUZZY_THRESHOLDS["org"]
    assert prog >= org, f"Program threshold ({prog}) should be >= org threshold ({org})"


# ---------------------------------------------------------------------------
# WI-2.0: program + org scope loading from YAML
# ---------------------------------------------------------------------------

def test_load_from_yaml(tmp_path):
    """EntityRegistry.load() correctly reads entities.yaml from disk."""
    programs_dir = tmp_path / "programs"
    prog_dir = programs_dir / "testprog" / "knowledge"
    prog_dir.mkdir(parents=True)
    entities_yaml = prog_dir / "entities.yaml"
    entities_yaml.write_text(textwrap.dedent("""
        entities:
          - entity_id: "e1"
            entity_type: "person"
            canonical_name: "Test User"
            aliases: ["tuser", "test.user"]
            scope: "program"
    """).strip())

    reg = EntityRegistry.load("testprog", programs_root=programs_dir, org_scope=False)
    assert reg.program_entity_count == 1
    r = reg.resolve("Test User")
    assert r is not None
    assert r.entity_id == "e1"

    by_id = reg.resolve("e1")
    assert by_id is not None
    assert by_id.canonical_name == "Test User"


def test_load_empty_when_no_yaml(tmp_path):
    """EntityRegistry.load() returns empty registry when no entities.yaml."""
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)
    reg = EntityRegistry.load("testprog", programs_root=programs_dir, org_scope=False)
    assert reg.program_entity_count == 0
    assert reg.resolve("anything") is None


def test_load_org_scope_from_yaml(tmp_path):
    """EntityRegistry.load() reads org-scope entities from vertex/knowledge/entities.yaml."""
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)
    org_dir = tmp_path / "vertex" / "knowledge"
    org_dir.mkdir(parents=True)
    (org_dir / "entities.yaml").write_text(textwrap.dedent("""
        entities:
          - entity_id: "org1"
            entity_type: "team"
            canonical_name: "Platform Team"
            aliases: ["platform", "plt"]
            scope: "org"
    """).strip())

    reg = EntityRegistry.load("testprog", programs_root=programs_dir, _repo_root=tmp_path)
    assert reg.org_entity_count == 1
    r = reg.resolve("Platform Team")
    assert r is not None
    assert r.entity_id == "org1"


# ---------------------------------------------------------------------------
# WI-2.0: program entities override org entities (same canonical name)
# ---------------------------------------------------------------------------

def test_program_overrides_org_entity():
    """Program entities take precedence over org entities."""
    org_e = _make_entity("org-alice", "Alice Wonderland", scope="org")
    prog_e = _make_entity("prog-alice", "Alice Wonderland", scope="program")
    reg = EntityRegistry(program_entities=(prog_e,), org_entities=(org_e,))
    r = reg.resolve("Alice Wonderland")
    assert r is not None
    assert r.entity_id == "prog-alice"  # program wins


# ---------------------------------------------------------------------------
# WI-2.0: register() (immutable update)
# ---------------------------------------------------------------------------

def test_register_adds_entity(small_registry):
    new_entity = _make_entity("p99", "New Person", ("newp",), scope="program")
    updated = small_registry.register(new_entity)
    # Original unchanged
    assert small_registry.program_entity_count == 2
    # Updated has new entity
    assert updated.program_entity_count == 3
    r = updated.resolve("New Person")
    assert r is not None
    assert r.entity_id == "p99"


# ---------------------------------------------------------------------------
# WI-2.1: all_entities() filter
# ---------------------------------------------------------------------------

def test_all_entities_no_filter(small_registry):
    all_e = small_registry.all_entities()
    assert len(all_e) == 3  # 2 program + 1 org


def test_all_entities_by_scope(small_registry):
    prog = small_registry.all_entities(scope="program")
    assert len(prog) == 2
    org = small_registry.all_entities(scope="org")
    assert len(org) == 1


def test_all_entities_by_type(small_registry):
    persons = small_registry.all_entities(entity_type="person")
    assert len(persons) == 3  # all are persons


# ---------------------------------------------------------------------------
# ResolutionRateBlock
# ---------------------------------------------------------------------------

def test_resolution_rate_block_full_resolution():
    block = ResolutionRateBlock(
        scope="program",
        total_attempts=100,
        resolved_exact=80,
        resolved_casefold=15,
        resolved_fuzzy=5,
        unresolved=0,
    )
    assert block.resolution_rate == 1.0


def test_resolution_rate_block_partial():
    block = ResolutionRateBlock(
        scope="program",
        total_attempts=100,
        resolved_exact=70,
        resolved_casefold=10,
        resolved_fuzzy=5,
        unresolved=15,
    )
    assert block.resolution_rate == pytest.approx(0.85)


def test_resolution_rate_block_zero_attempts():
    block = ResolutionRateBlock(
        scope="program",
        total_attempts=0,
        resolved_exact=0,
        resolved_casefold=0,
        resolved_fuzzy=0,
        unresolved=0,
    )
    assert block.resolution_rate == 1.0


# ---------------------------------------------------------------------------
# PPL-W3.3b: org-scope entities additionally source from the shared
# people/team registry (schema-2.0 `entities.yaml`), unioned with the
# legacy `vertex/knowledge/entities.yaml` path.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _schema2_alias(value: str, *, status: AliasStatus = AliasStatus.ACTIVE) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=status, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def test_load_org_scope_additionally_sources_from_shared_people_registry(tmp_path: Path) -> None:
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                Schema2CanonicalEntity(
                    workspace_id="ws", entity_id="person:jdoe", entity_type="person", canonical_name="Jane Doe",
                    aliases=(_schema2_alias("jdoe"),), scope="org", created_at=_NOW,
                ),
            ),
        ),
    )

    reg = EntityRegistry.load("testprog", programs_root=programs_dir)

    assert reg.org_entity_count == 1
    resolved = reg.resolve("jdoe")
    assert resolved is not None
    assert resolved.entity_id == "person:jdoe"
    assert resolved.canonical_name == "Jane Doe"


def test_load_org_scope_shared_registry_filters_to_person_and_team_only(tmp_path: Path) -> None:
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                Schema2CanonicalEntity(
                    workspace_id="ws", entity_id="milestone:m1", entity_type="milestone", canonical_name="M1 Ship",
                    aliases=(_schema2_alias("m1"),), scope="org", created_at=_NOW,
                ),
            ),
        ),
    )

    reg = EntityRegistry.load("testprog", programs_root=programs_dir)

    assert reg.org_entity_count == 0
    assert reg.resolve("m1") is None


def test_load_org_scope_shared_registry_resolves_retired_and_redirected_aliases(tmp_path: Path) -> None:
    """A person renamed/merged in the shared registry: their OLD alias
    (now retired on a tombstoned entity that redirects to the survivor)
    must still resolve, to the CURRENT canonical entity -- §7.2a's "alias
    history remains resolvable after rename," now honored through this
    module's own exact/casefold/fuzzy ladder."""
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                Schema2CanonicalEntity(
                    workspace_id="ws", entity_id="person:old", entity_type="person", canonical_name="Old Name",
                    aliases=(_schema2_alias("oldalias", status=AliasStatus.RETIRED),), scope="org",
                    created_at=_NOW, status=EntityStatus.TOMBSTONED, tombstoned_at=_NOW,
                ),
                Schema2CanonicalEntity(
                    workspace_id="ws", entity_id="person:new", entity_type="person", canonical_name="New Name",
                    aliases=(_schema2_alias("newalias"),), scope="org", created_at=_NOW,
                ),
            ),
            redirects=(
                EntityRedirect(from_entity_id="person:old", to_entity_id="person:new", recorded_at=_NOW, principal_id="steward", reason="merge"),
            ),
        ),
    )

    reg = EntityRegistry.load("testprog", programs_root=programs_dir)

    resolved = reg.resolve("oldalias")
    assert resolved is not None
    assert resolved.entity_id == "person:new"
    assert resolved.canonical_name == "New Name"
    # The tombstoned source entity itself is not directly resolvable.
    assert reg.resolve("Old Name") is None


def test_load_org_scope_legacy_wins_on_collision_with_shared_registry(tmp_path: Path) -> None:
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)
    org_dir = tmp_path / "vertex" / "knowledge"
    org_dir.mkdir(parents=True)
    (org_dir / "entities.yaml").write_text(textwrap.dedent("""
        entities:
          - entity_id: "person:jdoe"
            entity_type: "person"
            canonical_name: "Legacy Name"
            aliases: ["jdoe"]
            scope: "org"
    """).strip())
    knowledge_root = tmp_path / "knowledge"
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                Schema2CanonicalEntity(
                    workspace_id="ws", entity_id="person:jdoe", entity_type="person", canonical_name="Shared Name",
                    aliases=(_schema2_alias("jdoe"),), scope="org", created_at=_NOW,
                ),
            ),
        ),
    )

    reg = EntityRegistry.load("testprog", programs_root=programs_dir, _repo_root=tmp_path)

    assert reg.org_entity_count == 1
    resolved = reg.resolve("person:jdoe")
    assert resolved is not None
    assert resolved.canonical_name == "Legacy Name"


def test_load_org_scope_shared_registry_missing_is_a_true_no_op(tmp_path: Path) -> None:
    programs_dir = tmp_path / "programs"
    (programs_dir / "testprog").mkdir(parents=True)

    reg = EntityRegistry.load("testprog", programs_root=programs_dir)

    assert reg.org_entity_count == 0
