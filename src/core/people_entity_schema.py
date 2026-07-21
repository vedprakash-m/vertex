"""specs/people.md Phase 2a, PPL-W2A.1: `entities.yaml` schema 2.0 +
DIR-11 org-scope enforcement.

§7.2's binding `CanonicalEntity`/`EntityAlias`/`EntityIdentifier`/
`EntityRedirect` dataclasses (field names/types/defaults are binding, not
illustrative -- §7.2's own preamble). Deliberately a NEW module, not an
edit to `src/core/entity_registry.py`'s existing (older, flatter, 5-field)
`CanonicalEntity` in `src/core/program_reality.py` -- that type is real,
in-production, and used by `entity_alias_emitter.py`/`ProgramReality`
today; this work item "extends `entities.yaml`" with a new typed schema,
it does not redefine or destabilize the existing one. `entity_registry.py`
itself was NOT modified here -- its runtime dual-read/cutover to this
schema was later Phase 3 scope (PPL-W3.3b, complete: `EntityRegistry`'s
org-scope loading now additionally sources person/team entities from
this schema's shared `entities.yaml`, adapted back to the old
`CanonicalEntity` shape so every existing consumer/resolution-ladder
behavior stays byte-identical; program-scope loading and DIR-11
enforcement AT LOAD TIME remain deliberately untouched -- see
`entity_registry.py`'s own module docstring for the full rationale).

§6.6: "Existing versionless program-local `entities.yaml` is treated as
legacy schema 0 for migration preview only; it is never silently
promoted to shared schema 2.0." `preview_entities_migration` is that
preview: it reads a legacy flat-alias file and reports what schema-2.0
records WOULD be created, synthesizing typed `EntityAlias` records with
an honest, unverified provenance marker -- it never writes anything.

§8.3 DIR-11: "Person/org-team entity is program-scoped or overrides an
org binding" -- one of the 21 binding decisions already ratified in §5.6
("org-scoped people/teams only (DIR-11 rejects program-scope overrides)").
`check_dir11_compliance` implements this: any `person`/`team`-typed
`CanonicalEntity` found in a PROGRAM-scope document is a violation
outright (people/teams are always org-scoped in this platform), and
separately, any program-scope entity whose `entity_id` or any alias
value collides (casefold-normalized) with a *different* org-scope entity
is a violation. `src/core/entity_registry.py`'s current loader has no
such check today -- it silently lets a program-scope entity override an
org-scope one of the same alias (`EntityRegistry.load()`'s own comment:
"org entities loaded first (lower priority), then program overrides").
This module's check is the missing rejection; wiring it into
`EntityRegistry.load()` itself as an enforced load-time rejection
(rather than this module's own separately-callable check) remains
deliberately deferred -- PPL-W3.3b's org-scope data-source cutover did
not add new rejection/validation behavior to the loader, only a new
additive data source, to keep that change's own blast radius minimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError
from src.core.yaml_utils import fast_safe_load

ENTITIES_SCHEMA_VERSION = "2.0"

#: §5.6 binding decision: "org-scoped people/teams only (DIR-11 rejects
#: program-scope overrides)." CanonicalEntity.entity_type values that may
#: only ever appear in an org-scope entities.yaml document.
ORG_SCOPE_ONLY_ENTITY_TYPES = frozenset({"person", "team"})


class EntityStatus(str, Enum):
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"


class AliasStatus(str, Enum):
    ACTIVE = "active"
    HISTORICAL = "historical"
    RETIRED = "retired"


class IdentifierStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class EntityAlias:
    value: str
    kind: str
    status: AliasStatus
    valid_from: datetime | None
    valid_until: datetime | None
    source: str
    source_ref: str | None
    recorded_at: datetime
    verified_at: datetime
    verified_by_principal: str


@dataclass(frozen=True, slots=True)
class EntityIdentifier:
    provider: str
    kind: str
    subject_id: str
    tenant_id: str | None = None
    handle: str | None = None
    binding_method: str = "exact"
    binding_confidence: float = 1.0
    source_ref: str | None = None
    recorded_at: datetime | None = None
    verified_by_principal: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: IdentifierStatus = IdentifierStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class CanonicalEntity:
    workspace_id: str
    entity_id: str
    entity_type: str
    canonical_name: str
    aliases: tuple[EntityAlias, ...]
    scope: str
    created_at: datetime
    status: EntityStatus = EntityStatus.ACTIVE
    tombstoned_at: datetime | None = None
    identifiers: tuple[EntityIdentifier, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityRedirect:
    from_entity_id: str
    to_entity_id: str
    recorded_at: datetime
    principal_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class EntitiesDocument:
    schema_version: str
    entities: tuple[CanonicalEntity, ...]
    redirects: tuple[EntityRedirect, ...] = ()


# ---------------------------------------------------------------------------
# Load / write (schema 2.0 only -- dual-generation runtime reading is a
# later cutover work item's scope, not this one).
# ---------------------------------------------------------------------------


def _parse_datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _wire_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _wire_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _wire_datetime(value)


def _alias_from_payload(raw: dict) -> EntityAlias:
    return EntityAlias(
        value=str(raw["value"]),
        kind=str(raw["kind"]),
        status=AliasStatus(raw["status"]),
        valid_from=_parse_optional_datetime(raw.get("valid_from")),
        valid_until=_parse_optional_datetime(raw.get("valid_until")),
        source=str(raw["source"]),
        source_ref=raw.get("source_ref"),
        recorded_at=_parse_datetime(raw["recorded_at"]),
        verified_at=_parse_datetime(raw["verified_at"]),
        verified_by_principal=str(raw["verified_by_principal"]),
    )


def _alias_to_payload(alias: EntityAlias) -> dict:
    return {
        "value": alias.value,
        "kind": alias.kind,
        "status": alias.status.value,
        "valid_from": _wire_optional_datetime(alias.valid_from),
        "valid_until": _wire_optional_datetime(alias.valid_until),
        "source": alias.source,
        "source_ref": alias.source_ref,
        "recorded_at": _wire_datetime(alias.recorded_at),
        "verified_at": _wire_datetime(alias.verified_at),
        "verified_by_principal": alias.verified_by_principal,
    }


def _identifier_from_payload(raw: dict) -> EntityIdentifier:
    return EntityIdentifier(
        provider=str(raw["provider"]),
        kind=str(raw["kind"]),
        subject_id=str(raw["subject_id"]),
        tenant_id=raw.get("tenant_id"),
        handle=raw.get("handle"),
        binding_method=str(raw.get("binding_method", "exact")),
        binding_confidence=float(raw.get("binding_confidence", 1.0)),
        source_ref=raw.get("source_ref"),
        recorded_at=_parse_optional_datetime(raw.get("recorded_at")),
        verified_by_principal=raw.get("verified_by_principal"),
        valid_from=_parse_optional_datetime(raw.get("valid_from")),
        valid_until=_parse_optional_datetime(raw.get("valid_until")),
        status=IdentifierStatus(raw.get("status", "active")),
    )


def _identifier_to_payload(identifier: EntityIdentifier) -> dict:
    return {
        "provider": identifier.provider,
        "kind": identifier.kind,
        "subject_id": identifier.subject_id,
        "tenant_id": identifier.tenant_id,
        "handle": identifier.handle,
        "binding_method": identifier.binding_method,
        "binding_confidence": identifier.binding_confidence,
        "source_ref": identifier.source_ref,
        "recorded_at": _wire_optional_datetime(identifier.recorded_at),
        "verified_by_principal": identifier.verified_by_principal,
        "valid_from": _wire_optional_datetime(identifier.valid_from),
        "valid_until": _wire_optional_datetime(identifier.valid_until),
        "status": identifier.status.value,
    }


def _entity_from_payload(raw: dict, *, path: Path) -> CanonicalEntity:
    entity_id = str(raw.get("entity_id") or "").strip()
    if not entity_id:
        raise ConfigError(f"{path}: an entity is missing entity_id")
    return CanonicalEntity(
        workspace_id=str(raw["workspace_id"]),
        entity_id=entity_id,
        entity_type=str(raw["entity_type"]),
        canonical_name=str(raw["canonical_name"]),
        aliases=tuple(_alias_from_payload(a) for a in (raw.get("aliases") or [])),
        scope=str(raw["scope"]),
        created_at=_parse_datetime(raw["created_at"]),
        status=EntityStatus(raw.get("status", "active")),
        tombstoned_at=_parse_optional_datetime(raw.get("tombstoned_at")),
        identifiers=tuple(_identifier_from_payload(i) for i in (raw.get("identifiers") or [])),
    )


def entity_to_payload(entity: CanonicalEntity) -> dict:
    return {
        "workspace_id": entity.workspace_id,
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "canonical_name": entity.canonical_name,
        "scope": entity.scope,
        "status": entity.status.value,
        "created_at": _wire_datetime(entity.created_at),
        "tombstoned_at": _wire_optional_datetime(entity.tombstoned_at),
        "aliases": [_alias_to_payload(a) for a in entity.aliases],
        "identifiers": [_identifier_to_payload(i) for i in entity.identifiers],
    }


def _redirect_from_payload(raw: dict) -> EntityRedirect:
    return EntityRedirect(
        from_entity_id=str(raw["from_entity_id"]),
        to_entity_id=str(raw["to_entity_id"]),
        recorded_at=_parse_datetime(raw["recorded_at"]),
        principal_id=str(raw["principal_id"]),
        reason=str(raw["reason"]),
    )


def _redirect_to_payload(redirect: EntityRedirect) -> dict:
    return {
        "from_entity_id": redirect.from_entity_id,
        "to_entity_id": redirect.to_entity_id,
        "recorded_at": _wire_datetime(redirect.recorded_at),
        "principal_id": redirect.principal_id,
        "reason": redirect.reason,
    }


def is_legacy_schema_0_entities_document(path: Path) -> bool:
    """§6.6: "Existing versionless program-local `entities.yaml` is
    treated as legacy schema 0." True iff the file exists, parses, and
    carries no `schema_version` key at all."""
    if not path.exists():
        return False
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return False
    return "schema_version" not in raw


def load_entities_document(path: Path) -> EntitiesDocument | None:
    if not path.exists():
        return None
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    schema_version = str(raw.get("schema_version") or "")
    if schema_version != ENTITIES_SCHEMA_VERSION:
        raise ConfigError(
            f"{path}: expected entities.yaml schema_version {ENTITIES_SCHEMA_VERSION!r}, got {schema_version!r}. "
            "Use is_legacy_schema_0_entities_document()/preview_entities_migration() for a legacy (schema-0) file."
        )
    entities = tuple(_entity_from_payload(raw_entity, path=path) for raw_entity in (raw.get("entities") or []))
    redirects = tuple(_redirect_from_payload(raw_redirect) for raw_redirect in (raw.get("redirects") or []))
    return EntitiesDocument(schema_version=schema_version, entities=entities, redirects=redirects)


def write_entities_document(path: Path, document: EntitiesDocument) -> None:
    payload = {
        "schema_version": document.schema_version,
        "entities": [entity_to_payload(entity) for entity in document.entities],
        "redirects": [_redirect_to_payload(redirect) for redirect in document.redirects],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


# ---------------------------------------------------------------------------
# Migration preview (legacy schema 0 -> schema 2.0), never writes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntitiesMigrationPreview:
    source_path: Path
    would_create_entities: tuple[CanonicalEntity, ...]
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


def preview_entities_migration(legacy_path: Path, *, workspace_id: str, as_of: datetime | None = None) -> EntitiesMigrationPreview:
    """§6.6: legacy `entities.yaml` "is never silently promoted to shared
    schema 2.0" -- this function only PREVIEWS what the migration would
    produce; it never writes `legacy_path` or any other file. Legacy
    records have flat string aliases with no kind/status/verification
    metadata, so each synthesized `EntityAlias` is honestly marked
    `source="legacy_migration"` with `verified_by_principal="<unverified -- legacy migration>"`
    rather than fabricating a real verification event."""
    if not legacy_path.exists():
        raise ConfigError(f"No entities.yaml found at {legacy_path} to preview a migration for.")
    if not is_legacy_schema_0_entities_document(legacy_path):
        raise ConfigError(f"{legacy_path} already carries a schema_version; it is not a legacy schema-0 document to preview-migrate.")

    now = as_of or datetime.now(timezone.utc)
    raw = fast_safe_load(legacy_path.read_text(encoding="utf-8")) or {}
    raw_entities = raw.get("entities") or []
    if not isinstance(raw_entities, list):
        raise ConfigError(f"{legacy_path}: 'entities' must be a list")

    diagnostics: list[str] = []
    would_create: list[CanonicalEntity] = []
    for raw_entity in raw_entities:
        entity_id = str(raw_entity.get("entity_id") or raw_entity.get("id") or "").strip()
        entity_type = str(raw_entity.get("entity_type") or raw_entity.get("type") or "").strip()
        canonical_name = str(raw_entity.get("canonical_name") or raw_entity.get("name") or "").strip()
        scope = str(raw_entity.get("scope") or "program").strip()
        legacy_aliases = raw_entity.get("aliases") or []
        if not entity_id:
            diagnostics.append("skipped one legacy entity: missing entity_id/id")
            continue
        synthesized_aliases = tuple(
            EntityAlias(
                value=str(alias_value),
                kind="legacy_alias",
                status=AliasStatus.ACTIVE,
                valid_from=None,
                valid_until=None,
                source="legacy_migration",
                source_ref=None,
                recorded_at=now,
                verified_at=now,
                verified_by_principal="<unverified -- legacy migration>",
            )
            for alias_value in legacy_aliases
        )
        if not entity_type:
            diagnostics.append(f"entity {entity_id!r}: no entity_type/type in legacy record; migration preview leaves it blank")
        would_create.append(
            CanonicalEntity(
                workspace_id=workspace_id,
                entity_id=entity_id,
                entity_type=entity_type,
                canonical_name=canonical_name or entity_id,
                aliases=synthesized_aliases,
                scope=scope,
                created_at=now,
                status=EntityStatus.ACTIVE,
            )
        )
    diagnostics.append(
        f"{len(would_create)} entities would be created with {sum(len(e.aliases) for e in would_create)} "
        "synthesized, UNVERIFIED alias record(s) (source=legacy_migration). This is a preview only -- nothing was written."
    )
    return EntitiesMigrationPreview(source_path=legacy_path, would_create_entities=tuple(would_create), diagnostics=tuple(diagnostics))


# ---------------------------------------------------------------------------
# DIR-11: org-scoped people/teams only.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dir11Violation:
    entity_id: str
    reason: str  # "program_scoped_org_only_type" | "overrides_org_binding"
    detail: str


def check_dir11_compliance(
    *,
    org_entities: tuple[CanonicalEntity, ...],
    program_entities: tuple[CanonicalEntity, ...],
) -> tuple[Dir11Violation, ...]:
    """§8.3 DIR-11: "Person/org-team entity is program-scoped or overrides
    an org binding." Two independent conditions, either one is a
    violation for a given program-scope entity:

    1. `entity_type` is one of `ORG_SCOPE_ONLY_ENTITY_TYPES` -- people and
       teams are always org-scoped in this platform (§5.6 binding
       decision); a program-scope entities.yaml must never define one at
       all, collision or not.
    2. Its `entity_id` or any alias value (casefold-normalized) matches a
       DIFFERENT org-scope entity's `entity_id`/alias -- an override of an
       org binding, regardless of entity_type.
    """
    violations: list[Dir11Violation] = []

    org_entity_ids = {entity.entity_id.casefold() for entity in org_entities}
    org_alias_values: dict[str, str] = {}  # casefolded alias -> owning org entity_id
    for org_entity in org_entities:
        for alias in org_entity.aliases:
            org_alias_values[alias.value.casefold()] = org_entity.entity_id

    for program_entity in program_entities:
        if program_entity.entity_type in ORG_SCOPE_ONLY_ENTITY_TYPES:
            violations.append(
                Dir11Violation(
                    entity_id=program_entity.entity_id,
                    reason="program_scoped_org_only_type",
                    detail=f"entity_type {program_entity.entity_type!r} is org-scope-only; found in a program-scope entities.yaml.",
                )
            )
            continue  # Already flagged; avoid a redundant second violation for the same entity below.

        normalized_id = program_entity.entity_id.casefold()
        if normalized_id in org_entity_ids:
            violations.append(
                Dir11Violation(
                    entity_id=program_entity.entity_id,
                    reason="overrides_org_binding",
                    detail=f"entity_id {program_entity.entity_id!r} collides with an org-scope entity of the same ID.",
                )
            )
            continue

        for alias in program_entity.aliases:
            owning_org_entity_id = org_alias_values.get(alias.value.casefold())
            if owning_org_entity_id is not None and owning_org_entity_id != program_entity.entity_id:
                violations.append(
                    Dir11Violation(
                        entity_id=program_entity.entity_id,
                        reason="overrides_org_binding",
                        detail=f"alias {alias.value!r} collides with org-scope entity {owning_org_entity_id!r}.",
                    )
                )
                break

    return tuple(violations)
