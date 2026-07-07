"""FR-SG-32: Entity resolution — resolve people and work-item references.

Normalises entity references in signals through a two-layer lookup:
  1. Primary: existing PersonDirectory entries (alias, email, display_name)
  2. Overlay: optional alias_map.yaml in the program directory

This reduces the 10–25% entity binding error reported in the spec by
resolving aliases before signals are bound to decisions/DRIs/facts.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import re
import yaml

from src.core.config_loader import PROGRAMS_ROOT
from src.core.models_v2 import PersonDirectory, Signal


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AliasEntry:
    """One canonical identity with all known aliases."""

    canonical: str          # canonical alias (e.g. "jsmith")
    aliases: tuple[str, ...]  # additional aliases, emails, display names
    email: str | None = None
    display_name: str | None = None


# ---------------------------------------------------------------------------
# Alias map loader
# ---------------------------------------------------------------------------


def load_alias_map(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AliasEntry, ...]:
    """Load the optional alias_map.yaml from the program directory.

    Falls back to an empty tuple if the file does not exist.
    Schema::

        aliases:
          - canonical: jsmith
            aliases: [john.smith, john_smith, "John Smith"]
            email: jsmith@example.com
            display_name: "John Smith"
    """
    path = programs_root / program_id / "alias_map.yaml"
    if not path.exists():
        return ()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[AliasEntry] = []
    for item in raw.get("aliases") or []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical") or "").strip().lower()
        if not canonical:
            continue
        raw_aliases = item.get("aliases") or []
        aliases = tuple(str(a).strip().lower() for a in raw_aliases if str(a).strip())
        entries.append(
            AliasEntry(
                canonical=canonical,
                aliases=aliases,
                email=_opt_str(item.get("email")),
                display_name=_opt_str(item.get("display_name")),
            )
        )
    return tuple(entries)


def build_combined_directory(
    people: tuple[PersonDirectory, ...],
    alias_entries: tuple[AliasEntry, ...],
) -> tuple[AliasEntry, ...]:
    """Merge PersonDirectory entries with alias_map entries.

    PersonDirectory is the primary source; alias_map is an overlay.
    Returns a unified lookup sequence used by resolve_alias().
    """
    combined: list[AliasEntry] = []

    # Convert PersonDirectory → AliasEntry
    for person in people:
        aliases: list[str] = []
        if person.email:
            aliases.append(person.email.lower())
        if person.display_name:
            aliases.append(person.display_name.lower())
        combined.append(
            AliasEntry(
                canonical=person.alias.lower(),
                aliases=tuple(aliases),
                email=person.email,
                display_name=person.display_name,
            )
        )

    # Merge alias_map overlay: alias_map wins for its own canonical entries
    overlay_canonicals = {e.canonical for e in alias_entries}
    for entry in combined:
        if entry.canonical not in overlay_canonicals:
            alias_entries = alias_entries + (entry,)

    return alias_entries + tuple(
        e for e in combined if e.canonical not in {x.canonical for x in alias_entries}
    )


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_alias(
    raw: str,
    alias_entries: tuple[AliasEntry, ...],
) -> str | None:
    """Return the canonical alias for ``raw``, or None if not found.

    Lookup is case-insensitive. Checks both the canonical value and all aliases.
    """
    needle = raw.strip().lower()
    if not needle:
        return None
    for entry in alias_entries:
        if needle == entry.canonical:
            return entry.canonical
        if needle in entry.aliases:
            return entry.canonical
        if entry.email and needle == entry.email.lower():
            return entry.canonical
    return None


def extract_mention_candidates(text: str) -> tuple[str, ...]:
    """Extract @mention-style tokens and email addresses from signal text.

    Returns raw strings suitable for passing to resolve_alias().
    """
    candidates: list[str] = []

    # @mention tokens: @word or @first.last
    for match in re.finditer(r"@([\w.]+)", text):
        candidates.append(match.group(1))

    # Email addresses
    for match in re.finditer(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", text):
        candidates.append(match.group(0))

    return tuple(dict.fromkeys(candidates))  # dedupe while preserving order


def apply_entity_resolution(
    signal: Signal,
    alias_entries: tuple[AliasEntry, ...],
) -> Signal:
    """Return a copy of *signal* with entity_refs and sender_alias resolved.

    Resolution is a best-effort normalisation; if a reference cannot be
    resolved it is kept as-is so no information is lost.
    """
    # Resolve existing entity_refs
    resolved_refs: list[str] = []
    for ref in signal.entity_refs:
        canonical = resolve_alias(ref, alias_entries)
        resolved_refs.append(canonical if canonical is not None else ref)

    # Resolve sender_alias in metadata
    new_metadata = dict(signal.metadata) if signal.metadata else {}
    raw_sender = new_metadata.get("sender_alias")
    if isinstance(raw_sender, str):
        canonical_sender = resolve_alias(raw_sender, alias_entries)
        if canonical_sender is not None:
            new_metadata["sender_alias"] = canonical_sender

    # Extract and resolve any @mentions from signal text not already in entity_refs
    mention_candidates = extract_mention_candidates(signal.text)
    for candidate in mention_candidates:
        canonical = resolve_alias(candidate, alias_entries)
        if canonical is not None and canonical not in resolved_refs:
            resolved_refs.append(canonical)

    return replace(
        signal,
        entity_refs=tuple(resolved_refs),
        metadata=new_metadata,
    )


# ---------------------------------------------------------------------------
# Work-item reference resolution
# ---------------------------------------------------------------------------


_WI_REF_PATTERN = re.compile(r"(?:AB#|(?<!\w)#)(\d{4,8})\b")


def extract_work_item_refs(text: str) -> tuple[int, ...]:
    """Extract ADO work item IDs from text (AB#12345 or #12345 format)."""
    return tuple(dict.fromkeys(int(m.group(1)) for m in _WI_REF_PATTERN.finditer(text)))


# ---------------------------------------------------------------------------
# S-2c: Fact-store entity-ref resolution (before shaping, bridge-only)
# ---------------------------------------------------------------------------

_BRIDGE_WRITE_AUTHORITY = "bridge"
_UNRESOLVED_PREFIX = "UNRESOLVED:"


@dataclass(frozen=True, slots=True)
class FactEntityResolutionResult:
    """Outcome of resolving entity refs for one ProgramFactInput (S-2c)."""
    original_refs: tuple[str, ...]
    resolved_refs: tuple[str, ...]
    unresolved_count: int
    resolution_strategy: str   # "direct_match" | "partial_match" | "unresolved" | "passthrough"


def resolve_fact_entity_refs_for_store(
    entity_refs: tuple[str, ...],
    *,
    known_natural_keys: frozenset[str],
    write_authority: str,
) -> FactEntityResolutionResult:
    """S-2c: Resolve entity refs for a REV-sourced (bridge) fact before writing.

    Only resolves refs when ``write_authority == "bridge"`` (AI_EXTRACTED via REV).
    All other callers receive a passthrough result with unchanged refs.

    Resolution rules (v1 — 4 approved S-0g claim types):
      1. Exact match against ``known_natural_keys`` → canonical ref retained.
      2. Partial match: pure integer ref → matches key ending in ``:NNN`` or ``=NNN``.
      3. No match → prefix with ``UNRESOLVED:`` (never silently drops refs).
    """
    if write_authority != _BRIDGE_WRITE_AUTHORITY:
        return FactEntityResolutionResult(
            original_refs=entity_refs,
            resolved_refs=entity_refs,
            unresolved_count=0,
            resolution_strategy="passthrough",
        )

    resolved: list[str] = []
    unresolved = 0
    strategies: list[str] = []

    for ref in entity_refs:
        if ref in known_natural_keys:
            resolved.append(ref)
            strategies.append("direct_match")
        else:
            canonical = _try_partial_key_match(ref, known_natural_keys)
            if canonical is not None:
                resolved.append(canonical)
                strategies.append("partial_match")
            else:
                resolved.append(f"{_UNRESOLVED_PREFIX}{ref}")
                unresolved += 1
                strategies.append("unresolved")

    if not strategies:
        agg = "passthrough"
    elif all(s == "direct_match" for s in strategies):
        agg = "direct_match"
    elif unresolved == len(strategies):
        agg = "unresolved"
    else:
        agg = "partial_match"

    return FactEntityResolutionResult(
        original_refs=entity_refs,
        resolved_refs=tuple(resolved),
        unresolved_count=unresolved,
        resolution_strategy=agg,
    )


def _try_partial_key_match(ref: str, known_natural_keys: frozenset[str]) -> str | None:
    """Match a short (numeric) ref to a canonical ``namespace:NNN`` key."""
    ref_stripped = ref.strip()
    if re.fullmatch(r"\d+", ref_stripped):
        candidates = [
            k for k in known_natural_keys
            if k.endswith(f":{ref_stripped}") or k.endswith(f"={ref_stripped}")
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
