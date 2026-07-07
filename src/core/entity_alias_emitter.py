"""WI-2.3: entity.alias fact emission — idempotent alias recording in the fact store.

When the EntityRegistry learns a new alias (via alias learning or manual
registration), it emits a `entity.alias` fact to the program fact store.
This ensures all alias knowledge flows through the fact store and is
available in events_since() / to_dict() / diff().

Idempotence: emitting the same (entity_type, canonical_id, alias) twice
produces no second fact (natural-key deduplication).

Zone A module (INV-1 applies).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config_loader import PROGRAMS_ROOT
from src.core.program_reality import CanonicalEntity


@dataclass(frozen=True, slots=True)
class AliasEmissionResult:
    """Result of emitting entity.alias facts."""
    emitted: int
    skipped_duplicates: int


def build_entity_alias_natural_key(
    entity_type: str,
    canonical_id: str,
) -> str:
    """Build the natural key for an entity.alias fact (§6.3 fact-type table)."""
    return f"entity.alias|{entity_type}|{canonical_id}"


def emit_entity_alias_facts(
    program_id: str,
    entities: tuple[CanonicalEntity, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
    emitted_by: str = "entity_registry",
    dry_run: bool = False,
) -> AliasEmissionResult:
    """Emit entity.alias facts for all entities in the registry.

    Idempotent: the fact-store natural-key deduplication prevents double-writes.

    Args:
        program_id: Target program.
        entities: Canonical entities to emit alias facts for.
        programs_root: Programs root directory.
        emitted_by: Source identifier for audit trail.
        dry_run: If True, compute but don't write.

    Returns:
        AliasEmissionResult with emitted/skipped counts.
    """
    from src.core.program_fact_store import ProgramFactStore, ProgramFactInput, build_natural_key
    from src.core.config_loader import PROGRAMS_ROOT as _DEFAULT_PROGRAMS_ROOT

    # Resolve db_root from programs_root (mirrors _resolve_fact_db_root pattern)
    if programs_root == _DEFAULT_PROGRAMS_ROOT:
        _db_root: Path | None = None
    elif programs_root.name == "programs":
        _db_root = programs_root.parent
    else:
        _db_root = programs_root

    store = ProgramFactStore(program_id, db_root=_db_root)
    existing_natural_keys: set[str] = set()

    # Read existing entity.alias facts to check for duplicates
    try:
        snapshot = store.snapshot(as_of=None)
        existing_natural_keys = {
            f.natural_key
            for f in snapshot.facts
            if f.fact_type == "entity.alias"
        }
    except Exception:
        pass  # On first run, no existing facts

    emitted = 0
    skipped = 0
    now = datetime.now(timezone.utc)

    for entity in entities:
        nk = build_entity_alias_natural_key(entity.entity_type, entity.entity_id)
        if nk in existing_natural_keys:
            skipped += 1
            continue

        payload: dict[str, Any] = {
            "entity_type": entity.entity_type,
            "canonical_id": entity.entity_id,
            "alias": entity.canonical_name,
            "scope": entity.scope,
        }
        if entity.aliases:
            payload["additional_aliases"] = list(entity.aliases)

        if not dry_run:
            try:
                fact_input = ProgramFactInput(
                    fact_type="entity.alias",
                    entity_refs=(entity.entity_id,),
                    payload=payload,
                    scope="program",
                    source_signal_ids=(),
                    natural_key=nk,
                    created_by=emitted_by,
                )
                store.append_fact(fact_input, recorded_at=now)
                emitted += 1
            except Exception:
                skipped += 1
        else:
            emitted += 1

    return AliasEmissionResult(emitted=emitted, skipped_duplicates=skipped)
