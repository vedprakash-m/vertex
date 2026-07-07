from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import unified_diff
import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import portalocker
import yaml

from src.core.edition_resolver import _parse_scorecards
from src.core.exceptions import ConfigError
from src.core.knowledge_store import (
    KnowledgeStore,
    find_unknown_team_program_references,
    get_shared_knowledge_root,
    load_knowledge,
    validate_knowledge,
)
from src.core.profile_encryption import dump_people_profiles_document, load_people_profiles_document
from src.core.workstream_documents import _parse_workstreams


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"

OperationAction = Literal["set_fields", "add_list_value", "remove_list_value", "remove_entry"]
PlannerKind = Literal["deterministic", "ai"]


@dataclass(frozen=True, slots=True)
class KbDocumentSpec:
    relative_path: str
    collection_key: str
    identity_key: str
    schema_version: str

    @property
    def expected_major(self) -> str:
        return self.schema_version.split(".", 1)[0]

    def default_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            self.collection_key: [],
        }


@dataclass(frozen=True, slots=True)
class KbUpdateOperation:
    file_path: str
    action: OperationAction
    match_value: str
    fields: tuple[tuple[str, Any], ...] = ()
    field_name: str | None = None
    value: Any = None

    @property
    def field_mapping(self) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in self.fields}


@dataclass(frozen=True, slots=True)
class KbUpdatePlan:
    program_id: str
    correction: str
    planner: PlannerKind
    operations: tuple[KbUpdateOperation, ...]


@dataclass(frozen=True, slots=True)
class KbFileChange:
    relative_path: str
    absolute_path: Path
    before_text: str
    after_text: str
    before_document: dict[str, Any]
    after_document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KbUpdatePreview:
    program_id: str
    correction: str
    planner: PlannerKind
    diff: str
    validation_summary: str
    changes: tuple[KbFileChange, ...]


@dataclass(frozen=True, slots=True)
class KbUpdateApplyResult:
    preview: KbUpdatePreview
    audit_path: Path


_SUPPORTED_DOCUMENTS: dict[str, KbDocumentSpec] = {
    "knowledge/people_directory.yaml": KbDocumentSpec(
        relative_path="knowledge/people_directory.yaml",
        collection_key="people",
        identity_key="alias",
        schema_version="1.0",
    ),
    "knowledge/people_profiles.yaml": KbDocumentSpec(
        relative_path="knowledge/people_profiles.yaml",
        collection_key="profiles",
        identity_key="alias",
        schema_version="1.0",
    ),
    "knowledge/teams.yaml": KbDocumentSpec(
        relative_path="knowledge/teams.yaml",
        collection_key="teams",
        identity_key="id",
        schema_version="1.0",
    ),
    "knowledge/products.yaml": KbDocumentSpec(
        relative_path="knowledge/products.yaml",
        collection_key="products",
        identity_key="id",
        schema_version="1.0",
    ),
    "knowledge/golden_queries.yaml": KbDocumentSpec(
        relative_path="knowledge/golden_queries.yaml",
        collection_key="queries",
        identity_key="id",
        schema_version="1.0",
    ),
    "knowledge/engms_pages.yaml": KbDocumentSpec(
        relative_path="knowledge/engms_pages.yaml",
        collection_key="pages",
        identity_key="id",
        schema_version="1.0",
    ),
    "workstreams.yaml": KbDocumentSpec(
        relative_path="workstreams.yaml",
        collection_key="workstreams",
        identity_key="id",
        schema_version="2.0",
    ),
    "scorecards.yaml": KbDocumentSpec(
        relative_path="scorecards.yaml",
        collection_key="scorecards",
        identity_key="name",
        schema_version="2.0",
    ),
}

_PERSON_FIELD_ALIASES = {
    "display name": "display_name",
    "email": "email",
    "title": "title",
}

_WORKSTREAM_OWNER_FIELD_ALIASES = {
    "pm owner": "pm_owner",
    "eng owner": "eng_owner",
    "alternate owner": "alternate_owner",
}


def supported_kb_paths() -> tuple[str, ...]:
    return tuple(_SUPPORTED_DOCUMENTS)


def read_program_kb_documents(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, dict[str, Any]]:
    program_dir = programs_root / program_id
    documents: dict[str, dict[str, Any]] = {}
    for relative_path, spec in _SUPPORTED_DOCUMENTS.items():
        documents[relative_path] = _read_yaml_or_default(
            _resolve_document_path(relative_path, program_dir=program_dir, programs_root=programs_root),
            spec,
        )
    return documents


def parse_deterministic_kb_correction(
    correction: str,
    *,
    program_id: str,
) -> KbUpdatePlan | None:
    normalized = " ".join(correction.strip().split())
    if not normalized:
        return None

    person_field_match = re.fullmatch(
        r"set (?P<alias>[\w.@-]+) (?P<field>title|email|display name) to (?P<value>.+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if person_field_match is not None:
        field_name = _PERSON_FIELD_ALIASES[person_field_match.group("field").lower()]
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="deterministic",
            operations=(
                KbUpdateOperation(
                    file_path="knowledge/people_directory.yaml",
                    action="set_fields",
                    match_value=person_field_match.group("alias"),
                    fields=((field_name, person_field_match.group("value")),),
                ),
            ),
        )

    team_replace_match = re.fullmatch(
        r"set (?P<alias>[\w.@-]+) teams to (?P<value>.+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if team_replace_match is not None:
        team_ids = tuple(
            team_id
            for team_id in (part.strip() for part in team_replace_match.group("value").split(","))
            if team_id
        )
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="deterministic",
            operations=(
                KbUpdateOperation(
                    file_path="knowledge/people_directory.yaml",
                    action="set_fields",
                    match_value=team_replace_match.group("alias"),
                    fields=(("team_ids", list(team_ids)),),
                ),
            ),
        )

    team_add_match = re.fullmatch(
        r"add team (?P<team>[\w.-]+) to (?P<alias>[\w.@-]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if team_add_match is not None:
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="deterministic",
            operations=(
                KbUpdateOperation(
                    file_path="knowledge/people_directory.yaml",
                    action="add_list_value",
                    match_value=team_add_match.group("alias"),
                    field_name="team_ids",
                    value=team_add_match.group("team"),
                ),
            ),
        )

    team_remove_match = re.fullmatch(
        r"remove team (?P<team>[\w.-]+) from (?P<alias>[\w.@-]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if team_remove_match is not None:
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="deterministic",
            operations=(
                KbUpdateOperation(
                    file_path="knowledge/people_directory.yaml",
                    action="remove_list_value",
                    match_value=team_remove_match.group("alias"),
                    field_name="team_ids",
                    value=team_remove_match.group("team"),
                ),
            ),
        )

    owner_set_match = re.fullmatch(
        r"set (?P<workstream>[\w.-]+) (?P<field>pm owner|eng owner|alternate owner) to (?P<alias>[\w.@-]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if owner_set_match is not None:
        field_name = _WORKSTREAM_OWNER_FIELD_ALIASES[owner_set_match.group("field").lower()]
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="deterministic",
            operations=(
                KbUpdateOperation(
                    file_path="workstreams.yaml",
                    action="set_fields",
                    match_value=owner_set_match.group("workstream"),
                    fields=((field_name, owner_set_match.group("alias")),),
                ),
            ),
        )

    owner_clear_match = re.fullmatch(
        r"clear (?P<workstream>[\w.-]+) (?P<field>pm owner|eng owner|alternate owner)",
        normalized,
        flags=re.IGNORECASE,
    )
    if owner_clear_match is not None:
        field_name = _WORKSTREAM_OWNER_FIELD_ALIASES[owner_clear_match.group("field").lower()]
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="deterministic",
            operations=(
                KbUpdateOperation(
                    file_path="workstreams.yaml",
                    action="set_fields",
                    match_value=owner_clear_match.group("workstream"),
                    fields=((field_name, None),),
                ),
            ),
        )

    return None


def parse_kb_update_operations(payload: Any) -> tuple[KbUpdateOperation, ...]:
    if not isinstance(payload, dict):
        raise ValueError("AI KB update plan must be a JSON object.")

    operations_payload = payload.get("operations")
    if not isinstance(operations_payload, list) or not operations_payload:
        raise ValueError("AI KB update plan must contain a non-empty operations list.")

    operations: list[KbUpdateOperation] = []
    for entry in operations_payload:
        if not isinstance(entry, dict):
            raise ValueError("Each KB update operation must be a JSON object.")
        file_path = str(entry.get("path", "")).strip()
        action = str(entry.get("action", "")).strip()
        match_value = str(entry.get("match_value", "")).strip()
        if file_path not in _SUPPORTED_DOCUMENTS:
            raise ValueError(f"Unsupported KB document path: {file_path or '<empty>'}.")
        if action not in {"set_fields", "add_list_value", "remove_list_value", "remove_entry"}:
            raise ValueError(f"Unsupported KB update action: {action or '<empty>'}.")
        if not match_value:
            raise ValueError("KB update operations require match_value.")

        fields_payload = entry.get("fields")
        if fields_payload is not None and not isinstance(fields_payload, dict):
            raise ValueError("KB update fields must be a JSON object when present.")
        field_name = str(entry.get("field", "")).strip() or None

        operations.append(
            KbUpdateOperation(
                file_path=file_path,
                action=action,  # type: ignore[arg-type]
                match_value=match_value,
                fields=tuple((str(key), deepcopy(value)) for key, value in (fields_payload or {}).items()),
                field_name=field_name,
                value=deepcopy(entry.get("value")),
            )
        )

    return tuple(operations)


def validate_program_kb(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> KnowledgeStore:
    documents = read_program_kb_documents(program_id, programs_root=programs_root)
    return _validate_program_documents(program_id=program_id, documents=documents, programs_root=programs_root)


def prepare_kb_update(
    plan: KbUpdatePlan,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> KbUpdatePreview:
    program_dir = programs_root / plan.program_id
    current_documents = read_program_kb_documents(plan.program_id, programs_root=programs_root)
    updated_documents = {path: deepcopy(document) for path, document in current_documents.items()}

    for operation in plan.operations:
        spec = _SUPPORTED_DOCUMENTS.get(operation.file_path)
        if spec is None:
            raise ValueError(f"Unsupported KB document path: {operation.file_path}")
        _apply_operation(updated_documents[operation.file_path], spec, operation)

    knowledge = _validate_program_documents(
        program_id=plan.program_id,
        documents=updated_documents,
        programs_root=programs_root,
    )

    changes = _build_changes(program_dir=program_dir, before=current_documents, after=updated_documents)
    if not changes:
        raise ValueError("Correction produced no YAML changes.")

    return KbUpdatePreview(
        program_id=plan.program_id,
        correction=plan.correction,
        planner=plan.planner,
        diff=_render_diff(changes),
        validation_summary=(
            f"Schema and referential integrity checks passed ({len(knowledge.people_directory)} people, "
            f"{len(knowledge.golden_queries)} queries, {len(knowledge.engms_pages)} eng.ms pages)."
        ),
        changes=changes,
    )


def apply_kb_update(
    preview: KbUpdatePreview,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    edited_by: str = "vertex kb update",
) -> KbUpdateApplyResult:
    for change in preview.changes:
        change.absolute_path.parent.mkdir(parents=True, exist_ok=True)
        change.absolute_path.write_text(change.after_text, encoding="utf-8")

    audit_path = programs_root / preview.program_id / "journal" / "kb_edits.jsonl"
    _append_jsonl(
        audit_path,
        {
            "edited_at": datetime.now(timezone.utc).isoformat(),
            "edited_by": edited_by,
            "program_id": preview.program_id,
            "correction": preview.correction,
            "planner": preview.planner,
            "files": [
                {
                    "path": change.relative_path,
                    "before": change.before_document,
                    "after": change.after_document,
                }
                for change in preview.changes
            ],
        },
    )
    return KbUpdateApplyResult(preview=preview, audit_path=audit_path)


def _apply_operation(document: dict[str, Any], spec: KbDocumentSpec, operation: KbUpdateOperation) -> None:
    collection = document.setdefault(spec.collection_key, [])
    if not isinstance(collection, list):
        raise ValueError(f"Expected {spec.collection_key} to be a list in {operation.file_path}.")

    entity = _find_entity(collection, spec.identity_key, operation.match_value)

    if operation.action == "set_fields":
        if entity is None:
            entity = {spec.identity_key: operation.match_value}
            collection.append(entity)
        entity.update(operation.field_mapping)
        return

    if operation.action == "remove_entry":
        if entity is None:
            raise ValueError(f"No entry with {spec.identity_key}={operation.match_value} found in {operation.file_path}.")
        collection.remove(entity)
        return

    if entity is None:
        raise ValueError(f"No entry with {spec.identity_key}={operation.match_value} found in {operation.file_path}.")
    if operation.field_name is None:
        raise ValueError(f"KB update action {operation.action} requires field_name.")

    raw_values = entity.get(operation.field_name)
    if raw_values in (None, ""):
        values: list[Any] = []
    elif isinstance(raw_values, list):
        values = raw_values
    else:
        raise ValueError(f"Field {operation.field_name} in {operation.file_path} must be a list for {operation.action}.")

    if operation.action == "add_list_value":
        if operation.value not in values:
            values.append(deepcopy(operation.value))
        entity[operation.field_name] = values
        return

    if operation.action == "remove_list_value":
        entity[operation.field_name] = [entry for entry in values if entry != operation.value]
        return

    raise ValueError(f"Unsupported KB update action: {operation.action}")


def _build_changes(
    *,
    program_dir: Path,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> tuple[KbFileChange, ...]:
    changes: list[KbFileChange] = []
    for relative_path in _SUPPORTED_DOCUMENTS:
        before_document = before[relative_path]
        after_document = after[relative_path]
        if before_document == after_document:
            continue

        absolute_path = _resolve_document_path(relative_path, program_dir=program_dir, programs_root=program_dir.parent)
        before_text = absolute_path.read_text(encoding="utf-8") if absolute_path.exists() else ""
        after_text = _dump_document(relative_path, after_document, existing_path=absolute_path)
        changes.append(
            KbFileChange(
                relative_path=relative_path,
                absolute_path=absolute_path,
                before_text=before_text,
                after_text=after_text,
                before_document=deepcopy(before_document),
                after_document=deepcopy(after_document),
            )
        )
    return tuple(changes)


def _render_diff(changes: tuple[KbFileChange, ...]) -> str:
    chunks: list[str] = []
    for change in changes:
        diff_lines = unified_diff(
            change.before_text.splitlines(keepends=True),
            change.after_text.splitlines(keepends=True),
            fromfile=change.relative_path,
            tofile=change.relative_path,
        )
        chunks.append("".join(diff_lines).rstrip())
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def _resolve_document_path(relative_path: str, *, program_dir: Path, programs_root: Path) -> Path:
    if relative_path.startswith("knowledge/"):
        local_path = program_dir / relative_path
        # engms_pages.yaml is program-specific documentation — local content takes precedence over
        # the shared root, allowing each program to maintain its own eng.ms page registry.
        if relative_path == "knowledge/engms_pages.yaml" and local_path.exists():
            return local_path
        shared_root = get_shared_knowledge_root(programs_root)
        if shared_root.exists():
            return shared_root / Path(relative_path).name
        if local_path.exists():
            return local_path
    return program_dir / relative_path


def _validate_program_documents(
    *,
    program_id: str,
    documents: dict[str, dict[str, Any]],
    programs_root: Path,
) -> KnowledgeStore:
    for relative_path, spec in _SUPPORTED_DOCUMENTS.items():
        _validate_document_shape(relative_path, documents[relative_path], spec)

    with TemporaryDirectory() as temp_dir_name:
        temp_root = Path(temp_dir_name)
        temp_program_dir = temp_root / program_id
        temp_knowledge_dir = temp_program_dir / "knowledge"
        temp_knowledge_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, document in documents.items():
            target = temp_program_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_dump_yaml(document), encoding="utf-8")

        known_program_ids = tuple(
            sorted(
                path.name
                for path in programs_root.iterdir()
                if path.is_dir() and (path / "program.yaml").exists()
            )
        ) if programs_root.exists() else ()
        shared_knowledge_root = get_shared_knowledge_root(programs_root)
        uses_shared_knowledge = shared_knowledge_root.exists()

        knowledge = load_knowledge(knowledge_root=temp_knowledge_dir)
        validate_knowledge(knowledge)
        if known_program_ids and not uses_shared_knowledge:
            unknown_program_references = find_unknown_team_program_references(
                knowledge,
                known_program_ids=known_program_ids,
            )
            if unknown_program_references:
                raise ConfigError("; ".join(unknown_program_references))

        raw_workstreams = _read_yaml_or_default(
            temp_program_dir / "workstreams.yaml",
            _SUPPORTED_DOCUMENTS["workstreams.yaml"],
        )
        raw_scorecards = _read_yaml_or_default(
            temp_program_dir / "scorecards.yaml",
            _SUPPORTED_DOCUMENTS["scorecards.yaml"],
        )
        workstreams = _parse_workstreams(raw_workstreams, temp_program_dir / "workstreams.yaml")
        scorecards = _parse_scorecards(raw_scorecards, temp_program_dir / "scorecards.yaml")

    raw_program = _read_required_yaml_document(programs_root / program_id / "program.yaml")
    aliases = {person.alias for person in knowledge.people_directory}
    workstream_ids = {workstream.id for workstream in workstreams}
    problems: list[str] = []
    problems.extend(_validate_charter_references(program_id=program_id, raw_program=raw_program, aliases=aliases))
    problems.extend(_validate_workstream_raci_references(raw_workstreams=raw_workstreams, aliases=aliases))
    problems.extend(
        _validate_workstream_registry_references(
            program_id=program_id,
            programs_root=programs_root,
            aliases=aliases,
            people_directory=knowledge.people_directory,
        )
    )
    for workstream in workstreams:
        for field_name, owner in (
            ("pm_owner", workstream.pm_owner),
            ("eng_owner", workstream.eng_owner),
            ("alternate_owner", workstream.alternate_owner),
        ):
            if owner and owner not in aliases:
                problems.append(f"Unknown {field_name} '{owner}' referenced by workstream '{workstream.id}'.")

    for scorecard in scorecards:
        for dimension in scorecard.dimensions:
            if dimension.workstream_id not in workstream_ids:
                problems.append(
                    f"Unknown workstream_id '{dimension.workstream_id}' referenced by scorecard '{scorecard.name}'."
                )

    for query in knowledge.golden_queries:
        for workstream_id in query.workstream_ids:
            if workstream_id not in workstream_ids:
                problems.append(
                    f"Unknown workstream_id '{workstream_id}' referenced by golden query '{query.id}'."
                )

    if problems:
        raise ConfigError("; ".join(problems))
    return knowledge


def _read_required_yaml_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing required KB document: {path}")
    with path.open("r", encoding="utf-8") as handle:
        try:
            document = yaml.safe_load(handle) or {}
        except yaml.YAMLError as error:
            raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    return document


def _validate_charter_references(*, program_id: str, raw_program: dict[str, Any], aliases: set[str]) -> list[str]:
    charter = raw_program.get("charter")
    if charter in (None, ""):
        return []
    if not isinstance(charter, dict):
        return [f"Program '{program_id}' charter must be a mapping."]

    stakeholder_register = charter.get("stakeholder_register")
    if stakeholder_register in (None, ""):
        return []
    if not isinstance(stakeholder_register, list):
        return [f"Program '{program_id}' charter.stakeholder_register must be a list."]

    problems: list[str] = []
    for index, entry in enumerate(stakeholder_register, start=1):
        if not isinstance(entry, dict):
            problems.append(
                f"Program '{program_id}' charter.stakeholder_register entry #{index} must be a mapping."
            )
            continue
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            problems.append(
                f"Program '{program_id}' charter.stakeholder_register entry #{index} must define a non-empty alias."
            )
            continue
        normalized_alias = alias.strip()
        if normalized_alias not in aliases:
            problems.append(
                f"Unknown charter stakeholder alias '{normalized_alias}' referenced by program '{program_id}'."
            )
    return problems


def _validate_workstream_raci_references(*, raw_workstreams: dict[str, Any], aliases: set[str]) -> list[str]:
    workstreams_payload = raw_workstreams.get("workstreams")
    if not isinstance(workstreams_payload, list):
        return []

    problems: list[str] = []
    for index, entry in enumerate(workstreams_payload, start=1):
        if not isinstance(entry, dict):
            continue
        workstream_id = str(entry.get("id") or "").strip() or f"entry #{index}"
        raci = entry.get("raci")
        if raci in (None, ""):
            continue
        if not isinstance(raci, dict):
            problems.append(f"Workstream '{workstream_id}' raci must be a mapping.")
            continue

        if "accountable" in raci:
            accountable = raci.get("accountable")
            if accountable is not None:
                if not isinstance(accountable, str) or not accountable.strip():
                    problems.append(f"Workstream '{workstream_id}' raci.accountable must be a single alias string.")
                elif accountable.strip() not in aliases:
                    problems.append(
                        f"Unknown raci.accountable alias '{accountable.strip()}' referenced by workstream '{workstream_id}'."
                    )

        for field_name in ("responsible", "consulted", "informed"):
            if field_name not in raci:
                continue
            raw_aliases = raci.get(field_name)
            if not isinstance(raw_aliases, list):
                problems.append(f"Workstream '{workstream_id}' raci.{field_name} must be a list of aliases.")
                continue
            for alias in raw_aliases:
                if not isinstance(alias, str) or not alias.strip():
                    problems.append(f"Workstream '{workstream_id}' raci.{field_name} must be a list of aliases.")
                    break
                normalized_alias = alias.strip()
                if normalized_alias not in aliases:
                    problems.append(
                        f"Unknown raci.{field_name} alias '{normalized_alias}' referenced by workstream '{workstream_id}'."
                    )
    return problems


def _validate_workstream_registry_references(
    *,
    program_id: str,
    programs_root: Path,
    aliases: set[str],
    people_directory: tuple[Any, ...],
) -> list[str]:
    registry_path = programs_root / program_id / "workstream_registry.yaml"
    if not registry_path.exists():
        return []

    try:
        document = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {registry_path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {registry_path}.")

    raw_workstreams = document.get("workstreams") or []
    if not isinstance(raw_workstreams, list):
        raise ConfigError(f"Expected 'workstreams' list in {registry_path}.")

    known_display_names = {
        normalized
        for normalized in (_normalize_person_reference(getattr(person, "display_name", None)) for person in people_directory)
        if normalized is not None
    }
    known_emails = {
        str(getattr(person, "email", "")).strip().lower()
        for person in people_directory
        if str(getattr(person, "email", "")).strip()
    }

    problems: list[str] = []
    for index, entry in enumerate(raw_workstreams, start=1):
        if not isinstance(entry, dict):
            continue
        workstream_id = str(entry.get("id") or "").strip() or f"entry #{index}"
        raw_stakeholders = entry.get("stakeholders")
        if raw_stakeholders in (None, ""):
            continue
        if not isinstance(raw_stakeholders, list):
            problems.append(f"Registry workstream '{workstream_id}' stakeholders must be a list.")
            continue
        for stakeholder_index, raw_stakeholder in enumerate(raw_stakeholders, start=1):
            if not isinstance(raw_stakeholder, dict):
                problems.append(
                    f"Registry workstream '{workstream_id}' stakeholder entry #{stakeholder_index} must be a mapping."
                )
                continue
            stakeholder_name = str(raw_stakeholder.get("name") or "").strip()
            if not stakeholder_name:
                problems.append(
                    f"Registry workstream '{workstream_id}' stakeholder entry #{stakeholder_index} is missing name."
                )
                continue
            stakeholder_email = str(raw_stakeholder.get("email") or "").strip().lower()
            if stakeholder_email and stakeholder_email in known_emails:
                continue
            normalized_name = _normalize_person_reference(stakeholder_name)
            if normalized_name is None:
                problems.append(
                    f"Registry workstream '{workstream_id}' stakeholder entry #{stakeholder_index} has invalid name '{stakeholder_name}'."
                )
                continue
            if normalized_name not in known_display_names and normalized_name not in aliases:
                problems.append(
                    f"Unknown registry stakeholder '{stakeholder_name}' referenced by workstream '{workstream_id}'."
                )
    return problems


def _normalize_person_reference(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return re.sub(r"[^a-z0-9]+", "", normalized) or None


def _find_entity(collection: list[Any], identity_key: str, match_value: str) -> dict[str, Any] | None:
    for entry in collection:
        if isinstance(entry, dict) and str(entry.get(identity_key, "")).strip() == match_value:
            return entry
    return None


def _validate_document_shape(relative_path: str, document: dict[str, Any], spec: KbDocumentSpec) -> None:
    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ConfigError(f"schema_version is required in {relative_path}.")
    if schema_version.split(".", 1)[0] != spec.expected_major:
        raise ConfigError(
            f"Unsupported schema_version '{schema_version}' in {relative_path}; expected major {spec.expected_major}."
        )
    collection = document.get(spec.collection_key)
    if not isinstance(collection, list):
        raise ConfigError(f"{spec.collection_key} must be a list in {relative_path}.")


def _read_yaml_or_default(path: Path, spec: KbDocumentSpec) -> dict[str, Any]:
    if not path.exists():
        return spec.default_document()
    if spec.relative_path == "knowledge/people_profiles.yaml":
        return load_people_profiles_document(path)
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    return document


def _dump_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False)


def _dump_document(relative_path: str, document: dict[str, Any], *, existing_path: Path | None = None) -> str:
    if relative_path == "knowledge/people_profiles.yaml":
        return dump_people_profiles_document(document, existing_path=existing_path)
    return _dump_yaml(document)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)