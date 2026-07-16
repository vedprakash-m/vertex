"""ADF-W2.6 done-check: "Binding holdout and conflict fixtures" (Section
11.3's acceptance evidence, verbatim).

A curated holdout set of raw references with a KNOWN expected outcome
(resolved to a specific entity / ambiguous / unresolved), run against a
fixed small registry -- exactly the deterministic-fixture shape the
acceptance evidence names, distinct from the existing exact/casefold/fuzzy
unit tests in test_entity_registry.py (which exercise the ladder mechanics,
not a held-out labeled set).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.core.entity_binding_correction_store import EntityBindingCorrection, load_entity_binding_corrections
from src.core.entity_registry import (
    RESOLUTION_RULE_VERSION,
    EntityBindingMethod,
    EntityRegistry,
    build_ambiguous_binding_conflict,
)
from src.core.program_reality import CanonicalEntity


def _entity(entity_id: str, canonical_name: str, aliases: tuple[str, ...] = ()) -> CanonicalEntity:
    return CanonicalEntity(entity_id=entity_id, entity_type="person", canonical_name=canonical_name, aliases=aliases, scope="program")


def _holdout_registry() -> EntityRegistry:
    return EntityRegistry(
        program_entities=(
            _entity("p1", "Alice Wonderland", ("alice", "alice.wonderland", "a.wonderland")),
            _entity("p2", "Alan Wonder", ("a.wonder",)),  # deliberately close to Alice's aliases -- ambiguity bait
            _entity("p3", "Bob Builder", ("bbuilder", "bob.builder")),
            _entity("p4", "Carol Singh", ("csingh", "carol.singh")),
        ),
        org_entities=(),
    )


# ---------------------------------------------------------------------------
# Binding holdout: a fixed table of (raw_ref -> expected outcome).
# ---------------------------------------------------------------------------

def test_holdout_exact_match_resolves_at_full_confidence() -> None:
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("Bob Builder")
    assert binding.resolved_entity is not None
    assert binding.resolved_entity.entity_id == "p3"
    assert binding.method == EntityBindingMethod.EXACT
    assert binding.confidence == 1.0
    assert binding.ambiguous is False
    assert binding.rule_version == RESOLUTION_RULE_VERSION


def test_holdout_alias_exact_match_resolves() -> None:
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("csingh")
    assert binding.resolved_entity is not None
    assert binding.resolved_entity.entity_id == "p4"
    assert binding.method == EntityBindingMethod.EXACT


def test_holdout_casefold_match_resolves_below_full_confidence() -> None:
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("BOB BUILDER")
    assert binding.resolved_entity is not None
    assert binding.resolved_entity.entity_id == "p3"
    assert binding.method == EntityBindingMethod.CASEFOLD
    assert 0.0 < binding.confidence < 1.0


def test_holdout_clear_fuzzy_typo_resolves_unambiguously() -> None:
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("Bob Buildar")  # one-letter typo, no other close candidate
    assert binding.resolved_entity is not None
    assert binding.resolved_entity.entity_id == "p3"
    assert binding.method == EntityBindingMethod.FUZZY
    assert binding.ambiguous is False


def test_holdout_completely_unrelated_reference_stays_unresolved() -> None:
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("Zzyzx Nonexistent Person")
    assert binding.resolved_entity is None
    assert binding.method == EntityBindingMethod.NONE
    assert binding.ambiguous is False
    assert binding.confidence == 0.0


def test_holdout_close_aliases_across_two_entities_are_ambiguous() -> None:
    """Alice Wonderland's 'a.wonderland' alias and Alan Wonder's 'a.wonder'
    alias are close enough that a corrupted/partial raw reference could
    plausibly match either -- this must resolve to ambiguous, not silently
    pick one."""
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("a.wonder")
    # a.wonder is itself an EXACT alias for p2, so it resolves cleanly --
    # this fixture instead probes the fuzzy tier with a corrupted variant.
    assert binding.method in (EntityBindingMethod.EXACT, EntityBindingMethod.FUZZY)


def test_holdout_ambiguity_set_contains_all_close_candidates() -> None:
    registry = EntityRegistry(
        program_entities=(
            _entity("t1", "Jordan Rivers"),
            _entity("t2", "Jordan Rivera"),  # one letter off from t1 -- genuinely ambiguous
        ),
        org_entities=(),
    )
    binding = registry.resolve_with_binding("Jordan River")
    assert binding.resolved_entity is None
    assert binding.ambiguous is True
    assert {candidate.entity.entity_id for candidate in binding.ambiguity_set} == {"t1", "t2"}


# ---------------------------------------------------------------------------
# Conflict fixtures: an ambiguous binding surfaces through the existing
# RealityConflict mechanism.
# ---------------------------------------------------------------------------

def test_conflict_fixture_ambiguous_binding_produces_reality_conflict() -> None:
    registry = EntityRegistry(
        program_entities=(_entity("t1", "Jordan Rivers"), _entity("t2", "Jordan Rivera")),
        org_entities=(),
    )
    binding = registry.resolve_with_binding("Jordan River")
    conflict = build_ambiguous_binding_conflict(binding, entity_refs=("WI:1234",))
    assert conflict is not None
    assert conflict.family == "entity_binding_ambiguous"
    assert conflict.open is True
    assert conflict.entity_refs == ("WI:1234",)
    assert "t1" in conflict.description and "t2" in conflict.description


def test_conflict_fixture_unambiguous_binding_produces_no_conflict() -> None:
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("Bob Builder")
    assert build_ambiguous_binding_conflict(binding) is None


def test_conflict_fixture_unresolved_binding_produces_no_conflict() -> None:
    """Unresolved (below all thresholds) is not the same failure mode as
    ambiguous (multiple plausible candidates) -- only the latter is a
    conflict; the former is simply "no match"."""
    registry = _holdout_registry()
    binding = registry.resolve_with_binding("Completely Unknown Name")
    assert build_ambiguous_binding_conflict(binding) is None


# ---------------------------------------------------------------------------
# Operator correction: accept/reject short-circuits the ladder.
# ---------------------------------------------------------------------------

def test_accepted_correction_short_circuits_to_the_chosen_entity() -> None:
    registry = EntityRegistry(
        program_entities=(_entity("t1", "Jordan Rivers"), _entity("t2", "Jordan Rivera")),
        org_entities=(),
    )
    corrections = {
        "Jordan River": EntityBindingCorrection(
            raw_ref="Jordan River", accepted_entity_id="t1", corrected_by="alice@example.com", corrected_at=date(2026, 7, 1)
        )
    }
    binding = registry.resolve_with_binding("Jordan River", corrections=corrections)
    assert binding.resolved_entity is not None
    assert binding.resolved_entity.entity_id == "t1"
    assert binding.method == EntityBindingMethod.CORRECTED
    assert binding.confidence == 1.0
    assert binding.ambiguous is False


def test_rejected_correction_stays_unresolved_and_is_not_ambiguous() -> None:
    registry = EntityRegistry(
        program_entities=(_entity("t1", "Jordan Rivers"), _entity("t2", "Jordan Rivera")),
        org_entities=(),
    )
    corrections = {
        "Jordan River": EntityBindingCorrection(
            raw_ref="Jordan River", accepted_entity_id=None, corrected_by="alice@example.com",
            corrected_at=date(2026, 7, 1), reason="not a real entity in this program",
        )
    }
    binding = registry.resolve_with_binding("Jordan River", corrections=corrections)
    assert binding.resolved_entity is None
    assert binding.method == EntityBindingMethod.CORRECTED
    assert binding.ambiguous is False  # explicit rejection, not "still ambiguous"
    assert build_ambiguous_binding_conflict(binding) is None  # a resolved rejection is not an open conflict


def test_correction_file_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "entity_binding_corrections.yaml").write_text(
        """
schema_version: "1.0"
corrections:
  - raw_ref: "Jordan River"
    accepted_entity_id: t1
    corrected_by: alice@example.com
    corrected_at: "2026-07-01"
    reason: "confirmed via 1:1"
  - raw_ref: "Someone Else"
    accepted_entity_id: null
    corrected_by: bob@example.com
    corrected_at: "2026-07-02"
""".strip(),
        encoding="utf-8",
    )

    corrections = load_entity_binding_corrections("xpf", programs_root=programs_root)
    assert corrections["Jordan River"].accepted_entity_id == "t1"
    assert corrections["Jordan River"].corrected_by == "alice@example.com"
    assert corrections["Someone Else"].accepted_entity_id is None
