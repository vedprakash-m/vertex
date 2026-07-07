"""FR-SG-16: ExternalDependency model and JSONL store.

WS-2 (PB-33): schema evolution — the original model had only the ADO write-
back surface (`approval_type: Literal["ado", "manual"]`). The evolved model
adds a typed state machine for the GitHub / SharePoint connectors and a
criticality / resolution metadata surface for gating. All new fields have
defaults so legacy JSONL records (which lack them) deserialize unchanged.
"""
from __future__ import annotations

import json
from src.core.jsonl_utils import parse_jsonl_line
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from src.core.config_loader import PROGRAMS_ROOT


# Dep state machine — `state` is what the latest connector poll reported.
DependencyState = Literal["open", "closed", "merged", "fulfilled", "stale", "unknown"]
DependencyCriticality = Literal["normal", "high", "blocker"]


@dataclass(frozen=True, slots=True)
class ExternalDependency:
    dep_id: str
    team: str
    tracked_items: tuple[int, ...]
    approval_type: Literal["ado", "manual", "github", "sharepoint"]
    gates: tuple[str, ...]
    canonical_owner_program: str | None
    last_seen: datetime | None
    # WS-2 evolution — defaulted to keep legacy JSONL records loadable.
    state: DependencyState = "unknown"
    is_fulfilled: bool = False
    criticality: DependencyCriticality = "normal"
    resolved_at: datetime | None = None
    # Source hint for connectors (e.g. "github:owner/repo#42"); optional.
    source_ref: str | None = None


def _dep_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "external_dependencies.jsonl"


def load_external_dependencies(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ExternalDependency, ...]:
    """Load all external dependencies for a program from JSONL store.

    WS-2 PB-33: all new fields (state, is_fulfilled, criticality, resolved_at,
    source_ref) are defaulted in the dataclass. A legacy record (lacking the
    new keys) is upgraded to the current schema with state="unknown",
    is_fulfilled=False, criticality="normal". This keeps existing JSONL files
    loadable without a migration pass.
    """
    path = _dep_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    deps: list[ExternalDependency] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = parse_jsonl_line(line)
        if not isinstance(record, dict):
            raise TypeError("external dependency rows must be JSON objects")
        last_seen_raw = record.get("last_seen")
        deps.append(
            ExternalDependency(
                dep_id=_required_string(record["dep_id"], field_name="dep_id"),
                team=_required_string(record["team"], field_name="team"),
                tracked_items=_parse_tracked_items(record.get("tracked_items")),
                approval_type=_parse_approval_type(record["approval_type"]),
                gates=_parse_gates(record.get("gates")),
                canonical_owner_program=_parse_optional_string(
                    record.get("canonical_owner_program"), field_name="canonical_owner_program"
                ),
                last_seen=_parse_optional_datetime(last_seen_raw, field_name="last_seen"),
                state=_parse_state(record.get("state", "unknown")),
                is_fulfilled=_parse_bool(record.get("is_fulfilled"), field_name="is_fulfilled", default=False),
                criticality=_parse_criticality(record.get("criticality", "normal")),
                resolved_at=_parse_optional_datetime(record.get("resolved_at"), field_name="resolved_at"),
                source_ref=_parse_optional_string(record.get("source_ref"), field_name="source_ref"),
            )
        )
    return tuple(deps)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_optional_datetime(value: object, *, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _parse_optional_string(value: object, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_approval_type(value: object) -> Literal["ado", "manual", "github", "sharepoint"]:
    if not isinstance(value, str):
        raise TypeError("approval_type must be a string")
    if value not in ("ado", "manual", "github", "sharepoint"):
        raise ValueError(f"Unsupported approval_type '{value}'")
    return cast(Literal["ado", "manual", "github", "sharepoint"], value)


def _parse_state(value: object) -> DependencyState:
    if value in (None, ""):
        return "unknown"
    if not isinstance(value, str):
        raise TypeError("state must be a string")
    if value not in ("open", "closed", "merged", "fulfilled", "stale", "unknown"):
        raise ValueError(f"Unsupported dependency state '{value}'")
    return cast(DependencyState, value)


def _parse_criticality(value: object) -> DependencyCriticality:
    if value in (None, ""):
        return "normal"
    if not isinstance(value, str):
        raise TypeError("criticality must be a string")
    if value not in ("normal", "high", "blocker"):
        raise ValueError(f"Unsupported criticality '{value}'")
    return cast(DependencyCriticality, value)


def _parse_bool(value: object, *, field_name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _parse_gates(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise TypeError("gates must be a list of strings")
    gates: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError("gates must contain strings only")
        gates.append(entry)
    return tuple(gates)


def _parse_tracked_items(value: object) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise TypeError("tracked_items must be a list of integers")
    tracked_items: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise TypeError("tracked_items must contain integers only")
        tracked_items.append(entry)
    return tuple(tracked_items)


def save_external_dependency(
    program_id: str,
    dep: ExternalDependency,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append or update an external dependency record (upsert by dep_id).

    WS-2: the persisted schema now includes state / is_fulfilled / criticality
    / resolved_at / source_ref. Legacy fields are preserved; new fields
    default to their dataclass defaults when not provided.
    """
    path = _dep_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_external_dependencies(program_id, programs_root=programs_root)
    merged = {d.dep_id: d for d in existing}
    merged[dep.dep_id] = dep
    rows = []
    for d in merged.values():
        row = {
            "dep_id": d.dep_id,
            "team": d.team,
            "tracked_items": list(d.tracked_items),
            "approval_type": d.approval_type,
            "gates": list(d.gates),
            "canonical_owner_program": d.canonical_owner_program,
            "last_seen": d.last_seen.isoformat() if d.last_seen is not None else None,
            "state": d.state,
            "is_fulfilled": d.is_fulfilled,
            "criticality": d.criticality,
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at is not None else None,
            "source_ref": d.source_ref,
        }
        rows.append(json.dumps(row, ensure_ascii=False))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
