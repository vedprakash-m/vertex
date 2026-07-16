from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable
import os
from pathlib import Path

import yaml

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import ConfigError
from datetime import date

from src.core.integration_types import RelationKind, RelationTargetKind, WorkItemRelation
from src.core.models_v2 import (
    Dependency,
    DependencyEvidenceTier,
    DependencyScheduleStatus,
    DependencyStatus,
    DependencyType,
    LegacyDependency,
    Signal,
)
from src.core.models import Confidence, WorkItem
from src.core.trajectory_analyzer import DriftPattern

if TYPE_CHECKING:
    from src.core.program_fact_store import ProgramFactRevision


_SHARED_PROGRAM_ID_SENTINEL = "__shared_registry__"


def get_dependencies_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "dependencies.yaml"


def get_shared_dependencies_path(programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / "dependencies.yaml"


def load_dependencies(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[Dependency, ...]:
    path = get_dependencies_path(program_id, programs_root)
    shared_dependencies = _load_shared_dependencies(programs_root=programs_root)
    if not path.exists():
        local_dependencies = _convert_legacy_dependencies(
            program_id,
            _load_legacy_dependencies(program_id, programs_root=programs_root),
        )
        return _merge_dependencies(
            local_dependencies,
            tuple(item for item in shared_dependencies if item.from_program_id == program_id),
            context=f"program '{program_id}' dependency load",
        )

    local_dependencies = _load_dependency_document(program_id, path=path)
    return _merge_dependencies(
        local_dependencies,
        tuple(item for item in shared_dependencies if item.from_program_id == program_id),
        context=f"program '{program_id}' dependency load",
    )


def save_dependencies(
    program_id: str,
    dependencies: tuple[Dependency, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_dependencies_path(program_id, programs_root)
    for dependency in dependencies:
        if dependency.from_program_id != program_id:
            raise ConfigError(
                f"Dependency '{dependency.id}' has from_program_id={dependency.from_program_id!r}; expected {program_id!r}."
            )
    payload = {
        "schema_version": "1.0",
        "dependencies": [_serialize_dependency(dependency, default_program_id=program_id) for dependency in dependencies],
    }
    _write_atomic_yaml(path, payload)
    _sync_dependency_facts(program_id, dependencies, programs_root=programs_root)
    return path


def _sync_dependency_facts(
    program_id: str,
    dependencies: tuple[Dependency, ...],
    *,
    programs_root: Path,
) -> None:
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
        build_natural_key,
    )

    store = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root))
    sync_time = datetime.now(timezone.utc)
    active_snapshot = store.snapshot(as_of=sync_time)
    active_dependencies = {
        fact.natural_key: fact
        for fact in active_snapshot.facts
        if fact.fact_type == "dependency.link" and fact.lifecycle_state == FactLifecycleState.ACTIVE
    }
    current_natural_keys: set[str] = set()

    for dependency in dependencies:
        entity_refs = (f"DEPENDENCY:{dependency.id}",)
        natural_key = build_natural_key("dependency.link", entity_refs=entity_refs, scope="program")
        current_natural_keys.add(natural_key)
        store.append_fact(
            ProgramFactInput(
                fact_type="dependency.link",
                scope="program",
                entity_refs=entity_refs,
                payload=_dependency_fact_payload(dependency),
                source_signal_ids=dependency.evidence_refs,
                precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                natural_key=natural_key,
                created_by="vertex.dependency_graph",
            ),
            recorded_at=sync_time,
        )

    for natural_key, fact in active_dependencies.items():
        if natural_key in current_natural_keys:
            continue
        store.append_fact(
            ProgramFactInput(
                fact_type="dependency.link",
                scope=fact.scope,
                entity_refs=fact.entity_refs,
                payload=fact.payload,
                source_signal_ids=fact.source_signal_ids,
                confidence=fact.confidence,
                precedence=fact.precedence,
                review_state=fact.review_state,
                lifecycle_state=FactLifecycleState.CLOSED,
                valid_from=fact.valid_from,
                valid_until=sync_time,
                projection_history=fact.projection_history,
                natural_key=natural_key,
                created_by="vertex.dependency_graph",
                privacy_classification=fact.privacy_classification,
                accepted_by=fact.accepted_by,
            ),
            recorded_at=sync_time,
        )


def _resolve_fact_db_root(programs_root: Path) -> Path | None:
    if programs_root == PROGRAMS_ROOT:
        return None
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def _dependency_fact_payload(dependency: Dependency) -> dict[str, object]:
    return {
        "id": dependency.id,
        "from_program_id": dependency.from_program_id,
        "from_workstream_id": dependency.from_workstream_id,
        "from_item_id": dependency.from_item_id,
        "from_milestone_id": dependency.from_milestone_id,
        "to_program_id": dependency.to_program_id,
        "to_workstream_id": dependency.to_workstream_id,
        "to_item_id": dependency.to_item_id,
        "to_milestone_id": dependency.to_milestone_id,
        "dependency_type": dependency.dependency_type.value,
        "risk_if_broken": dependency.risk_if_broken,
        "mitigation": dependency.mitigation,
        "status": dependency.status.value,
        "owner_alias": dependency.owner_alias,
        "resolution_path": dependency.resolution_path,
        "planned_resolution_date": (
            dependency.planned_resolution_date.isoformat()
            if dependency.planned_resolution_date is not None
            else None
        ),
        "schedule_status": (
            dependency.schedule_status.value if dependency.schedule_status is not None else None
        ),
        "linked_risk_ids": list(dependency.linked_risk_ids),
        # ADF-W4.4 (Section 8.10.2): evidence provenance. Persisted so the
        # AUTHORITATIVE_RELATION tier round-trips through the fact store and
        # the read-side projection (project_dependencies) can distinguish a
        # typed ADO relation from an AUTHORED/human-authored link. Additive:
        # legacy facts without these keys default to AUTHORED / empty on read.
        "evidence_tier": dependency.evidence_tier.value,
        "evidence_refs": list(dependency.evidence_refs),
    }


def load_inbound_cross_program_dependencies(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Dependency, ...]:
    inbound: list[Dependency] = list(
        item
        for item in _load_shared_dependencies(programs_root=programs_root)
        if item.from_program_id != item.to_program_id and item.to_program_id == program_id
    )
    for program_dir in sorted(programs_root.iterdir(), key=lambda entry: entry.name.lower()):
        if not program_dir.is_dir() or program_dir.name == program_id or not (program_dir / "program.yaml").exists():
            continue
        try:
            loaded_dependencies = load_dependencies(program_dir.name, programs_root=programs_root)
        except ConfigError:
            continue
        inbound.extend(
            dependency
            for dependency in loaded_dependencies
            if dependency.from_program_id != dependency.to_program_id and dependency.to_program_id == program_id
        )
    deduped_map: dict[str, Dependency] = {}
    for dependency in inbound:
        deduped_map.setdefault(dependency.id, dependency)
    deduped = tuple(deduped_map.values())
    return tuple(
        sorted(
            deduped,
            key=lambda dependency: (
                dependency.from_program_id,
                dependency_target_label(dependency),
                dependency_source_label(dependency),
                dependency.id,
            ),
        )
    )


def build_dependency_dag(dependencies: tuple[Dependency, ...]) -> dict[str, list[str]]:
    adjacency: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    for dependency in dependencies:
        source = _dependency_source_key(dependency)
        target = _dependency_target_key(dependency)
        labels[source] = dependency_source_label(dependency)
        labels[target] = dependency_target_label(dependency)
        adjacency.setdefault(source, set())
        adjacency.setdefault(target, set())
        adjacency[source].add(target)

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def _visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle_start = stack.index(node)
            cycle_path = [labels.get(item, item) for item in (stack[cycle_start:] + [node])]
            raise ConfigError(f"Dependency cycle detected: {' -> '.join(cycle_path)}")
        visiting.add(node)
        stack.append(node)
        for successor in sorted(adjacency.get(node, ())):
            _visit(successor)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in sorted(adjacency):
        _visit(node)

    return {
        labels.get(node, node): [labels.get(successor, successor) for successor in sorted(successors)]
        for node, successors in adjacency.items()
    }


def compute_blast_radius(
    trigger_item_id: int | str,
    dependencies: tuple[Dependency, ...],
    max_hops: int = 2,
) -> tuple[Dependency, ...]:
    if max_hops < 1 or not dependencies:
        return ()

    source_to_dependencies: dict[str, list[Dependency]] = {}
    for dependency in dependencies:
        source_to_dependencies.setdefault(_dependency_source_key(dependency), []).append(dependency)

    queue: deque[tuple[str, int]] = deque()
    seen_sources: set[str] = set()
    seen_dependency_ids: set[str] = set()
    impacted: list[Dependency] = []

    for dependency in dependencies:
        if _trigger_matches_dependency(trigger_item_id, dependency):
            source = _dependency_source_key(dependency)
            if source not in seen_sources:
                seen_sources.add(source)
                queue.append((source, 0))

    while queue:
        source, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for dependency in source_to_dependencies.get(source, ()): 
            if dependency.id not in seen_dependency_ids:
                seen_dependency_ids.add(dependency.id)
                impacted.append(dependency)
            queue.append((_dependency_target_key(dependency), depth + 1))

    return tuple(impacted)


def detect_cross_program_cascades(
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    dependencies: tuple[Dependency, ...],
) -> tuple[object, ...]:
    from src.core.cascade_detector import DependencyCascade

    cascades: list[DependencyCascade] = []
    for signal in signals:
        blast_radius = compute_blast_radius(signal.raw_ref or signal.text, dependencies)
        if signal.entity_refs:
            for entity_ref in signal.entity_refs:
                blast_radius += tuple(
                    dependency
                    for dependency in compute_blast_radius(entity_ref, dependencies)
                    if dependency.id not in {item.id for item in blast_radius}
                )
        for dependency in blast_radius:
            if dependency.from_program_id == dependency.to_program_id:
                continue
            cascades.append(
                DependencyCascade(
                    source_item=dependency_source_label(dependency),
                    target_item=dependency_target_label(dependency),
                    impact=dependency_impact_text(dependency),
                    resolution_path=dependency.resolution_path,
                    trigger_kind="signal",
                    trigger_detail=signal.text,
                    work_item_id=_signal_work_item_id(signal),
                    target_sections=(),
                    target_workstream_ids=_dependency_target_workstream_refs(dependency),
                    confidence=signal.confidence,
                )
            )
    for pattern in drift_patterns:
        for dependency in compute_blast_radius(pattern.work_item_id, dependencies):
            if dependency.from_program_id == dependency.to_program_id:
                continue
            cascades.append(
                DependencyCascade(
                    source_item=dependency_source_label(dependency),
                    target_item=dependency_target_label(dependency),
                    impact=dependency_impact_text(dependency),
                    resolution_path=dependency.resolution_path,
                    trigger_kind="drift",
                    trigger_detail=pattern.detail,
                    work_item_id=pattern.work_item_id,
                    target_sections=(),
                    target_workstream_ids=_dependency_target_workstream_refs(dependency),
                    confidence=(Confidence.HIGH if pattern.severity == "high" else Confidence.MEDIUM if pattern.severity == "medium" else Confidence.LOW),
                )
            )
    return tuple(cascades)


def dependency_source_label(dependency: Dependency | LegacyDependency) -> str:
    if isinstance(dependency, LegacyDependency):
        return dependency.from_item
    return _endpoint_label(
        workstream_id=dependency.from_workstream_id,
        item_id=dependency.from_item_id,
        milestone_id=dependency.from_milestone_id,
        include_program=False,
        program_id=dependency.from_program_id,
    )


def dependency_target_label(dependency: Dependency | LegacyDependency) -> str:
    if isinstance(dependency, LegacyDependency):
        return dependency.to_item
    return _endpoint_label(
        workstream_id=dependency.to_workstream_id,
        item_id=dependency.to_item_id,
        milestone_id=dependency.to_milestone_id,
        include_program=dependency.from_program_id != dependency.to_program_id,
        program_id=dependency.to_program_id,
    )


def dependency_impact_text(dependency: Dependency | LegacyDependency) -> str:
    if isinstance(dependency, LegacyDependency):
        return dependency.impact
    return dependency.risk_if_broken


def _parse_dependency(program_id: str, raw_dependency: dict[str, object], *, path: Path, index: int) -> Dependency:
    dependency_id = str(raw_dependency.get("id") or "").strip()
    if not dependency_id:
        raise ConfigError(f"Dependency entry #{index} in {path} is missing id.")

    from_program_id, from_refs = _parse_dependency_side(
        program_id,
        raw_dependency,
        side="from",
        path=path,
        index=index,
    )
    to_program_id, to_refs = _parse_dependency_side(
        program_id,
        raw_dependency,
        side="to",
        path=path,
        index=index,
    )

    risk_if_broken = str(raw_dependency.get("risk_if_broken") or "").strip()
    if not risk_if_broken:
        raise ConfigError(f"Dependency '{dependency_id}' in {path} is missing risk_if_broken.")

    try:
        dependency_type = DependencyType.from_string(str(raw_dependency.get("dependency_type") or ""))
        status = DependencyStatus.from_string(str(raw_dependency.get("status") or ""))
    except ValueError as error:
        raise ConfigError(f"Invalid dependency entry '{dependency_id}' in {path}: {error}") from error

    mitigation = str(raw_dependency.get("mitigation") or "").strip() or None
    owner_alias = str(raw_dependency.get("owner_alias") or "").strip() or None
    resolution_path = str(raw_dependency.get("resolution_path") or "").strip() or None

    planned_resolution_date_str = str(raw_dependency.get("planned_resolution_date") or "").strip()
    planned_resolution_date: date | None = None
    if planned_resolution_date_str:
        try:
            planned_resolution_date = date.fromisoformat(planned_resolution_date_str)
        except ValueError:
            pass  # ignore malformed dates — forward-compat

    schedule_status_str = str(raw_dependency.get("schedule_status") or "").strip()
    schedule_status: DependencyScheduleStatus | None = None
    if schedule_status_str:
        try:
            schedule_status = DependencyScheduleStatus.from_string(schedule_status_str)
        except ValueError:
            pass  # ignore unknown values — forward-compat

    raw_linked_risk_ids = raw_dependency.get("linked_risk_ids")
    linked_risk_ids = tuple(
        str(value).strip()
        for value in (raw_linked_risk_ids if isinstance(raw_linked_risk_ids, (list, tuple)) else ())
        if str(value).strip()
    )

    next_proving_event = str(raw_dependency.get("next_proving_event") or "").strip() or None
    blast_radius_narrative = str(raw_dependency.get("blast_radius_narrative") or "").strip() or None

    return Dependency(
        id=dependency_id,
        from_program_id=from_program_id,
        from_workstream_id=str(from_refs["workstream_id"]) if from_refs["workstream_id"] is not None else None,
        from_item_id=int(from_refs["item_id"]) if isinstance(from_refs["item_id"], (int, str)) and from_refs["item_id"] is not None else None,
        from_milestone_id=str(from_refs["milestone_id"]) if from_refs["milestone_id"] is not None else None,
        to_program_id=to_program_id,
        to_workstream_id=str(to_refs["workstream_id"]) if to_refs["workstream_id"] is not None else None,
        to_item_id=int(to_refs["item_id"]) if isinstance(to_refs["item_id"], (int, str)) and to_refs["item_id"] is not None else None,
        to_milestone_id=str(to_refs["milestone_id"]) if to_refs["milestone_id"] is not None else None,
        dependency_type=dependency_type,
        risk_if_broken=risk_if_broken,
        mitigation=mitigation,
        status=status,
        owner_alias=owner_alias,
        resolution_path=resolution_path,
        planned_resolution_date=planned_resolution_date,
        schedule_status=schedule_status,
        linked_risk_ids=linked_risk_ids,
        next_proving_event=next_proving_event,
        blast_radius_narrative=blast_radius_narrative,
    )


def _load_dependency_document(program_id: str, *, path: Path) -> tuple[Dependency, ...]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error

    schema_version = str(document.get("schema_version") or "").strip()
    if not schema_version:
        raise ConfigError(f"schema_version is required in {path}.")
    if schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported dependencies schema_version {schema_version!r} in {path}.")

    raw_dependencies = document.get("dependencies") or ()
    if not isinstance(raw_dependencies, list):
        raise ConfigError(f"Expected 'dependencies' list in {path}.")

    dependencies: list[Dependency] = []
    for index, raw_dependency in enumerate(raw_dependencies, start=1):
        if not isinstance(raw_dependency, dict):
            raise ConfigError(f"Dependency entry #{index} in {path} must be a mapping.")
        dependencies.append(_parse_dependency(program_id, raw_dependency, path=path, index=index))
    return tuple(dependencies)


def _load_shared_dependencies(*, programs_root: Path) -> tuple[Dependency, ...]:
    path = get_shared_dependencies_path(programs_root)
    if not path.exists():
        return ()

    dependencies = _load_dependency_document(_SHARED_PROGRAM_ID_SENTINEL, path=path)
    for dependency in dependencies:
        if _SHARED_PROGRAM_ID_SENTINEL in {dependency.from_program_id, dependency.to_program_id}:
            raise ConfigError(
                f"Shared dependency registry {path} requires explicit program scoping on both sides for dependency '{dependency.id}'."
            )
        if dependency.from_program_id == dependency.to_program_id:
            raise ConfigError(
                f"Shared dependency registry {path} only supports cross-program edges; dependency '{dependency.id}' stays within {dependency.from_program_id}."
            )
    return dependencies


def _merge_dependencies(
    primary: tuple[Dependency, ...],
    additional: tuple[Dependency, ...],
    *,
    context: str,
) -> tuple[Dependency, ...]:
    merged: list[Dependency] = []
    seen_ids: set[str] = set()
    for dependency in (*primary, *additional):
        if dependency.id in seen_ids:
            raise ConfigError(f"Duplicate dependency id '{dependency.id}' in {context}.")
        seen_ids.add(dependency.id)
        merged.append(dependency)
    return tuple(merged)


def _parse_dependency_side(
    program_id: str,
    raw_dependency: dict[str, object],
    *,
    side: str,
    path: Path,
    index: int,
) -> tuple[str, dict[str, str | int | None]]:
    explicit_program_id = str(raw_dependency.get(f"{side}_program_id") or "").strip() or None
    side_program_id = explicit_program_id
    values: dict[str, str | int | None] = {
        "workstream_id": None,
        "item_id": None,
        "milestone_id": None,
    }

    for field_name, allows_int in (("workstream_id", False), ("item_id", True), ("milestone_id", False)):
        parsed_program_id, parsed_value = _parse_scoped_ref(
            raw_dependency.get(f"{side}_{field_name}"),
            default_program_id=explicit_program_id or program_id,
            allows_int=allows_int,
            dependency_index=index,
            field_name=f"{side}_{field_name}",
            path=path,
        )
        if parsed_value is None:
            continue
        if side_program_id is None:
            side_program_id = parsed_program_id
        elif parsed_program_id != side_program_id:
            raise ConfigError(
                f"Dependency entry #{index} in {path} mixes {side}_program_id values ({side_program_id!r}, {parsed_program_id!r})."
            )
        values[field_name] = parsed_value

    if values["workstream_id"] is None and values["item_id"] is None and values["milestone_id"] is None:
        raise ConfigError(f"Dependency entry #{index} in {path} must define at least one {side}_* reference.")
    return side_program_id or program_id, values


def _parse_scoped_ref(
    raw_value: object,
    *,
    default_program_id: str,
    allows_int: bool,
    dependency_index: int,
    field_name: str,
    path: Path,
) -> tuple[str, str | int | None]:
    if raw_value in (None, ""):
        return default_program_id, None
    if allows_int and isinstance(raw_value, int):
        return default_program_id, raw_value
    if not isinstance(raw_value, str):
        raise ConfigError(f"Dependency entry #{dependency_index} in {path} has invalid {field_name} value.")

    value = raw_value.strip()
    if not value:
        return default_program_id, None
    program_id = default_program_id
    ref_value = value
    if ":" in value:
        program_id, ref_value = (part.strip() for part in value.split(":", 1))
    if not program_id or not ref_value:
        raise ConfigError(f"Dependency entry #{dependency_index} in {path} has invalid {field_name} value {raw_value!r}.")
    if allows_int:
        try:
            return program_id, int(ref_value)
        except ValueError as error:
            raise ConfigError(
                f"Dependency entry #{dependency_index} in {path} has non-integer {field_name} value {raw_value!r}."
            ) from error
    return program_id, ref_value


def _load_legacy_dependencies(program_id: str, *, programs_root: Path) -> tuple[LegacyDependency, ...]:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return ()
    try:
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {program_path}: {error}") from error
    raw_dependencies = program_document.get("key_dependencies") or ()
    if not isinstance(raw_dependencies, list):
        return ()
    return tuple(
        LegacyDependency(
            from_item=str(entry.get("from_item") or entry.get("from") or "").strip(),
            to_item=str(entry.get("to_item") or entry.get("to") or "").strip(),
            impact=str(entry.get("impact") or "").strip(),
        )
        for entry in raw_dependencies
        if isinstance(entry, dict)
        and str(entry.get("from_item") or entry.get("from") or "").strip()
        and str(entry.get("to_item") or entry.get("to") or "").strip()
        and str(entry.get("impact") or "").strip()
    )


def _convert_legacy_dependencies(program_id: str, dependencies: tuple[LegacyDependency, ...]) -> tuple[Dependency, ...]:
    converted: list[Dependency] = []
    for index, dependency in enumerate(dependencies, start=1):
        converted.append(
            Dependency(
                id=f"legacy-{program_id}-{index}",
                from_program_id=program_id,
                from_workstream_id=dependency.from_item,
                from_item_id=None,
                from_milestone_id=None,
                to_program_id=program_id,
                to_workstream_id=dependency.to_item,
                to_item_id=None,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken=dependency.impact,
                mitigation=None,
                status=DependencyStatus.ACTIVE,
                owner_alias=None,
            )
        )
    return tuple(converted)


def _trigger_matches_dependency(trigger_item_id: int | str, dependency: Dependency) -> bool:
    if isinstance(trigger_item_id, int):
        return dependency.from_item_id == trigger_item_id
    trigger_text = str(trigger_item_id or "").strip().lower()
    if not trigger_text:
        return False
    if dependency.from_item_id is not None:
        numeric_ref = _extract_numeric_trigger_ref(trigger_text)
        if numeric_ref == dependency.from_item_id:
            return True
    return any(candidate == trigger_text for candidate in _dependency_source_candidates(dependency))


def _extract_numeric_trigger_ref(value: str) -> int | None:
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        return None
    return int(digits)


def _dependency_source_candidates(dependency: Dependency) -> tuple[str, ...]:
    candidates = {
        dependency_source_label(dependency).lower(),
    }
    if dependency.from_item_id is not None:
        candidates.add(str(dependency.from_item_id))
    if dependency.from_workstream_id:
        candidates.add(dependency.from_workstream_id.lower())
    if dependency.from_milestone_id:
        candidates.add(dependency.from_milestone_id.lower())
    return tuple(candidate for candidate in candidates if candidate)


def _dependency_target_workstream_refs(dependency: Dependency) -> tuple[str, ...]:
    if not dependency.to_workstream_id:
        return ()
    return (f"{dependency.to_program_id}:{dependency.to_workstream_id}",)


def _endpoint_label(
    *,
    workstream_id: str | None,
    item_id: int | None,
    milestone_id: str | None,
    include_program: bool,
    program_id: str,
) -> str:
    if item_id is not None:
        label = f"WI#{item_id}"
        return f"{program_id}:{label}" if include_program else label
    if milestone_id:
        return f"{program_id}:{milestone_id}" if include_program else milestone_id
    if workstream_id:
        return f"{program_id}:{workstream_id}" if include_program else workstream_id
    return program_id


def _endpoint_key(
    program_id: str,
    *,
    workstream_id: str | None,
    item_id: int | None,
    milestone_id: str | None,
) -> str:
    if item_id is not None:
        return f"{program_id}:item:{item_id}"
    if milestone_id:
        return f"{program_id}:milestone:{milestone_id}"
    if workstream_id:
        return f"{program_id}:workstream:{workstream_id}"
    return f"{program_id}:program"


def _dependency_source_key(dependency: Dependency) -> str:
    return _endpoint_key(
        dependency.from_program_id,
        workstream_id=dependency.from_workstream_id,
        item_id=dependency.from_item_id,
        milestone_id=dependency.from_milestone_id,
    )


def _dependency_target_key(dependency: Dependency) -> str:
    return _endpoint_key(
        dependency.to_program_id,
        workstream_id=dependency.to_workstream_id,
        item_id=dependency.to_item_id,
        milestone_id=dependency.to_milestone_id,
    )


def _signal_work_item_id(signal: Signal) -> int | None:
    for candidate in signal.entity_refs:
        digits = "".join(character for character in candidate if character.isdigit())
        if digits:
            return int(digits)
    return None


def _serialize_dependency(dependency: Dependency, *, default_program_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": dependency.id,
        "dependency_type": dependency.dependency_type.value,
        "risk_if_broken": dependency.risk_if_broken,
        "status": dependency.status.value,
    }
    _set_scoped_ref(
        payload,
        "from_workstream_id",
        program_id=dependency.from_program_id,
        value=dependency.from_workstream_id,
        default_program_id=default_program_id,
    )
    _set_scoped_ref(
        payload,
        "from_item_id",
        program_id=dependency.from_program_id,
        value=dependency.from_item_id,
        default_program_id=default_program_id,
    )
    _set_scoped_ref(
        payload,
        "from_milestone_id",
        program_id=dependency.from_program_id,
        value=dependency.from_milestone_id,
        default_program_id=default_program_id,
    )
    _set_scoped_ref(
        payload,
        "to_workstream_id",
        program_id=dependency.to_program_id,
        value=dependency.to_workstream_id,
        default_program_id=default_program_id,
    )
    _set_scoped_ref(
        payload,
        "to_item_id",
        program_id=dependency.to_program_id,
        value=dependency.to_item_id,
        default_program_id=default_program_id,
    )
    _set_scoped_ref(
        payload,
        "to_milestone_id",
        program_id=dependency.to_program_id,
        value=dependency.to_milestone_id,
        default_program_id=default_program_id,
    )
    if dependency.mitigation:
        payload["mitigation"] = dependency.mitigation
    if dependency.owner_alias:
        payload["owner_alias"] = dependency.owner_alias
    if dependency.resolution_path:
        payload["resolution_path"] = dependency.resolution_path
    if dependency.planned_resolution_date is not None:
        payload["planned_resolution_date"] = dependency.planned_resolution_date.isoformat()
    if dependency.schedule_status is not None:
        payload["schedule_status"] = dependency.schedule_status.value
    if dependency.linked_risk_ids:
        payload["linked_risk_ids"] = list(dependency.linked_risk_ids)
    # ADF-W4.5 (Section 8.10.2): persist the two blast-radius-proposal output
    # fields so an applied DependencyBlastRadiusProposal (dependency_blast_radius.py::
    # apply_dependency_blast_radius_proposal) actually round-trips through
    # save_dependencies/load_dependencies, not just the in-memory dataclass.
    if dependency.next_proving_event:
        payload["next_proving_event"] = dependency.next_proving_event
    if dependency.blast_radius_narrative:
        payload["blast_radius_narrative"] = dependency.blast_radius_narrative
    return payload


def _set_scoped_ref(
    payload: dict[str, object],
    field_name: str,
    *,
    program_id: str,
    value: str | int | None,
    default_program_id: str,
) -> None:
    if value is None:
        return
    if isinstance(value, int):
        payload[field_name] = value if program_id == default_program_id else f"{program_id}:{value}"
        return
    payload[field_name] = value if program_id == default_program_id else f"{program_id}:{value}"


def _write_atomic_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


# --------------------------------------------------------------------------------------
# ADF-W4.4 (Section 8.10.2): AUTHORITATIVE_RELATION evidence-tier dependencies
# derived from ADF-W4.1's typed ADO relations (src/core/ado_relations.py /
# ADOHydrationProvider.hydrate's `relations` output). This is the only
# evidence tier this codebase can assign non-speculatively -- an ADO
# PREDECESSOR/SUCCESSOR link is a real, explicit statement by the work-item
# authors, not an inference. Every other Dependency construction site in
# this module builds from human-authored YAML (AUTHORED, the default) --
# this is the first and only AUTHORITATIVE_RELATION producer.
# --------------------------------------------------------------------------------------

#: Only these relation kinds are dependency-shaped. Hierarchy (parent/child),
#: RELATED, ARTIFACT_LINK, EXTERNAL_LINK, and UNKNOWN are not dependencies.
_DEPENDENCY_RELATION_KINDS = frozenset({RelationKind.PREDECESSOR, RelationKind.SUCCESSOR})


def dependency_from_authoritative_relation(
    relation: WorkItemRelation,
    *,
    program_id: str,
) -> Dependency | None:
    """Converts one typed ADO relation into an AUTHORITATIVE_RELATION-tier
    ``Dependency``. Returns ``None`` for relation kinds that aren't
    dependency-shaped or a non-work-item target -- not every relation is a
    dependency, and this function never guesses.

    Direction: this codebase's own convention (verified against real
    ``dependencies.yaml`` entries, e.g. ``from_milestone_id: m3-code-complete
    -> to_milestone_id: m5-ramp-review`` with ``risk_if_broken: "Ramp review
    cannot proceed if M3 code complete slips"``) is ``from`` = the item that
    must complete FIRST (the blocker/predecessor), ``to`` = the item that is
    BLOCKED (the successor, which depends on ``from``). ADO's
    ``System.LinkTypes.DependencyPredecessor`` link on a source item names a
    work item that must complete before the source -- so the TARGET is the
    blocker (``from``) and the SOURCE is blocked (``to``);
    ``DependencySuccessor`` is the exact mirror. ``dependency_type`` is
    always ``BLOCKS``, the one ``DependencyType`` member an ADO relation
    alone can support (``INFORMS``/``SHARES_RESOURCE`` need semantic
    judgment this function does not have).
    """
    if relation.relation_kind not in _DEPENDENCY_RELATION_KINDS:
        return None
    if relation.target_kind != RelationTargetKind.WORK_ITEM:
        return None
    try:
        target_item_id = int(relation.target_id)
    except (TypeError, ValueError):
        return None

    if relation.relation_kind == RelationKind.PREDECESSOR:
        # target is source's predecessor: target blocks source.
        blocker_item_id, blocked_item_id = target_item_id, relation.source_work_item_id
    else:  # SUCCESSOR: source is target's predecessor: source blocks target.
        blocker_item_id, blocked_item_id = relation.source_work_item_id, target_item_id

    return Dependency(
        id=f"ado-relation-{blocker_item_id}-blocks-{blocked_item_id}",
        from_program_id=program_id,
        from_workstream_id=None,
        from_item_id=blocker_item_id,  # from = predecessor/blocker (finishes first)
        from_milestone_id=None,
        to_program_id=program_id,
        to_workstream_id=None,
        to_item_id=blocked_item_id,  # to = successor/blocked (depends on "from")
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="",  # unknown from a relation alone -- honest empty, never guessed
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias=None,
        evidence_tier=DependencyEvidenceTier.AUTHORITATIVE_RELATION,
        evidence_refs=(relation.rel_type_name,),
    )


def relations_to_dependencies(
    relations: Iterable[WorkItemRelation],
    *,
    program_id: str,
) -> tuple[Dependency, ...]:
    """Converts a batch of typed ADO relations into AUTHORITATIVE_RELATION
    dependency candidates, skipping non-dependency-shaped relations and
    deduplicating the same predecessor/successor edge (ADO commonly reports
    both directions -- the predecessor's own PREDECESSOR link and the
    successor's mirrored SUCCESSOR link describe one edge, not two)."""
    seen: set[str] = set()
    out: list[Dependency] = []
    for relation in relations:
        candidate = dependency_from_authoritative_relation(relation, program_id=program_id)
        if candidate is None or candidate.id in seen:
            continue
        seen.add(candidate.id)
        out.append(candidate)
    return tuple(out)

#: Prefix for all relation-derived dependency ids (see
#: ``dependency_from_authoritative_relation``). Used by
#: ``sync_authoritative_relation_dependencies`` to scope its closure so ONLY
#: relation-derived ``dependency.link`` facts are reconciled -- a hand-authored
#: dependency is never closed by an automated gather pass.
_ADO_RELATION_ID_PREFIX = "ado-relation-"


def sync_authoritative_relation_dependencies(
    program_id: str,
    relations: Iterable[WorkItemRelation],
    *,
    scope_item_ids: frozenset[int] | None = None,
    programs_root: Path,
) -> tuple[Dependency, ...]:
    """ADF-W4.4 (Section 8.10.3): the gather-time producer of
    AUTHORITATIVE_RELATION dependencies from typed ADO relations.

    Converts relations via :func:`relations_to_dependencies`, then appends
    ``dependency.link`` facts for them -- fact-store ONLY, never rewriting
    ``dependencies.yaml`` (which is the human-authored source of truth;
    rewriting it from an automated pipeline would race hand-authored content
    and violate the NCFL ``writable: False`` policy on that file).

    Idempotent across gathers: each relation-derived dependency's natural key
    is deterministic (``DEPENDENCY:ado-relation-<blocker>-blocks-<blocked>``),
    so a repeat gather produces a revision of the same fact, not a duplicate.

    Scoped staleness closure: ``dependency.link`` facts whose entity-ref id
    carries the ``ado-relation-`` prefix AND is no longer in the current
    relation set are CLOSED (a relation that disappeared from ADO should not
    linger as an active dependency). Authored dependencies (no such prefix)
    are never touched by this reconcile -- they are owned by
    :func:`save_dependencies` / the ``vertex dependencies`` CLI.

    ADF-W2.2: ``scope_item_ids``, when provided, is the set of work-item ids
    whose relations were actually queried this cycle. A stale relation-derived
    fact is only closed if at least one of its two endpoints
    (``from_item_id``/``to_item_id``) is in that scope -- i.e. we only close
    on real evidence of removal from a directly-queried endpoint. A fact whose
    both endpoints fall outside this cycle's scope (their revisions didn't
    change, so their relations weren't re-fetched) is left untouched: the fact
    store itself is the reconstitution of "the full current relation set" for
    those items, not this cycle's necessarily-partial ``relations`` argument.
    ``scope_item_ids=None`` (the default) preserves the exact pre-ADF-W2.2
    behavior -- every fetch is treated as full, so anything absent is closed.
    This keeps the function safe to call from a genuinely incremental relation
    fetch (not yet wired) without any closure-logic change on the caller's
    part when it lands.

    Returns the converted dependencies (possibly empty) so a caller can log or
    surface the count; persistence is a side effect.
    """
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
        build_natural_key,
    )

    dependencies = relations_to_dependencies(relations, program_id=program_id)
    if not dependencies:
        # Still reconcile: a prior gather may have relation-derived facts that
        # have since all disappeared from ADO. An empty current set closes them.
        pass

    store = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root))
    sync_time = datetime.now(timezone.utc)
    active_snapshot = store.snapshot(as_of=sync_time)
    # Only relation-derived dependency facts are in scope for this reconcile.
    relation_active_facts = {
        fact.natural_key: fact
        for fact in active_snapshot.facts
        if fact.fact_type == "dependency.link"
        and fact.lifecycle_state == FactLifecycleState.ACTIVE
        and _fact_entity_ref_is_relation_derived(fact)
    }
    current_natural_keys: set[str] = set()

    for dependency in dependencies:
        entity_refs = (f"DEPENDENCY:{dependency.id}",)
        natural_key = build_natural_key("dependency.link", entity_refs=entity_refs, scope="program")
        current_natural_keys.add(natural_key)
        store.append_fact(
            ProgramFactInput(
                fact_type="dependency.link",
                scope="program",
                entity_refs=entity_refs,
                payload=_dependency_fact_payload(dependency),
                precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                natural_key=natural_key,
                created_by="vertex.dependency_graph.relations",
            ),
            recorded_at=sync_time,
        )

    # Scoped closure: relation-derived facts no longer present in the current
    # relation set are closed. Authored deps are never in relation_active_facts.
    for natural_key, fact in relation_active_facts.items():
        if natural_key in current_natural_keys:
            continue
        if scope_item_ids is not None and not _relation_fact_endpoint_in_scope(fact, scope_item_ids):
            continue
        store.append_fact(
            ProgramFactInput(
                fact_type="dependency.link",
                scope=fact.scope,
                entity_refs=fact.entity_refs,
                payload=fact.payload,
                source_signal_ids=fact.source_signal_ids,
                confidence=fact.confidence,
                precedence=fact.precedence,
                review_state=fact.review_state,
                lifecycle_state=FactLifecycleState.CLOSED,
                valid_from=fact.valid_from,
                valid_until=sync_time,
                projection_history=fact.projection_history,
                natural_key=natural_key,
                created_by="vertex.dependency_graph.relations",
                privacy_classification=fact.privacy_classification,
                accepted_by=fact.accepted_by,
            ),
            recorded_at=sync_time,
        )

    return dependencies


def _relation_fact_endpoint_in_scope(fact: ProgramFactRevision, scope_item_ids: frozenset[int]) -> bool:
    """ADF-W2.2: True iff either endpoint of this relation-derived fact
    (``from_item_id``/``to_item_id``, read from the stale fact's own payload
    since it's no longer in the current ``relations`` argument) was among the
    work items whose relations were actually queried this cycle."""
    for key in ("from_item_id", "to_item_id"):
        value = fact.payload.get(key)
        if isinstance(value, int) and value in scope_item_ids:
            return True
    return False


def _fact_entity_ref_is_relation_derived(fact: ProgramFactRevision) -> bool:
    """True iff this fact's ``DEPENDENCY:<id>`` entity ref carries the
    ``ado-relation-`` prefix -- i.e. it was produced by
    ``sync_authoritative_relation_dependencies``, not hand-authored."""
    for ref in fact.entity_refs:
        if isinstance(ref, str) and ref.startswith("DEPENDENCY:" + _ADO_RELATION_ID_PREFIX):
            return True
    return False
