"""specs/people.md Phase 2a, PPL-W2A.2: `people_directory.yaml`/
`teams.yaml` schema 2.0 with legacy dual-read.

§7.2's exact binding `PersonDirectory`/`PersonProfile`/`Team`/
`ContactPoint`/`FieldVerification` dataclasses. Deliberately a NEW
module -- three DIFFERENT, unrelated, real, in-production types already
carry these exact names elsewhere and must not be touched or collided
with: `src/core/models_v2.py`'s flat schema-1.0 `PersonDirectory`/
`PersonProfile`/`Team` (used today by `knowledge_store.py` and ~30
command modules), `src/core/models.py`'s narrower `PersonProfile`
(`ProgramContext.people`), and `src/core/kb_changelog.py`'s
`PersonDirectorySnapshot` (git-history diffing only). This module's
loader does not replace `knowledge_store.load_knowledge` -- that cutover
is later Phase 2a/2b scope; this ships the typed schema plus a
dual-read-capable parser first.

§6.6/§7.2's dual-read contract (exact spec text): "`PersonDirectory.alias`
remains a lookup/display field. `team_ids`, `org_chain`, and
`manager_alias` are accepted as legacy inputs but are migrated to typed
memberships/manager entity references... `Team.programs` is parsed into
a temporary `legacy_programs` carrier and emitted as low-precedence
`legacy_team_program` affiliation edges until typed program references
supersede it; it is never silently dropped... `legacy_programs`, legacy
person `team_ids`/`org_chain`/`manager_alias`... WARN throughout schema
2.x." Every legacy field actually present in a record produces a WARN
`RegistryDiagnostic` (§7.2, first populated here) -- load still succeeds.

Two legacy fields cannot be reconstructed to their typed replacement at
this schema level alone: `team_ids`/`org_chain` become typed
`TeamMembership` records only once PPL-W2A.3 exists, and `manager_alias`
resolves to `manager_entity_id` only once `EntityRegistry` dual-read
exists (PPL-W2A.4). Both are honestly recorded as WARN diagnostics
("present, not yet resolved") rather than silently dropped or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError
from src.core.people_registry_diagnostics import DiagnosticSeverity, RegistryDiagnostic
from src.core.profile_encryption import load_people_profiles_document
from src.core.yaml_utils import fast_safe_load

PEOPLE_DIRECTORY_SCHEMA_VERSION = "2.0"
TEAMS_SCHEMA_VERSION = "2.0"

_LEGACY_MIGRATION_PRINCIPAL = "<unverified -- legacy migration>"


class PersonStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEPARTED = "departed"
    UNKNOWN = "unknown"


class TenantRelationship(str, Enum):
    INTERNAL = "internal"
    CONTRACTOR = "contractor"
    GUEST = "guest"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class TeamKind(str, Enum):
    ORG_TEAM = "org_team"
    PROGRAM_GROUP = "program_group"
    VIRTUAL_GROUP = "virtual_group"


class TeamStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    RETIRED = "retired"
    UNKNOWN = "unknown"


class ContactKind(str, Enum):
    PRIMARY_EMAIL = "primary_email"
    UPN = "upn"
    ALTERNATE_EMAIL = "alternate_email"


class ContactStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"
    UNDELIVERABLE = "undeliverable"


@dataclass(frozen=True, slots=True)
class ContactPoint:
    kind: ContactKind
    value: str
    status: ContactStatus
    valid_from: datetime | None
    valid_until: datetime | None
    source: str
    source_ref: str | None
    recorded_at: datetime
    verified_at: datetime
    verified_by_principal: str
    delivery_eligible: bool


@dataclass(frozen=True, slots=True)
class FieldVerification:
    field_name: str
    source: str
    source_ref: str | None
    observed_at: datetime
    verified_at: datetime
    recorded_at: datetime
    verified_by_principal: str
    refresh_run_id: str | None = None
    pinned: bool = False
    pin_reason: str | None = None
    pin_review_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PersonDirectory:
    entity_id: str
    alias: str  # Compatibility/human hint; EntityRegistry owns alias bindings.
    contacts: tuple[ContactPoint, ...] = ()
    display_name: str | None = None
    title: str | None = None
    manager_entity_id: str | None = None
    department: str | None = None
    status: PersonStatus = PersonStatus.UNKNOWN
    tenant_relationship: TenantRelationship = TenantRelationship.UNKNOWN
    departed_at: datetime | None = None
    exempt_from_vitality: bool = False
    verifications: tuple[FieldVerification, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonProfile:
    entity_id: str
    alias: str  # Human hint only.
    comm_style: str | None = None
    cares_about: tuple[str, ...] = ()
    pet_peeves: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Team:
    entity_id: str
    id: str  # Legacy/config lookup key during migration; entity_id is canonical.
    name: str
    kind: TeamKind
    parent_team_id: str | None = None
    status: TeamStatus = TeamStatus.UNKNOWN
    area_paths: tuple[str, ...] = ()
    legacy_programs: tuple[str, ...] = ()  # Migration-only carrier; not v2 authority.
    verifications: tuple[FieldVerification, ...] = ()


def _parse_datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _wire_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _wire_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _wire_datetime(value)


def _contact_from_payload(raw: dict) -> ContactPoint:
    return ContactPoint(
        kind=ContactKind(raw["kind"]),
        value=str(raw["value"]),
        status=ContactStatus(raw["status"]),
        valid_from=_parse_optional_datetime(raw.get("valid_from")),
        valid_until=_parse_optional_datetime(raw.get("valid_until")),
        source=str(raw["source"]),
        source_ref=raw.get("source_ref"),
        recorded_at=_parse_datetime(raw["recorded_at"]),
        verified_at=_parse_datetime(raw["verified_at"]),
        verified_by_principal=str(raw["verified_by_principal"]),
        delivery_eligible=bool(raw["delivery_eligible"]),
    )


def _verification_from_payload(raw: dict) -> FieldVerification:
    return FieldVerification(
        field_name=str(raw["field_name"]),
        source=str(raw["source"]),
        source_ref=raw.get("source_ref"),
        observed_at=_parse_datetime(raw["observed_at"]),
        verified_at=_parse_datetime(raw["verified_at"]),
        recorded_at=_parse_datetime(raw["recorded_at"]),
        verified_by_principal=str(raw["verified_by_principal"]),
        refresh_run_id=raw.get("refresh_run_id"),
        pinned=bool(raw.get("pinned", False)),
        pin_reason=raw.get("pin_reason"),
        pin_review_at=_parse_optional_datetime(raw.get("pin_review_at")),
    )


def _person_from_payload(raw: dict, *, path: str, as_of: datetime) -> tuple[PersonDirectory, tuple[RegistryDiagnostic, ...]]:
    diagnostics: list[RegistryDiagnostic] = []
    entity_id = str(raw.get("entity_id") or "").strip()
    if not entity_id:
        diagnostics.append(
            RegistryDiagnostic(
                code="missing_entity_id",
                severity=DiagnosticSeverity.WARN,
                entity_id=None,
                detail=f"person record with alias {raw.get('alias')!r} has no entity_id -- a migration gap, not a new identity.",
                source_path=path,
            )
        )

    contacts: tuple[ContactPoint, ...]
    raw_contacts = raw.get("contacts")
    if raw_contacts:
        contacts = tuple(_contact_from_payload(c) for c in raw_contacts)
    elif raw.get("email"):
        diagnostics.append(
            RegistryDiagnostic(
                code="legacy_email_field",
                severity=DiagnosticSeverity.WARN,
                entity_id=entity_id or None,
                detail="legacy flat 'email' field present; synthesized an unverified ContactPoint.",
                source_path=path,
            )
        )
        contacts = (
            ContactPoint(
                kind=ContactKind.PRIMARY_EMAIL,
                value=str(raw["email"]),
                status=ContactStatus.ACTIVE,
                valid_from=None,
                valid_until=None,
                source="legacy_migration",
                source_ref=None,
                recorded_at=as_of,
                verified_at=as_of,
                verified_by_principal=_LEGACY_MIGRATION_PRINCIPAL,
                delivery_eligible=True,
            ),
        )
    else:
        contacts = ()

    manager_entity_id = raw.get("manager_entity_id")
    if manager_entity_id is None and raw.get("manager_alias"):
        diagnostics.append(
            RegistryDiagnostic(
                code="legacy_manager_alias",
                severity=DiagnosticSeverity.WARN,
                entity_id=entity_id or None,
                detail=f"legacy 'manager_alias' ({raw['manager_alias']!r}) present but not resolved to manager_entity_id "
                "(requires EntityRegistry dual-read, PPL-W2A.4).",
                source_path=path,
            )
        )

    if raw.get("team_ids"):
        diagnostics.append(
            RegistryDiagnostic(
                code="legacy_team_ids",
                severity=DiagnosticSeverity.WARN,
                entity_id=entity_id or None,
                detail="legacy 'team_ids' field present; migrated to typed TeamMembership records (PPL-W2A.3), not carried on PersonDirectory.",
                source_path=path,
            )
        )
    if raw.get("org_chain"):
        diagnostics.append(
            RegistryDiagnostic(
                code="legacy_org_chain",
                severity=DiagnosticSeverity.WARN,
                entity_id=entity_id or None,
                detail="legacy 'org_chain' field present; org_chain is derived by traversing manager relationships in v2, not independently authored.",
                source_path=path,
            )
        )

    person = PersonDirectory(
        entity_id=entity_id,
        alias=str(raw.get("alias") or ""),
        contacts=contacts,
        display_name=raw.get("display_name"),
        title=raw.get("title"),
        manager_entity_id=manager_entity_id,
        department=raw.get("department"),
        status=PersonStatus(raw.get("status", "unknown")),
        tenant_relationship=TenantRelationship(raw.get("tenant_relationship", "unknown")),
        departed_at=_parse_optional_datetime(raw.get("departed_at")),
        exempt_from_vitality=bool(raw.get("exempt_from_vitality", False)),
        verifications=tuple(_verification_from_payload(v) for v in (raw.get("verifications") or [])),
    )
    return person, tuple(diagnostics)


def _team_from_payload(raw: dict, *, path: str) -> tuple[Team, tuple[RegistryDiagnostic, ...]]:
    diagnostics: list[RegistryDiagnostic] = []
    entity_id = str(raw.get("entity_id") or "").strip()
    if not entity_id:
        diagnostics.append(
            RegistryDiagnostic(
                code="missing_entity_id",
                severity=DiagnosticSeverity.WARN,
                entity_id=None,
                detail=f"team record with id {raw.get('id')!r} has no entity_id -- a migration gap, not a new identity.",
                source_path=path,
            )
        )

    raw_kind = raw.get("kind")
    if raw_kind is None:
        diagnostics.append(
            RegistryDiagnostic(
                code="missing_team_kind",
                severity=DiagnosticSeverity.WARN,
                entity_id=entity_id or None,
                detail="legacy team record has no typed 'kind'; defaulted to program_group -- review and set explicitly.",
                source_path=path,
            )
        )
        kind = TeamKind.PROGRAM_GROUP
    else:
        kind = TeamKind(raw_kind)

    legacy_programs = raw.get("legacy_programs")
    if legacy_programs is None:
        legacy_programs = raw.get("programs")
        if legacy_programs:
            diagnostics.append(
                RegistryDiagnostic(
                    code="legacy_team_programs_key",
                    severity=DiagnosticSeverity.WARN,
                    entity_id=entity_id or None,
                    detail="legacy 'programs' key present; carried into legacy_programs (§7.6 low-precedence affiliation edges), never silently dropped.",
                    source_path=path,
                )
            )
    legacy_programs = tuple(legacy_programs or ())

    team = Team(
        entity_id=entity_id,
        id=str(raw.get("id") or entity_id),
        name=str(raw.get("name") or raw.get("id") or entity_id),
        kind=kind,
        parent_team_id=raw.get("parent_team_id"),
        status=TeamStatus(raw.get("status", "unknown")),
        area_paths=tuple(raw.get("area_paths") or ()),
        legacy_programs=legacy_programs,
        verifications=tuple(_verification_from_payload(v) for v in (raw.get("verifications") or [])),
    )
    return team, tuple(diagnostics)


@dataclass(frozen=True, slots=True)
class PeopleDirectoryLoadResult:
    people: tuple[PersonDirectory, ...]
    diagnostics: tuple[RegistryDiagnostic, ...]


def load_people_directory(path: Path, *, as_of: datetime | None = None) -> PeopleDirectoryLoadResult | None:
    """Dual-read: accepts both a fully-typed schema-2.0 record and a
    schema-1.0-shaped legacy record (flat `email`, `manager_alias`,
    `team_ids`, `org_chain`) in the SAME document -- migration is
    per-record, not all-or-nothing per file. Every legacy field actually
    used produces a WARN diagnostic; the load still succeeds."""
    if not path.exists():
        return None
    now = as_of or datetime.now(timezone.utc)
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    raw_people = raw.get("people") or []
    if not isinstance(raw_people, list):
        raise ConfigError(f"{path}: 'people' must be a list")

    people: list[PersonDirectory] = []
    diagnostics: list[RegistryDiagnostic] = []
    for raw_person in raw_people:
        person, person_diagnostics = _person_from_payload(raw_person, path=str(path), as_of=now)
        people.append(person)
        diagnostics.extend(person_diagnostics)
    return PeopleDirectoryLoadResult(people=tuple(people), diagnostics=tuple(diagnostics))


@dataclass(frozen=True, slots=True)
class TeamsLoadResult:
    teams: tuple[Team, ...]
    diagnostics: tuple[RegistryDiagnostic, ...]


def load_teams(path: Path) -> TeamsLoadResult | None:
    if not path.exists():
        return None
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    raw_teams = raw.get("teams") or []
    if not isinstance(raw_teams, list):
        raise ConfigError(f"{path}: 'teams' must be a list")

    teams: list[Team] = []
    diagnostics: list[RegistryDiagnostic] = []
    for raw_team in raw_teams:
        team, team_diagnostics = _team_from_payload(raw_team, path=str(path))
        teams.append(team)
        diagnostics.extend(team_diagnostics)
    return TeamsLoadResult(teams=tuple(teams), diagnostics=tuple(diagnostics))


# ---------------------------------------------------------------------------
# PPL-W2B.1: fully-typed write path (bootstrap/migrate-shared writer). Only
# ever writes fully-typed schema-2.0 records -- a legacy-shaped record must
# already have gone through this module's own dual-read loaders (which
# convert it to a typed dataclass, synthesizing the honest legacy-migration
# provenance markers) before reaching here. Mirrors
# `people_entity_schema.py::write_entities_document`'s exact write-temp-
# then-`os.replace` atomic idiom.
# ---------------------------------------------------------------------------


def _contact_to_payload(contact: ContactPoint) -> dict:
    return {
        "kind": contact.kind.value,
        "value": contact.value,
        "status": contact.status.value,
        "valid_from": _wire_optional_datetime(contact.valid_from),
        "valid_until": _wire_optional_datetime(contact.valid_until),
        "source": contact.source,
        "source_ref": contact.source_ref,
        "recorded_at": _wire_datetime(contact.recorded_at),
        "verified_at": _wire_datetime(contact.verified_at),
        "verified_by_principal": contact.verified_by_principal,
        "delivery_eligible": contact.delivery_eligible,
    }


def _verification_to_payload(verification: FieldVerification) -> dict:
    return {
        "field_name": verification.field_name,
        "source": verification.source,
        "source_ref": verification.source_ref,
        "observed_at": _wire_datetime(verification.observed_at),
        "verified_at": _wire_datetime(verification.verified_at),
        "recorded_at": _wire_datetime(verification.recorded_at),
        "verified_by_principal": verification.verified_by_principal,
        "refresh_run_id": verification.refresh_run_id,
        "pinned": verification.pinned,
        "pin_reason": verification.pin_reason,
        "pin_review_at": _wire_optional_datetime(verification.pin_review_at),
    }


def person_to_payload(person: PersonDirectory) -> dict:
    return {
        "entity_id": person.entity_id,
        "alias": person.alias,
        "contacts": [_contact_to_payload(c) for c in person.contacts],
        "display_name": person.display_name,
        "title": person.title,
        "manager_entity_id": person.manager_entity_id,
        "department": person.department,
        "status": person.status.value,
        "tenant_relationship": person.tenant_relationship.value,
        "departed_at": _wire_optional_datetime(person.departed_at),
        "exempt_from_vitality": person.exempt_from_vitality,
        "verifications": [_verification_to_payload(v) for v in person.verifications],
    }


def team_to_payload(team: Team) -> dict:
    return {
        "entity_id": team.entity_id,
        "id": team.id,
        "name": team.name,
        "kind": team.kind.value,
        "parent_team_id": team.parent_team_id,
        "status": team.status.value,
        "area_paths": list(team.area_paths),
        "legacy_programs": list(team.legacy_programs),
        "verifications": [_verification_to_payload(v) for v in team.verifications],
    }


def _atomic_write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def write_people_directory(path: Path, people: tuple[PersonDirectory, ...]) -> None:
    payload = {
        "schema_version": PEOPLE_DIRECTORY_SCHEMA_VERSION,
        "people": [person_to_payload(p) for p in sorted(people, key=lambda p: p.entity_id or p.alias)],
    }
    _atomic_write_yaml(path, payload)


def write_teams(path: Path, teams: tuple[Team, ...]) -> None:
    payload = {
        "schema_version": TEAMS_SCHEMA_VERSION,
        "teams": [team_to_payload(t) for t in sorted(teams, key=lambda t: t.entity_id or t.id)],
    }
    _atomic_write_yaml(path, payload)


# ---------------------------------------------------------------------------
# PPL-W2A.5: people_profiles.yaml schema 2.0, encrypted-envelope-aware.
# ---------------------------------------------------------------------------


def _profile_from_payload(raw: dict, *, path: str) -> tuple[PersonProfile, tuple[RegistryDiagnostic, ...]]:
    diagnostics: list[RegistryDiagnostic] = []
    entity_id = str(raw.get("entity_id") or "").strip()
    if not entity_id:
        diagnostics.append(
            RegistryDiagnostic(
                code="missing_entity_id",
                severity=DiagnosticSeverity.WARN,
                entity_id=None,
                detail=f"profile record with alias {raw.get('alias')!r} has no entity_id -- a migration gap, not a new identity.",
                source_path=path,
            )
        )
    profile = PersonProfile(
        entity_id=entity_id,
        alias=str(raw.get("alias") or ""),
        comm_style=raw.get("comm_style"),
        cares_about=tuple(raw.get("cares_about") or ()),
        pet_peeves=tuple(raw.get("pet_peeves") or ()),
    )
    return profile, tuple(diagnostics)


@dataclass(frozen=True, slots=True)
class PeopleProfilesLoadResult:
    profiles: tuple[PersonProfile, ...]
    diagnostics: tuple[RegistryDiagnostic, ...]


def load_people_profiles(path: Path) -> PeopleProfilesLoadResult | None:
    """§7.9: "Add canonical `entity_id`, retain alias as a human hint,
    migrate encrypted documents, and preserve profile resolution across
    alias rename." Reuses `profile_encryption.load_people_profiles_document`
    (transparently decrypts an encrypted envelope, or returns the
    plaintext document unchanged) rather than reading the YAML directly --
    that module is schema-agnostic about the payload shape, so adding
    `entity_id` inside individual profile records required zero changes
    to it; only THIS loader needed to learn the new field."""
    document = load_people_profiles_document(path)
    if not document:
        return None
    raw_profiles = document.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise ConfigError(f"{path}: 'profiles' must be a list")

    profiles: list[PersonProfile] = []
    diagnostics: list[RegistryDiagnostic] = []
    for raw_profile in raw_profiles:
        profile, profile_diagnostics = _profile_from_payload(raw_profile, path=str(path))
        profiles.append(profile)
        diagnostics.extend(profile_diagnostics)
    return PeopleProfilesLoadResult(profiles=tuple(profiles), diagnostics=tuple(diagnostics))
