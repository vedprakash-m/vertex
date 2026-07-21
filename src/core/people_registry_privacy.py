"""Registry privacy summaries plus PPL-W2B.5's safe DSAR export surface.

§7.8: "`src/core/support_bundle.py` excludes or redacts registry PII by
default." This module gives that requirement an executable, testable
shape: `build_registry_privacy_summary` returns counts/metadata ONLY by
default -- no raw journal record content (no `entity_id`, `field`,
`before`/`after`, or `authenticated_principal` values) -- and only
includes the raw records when the caller explicitly passes
`include_pii=True`, matching §7.8's `--reveal-pii` "intentional common
sensitive-read switch" pattern: a deliberate, explicit opt-in, never a
default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import copy

from src.core.exceptions import ConfigError
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.ulid import new_ulid
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS, append_people_change_record, read_journal_records
from src.core.people_directory_schema import load_people_directory
from src.core.people_entity_schema import CanonicalEntity, load_entities_document
from src.core.people_membership_schema import load_memberships
from src.core.people_registry_governance import require_adopted_registry
from src.core.people_registry_identity import RegistryConfig, RegistryManifest, load_registry_config, load_registry_manifest
from src.core.people_registry_lease import read_force_release_audit_records
from src.core.people_registry_storage_class import load_registry_storage_status
from src.core.people_registry_writer import SharedRegistryPrivacyForgetResult, forget_shared_registry_person
from src.core.profile_encryption import load_people_profiles_document
from src.core.yaml_utils import load_optional_yaml_mapping


@dataclass(frozen=True, slots=True)
class RegistryPrivacySummary:
    bootstrapped: bool
    generation_id: str | None
    people_change_record_count: int
    people_conflict_record_count: int
    force_release_audit_count: int
    storage_class: str | None
    raw_people_change_records: tuple[dict, ...] = ()
    raw_people_conflict_records: tuple[dict, ...] = ()

    def to_payload(self) -> dict:
        payload: dict = {
            "bootstrapped": self.bootstrapped,
            "generation_id": self.generation_id,
            "people_change_record_count": self.people_change_record_count,
            "people_conflict_record_count": self.people_conflict_record_count,
            "force_release_audit_count": self.force_release_audit_count,
            "storage_class": self.storage_class,
        }
        if self.raw_people_change_records or self.raw_people_conflict_records:
            payload["raw_people_change_records"] = list(self.raw_people_change_records)
            payload["raw_people_conflict_records"] = list(self.raw_people_conflict_records)
        return payload


def build_registry_privacy_summary(knowledge_root: Path, *, include_pii: bool = False) -> RegistryPrivacySummary:
    manifest = load_registry_manifest(knowledge_root)
    change_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    conflict_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS)
    audit_records = read_force_release_audit_records(knowledge_root)
    storage_status = load_registry_storage_status(knowledge_root)

    return RegistryPrivacySummary(
        bootstrapped=manifest is not None,
        generation_id=manifest.generation_id if manifest is not None else None,
        people_change_record_count=len(change_records),
        people_conflict_record_count=len(conflict_records),
        force_release_audit_count=len(audit_records),
        storage_class=storage_status.storage_class if storage_status is not None else None,
        raw_people_change_records=change_records if include_pii else (),
        raw_people_conflict_records=conflict_records if include_pii else (),
    )


@dataclass(frozen=True, slots=True)
class PeopleDsarExport:
    entity_id: str
    generation_id: str
    person: dict[str, object] | None
    profiles: tuple[dict[str, object], ...]
    memberships: tuple[dict[str, object], ...]
    delegations: tuple[dict[str, object], ...]
    historical_artifacts: dict[str, int]
    audit_event_id: str
    external_backup_action_required: bool = True

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": "people-dsar.v1",
            "entity_id": self.entity_id,
            "generation_id": self.generation_id,
            "person": self.person,
            "profiles": list(self.profiles),
            "memberships": list(self.memberships),
            "delegations": list(self.delegations),
            "historical_artifacts": self.historical_artifacts,
            "external_backup_action_required": self.external_backup_action_required,
            "external_backup_action": (
                "Customer-managed registry backups outside the shared knowledge root "
                "must be erased or cryptographically shredded by their owner."
            ),
            "audit_event_id": self.audit_event_id,
        }


def _require_privacy_authorized(
    knowledge_root: Path,
    *,
    actor: str,
) -> tuple[RegistryConfig, RegistryManifest]:
    if not actor.strip() or actor == "<preview>":
        raise ConfigError("People privacy export requires an authenticated privacy-authorized principal.")
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError("The registry has not been bootstrapped yet; no managed registry generation exists.")
    if actor not in config.pii_reveal_principals:
        raise ConfigError(
            f"Authenticated principal {actor!r} is not authorized for privacy export/forget. "
            "Add it to registry.yaml pii_reveal_principals through the governed configuration flow."
        )
    require_adopted_registry(knowledge_root, consumer="People privacy export")
    return config, manifest


def _resolve_person_entity(knowledge_root: Path, person_ref: str) -> CanonicalEntity:
    reference = person_ref.strip()
    if not reference:
        raise ConfigError("--person must be a non-empty canonical person ID or uniquely resolving alias.")
    document = load_entities_document(knowledge_root / "entities.yaml")
    if document is None:
        raise ConfigError("People privacy export requires committed shared entities.yaml.")
    direct_matches = [entity for entity in document.entities if entity.entity_id == reference]
    alias_matches = [
        entity
        for entity in document.entities
        if any(alias.value.casefold() == reference.casefold() for alias in entity.aliases)
    ]
    matches = direct_matches or alias_matches
    people = [entity for entity in matches if entity.entity_type == "person"]
    if len(people) != 1:
        raise ConfigError(f"Person reference {person_ref!r} must resolve to exactly one canonical person entity.")
    return people[0]


def _profile_matches_person(profile: object, *, entity_id: str, aliases: frozenset[str]) -> bool:
    if not isinstance(profile, dict):
        return False
    if profile.get("entity_id") == entity_id:
        return True
    alias = profile.get("alias")
    return isinstance(alias, str) and alias.casefold() in aliases


_DELEGATION_PERSON_REFERENCE_FIELDS = frozenset(
    {
        "from_person_entity_id",
        "to_person_entity_id",
        "person_entity_id",
        "delegate_entity_id",
        "owner_entity_id",
    }
)


def _delegation_references_person(record: object, *, entity_id: str) -> bool:
    return isinstance(record, dict) and any(
        record.get(field_name) == entity_id
        for field_name in _DELEGATION_PERSON_REFERENCE_FIELDS
    )


def _safe_person_payload(person: object) -> dict[str, object] | None:
    if person is None:
        return None
    contacts = [
        {
            "kind": contact.kind.value,
            "value": contact.value,
            "status": contact.status.value,
            "delivery_eligible": contact.delivery_eligible,
        }
        for contact in person.contacts
    ]
    return {
        "entity_id": person.entity_id,
        "alias": person.alias,
        "display_name": person.display_name,
        "title": person.title,
        "department": person.department,
        "status": person.status.value,
        "manager_entity_id": person.manager_entity_id,
        "contacts": contacts,
    }


def _safe_membership_payload(membership: object) -> dict[str, object]:
    return {
        "membership_id": membership.membership_id,
        "team_entity_id": membership.team_entity_id,
        "role": membership.role,
        "valid_from": membership.valid_from.isoformat() if membership.valid_from else None,
        "valid_until": membership.valid_until.isoformat() if membership.valid_until else None,
        "status": membership.status.value,
        "source": membership.source,
    }


def _safe_delegation_payload(delegation: dict[str, Any]) -> dict[str, object]:
    return {
        key: value
        for key, value in delegation.items()
        if key
        in {
            "delegation_id",
            "from_person_entity_id",
            "to_person_entity_id",
            "person_entity_id",
            "delegate_entity_id",
            "owner_entity_id",
            "surfaces",
            "valid_from",
            "valid_until",
            "program_ids",
            "workstream_ids",
            "status",
        }
    }


def export_shared_registry_person(
    *,
    programs_root: Path,
    person_ref: str,
    reason: str,
    actor: str,
    on_behalf_of: str | None = None,
    as_of: datetime | None = None,
) -> PeopleDsarExport:
    """Produce an authorized, target-scoped DSAR export and append a minimal
    audit event.  Raw journal/conflict history is deliberately summarized,
    never emitted, because it can contain unrelated operators or people."""
    if not reason.strip():
        raise ConfigError("A non-empty DSAR export reason is required.")
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config, manifest = _require_privacy_authorized(knowledge_root, actor=actor)
    entity = _resolve_person_entity(knowledge_root, person_ref)
    directory = load_people_directory(knowledge_root / "people_directory.yaml")
    person_matches = [] if directory is None else [
        person for person in directory.people if person.entity_id == entity.entity_id
    ]
    if len(person_matches) > 1:
        raise ConfigError(f"Canonical person {entity.entity_id!r} has duplicate people-directory records.")
    person = person_matches[0] if person_matches else None
    aliases = frozenset(alias.value.casefold() for alias in entity.aliases)
    if person is not None:
        aliases = aliases | frozenset({person.alias.casefold()})

    profiles_document = load_people_profiles_document(knowledge_root / "people_profiles.yaml")
    raw_profiles = profiles_document.get("profiles") or []
    if not isinstance(raw_profiles, list):
        raise ConfigError("people_profiles.yaml 'profiles' must be a list.")
    profiles = tuple(
        copy.deepcopy(profile)
        for profile in raw_profiles
        if _profile_matches_person(profile, entity_id=entity.entity_id, aliases=aliases)
    )
    memberships = tuple(
        _safe_membership_payload(membership)
        for membership in load_memberships(knowledge_root / "memberships.yaml")
        if membership.person_entity_id == entity.entity_id
    )
    delegations_document = load_optional_yaml_mapping(knowledge_root / "delegations.yaml") or {}
    raw_delegations = delegations_document.get("delegations") or []
    if not isinstance(raw_delegations, list):
        raise ConfigError("delegations.yaml 'delegations' must be a list.")
    delegations = tuple(
        _safe_delegation_payload(delegation)
        for delegation in raw_delegations
        if _delegation_references_person(delegation, entity_id=entity.entity_id)
    )
    change_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    conflict_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS)
    historical_artifacts = {
        "people_change_records": sum(record.get("entity_id") == entity.entity_id for record in change_records),
        "people_conflict_records": sum(record.get("entity_id") == entity.entity_id for record in conflict_records),
        "journal_values_included": 0,
        "customer_managed_backup_action_required": 1,
    }
    audit = append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=f"privacy-export-{new_ulid(now)}",
        generation_id=manifest.generation_id,
        authenticated_principal=actor,
        on_behalf_of=on_behalf_of,
        operation="privacy_export",
        entity_id=entity.entity_id,
        field="dsar_export",
        before=None,
        after={
            "profile_count": len(profiles),
            "membership_count": len(memberships),
            "delegation_count": len(delegations),
            "historical_artifact_counts": {
                key: value
                for key, value in historical_artifacts.items()
                if key != "journal_values_included"
            },
        },
        source="privacy",
        reason=reason,
        as_of=now,
    )
    return PeopleDsarExport(
        entity_id=entity.entity_id,
        generation_id=manifest.generation_id,
        person=_safe_person_payload(person),
        profiles=profiles,
        memberships=memberships,
        delegations=delegations,
        historical_artifacts=historical_artifacts,
        audit_event_id=audit["event_id"],
    )


__all__ = [
    "PeopleDsarExport",
    "RegistryPrivacySummary",
    "SharedRegistryPrivacyForgetResult",
    "build_registry_privacy_summary",
    "export_shared_registry_person",
    "forget_shared_registry_person",
]
