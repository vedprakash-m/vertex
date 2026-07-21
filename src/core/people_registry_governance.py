"""PPL-W2B.2: adoption and human stewardship of shared people records.

Manual changes remain visible as manifest-hash drift until an authenticated
operator adopts them.  Adoption is deliberately the only flow that may turn
such bytes into a new committed generation; pin, unpin, and attest mutations
require a clean committed generation and use the same staged writer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum
from src.core.people_change_journal import append_people_change_record
from src.core.people_directory_schema import (
    FieldVerification,
    PersonDirectory,
    load_people_directory,
    load_teams,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import EntitiesDocument, load_entities_document, write_entities_document
from src.core.people_membership_schema import load_memberships, write_memberships
from src.core.people_registry_identity import RegistryConfig, RegistryManifest, load_registry_config, load_registry_manifest
from src.core.people_registry_transaction import (
    commit_registry_files_transaction,
    prepare_registry_files_transaction,
)
from src.core.yaml_utils import load_optional_yaml_mapping

_MANAGED_FILES = frozenset({"entities.yaml", "people_directory.yaml", "teams.yaml", "memberships.yaml"})
_PERSON_FIELDS = frozenset(
    {
        "alias",
        "contacts",
        "display_name",
        "title",
        "manager_entity_id",
        "department",
        "status",
        "tenant_relationship",
        "departed_at",
        "exempt_from_vitality",
    }
)
_INFORMATIONAL_PERSON_FIELDS = frozenset({"display_name", "title", "department", "manager_entity_id", "exempt_from_vitality"})
#: Public alias for cross-module reuse (PPL-W4.3's provider-field validation
#: needs the same registered field vocabulary this module already enforces).
PERSON_FIELDS = _PERSON_FIELDS


@dataclass(frozen=True, slots=True)
class ManagedRegistryEdit:
    relative_path: str
    expected_hash: str
    actual_hash: str | None
    changed_fields: tuple[str, ...]
    critical: bool


@dataclass(frozen=True, slots=True)
class RegistryManifestIntegrity:
    generation_id: str | None
    edits: tuple[ManagedRegistryEdit, ...]

    @property
    def is_clean(self) -> bool:
        return not self.edits

    @property
    def has_critical_edits(self) -> bool:
        return any(edit.critical for edit in self.edits)


@dataclass(frozen=True, slots=True)
class RegistryAdoptionResult:
    integrity: RegistryManifestIntegrity
    transaction_id: str | None = None
    generation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PersonGovernanceResult:
    operation: str
    entity_id: str
    fields: tuple[str, ...]
    transaction_id: str | None = None
    generation_id: str | None = None


class RegistryUnadoptedCriticalEdit(ConfigError):
    """Raised before an authoritative consumer observes critical drift."""


def _generation_snapshot_path(knowledge_root: Path, manifest: RegistryManifest, relative_path: str) -> Path:
    return knowledge_root / ".state" / "registry_snapshots" / manifest.generation_id / relative_path


def _records_by_key(raw: dict, collection_name: str, key_name: str) -> dict[str, object]:
    records = raw.get(collection_name) or []
    if not isinstance(records, list):
        raise ConfigError(f"Managed registry {collection_name!r} must be a list for manifest-drift inspection.")
    result: dict[str, object] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ConfigError(f"Managed registry {collection_name}[{index}] must be a mapping for manifest-drift inspection.")
        key = str(record.get(key_name) or "").strip()
        if not key or key in result:
            raise ConfigError(f"Managed registry {collection_name} has a missing or duplicate {key_name!r}; repair it before adoption.")
        result[key] = record
    return result


def _diff_value(before: object, after: object, *, prefix: str) -> tuple[str, ...]:
    if type(before) is not type(after):
        return (prefix,)
    if isinstance(before, dict):
        fields: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            fields.extend(_diff_value(before.get(key), after.get(key), prefix=child_prefix))
        return tuple(fields)
    if isinstance(before, list):
        if len(before) != len(after):
            return (prefix,)
        fields: list[str] = []
        for index, (before_item, after_item) in enumerate(zip(before, after, strict=True)):
            fields.extend(_diff_value(before_item, after_item, prefix=f"{prefix}[{index}]"))
        return tuple(fields)
    return () if before == after else (prefix,)


def _changed_fields(relative_path: str, *, baseline: dict | None, current: dict) -> tuple[str, ...]:
    if baseline is None:
        return ("<baseline unavailable>",)
    shape = {
        "entities.yaml": ("entities", "entity_id"),
        "people_directory.yaml": ("people", "entity_id"),
        "teams.yaml": ("teams", "entity_id"),
        "memberships.yaml": ("memberships", "membership_id"),
    }.get(relative_path)
    if shape is None:
        return ("<unsupported managed file>",)
    collection_name, key_name = shape
    before_records = _records_by_key(baseline, collection_name, key_name)
    after_records = _records_by_key(current, collection_name, key_name)
    changes: list[str] = []
    for key in sorted(set(before_records) | set(after_records)):
        prefix = f"{collection_name}[{key}]"
        if key not in before_records or key not in after_records:
            changes.append(prefix)
        else:
            changes.extend(_diff_value(before_records[key], after_records[key], prefix=prefix))
    return tuple(sorted(set(changes)))


def _is_critical_field(relative_path: str, field: str) -> bool:
    if field.startswith("<"):
        return True
    if relative_path == "people_directory.yaml":
        field_name = field.split("].", 1)[1].split(".", 1)[0] if "]." in field else ""
        return field_name not in _INFORMATIONAL_PERSON_FIELDS
    if relative_path == "entities.yaml":
        field_name = field.split("].", 1)[1].split(".", 1)[0] if "]." in field else ""
        return field_name not in {"canonical_name", "created_at"}
    if relative_path == "teams.yaml":
        field_name = field.split("].", 1)[1].split(".", 1)[0] if "]." in field else ""
        return field_name not in {"name", "area_paths", "verifications", "created_at"}
    return True  # Membership data is audience-driving by definition.


_FIELD_RECORD_RE = re.compile(r"^(?P<collection>[a-z_]+)\[(?P<record_key>[^\]]+)\](?:\.(?P<tail>.+))?$")
_FIELD_PART_RE = re.compile(r"^(?P<key>[a-z_]+)(?:\[(?P<index>\d+)\])?$")


def _field_value(raw: dict | None, field: str) -> tuple[str, object]:
    """Return the journal entity key and before/after value for one diff path."""
    match = _FIELD_RECORD_RE.match(field)
    if raw is None or match is None:
        return "registry:unavailable", None
    collection = match.group("collection")
    record_key = match.group("record_key")
    key_name = {
        "entities": "entity_id",
        "people": "entity_id",
        "teams": "entity_id",
        "memberships": "membership_id",
    }.get(collection)
    records = raw.get(collection)
    if key_name is None or not isinstance(records, list):
        return f"registry:{collection}", None
    record = next(
        (
            candidate
            for candidate in records
            if isinstance(candidate, dict) and str(candidate.get(key_name) or "").strip() == record_key
        ),
        None,
    )
    if record is None:
        return record_key, None
    current: object = record
    tail = match.group("tail")
    if tail is None:
        return record_key, current
    for part in tail.split("."):
        part_match = _FIELD_PART_RE.match(part)
        if part_match is None or not isinstance(current, dict):
            return record_key, None
        current = current.get(part_match.group("key"))
        index = part_match.group("index")
        if index is not None:
            if not isinstance(current, list) or int(index) >= len(current):
                return record_key, None
            current = current[int(index)]
    return record_key, current


def inspect_registry_manifest_integrity(knowledge_root: Path) -> RegistryManifestIntegrity:
    """Compare every manifest-managed file to its committed source hash.

    A stored generation snapshot permits field-granular DIR-14 classification.
    If an older generation predates snapshots, the result stays fail-closed:
    the edit is detected but classified critical rather than guessed.
    """
    manifest = load_registry_manifest(knowledge_root)
    if manifest is None:
        return RegistryManifestIntegrity(generation_id=None, edits=())
    edits: list[ManagedRegistryEdit] = []
    for relative_path, expected_hash in manifest.source_hashes:
        if relative_path not in _MANAGED_FILES:
            continue
        path = knowledge_root / relative_path
        actual_hash = compute_file_checksum(path) if path.is_file() else None
        if actual_hash == expected_hash:
            continue
        current = load_optional_yaml_mapping(path) if path.exists() else None
        baseline = load_optional_yaml_mapping(_generation_snapshot_path(knowledge_root, manifest, relative_path))
        fields = _changed_fields(relative_path, baseline=baseline, current=current or {}) if current is not None else ("<file missing>",)
        edits.append(
            ManagedRegistryEdit(
                relative_path=relative_path,
                expected_hash=expected_hash,
                actual_hash=actual_hash,
                changed_fields=fields,
                critical=any(_is_critical_field(relative_path, field) for field in fields),
            )
        )
    return RegistryManifestIntegrity(generation_id=manifest.generation_id, edits=tuple(edits))


def require_adopted_registry(knowledge_root: Path, *, consumer: str) -> RegistryManifestIntegrity:
    """Fail closed only for critical drift; informational drift remains visible."""
    integrity = inspect_registry_manifest_integrity(knowledge_root)
    critical = tuple(edit.relative_path for edit in integrity.edits if edit.critical)
    if critical:
        raise RegistryUnadoptedCriticalEdit(
            f"{consumer} cannot use the shared registry because unadopted critical edit(s) affect "
            f"{', '.join(critical)}. Run 'vertex kb registry adopt --reason <text> --apply' or restore "
            "the committed generation."
        )
    return integrity


def _require_bootstrapped(knowledge_root: Path) -> tuple[RegistryConfig, RegistryManifest]:
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError("The registry has not been bootstrapped yet; no managed registry generation exists.")
    return config, manifest


def _write_managed_file_from_live(relative_path: str, *, knowledge_root: Path, staged_dir: Path) -> None:
    destination = staged_dir / relative_path
    if relative_path == "entities.yaml":
        document = load_entities_document(knowledge_root / relative_path)
        if document is None:
            raise ConfigError("Cannot adopt a missing entities.yaml.")
        write_entities_document(destination, document)
    elif relative_path == "people_directory.yaml":
        result = load_people_directory(knowledge_root / relative_path)
        if result is None:
            raise ConfigError("Cannot adopt a missing people_directory.yaml.")
        write_people_directory(destination, result.people)
    elif relative_path == "teams.yaml":
        result = load_teams(knowledge_root / relative_path)
        if result is None:
            raise ConfigError("Cannot adopt a missing teams.yaml.")
        write_teams(destination, result.teams)
    elif relative_path == "memberships.yaml":
        write_memberships(destination, load_memberships(knowledge_root / relative_path))
    else:
        raise ConfigError(f"Refusing to adopt unsupported managed registry file {relative_path!r}.")


def _validate_managed_staging(relative_path: str, staged_dir: Path) -> None:
    path = staged_dir / relative_path
    if relative_path == "entities.yaml":
        if load_entities_document(path) is None:
            raise ConfigError("Staged entities.yaml is missing.")
    elif relative_path == "people_directory.yaml":
        if load_people_directory(path) is None:
            raise ConfigError("Staged people_directory.yaml is missing.")
    elif relative_path == "teams.yaml":
        if load_teams(path) is None:
            raise ConfigError("Staged teams.yaml is missing.")
    elif relative_path == "memberships.yaml":
        load_memberships(path)
    else:
        raise ConfigError(f"Refusing to validate unsupported managed registry file {relative_path!r}.")


def adopt_registry_edits(
    knowledge_root: Path,
    *,
    actor: str,
    reason: str,
    on_behalf_of: str | None = None,
    apply: bool,
    as_of: datetime | None = None,
) -> RegistryAdoptionResult:
    """Validate and commit manifest drift through the typed staged writer."""
    if not actor.strip():
        raise ConfigError("An authenticated operator principal is required to adopt managed registry edits.")
    if not reason.strip():
        raise ConfigError("A non-empty adoption reason is required.")
    config, manifest = _require_bootstrapped(knowledge_root)
    integrity = inspect_registry_manifest_integrity(knowledge_root)
    if integrity.is_clean:
        return RegistryAdoptionResult(integrity=integrity)
    paths = tuple(edit.relative_path for edit in integrity.edits)
    if not apply:
        return RegistryAdoptionResult(integrity=integrity)
    expected_hashes = {edit.relative_path: edit.actual_hash for edit in integrity.edits}
    now = as_of or datetime.now(timezone.utc)

    def write_staged_files(staged_dir: Path) -> None:
        current = inspect_registry_manifest_integrity(knowledge_root)
        current_hashes = {edit.relative_path: edit.actual_hash for edit in current.edits}
        if current_hashes != expected_hashes:
            raise ConfigError("Managed registry edits changed while waiting for the writer lease; re-run adoption preview.")
        for relative_path in paths:
            _write_managed_file_from_live(relative_path, knowledge_root=knowledge_root, staged_dir=staged_dir)

    def validate_staged_files(staged_dir: Path) -> None:
        for relative_path in paths:
            _validate_managed_staging(relative_path, staged_dir)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=now,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=now)
    for edit in integrity.edits:
        baseline = load_optional_yaml_mapping(_generation_snapshot_path(knowledge_root, manifest, edit.relative_path))
        current = load_optional_yaml_mapping(knowledge_root / edit.relative_path)
        append_people_change_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            transaction_id=committed.transaction_id,
            generation_id=committed.manifest.generation_id,
            authenticated_principal=actor,
            on_behalf_of=on_behalf_of,
            operation="adopt",
            entity_id=f"registry:{edit.relative_path}",
            field="manifest_hash",
            before=edit.expected_hash,
            after=edit.actual_hash,
            source="manual_adoption",
            reason=reason,
            as_of=now,
        )
        for field in edit.changed_fields:
            entity_id, before = _field_value(baseline, field)
            _, after = _field_value(current, field)
            append_people_change_record(
                knowledge_root,
                workspace_id=config.workspace_id,
                transaction_id=committed.transaction_id,
                generation_id=committed.manifest.generation_id,
                authenticated_principal=actor,
                on_behalf_of=on_behalf_of,
                operation="adopt",
                entity_id=entity_id,
                field=field,
                before=before,
                after=after,
                source="manual_adoption",
                reason=reason,
                as_of=now,
            )
    return RegistryAdoptionResult(
        integrity=integrity,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
    )


def _resolve_person(knowledge_root: Path, person_ref: str) -> PersonDirectory:
    document = load_entities_document(knowledge_root / "entities.yaml")
    directory = load_people_directory(knowledge_root / "people_directory.yaml")
    if document is None or directory is None:
        raise ConfigError("People stewardship requires committed shared entities.yaml and people_directory.yaml.")
    reference = person_ref.strip()
    if not reference:
        raise ConfigError("--person must be a non-empty canonical person ID or alias.")
    direct = [entity for entity in document.entities if entity.entity_id == reference]
    alias_matches = [
        entity
        for entity in document.entities
        if any(alias.value.casefold() == reference.casefold() for alias in entity.aliases)
    ]
    matches = direct or alias_matches
    people_matches = [entity for entity in matches if entity.entity_type == "person"]
    if len(people_matches) != 1:
        raise ConfigError(f"Person reference {person_ref!r} must resolve to exactly one canonical person entity.")
    person = [candidate for candidate in directory.people if candidate.entity_id == people_matches[0].entity_id]
    if len(person) != 1:
        raise ConfigError(f"Canonical person {people_matches[0].entity_id!r} must have exactly one people-directory record.")
    return person[0]


def _validate_person_fields(fields: tuple[str, ...], person: PersonDirectory) -> tuple[str, ...]:
    if not fields:
        raise ConfigError("At least one --field is required.")
    if len(set(fields)) != len(fields):
        raise ConfigError("Each --field may be specified only once.")
    unknown = sorted(set(fields) - _PERSON_FIELDS)
    if unknown:
        raise ConfigError(f"Unsupported people field(s): {', '.join(unknown)}.")
    for field in fields:
        value = getattr(person, field)
        if value is None or value == "" or value == ():
            raise ConfigError(f"Cannot verify {field!r} for {person.entity_id}: the field has no current value.")
        if getattr(value, "value", None) == "unknown":
            raise ConfigError(f"Cannot verify {field!r} for {person.entity_id}: its value is unknown.")
    return fields


def _replace_verification(
    person: PersonDirectory,
    *,
    field: str,
    source: str,
    actor: str,
    reason: str,
    now: datetime,
    pinned: bool | None,
    review_at: datetime | None,
) -> tuple[PersonDirectory, FieldVerification | None, FieldVerification]:
    matching = [verification for verification in person.verifications if verification.field_name == field]
    if len(matching) > 1:
        raise ConfigError(f"Person {person.entity_id!r} has duplicate verification records for {field!r}; repair before governance mutation.")
    before = matching[0] if matching else None
    if pinned is False and (before is None or not before.pinned):
        raise ConfigError(f"Field {field!r} for {person.entity_id!r} is not pinned.")
    after = FieldVerification(
        field_name=field,
        source=source,
        source_ref=None,
        observed_at=now,
        verified_at=now,
        recorded_at=now,
        verified_by_principal=actor,
        refresh_run_id=None,
        pinned=before.pinned if pinned is None and before is not None else bool(pinned),
        pin_reason=before.pin_reason if pinned is None and before is not None else (reason if pinned else None),
        pin_review_at=before.pin_review_at if pinned is None and before is not None else (review_at if pinned else None),
    )
    verifications = tuple(
        verification for verification in person.verifications if verification.field_name != field
    ) + (after,)
    return dataclasses.replace(person, verifications=tuple(sorted(verifications, key=lambda value: value.field_name))), before, after


def govern_person_fields(
    knowledge_root: Path,
    *,
    operation: str,
    person_ref: str,
    fields: tuple[str, ...],
    reason: str,
    actor: str,
    on_behalf_of: str | None = None,
    review_at: datetime | None = None,
    apply: bool,
    as_of: datetime | None = None,
) -> PersonGovernanceResult:
    """Pin, unpin, or attest explicit person fields using the canonical writer."""
    if operation not in {"pin", "unpin", "attest"}:
        raise ConfigError(f"Unsupported people governance operation {operation!r}.")
    if not actor.strip():
        raise ConfigError("An authenticated operator principal is required for people governance mutations.")
    if not reason.strip():
        raise ConfigError("A non-empty reason is required.")
    config, manifest = _require_bootstrapped(knowledge_root)
    require_adopted_registry(knowledge_root, consumer=f"People {operation}")
    person = _resolve_person(knowledge_root, person_ref)
    checked_fields = _validate_person_fields(fields, person)
    now = as_of or datetime.now(timezone.utc)
    modified = person
    changes: list[tuple[str, FieldVerification | None, FieldVerification]] = []
    for field in checked_fields:
        pinned = True if operation == "pin" else False if operation == "unpin" else None
        source = "operator_pin" if operation == "pin" else "operator_unpin" if operation == "unpin" else "human_attestation"
        modified, before, after = _replace_verification(
            modified,
            field=field,
            source=source,
            actor=actor,
            reason=reason,
            now=now,
            pinned=pinned,
            review_at=review_at,
        )
        changes.append((field, before, after))
    result = PersonGovernanceResult(operation=operation, entity_id=person.entity_id, fields=checked_fields)
    if not apply:
        return result

    def write_staged_files(staged_dir: Path) -> None:
        require_adopted_registry(knowledge_root, consumer=f"People {operation}")
        current_person = _resolve_person(knowledge_root, person_ref)
        if current_person != person:
            raise ConfigError("Person record changed while waiting for the writer lease; re-run the preview.")
        directory = load_people_directory(knowledge_root / "people_directory.yaml")
        assert directory is not None
        replacement = tuple(modified if candidate.entity_id == person.entity_id else candidate for candidate in directory.people)
        write_people_directory(staged_dir / "people_directory.yaml", replacement)

    def validate_staged_files(staged_dir: Path) -> None:
        if load_people_directory(staged_dir / "people_directory.yaml") is None:
            raise ConfigError("Staged people_directory.yaml is missing.")

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        ("people_directory.yaml",),
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=now,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=now)
    for field, before, after in changes:
        append_people_change_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            transaction_id=committed.transaction_id,
            generation_id=committed.manifest.generation_id,
            authenticated_principal=actor,
            on_behalf_of=on_behalf_of,
            operation=operation,
            entity_id=person.entity_id,
            field=f"verification.{field}",
            before=None if before is None else dataclasses.asdict(before),
            after=dataclasses.asdict(after),
            source=after.source,
            reason=reason,
            as_of=now,
        )
    return dataclasses.replace(result, transaction_id=committed.transaction_id, generation_id=committed.manifest.generation_id)
