"""specs/people.md Phase 5b, PPL-W5b.1: `delegations.yaml` schema 1.0.

§7.2's exact binding `Delegation` dataclass and `DelegationStatus` enum,
verified to parse the real `knowledge/delegations.example.yaml` fixture
(a Phase 0a artifact predating this code). Loader/writer mirror
`people_membership_schema.py::load_memberships`/`write_memberships`'s
exact pattern (`fast_safe_load` on read; write-temp-then-fsync-then-
`os.replace` on write) -- the established sibling precedent for a flat
list-of-typed-records shared-registry schema, reused rather than
reinvented.

Only the schema/loader/writer layer -- Zone A types, nothing more. The
canonical staged-writer CREATE/REVOKE lifecycle (steward-authorized,
journaled, gated by the `delegation_enabled` kill switch) is PPL-W5b.2's
scope; overlap-conflict detection is PPL-W5b.3's; resolution is
PPL-W5b.4's.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError
from src.core.yaml_utils import fast_safe_load

DELEGATIONS_SCHEMA_VERSION = "1.0"


class DelegationStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class Delegation:
    delegation_id: str
    from_person_entity_id: str
    to_person_entity_id: str
    surfaces: tuple[str, ...]
    valid_from: datetime
    valid_until: datetime
    reason: str
    actor_principal: str
    program_ids: tuple[str, ...] = ()
    workstream_ids: tuple[str, ...] = ()
    status: DelegationStatus = DelegationStatus.ACTIVE


def delegations_path(knowledge_root: Path) -> Path:
    return knowledge_root / "delegations.yaml"


def _delegation_from_payload(raw: dict[str, Any]) -> Delegation:
    return Delegation(
        delegation_id=str(raw["delegation_id"]),
        from_person_entity_id=str(raw["from_person_entity_id"]),
        to_person_entity_id=str(raw["to_person_entity_id"]),
        surfaces=tuple(str(surface) for surface in (raw.get("surfaces") or ())),
        valid_from=datetime.fromisoformat(str(raw["valid_from"]).replace("Z", "+00:00")),
        valid_until=datetime.fromisoformat(str(raw["valid_until"]).replace("Z", "+00:00")),
        reason=str(raw["reason"]),
        actor_principal=str(raw["actor_principal"]),
        program_ids=tuple(str(program_id) for program_id in (raw.get("program_ids") or ())),
        workstream_ids=tuple(str(workstream_id) for workstream_id in (raw.get("workstream_ids") or ())),
        status=DelegationStatus(raw.get("status", "active")),
    )


def delegation_to_payload(delegation: Delegation) -> dict[str, Any]:
    return {
        "delegation_id": delegation.delegation_id,
        "from_person_entity_id": delegation.from_person_entity_id,
        "to_person_entity_id": delegation.to_person_entity_id,
        "surfaces": list(delegation.surfaces),
        "valid_from": delegation.valid_from.isoformat(),
        "valid_until": delegation.valid_until.isoformat(),
        "reason": delegation.reason,
        "actor_principal": delegation.actor_principal,
        "program_ids": list(delegation.program_ids),
        "workstream_ids": list(delegation.workstream_ids),
        "status": delegation.status.value,
    }


def load_delegations(path: Path) -> tuple[Delegation, ...]:
    if not path.exists():
        return ()
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    schema_version = str(raw.get("schema_version") or DELEGATIONS_SCHEMA_VERSION)
    if schema_version.split(".", 1)[0] != DELEGATIONS_SCHEMA_VERSION.split(".", 1)[0]:
        raise ConfigError(
            f"{path}: expected delegations.yaml schema_version major "
            f"{DELEGATIONS_SCHEMA_VERSION.split('.', 1)[0]}, got {schema_version!r}."
        )
    raw_delegations = raw.get("delegations") or []
    if not isinstance(raw_delegations, list):
        raise ConfigError(f"{path}: 'delegations' must be a list.")
    return tuple(_delegation_from_payload(entry) for entry in raw_delegations)


def write_delegations(path: Path, delegations: tuple[Delegation, ...]) -> None:
    payload = {
        "schema_version": DELEGATIONS_SCHEMA_VERSION,
        "delegations": [delegation_to_payload(delegation) for delegation in sorted(delegations, key=lambda d: d.delegation_id)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
