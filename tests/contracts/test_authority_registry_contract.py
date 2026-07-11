"""arch-fix.md Phase 0 (§0.5, H8): the composed authority registry must cover
every persisted family referenced by any of its source registries — it's a
generated view, so a coverage gap here means a real family is falling
through the cracks of the authority matrix, not a bug in this module.
"""
from __future__ import annotations

from src.core.authority_registry import (
    build_authority_registry,
    families_referenced_by_family_map,
    families_referenced_by_ledger_registry,
    ledger_readers_for_family,
    resolve_program_family_mode,
)
from src.core.fact_sor_state import AUTHORITY_FAMILIES


def test_every_ledger_registry_family_is_covered() -> None:
    registry = build_authority_registry()
    referenced = families_referenced_by_ledger_registry()
    missing = referenced - registry.keys()
    assert not missing, f"authority_family(s) used by LEDGER_EVENT_REGISTRY but missing from the authority matrix: {sorted(missing)}"


def test_every_family_map_family_is_covered() -> None:
    registry = build_authority_registry()
    referenced = families_referenced_by_family_map()
    missing = referenced - registry.keys()
    assert not missing, f"authority_family(s) used by source_authority.yaml family_map but missing from the authority matrix: {sorted(missing)}"


def test_every_sor_trackable_family_is_covered() -> None:
    """Every family fact_sor_state.py can carry a live SoR mode for must have
    an authority-matrix entry (the reverse direction — the matrix must not be
    missing an entry for something the SoR machinery already tracks)."""
    registry = build_authority_registry()
    missing = set(AUTHORITY_FAMILIES) - registry.keys()
    assert not missing, f"AUTHORITY_FAMILIES entries missing from the authority matrix: {sorted(missing)}"


def test_view_composes_fact_types_and_event_prefixes() -> None:
    registry = build_authority_registry()
    workitem = registry["workitem.state"]
    assert "milestone.entry" in workitem.fact_types
    assert "milestone." in workitem.event_prefixes
    assert workitem.primary_source == "ado"


def test_resolve_program_family_mode_defaults_to_legacy(tmp_path) -> None:
    # No fact_store_sor.yaml exists for this program -> legacy default,
    # delegating to fact_sor_state.resolve_family_sor_mode rather than
    # re-implementing state-file resolution.
    mode = resolve_program_family_mode("nonexistent-program-xyz", "workitem.state", programs_root=tmp_path)
    assert mode == "legacy"


def test_ledger_readers_for_family_returns_event_log_reader() -> None:
    readers = ledger_readers_for_family("workitem.state")
    assert any(r.state_name == "ledger_event_log" for r in readers)


def test_ledger_readers_for_unknown_family_is_empty() -> None:
    assert ledger_readers_for_family("not-a-real-family") == ()
