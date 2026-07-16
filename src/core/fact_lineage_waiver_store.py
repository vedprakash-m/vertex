"""ADF-W2.5 (specs/arch-data-fix.md Section 8.14.2): explicit lineage
exemptions for individual facts.

Mirrors ``source_waiver_store.py``'s shape (owner/reason/granted/expires)
at fact granularity (keyed by ``natural_key``) instead of source-contract
granularity -- a legacy fact with no durable evidence to backfill lineage
from gets an explicit, owned, time-bounded waiver rather than silently
counting as an unlineaged defect forever.
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
class FactLineageWaiver:
    natural_key: str
    owner: str
    reason: str
    granted: date
    expires: date

    def is_active(self, *, as_of: date) -> bool:
        return self.granted <= as_of <= self.expires


def load_fact_lineage_waivers(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[FactLineageWaiver, ...]:
    """Read ``programs/<id>/fact_lineage_waivers.yaml``. Returns an empty
    tuple if the file is absent (the common case -- most programs have no
    waivers yet)."""
    path = programs_root / program_id / "fact_lineage_waivers.yaml"
    if not path.exists():
        return ()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid fact lineage waiver document in {path}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"Unsupported fact lineage waiver schema version in {path} (expected {SCHEMA_VERSION!r})")

    raw_waivers = document.get("waivers", [])
    if not isinstance(raw_waivers, list):
        raise ConfigError(f"'waivers' must be a list in {path}")
    return tuple(_parse_waiver(entry, path=path) for entry in raw_waivers)


def _parse_waiver(entry: object, *, path: Path) -> FactLineageWaiver:
    if not isinstance(entry, dict):
        raise ConfigError(f"each waiver entry in {path} must be a mapping")
    natural_key = entry.get("natural_key")
    owner = entry.get("owner")
    reason = entry.get("reason")
    if not isinstance(natural_key, str) or not natural_key.strip():
        raise ConfigError(f"waiver missing non-empty 'natural_key' in {path}")
    if not isinstance(owner, str) or not owner.strip():
        raise ConfigError(f"waiver {natural_key!r} missing non-empty 'owner' in {path}")
    if not isinstance(reason, str) or not reason.strip():
        raise ConfigError(f"waiver {natural_key!r} missing non-empty 'reason' in {path}")
    granted = _coerce_date(entry.get("granted"), field_name="granted", natural_key=natural_key, path=path)
    expires = _coerce_date(entry.get("expires"), field_name="expires", natural_key=natural_key, path=path)
    return FactLineageWaiver(natural_key=natural_key, owner=owner, reason=reason, granted=granted, expires=expires)


def _coerce_date(value: object, *, field_name: str, natural_key: str, path: Path) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"waiver {natural_key!r} has an invalid {field_name!r} date in {path}")


__all__ = ["FactLineageWaiver", "load_fact_lineage_waivers", "SCHEMA_VERSION"]
