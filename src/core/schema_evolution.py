"""GAP-40: Schema Evolution / State Migration Engine.

Long-lived programs outlive any single schema version. When ``program.yaml``
flips from 2.0 → 3.0 (or 3.0 → 4.0) the operator must run a **safe,
auditable** transformation — not a hand-edit. ``vertex migrate`` already
handles storage backend; this module handles **schema-version evolution**.

Design:
  - A ``SchemaEvolution`` is a sequence of ``SchemaVersionStep`` entries.
  - Each step declares:
      - ``from_version`` / ``to_version`` (semver-ish, major.minor)
      - ``description`` (one-line summary for the audit log)
      - ``transform(document: dict) -> dict`` (pure function)
  - The engine ``run_evolution(document, evolution)`` walks the steps,
    applying them in order, and returns a ``SchemaEvolutionResult`` with
    per-step audit rows.
  - Each step writes a single line to ``migration_log.jsonl`` (per
    `privacy_matrix.py`'s ``migration_log.jsonl`` sidecar) with the
    from/to versions, the operator identity, and a SHA-256 of the
    pre-transform document for forensic rollback.
  - Steps are **idempotent**: re-running on a document already at the
    target version is a no-op (recorded as such).

This module is pure-Python; the actual ``vertex admin upgrade-state``
CLI command lives in ``src/commands/admin.py`` and wraps it.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.core.jsonl_utils import append_jsonl_line


@dataclass(frozen=True, slots=True)
class SchemaVersionStep:
    """One step in a schema evolution."""

    from_version: str
    to_version: str
    description: str
    transform: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SchemaEvolutionResult:
    """Audit-friendly per-evolution summary."""

    artifact: str
    starting_version: str
    ending_version: str
    steps_applied: tuple[SchemaVersionStep, ...] = field(default_factory=tuple)
    migration_log_path: Path | None = None
    applied: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "starting_version": self.starting_version,
            "ending_version": self.ending_version,
            "steps_applied": [
                {
                    "from_version": step.from_version,
                    "to_version": step.to_version,
                    "description": step.description,
                }
                for step in self.steps_applied
            ],
            "migration_log_path": (
                str(self.migration_log_path)
                if self.migration_log_path is not None
                else None
            ),
            "applied": self.applied,
        }


def _document_version(document: dict[str, Any]) -> str:
    """Return the document's ``schema_version`` (default '0.0')."""
    value = document.get("schema_version")
    if isinstance(value, str) and value:
        return value
    return "0.0"


def _document_hash(document: dict[str, Any]) -> str:
    payload = json.dumps(document, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _append_migration_log(
    path: Path,
    *,
    artifact: str,
    from_version: str,
    to_version: str,
    description: str,
    pre_hash: str,
    operator: str,
    applied_at: datetime,
) -> None:
    """Append a single line to the migration_log.jsonl sidecar.

    Uses ``append_jsonl_line`` so the write is file-locked and consistent
    with the PB-37 contract that bans raw ``open(..., 'a')`` JSONL writes
    anywhere in ``src/``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "artifact": artifact,
        "from_version": from_version,
        "to_version": to_version,
        "description": description,
        "pre_hash": pre_hash,
        "operator": operator,
        "applied_at": applied_at.isoformat(),
    }
    append_jsonl_line(path, json.dumps(entry, sort_keys=True) + "\n")


def run_evolution(
    document: dict[str, Any],
    *,
    artifact: str,
    evolution: tuple[SchemaVersionStep, ...],
    apply: bool = True,
    migration_log_path: Path | None = None,
    operator: str = "vertex.admin",
    now: datetime | None = None,
) -> SchemaEvolutionResult:
    """Walk ``evolution`` from the document's current version forward.

    Returns a ``SchemaEvolutionResult``. If ``apply=False``, the steps
    are evaluated against a deep copy of the document and the original
    is not mutated; this is the dry-run path.
    """
    if apply:
        working: dict[str, Any] = document
    else:
        working = json.loads(json.dumps(document, default=str))

    starting_version = _document_version(working)
    current_version = starting_version
    applied: list[SchemaVersionStep] = []
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    for step in evolution:
        if step.from_version != current_version:
            # Skip — either already at or past this step's target.
            continue
        pre_hash = _document_hash(working) if apply else "<dry-run>"
        working = step.transform(working)
        # The step is expected to set the new schema_version; if not,
        # do it for it (best-effort, never fails).
        if _document_version(working) == current_version:
            working["schema_version"] = step.to_version
        if apply and migration_log_path is not None:
            _append_migration_log(
                migration_log_path,
                artifact=artifact,
                from_version=step.from_version,
                to_version=step.to_version,
                description=step.description,
                pre_hash=pre_hash,
                operator=operator,
                applied_at=timestamp,
            )
        applied.append(step)
        current_version = step.to_version

    if apply:
        # Mutate the caller's document in place so the result survives.
        # Snapshot first because ``working`` may share identity with
        # ``document`` (the in-place case) and a naive clear() would
        # wipe the source we're about to read.
        snapshot = dict(working)
        document.clear()
        document.update(snapshot)

    return SchemaEvolutionResult(
        artifact=artifact,
        starting_version=starting_version,
        ending_version=current_version,
        steps_applied=tuple(applied),
        migration_log_path=migration_log_path,
        applied=apply,
    )


# ---------------------------------------------------------------------------
# Built-in evolution: program.yaml 2.0 → 3.0
# ---------------------------------------------------------------------------


def program_yaml_2_to_3(document: dict[str, Any]) -> dict[str, Any]:
    """Transform program.yaml schema 2.0 → 3.0.

    The 2.0→3.0 evolution adds:
      - ``retention_days`` (default 365) at the top level
      - ``privacy_classification`` (default 'internal') on every nested
        workstream / signal block
      - ``schema_version`` bumped to 3.0
    """
    document = dict(document)
    document.setdefault("retention_days", 365)
    workstreams = document.get("workstreams") or []
    if isinstance(workstreams, list):
        for entry in workstreams:
            if isinstance(entry, dict):
                entry.setdefault("privacy_classification", "internal")
    signals = document.get("signals") or []
    if isinstance(signals, list):
        for entry in signals:
            if isinstance(entry, dict):
                entry.setdefault("privacy_classification", "internal")
    document["schema_version"] = "3.0"
    return document


def program_yaml_3_to_4(document: dict[str, Any]) -> dict[str, Any]:
    """Transform program.yaml schema 3.0 → 4.0.

    The 3.0→4.0 evolution adds:
      - ``fact_store_sor`` (default 'legacy') at the top level
      - ``decisions_corroboration_required`` (default False) flag
      - ``schema_version`` bumped to 4.0
    """
    document = dict(document)
    document.setdefault("fact_store_sor", "legacy")
    document.setdefault("decisions_corroboration_required", False)
    document["schema_version"] = "4.0"
    return document


PROGRAM_YAML_EVOLUTION: tuple[SchemaVersionStep, ...] = (
    SchemaVersionStep(
        from_version="2.0",
        to_version="3.0",
        description="Add retention_days + privacy_classification; bump to 3.0",
        transform=program_yaml_2_to_3,
    ),
    SchemaVersionStep(
        from_version="3.0",
        to_version="4.0",
        description="Add fact_store_sor + decisions_corroboration_required; bump to 4.0",
        transform=program_yaml_3_to_4,
    ),
)
