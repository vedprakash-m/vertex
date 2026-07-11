"""Composed authority registry (arch-fix.md Phase 0, §0.5/H8).

For any persisted fact family, answers: which source is authoritative, what
its per-program SoR state is, and which ledger event-type prefixes / fact
types back it. This module composes the four existing sources of truth —
it does not duplicate or re-derive them:

- ``vertex/policies/source_authority.yaml`` (via ``truth_model.py``) — the
  authority matrix (primary/secondary source, human role, mirror fields)
  and the fact_type -> authority_family map.
- ``src/core/ledger/event_type_registry.py`` — which ledger event-type
  prefixes are PROJECTABLE into which authority family.
- ``src/core/fact_sor_state.py`` — per-program SoR mode resolution
  (legacy/shadow/primary), including per-family overrides.
- ``src/core/state_reader_registry.py`` — which module/symbols read a
  given persisted JSONL/state artifact.

This is a read-only, generated *view* (INV-AF-6's eventual enforcement —
"invalid SoR config raises" — is Phase-1/AF-6 CPK scope; this module only
provides the inventory + a coverage contract test).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.fact_sor_state import AUTHORITY_FAMILIES, resolve_family_sor_mode
from src.core.ledger.event_type_registry import LEDGER_EVENT_REGISTRY, EventDisposition
from src.core.state_reader_registry import STATE_READER_REGISTRY, StateReaderRegistration
from src.core.truth_model import (
    AuthorityEntry,
    SorFlipFamilyConfig,
    SourceAuthorityPolicy,
    load_source_authority_policy,
)


@dataclass(frozen=True, slots=True)
class AuthorityFamilyView:
    """Composed, read-only view of one authority family."""

    family: str
    primary_source: str
    secondary_sources: tuple[str, ...]
    human_role: str
    mirror_fields: tuple[str, ...]
    fact_types: tuple[str, ...]
    event_prefixes: tuple[str, ...]
    sor_flip: SorFlipFamilyConfig


def build_authority_registry(
    *, policy: SourceAuthorityPolicy | None = None
) -> dict[str, AuthorityFamilyView]:
    """Compose one view per authority family declared in the authority matrix.

    Families come from ``source_authority.yaml``'s ``authority`` section
    (the ``AuthorityEntry`` map); fact types and event prefixes are pulled in
    by cross-referencing the family_map and the ledger event registry.
    """
    resolved_policy = policy if policy is not None else load_source_authority_policy()

    fact_types_by_family: dict[str, list[str]] = {}
    for fact_type, family in resolved_policy.family_map.items():
        fact_types_by_family.setdefault(family, []).append(fact_type)

    event_prefixes_by_family: dict[str, list[str]] = {}
    for spec in LEDGER_EVENT_REGISTRY:
        if spec.authority_family is None:
            continue
        event_prefixes_by_family.setdefault(spec.authority_family, []).append(spec.prefix)

    registry: dict[str, AuthorityFamilyView] = {}
    for family, entry in resolved_policy.authority.items():
        registry[family] = AuthorityFamilyView(
            family=family,
            primary_source=entry.primary,
            secondary_sources=entry.secondary,
            human_role=entry.human_role,
            mirror_fields=entry.mirror_fields,
            fact_types=tuple(sorted(fact_types_by_family.get(family, ()))),
            event_prefixes=tuple(sorted(event_prefixes_by_family.get(family, ()))),
            sor_flip=resolved_policy.sor_flip.for_family(family),
        )
    return registry


def resolve_program_family_mode(
    program_id: str, family: str, *, programs_root=None
) -> str:
    """Thin pass-through to ``fact_sor_state.resolve_family_sor_mode`` so
    callers can resolve a family's live SoR mode from this same module,
    without this module re-implementing state-file parsing."""
    if programs_root is None:
        from src.core.fact_sor_state import PROGRAMS_ROOT as _default_root

        programs_root = _default_root
    return resolve_family_sor_mode(program_id, family, programs_root=programs_root)


def ledger_readers_for_family(family: str) -> tuple[StateReaderRegistration, ...]:
    """Best-effort cross-reference: state readers whose owner module is the
    ledger event log or program-views projector, relevant to any PROJECTABLE
    family (the ledger event log is the shared substrate every family's
    events flow through before bridging to the fact store)."""
    if family not in AUTHORITY_FAMILIES and family not in {
        spec.authority_family for spec in LEDGER_EVENT_REGISTRY if spec.authority_family
    }:
        return ()
    return tuple(
        reg
        for reg in STATE_READER_REGISTRY.values()
        if reg.state_name == "ledger_event_log"
    )


def families_referenced_by_ledger_registry() -> frozenset[str]:
    """Every authority_family value actually used by a ledger event spec."""
    return frozenset(
        spec.authority_family for spec in LEDGER_EVENT_REGISTRY if spec.authority_family is not None
    )


def families_referenced_by_family_map(*, policy: SourceAuthorityPolicy | None = None) -> frozenset[str]:
    """Every authority_family value used in source_authority.yaml's family_map,
    excluding the BY_SIGNAL_CLASS routing sentinel (that's resolved dynamically
    per signal, not a single family) and the "unknown" fallback."""
    resolved_policy = policy if policy is not None else load_source_authority_policy()
    return frozenset(
        family
        for family in resolved_policy.family_map.values()
        if family not in {"BY_SIGNAL_CLASS", "unknown"}
    )
