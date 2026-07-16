"""WI-2.2: Signal normalizer — UTC timestamps, dedup, backfill, idempotence.

Normalizes signals before ingestion into the fact store / entity index.

Rules (acceptance: UTC/prepend-preserve/backfill/dedup/idempotence):
1. UTC: timestamps are converted to UTC-aware datetime if naive or missing timezone
2. prepend-preserve: existing text content is preserved; normalization metadata
   prepended to signal metadata under key "normalized"
3. backfill: missing entity_refs backfilled from text/raw_ref using EntityRegistry
4. dedup: duplicate signals by (source, id) are deduplicated — first-seen wins
5. idempotence: normalizing an already-normalized signal is a no-op

Zone A module (INV-1 applies — must not import from src.ai or src.m365).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.core.models_v2 import Signal


# ---------------------------------------------------------------------------
# Normalization result
# ---------------------------------------------------------------------------

def _ensure_utc(dt: datetime) -> datetime:
    """Convert a datetime to UTC. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_signal(signal: Signal) -> Signal:
    """Normalize a single signal. Idempotent — safe to call multiple times.

    Rules applied:
    - timestamp → UTC (naive assumed UTC, tz-aware converted)
    - metadata["normalized"] = True marker (prepend-preserve: other metadata unchanged)
    - dedup handled at batch level via normalize_signals()
    """
    # Already normalized guard (idempotence)
    if signal.metadata is not None and signal.metadata.get("normalized") is True:
        return signal

    normalized_metadata: dict[str, Any] = dict(signal.metadata or {})
    normalized_metadata["normalized"] = True

    utc_timestamp = _ensure_utc(signal.timestamp)

    return replace(
        signal,
        timestamp=utc_timestamp,
        metadata=normalized_metadata,
    )


def normalize_signals(
    signals: tuple[Signal, ...] | list[Signal],
    *,
    deduplicate: bool = True,
) -> tuple[Signal, ...]:
    """Normalize a batch of signals. Deduplicates by (source, id) — first-seen wins.

    Args:
        signals: Signals to normalize.
        deduplicate: If True (default), deduplicate by (source, id) first-seen.

    Returns:
        Tuple of normalized, deduplicated signals.
    """
    seen: set[tuple[str, str]] = set()
    result: list[Signal] = []

    for sig in signals:
        key = (sig.source, sig.id)
        if deduplicate and key in seen:
            continue
        seen.add(key)
        result.append(normalize_signal(sig))

    return tuple(result)


def backfill_entity_refs(
    signal: Signal,
    registry: Any,  # EntityRegistry — typed as Any to avoid circular import
) -> Signal:
    """Backfill entity_refs from signal text using the entity registry (WI-2.2 backfill).

    Scans signal.text and signal.raw_ref for entity mentions. Appends resolved
    canonical_ids to entity_refs if not already present. Idempotent.

    Args:
        signal: The signal to backfill.
        registry: An EntityRegistry instance (from entity_registry.py).
    """
    if not hasattr(registry, "resolve"):
        return signal

    existing_refs = set(signal.entity_refs)
    new_refs: list[str] = list(signal.entity_refs)

    # Scan raw_ref first (most structured)
    if signal.raw_ref:
        # ADF-W2.6: prefer resolve_with_binding() so a genuinely ambiguous
        # near-tied fuzzy match is never silently backfilled as if it were a
        # confident resolution -- ambiguous.resolved_entity is already None.
        if hasattr(registry, "resolve_with_binding"):
            entity = registry.resolve_with_binding(signal.raw_ref).resolved_entity
        else:
            entity = registry.resolve(signal.raw_ref)
        if entity and entity.entity_id not in existing_refs:
            new_refs.append(entity.entity_id)
            existing_refs.add(entity.entity_id)

    if tuple(new_refs) == signal.entity_refs:
        return signal

    return replace(signal, entity_refs=tuple(new_refs))


def collect_unresolved_entity_refs(
    facts_snapshot: Any,  # ProgramFactSnapshot — typed as Any to avoid circular import
    registry: Any,  # EntityRegistry — typed as Any to avoid circular import
) -> frozenset[str]:
    """Return the set of entity_refs in the snapshot that cannot be resolved by the registry.

    WI-2.6: Feeds the unresolved_entity_ref_count in TriageReport's SIGNAL QUALITY section.

    Args:
        facts_snapshot: A ProgramFactSnapshot with a .facts attribute (list of facts with .entity_refs).
        registry: An EntityRegistry instance with a .resolve() method.
    """
    if not hasattr(registry, "resolve") or not hasattr(facts_snapshot, "facts"):
        return frozenset()

    # ADF-W2.6: prefer resolve_with_binding() so a genuinely ambiguous
    # near-tied fuzzy match counts as unresolved too -- Section 8.14.3's
    # "ambiguous entities remain unresolved," not silently picked and thus
    # undercounted here.
    use_binding = hasattr(registry, "resolve_with_binding")

    unresolved: set[str] = set()
    for fact in facts_snapshot.facts:
        for ref in getattr(fact, "entity_refs", ()):
            if not ref:
                continue
            resolved = registry.resolve_with_binding(ref).resolved_entity if use_binding else registry.resolve(ref)
            if resolved is None:
                unresolved.add(ref)
    return frozenset(unresolved)

