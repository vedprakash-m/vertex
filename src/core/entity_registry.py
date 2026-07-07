"""WI-2.0 / WI-2.1: Entity Registry — exact + casefold + fuzzy ladder; program + org scope.

Resolution ladder (§6.4):
  1. Exact match (case-sensitive)
  2. Casefold (lowercase normalization)
  3. rapidfuzz fuzzy match — per-scope thresholds, configurable

Zone A module (INV-1 applies — must not import from src.ai or src.m365).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.core.config_loader import PROGRAMS_ROOT
from src.core.program_reality import CanonicalEntity

# ---------------------------------------------------------------------------
# Fuzzy matching thresholds (per-scope, WI-2.1)
# ---------------------------------------------------------------------------

# Default score thresholds per entity scope.
# rapidfuzz returns scores 0-100; below threshold → no match.
_DEFAULT_FUZZY_THRESHOLDS: dict[str, float] = {
    "program": 88.0,  # tighter for within-program entities
    "org": 85.0,      # slightly looser for org-wide entities
}

# ---------------------------------------------------------------------------
# Org-level entities path
# ---------------------------------------------------------------------------

_ORG_ENTITIES_PATH = Path("vertex/knowledge/entities.yaml")
_PROGRAM_ENTITIES_FILENAME = "entities.yaml"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_entities_yaml(data: dict[str, Any]) -> tuple[CanonicalEntity, ...]:
    """Parse the entities YAML format into CanonicalEntity instances."""
    raw_entities = data.get("entities", [])
    result: list[CanonicalEntity] = []
    for item in raw_entities:
        entity_id = str(item.get("entity_id") or item.get("id", ""))
        entity_type = str(item.get("entity_type") or item.get("type", "person"))
        canonical_name = str(item.get("canonical_name") or item.get("name", ""))
        aliases: tuple[str, ...] = tuple(str(a) for a in (item.get("aliases") or []))
        scope = str(item.get("scope", "program"))
        if not entity_id or not canonical_name:
            continue
        result.append(CanonicalEntity(
            entity_id=entity_id,
            entity_type=entity_type,
            canonical_name=canonical_name,
            aliases=aliases,
            scope=scope,
        ))
    return tuple(result)


# ---------------------------------------------------------------------------
# EntityRegistry
# ---------------------------------------------------------------------------

class EntityRegistry:
    """Resolves raw entity references to canonical identities (WI-2.0).

    Supports program-scope entities (programs/<id>/knowledge/entities.yaml)
    and org-scope entities (vertex/knowledge/entities.yaml).

    Resolution order (Phase 1 — WI-2.1 adds fuzzy tier):
    1. Exact match against canonical_name or any alias
    2. Casefold (lowercase) match against canonical_name or any alias
    3. None if below threshold (WI-2.1 adds fuzzy below this point)
    """

    def __init__(
        self,
        program_entities: tuple[CanonicalEntity, ...],
        org_entities: tuple[CanonicalEntity, ...],
    ) -> None:
        self._program_entities = program_entities
        self._org_entities = org_entities
        # Build lookup indexes
        self._exact_index: dict[str, CanonicalEntity] = {}
        self._casefold_index: dict[str, CanonicalEntity] = {}
        # org entities loaded first (lower priority), then program overrides
        for entity in (*org_entities, *program_entities):
            self._exact_index[entity.entity_id] = entity
            self._casefold_index[entity.entity_id.casefold()] = entity
            self._exact_index[entity.canonical_name] = entity
            self._casefold_index[entity.canonical_name.casefold()] = entity
            for alias in entity.aliases:
                self._exact_index[alias] = entity
                self._casefold_index[alias.casefold()] = entity

    @classmethod
    def load(
        cls,
        program_id: str,
        *,
        programs_root: Path = PROGRAMS_ROOT,
        org_scope: bool = True,
        _repo_root: Path | None = None,
    ) -> "EntityRegistry":
        """Load entity registry from disk. Call once per command invocation."""
        # Program-scope entities
        program_path = programs_root / program_id / "knowledge" / _PROGRAM_ENTITIES_FILENAME
        program_entities: tuple[CanonicalEntity, ...] = ()
        if program_path.exists():
            raw = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
            program_entities = _parse_entities_yaml(raw)

        # Org-scope entities
        org_entities: tuple[CanonicalEntity, ...] = ()
        if org_scope:
            repo_root = _repo_root or (programs_root.parent)
            org_path = repo_root / _ORG_ENTITIES_PATH
            if org_path.exists():
                raw = yaml.safe_load(org_path.read_text(encoding="utf-8")) or {}
                org_entities = _parse_entities_yaml(raw)

        return cls(
            program_entities=program_entities,
            org_entities=org_entities,
        )

    def resolve(
        self,
        raw: str,
        *,
        entity_type: str | None = None,
        scope: str = "program",
    ) -> CanonicalEntity | None:
        """Resolve a raw entity reference to a CanonicalEntity.

        Phase 1: exact → casefold.
        WI-2.1 adds fuzzy tier below casefold.

        Args:
            raw: Raw entity reference string.
            entity_type: Optional filter by entity type.
            scope: Preferred scope ('program' or 'org').
        """
        if not raw:
            return None

        def _type_matches(e: CanonicalEntity) -> bool:
            return entity_type is None or e.entity_type == entity_type

        # 1. Exact match
        if raw in self._exact_index:
            entity = self._exact_index[raw]
            if _type_matches(entity):
                return entity

        # 2. Casefold match
        casefolded = raw.casefold()
        if casefolded in self._casefold_index:
            entity = self._casefold_index[casefolded]
            if _type_matches(entity):
                return entity

        # 3. rapidfuzz fuzzy match (WI-2.1)
        # Build the candidate pool (all canonical names and aliases)
        try:
            from rapidfuzz import fuzz as _fuzz, process as _process
        except ImportError:
            return None  # graceful degradation if rapidfuzz unavailable

        threshold = _DEFAULT_FUZZY_THRESHOLDS.get(scope, 85.0)
        all_entities_list = list(self._exact_index.items())  # (string, entity) pairs
        if not all_entities_list:
            return None

        # Use rapidfuzz WRatio scorer for robustness
        best_match = _process.extractOne(
            raw,
            [k for k, _ in all_entities_list],
            scorer=_fuzz.WRatio,
            score_cutoff=threshold,
        )
        if best_match is not None:
            matched_key = best_match[0]
            entity = self._exact_index[matched_key]
            if _type_matches(entity):
                return entity

        # 4. None — below all thresholds
        return None

    def all_entities(
        self,
        *,
        scope: str | None = None,
        entity_type: str | None = None,
    ) -> tuple[CanonicalEntity, ...]:
        """Return all entities, optionally filtered by scope and/or type."""
        all_e = (*self._org_entities, *self._program_entities)
        if scope is not None:
            all_e = tuple(e for e in all_e if e.scope == scope)
        if entity_type is not None:
            all_e = tuple(e for e in all_e if e.entity_type == entity_type)
        return all_e

    def register(self, entity: CanonicalEntity) -> "EntityRegistry":
        """Return a new EntityRegistry with the given entity added.

        The entity is treated as a program-scope entity. Immutable update.
        WI-2.3: used for entity.alias fact emission.
        """
        new_program = self._program_entities + (entity,)
        return EntityRegistry(
            program_entities=new_program,
            org_entities=self._org_entities,
        )

    @property
    def program_entity_count(self) -> int:
        return len(self._program_entities)

    @property
    def org_entity_count(self) -> int:
        return len(self._org_entities)


# ---------------------------------------------------------------------------
# Resolution-rate tracking (WI-2.4 groundwork)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResolutionRateBlock:
    """Per-scope resolution rate summary (for WI-2.4 gather_state integration)."""
    scope: str
    total_attempts: int
    resolved_exact: int
    resolved_casefold: int
    resolved_fuzzy: int
    unresolved: int

    @property
    def resolution_rate(self) -> float:
        if self.total_attempts == 0:
            return 1.0
        resolved = self.resolved_exact + self.resolved_casefold + self.resolved_fuzzy
        return resolved / self.total_attempts
