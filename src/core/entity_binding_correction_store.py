"""ADF-W2.6 (specs/arch-data-fix.md Section 8.14.3): "accepted/rejected
correction" for entity bindings.

An operator can accept a specific raw-reference-to-entity binding (short-
circuits ``EntityRegistry.resolve_with_binding`` straight to that entity at
confidence 1.0, bypassing an ambiguous or below-threshold fuzzy result) or
explicitly reject one (the raw reference stays permanently unresolved
rather than being re-attempted through the fuzzy tier on every run).
Mirrors ``source_waiver_store.py``/``fact_lineage_waiver_store.py``'s
YAML-file-per-program shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import ConfigError

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class EntityBindingCorrection:
    raw_ref: str
    #: None means an explicit rejection: this raw_ref must never auto-resolve.
    accepted_entity_id: str | None
    corrected_by: str
    corrected_at: date
    reason: str | None = None


def load_entity_binding_corrections(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, EntityBindingCorrection]:
    """Read ``programs/<id>/entity_binding_corrections.yaml``, keyed by
    ``raw_ref``. Returns an empty dict if the file is absent (the common
    case -- most programs have no corrections yet)."""
    path = programs_root / program_id / "entity_binding_corrections.yaml"
    if not path.exists():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid entity binding correction document in {path}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"Unsupported entity binding correction schema version in {path} (expected {SCHEMA_VERSION!r})")

    raw_corrections = document.get("corrections", [])
    if not isinstance(raw_corrections, list):
        raise ConfigError(f"'corrections' must be a list in {path}")

    corrections: dict[str, EntityBindingCorrection] = {}
    for entry in raw_corrections:
        correction = _parse_correction(entry, path=path)
        corrections[correction.raw_ref] = correction
    return corrections


def _parse_correction(entry: object, *, path: Path) -> EntityBindingCorrection:
    if not isinstance(entry, dict):
        raise ConfigError(f"each correction entry in {path} must be a mapping")
    raw_ref = entry.get("raw_ref")
    corrected_by = entry.get("corrected_by")
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        raise ConfigError(f"correction missing non-empty 'raw_ref' in {path}")
    if not isinstance(corrected_by, str) or not corrected_by.strip():
        raise ConfigError(f"correction {raw_ref!r} missing non-empty 'corrected_by' in {path}")

    accepted_entity_id = entry.get("accepted_entity_id")
    if accepted_entity_id is not None and (not isinstance(accepted_entity_id, str) or not accepted_entity_id.strip()):
        raise ConfigError(f"correction {raw_ref!r} has an invalid 'accepted_entity_id' in {path}")

    corrected_at = _coerce_date(entry.get("corrected_at"), raw_ref=raw_ref, path=path)
    reason = entry.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ConfigError(f"correction {raw_ref!r} has a non-string 'reason' in {path}")

    return EntityBindingCorrection(
        raw_ref=raw_ref,
        accepted_entity_id=accepted_entity_id,
        corrected_by=corrected_by,
        corrected_at=corrected_at,
        reason=reason,
    )


def _coerce_date(value: object, *, raw_ref: str, path: Path) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"correction {raw_ref!r} has an invalid 'corrected_at' date in {path}")


__all__ = ["EntityBindingCorrection", "SCHEMA_VERSION", "load_entity_binding_corrections"]
