from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from src.core.config_loader import ProgramDependency
from src.core.dependency_graph import dependency_impact_text, dependency_source_label, dependency_target_label
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Dependency, LegacyDependency, Scorecard, Signal, Workstream
from src.core.trajectory_analyzer import DriftPattern


@dataclass(frozen=True, slots=True)
class DependencyCascade:
    source_item: str
    target_item: str
    impact: str
    resolution_path: str | None
    trigger_kind: Literal["signal", "drift"]
    trigger_detail: str
    work_item_id: int | None
    target_sections: tuple[tuple[str, str], ...]
    target_workstream_ids: tuple[str, ...]
    confidence: Confidence = Confidence.NONE


def detect_dependency_cascades(
    *,
    dependencies: tuple[ProgramDependency | Dependency | LegacyDependency, ...],
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    items: tuple[WorkItem, ...],
    scorecards: tuple[Scorecard, ...],
    workstreams: tuple[Workstream, ...],
    max_hops: int = 2,
) -> tuple[DependencyCascade, ...]:
    if not dependencies or max_hops < 1:
        return ()

    item_lookup = {item.id: item for item in items}
    source_keys = tuple(_dependency_source_keys(dependency) for dependency in dependencies)
    target_keys = tuple(_dependency_target_keys(dependency) for dependency in dependencies)
    cascades: list[DependencyCascade] = []

    for dependency in dependencies:
        continue

    for signal in signals:
        seed_indexes = tuple(
            index
            for index, dependency in enumerate(dependencies)
            if _signal_matches_dependency(signal, dependency)
        )
        cascades.extend(
            _expand_cascades(
                dependencies=dependencies,
                seed_indexes=seed_indexes,
                source_keys=source_keys,
                target_keys=target_keys,
                trigger_kind="signal",
                trigger_detail=signal.text,
                work_item_id=_signal_work_item_id(signal),
                trigger_confidence=signal.confidence,
                scorecards=scorecards,
                workstreams=workstreams,
                max_hops=max_hops,
            )
        )

    for pattern in drift_patterns:
        item = item_lookup.get(pattern.work_item_id)
        if item is None:
            continue
        seed_indexes = tuple(
            index
            for index, dependency in enumerate(dependencies)
            if _item_matches_dependency(item, dependency)
        )
        cascades.extend(
            _expand_cascades(
                dependencies=dependencies,
                seed_indexes=seed_indexes,
                source_keys=source_keys,
                target_keys=target_keys,
                trigger_kind="drift",
                trigger_detail=pattern.detail,
                work_item_id=pattern.work_item_id,
                trigger_confidence=_drift_pattern_confidence(pattern.severity),
                scorecards=scorecards,
                workstreams=workstreams,
                max_hops=max_hops,
            )
        )

    return _dedupe_cascades(tuple(cascades))


def _expand_cascades(
    *,
    dependencies: tuple[ProgramDependency | Dependency | LegacyDependency, ...],
    seed_indexes: tuple[int, ...],
    source_keys: tuple[tuple[str, ...], ...],
    target_keys: tuple[tuple[str, ...], ...],
    trigger_kind: Literal["signal", "drift"],
    trigger_detail: str,
    work_item_id: int | None,
    trigger_confidence: Confidence,
    scorecards: tuple[Scorecard, ...],
    workstreams: tuple[Workstream, ...],
    max_hops: int,
) -> tuple[DependencyCascade, ...]:
    if not seed_indexes:
        return ()

    cascades: list[DependencyCascade] = []
    queue: list[tuple[int, int]] = [(index, 1) for index in seed_indexes]
    seen_indexes: set[int] = set(seed_indexes)
    queue_index = 0

    while queue_index < len(queue):
        dependency_index, depth = queue[queue_index]
        queue_index += 1
        dependency = dependencies[dependency_index]
        target_sections, target_workstream_ids = _resolve_dependency_targets(
            dependency,
            scorecards=scorecards,
            workstreams=workstreams,
        )
        if target_sections or target_workstream_ids or _preserve_cross_program_target(dependency):
            cascades.append(
                DependencyCascade(
                    source_item=_dependency_source_item(dependency),
                    target_item=_dependency_target_item(dependency),
                    impact=_dependency_impact(dependency),
                    resolution_path=(dependency.resolution_path if isinstance(dependency, Dependency) else None),
                    trigger_kind=trigger_kind,
                    trigger_detail=trigger_detail,
                    work_item_id=work_item_id,
                    target_sections=target_sections,
                    target_workstream_ids=target_workstream_ids,
                    confidence=trigger_confidence,
                )
            )
        if depth >= max_hops:
            continue
        current_target_keys = target_keys[dependency_index]
        for next_index, next_source_keys in enumerate(source_keys):
            if next_index in seen_indexes:
                continue
            if _dependency_keys_link(current_target_keys, next_source_keys):
                seen_indexes.add(next_index)
                queue.append((next_index, depth + 1))

    return tuple(cascades)


def _resolve_dependency_targets(
    dependency: ProgramDependency | Dependency | LegacyDependency,
    *,
    scorecards: tuple[Scorecard, ...],
    workstreams: tuple[Workstream, ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    if isinstance(dependency, ProgramDependency):
        return _resolve_targets(dependency.target, scorecards=scorecards, workstreams=workstreams)
    if isinstance(dependency, LegacyDependency):
        return _resolve_targets(dependency.to_item, scorecards=scorecards, workstreams=workstreams)

    matched_sections: list[tuple[str, str]] = []
    matched_workstream_ids: set[str] = set()

    if dependency.to_workstream_id:
        for workstream in workstreams:
            labels = (workstream.id, workstream.name, *workstream.aliases)
            if any(_labels_match(dependency.to_workstream_id, label) for label in labels if label):
                matched_workstream_ids.add(workstream.id)
        if matched_workstream_ids:
            matched_sections.extend(
                (scorecard.name, dimension.name)
                for scorecard in scorecards
                for dimension in scorecard.dimensions
                if dimension.workstream_id in matched_workstream_ids
            )

    if not matched_sections and not matched_workstream_ids:
        return _resolve_targets(
            dependency_target_label(dependency),
            scorecards=scorecards,
            workstreams=workstreams,
        )

    unique_sections = tuple(dict.fromkeys(matched_sections))
    return unique_sections, tuple(sorted(matched_workstream_ids))


def _resolve_targets(
    target_item: str,
    *,
    scorecards: tuple[Scorecard, ...],
    workstreams: tuple[Workstream, ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    matched_sections: list[tuple[str, str]] = []
    matched_workstream_ids: set[str] = set()

    for scorecard in scorecards:
        for dimension in scorecard.dimensions:
            if _labels_match(target_item, dimension.name):
                matched_sections.append((scorecard.name, dimension.name))
                matched_workstream_ids.add(dimension.workstream_id)

    for workstream in workstreams:
        labels = (workstream.id, workstream.name, *workstream.aliases)
        if not any(_labels_match(target_item, label) for label in labels if label):
            continue
        matched_workstream_ids.add(workstream.id)
        matched_sections.extend(
            (scorecard.name, dimension.name)
            for scorecard in scorecards
            for dimension in scorecard.dimensions
            if dimension.workstream_id == workstream.id
        )

    unique_sections = tuple(dict.fromkeys(matched_sections))
    return unique_sections, tuple(sorted(matched_workstream_ids))


def _signal_matches_dependency(
    signal: Signal,
    dependency: ProgramDependency | Dependency | LegacyDependency,
) -> bool:
    if isinstance(dependency, Dependency) and dependency.from_item_id is not None:
        if any(_extract_numeric_ref(candidate) == dependency.from_item_id for candidate in signal.entity_refs):
            return True
        if _extract_numeric_ref(signal.raw_ref) == dependency.from_item_id:
            return True
    source_candidates = _dependency_source_candidates(dependency)
    candidates = [signal.text]
    if signal.raw_ref:
        candidates.append(signal.raw_ref)
    if signal.workstream_id:
        candidates.append(signal.workstream_id)
    candidates.extend(signal.entity_refs)
    return any(
        _labels_match(source_item, candidate)
        for source_item in source_candidates
        for candidate in candidates
    )


def _item_matches_dependency(
    item: WorkItem,
    dependency: ProgramDependency | Dependency | LegacyDependency,
) -> bool:
    if isinstance(dependency, Dependency) and dependency.from_item_id is not None and dependency.from_item_id == item.id:
        return True
    source_candidates = _dependency_source_candidates(dependency)
    return any(
        _labels_match(source_item, candidate)
        for source_item in source_candidates
        for candidate in (item.title, item.area_path)
    )


def _signal_work_item_id(signal: Signal) -> int | None:
    for candidate in signal.entity_refs:
        digits = "".join(character for character in candidate if character.isdigit())
        if digits:
            return int(digits)
    return None


def _dependency_source_item(dependency: ProgramDependency | Dependency | LegacyDependency) -> str:
    if isinstance(dependency, ProgramDependency):
        return dependency.source
    return dependency_source_label(dependency)


def _dependency_target_item(dependency: ProgramDependency | Dependency | LegacyDependency) -> str:
    if isinstance(dependency, ProgramDependency):
        return dependency.target
    return dependency_target_label(dependency)


def _dependency_impact(dependency: ProgramDependency | Dependency | LegacyDependency) -> str:
    if isinstance(dependency, ProgramDependency):
        return dependency.impact
    return dependency_impact_text(dependency)


def _preserve_cross_program_target(dependency: ProgramDependency | Dependency | LegacyDependency) -> bool:
    return isinstance(dependency, Dependency) and dependency.from_program_id != dependency.to_program_id


def _dependency_source_candidates(dependency: ProgramDependency | Dependency | LegacyDependency) -> tuple[str, ...]:
    if isinstance(dependency, ProgramDependency):
        return (dependency.source,)
    if isinstance(dependency, LegacyDependency):
        return (dependency.from_item,)

    candidates = [dependency_source_label(dependency)]
    if dependency.from_workstream_id:
        candidates.append(dependency.from_workstream_id)
    if dependency.from_milestone_id:
        candidates.append(dependency.from_milestone_id)
    if dependency.from_item_id is not None:
        candidates.append(str(dependency.from_item_id))
        candidates.append(f"WI#{dependency.from_item_id}")
    return tuple(candidate for candidate in dict.fromkeys(candidates) if candidate)


def _dependency_source_keys(dependency: ProgramDependency | Dependency | LegacyDependency) -> tuple[str, ...]:
    return _dependency_source_candidates(dependency)


def _dependency_target_keys(dependency: ProgramDependency | Dependency | LegacyDependency) -> tuple[str, ...]:
    if isinstance(dependency, ProgramDependency):
        return (dependency.target,)
    if isinstance(dependency, LegacyDependency):
        return (dependency.to_item,)

    candidates = [dependency_target_label(dependency)]
    if dependency.to_workstream_id:
        candidates.append(dependency.to_workstream_id)
    if dependency.to_milestone_id:
        candidates.append(dependency.to_milestone_id)
    if dependency.to_item_id is not None:
        candidates.append(str(dependency.to_item_id))
        candidates.append(f"WI#{dependency.to_item_id}")
        candidates.append(f"WI:{dependency.to_item_id}")
    return tuple(candidate for candidate in dict.fromkeys(candidates) if candidate)


def _dependency_keys_link(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(_labels_match(source, target) for source in left for target in right)


def _drift_pattern_confidence(severity: str) -> Confidence:
    normalized = severity.strip().lower()
    if normalized == "high":
        return Confidence.HIGH
    if normalized == "medium":
        return Confidence.MEDIUM
    return Confidence.LOW


def _extract_numeric_ref(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        return None
    return int(digits)


def _dedupe_cascades(cascades: tuple[DependencyCascade, ...]) -> tuple[DependencyCascade, ...]:
    unique: dict[tuple[object, ...], DependencyCascade] = {}
    for cascade in cascades:
        key = (
            cascade.source_item,
            cascade.target_item,
            cascade.impact,
            cascade.resolution_path,
            cascade.trigger_kind,
            cascade.trigger_detail,
            cascade.work_item_id,
            cascade.target_sections,
            cascade.target_workstream_ids,
            cascade.confidence,
        )
        unique.setdefault(key, cascade)
    return tuple(
        sorted(
            unique.values(),
            key=lambda cascade: (
                cascade.target_item.lower(),
                cascade.source_item.lower(),
                cascade.trigger_kind,
                cascade.work_item_id or 0,
            ),
        )
    )


def _labels_match(left: str | None, right: str | None) -> bool:
    normalized_left = _normalize(left)
    normalized_right = _normalize(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return True
    left_tokens = _tokenize(normalized_left)
    right_tokens = _tokenize(normalized_right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    if len(left_tokens) == 1:
        return bool(overlap)
    return len(overlap) >= min(2, len(left_tokens), len(right_tokens))


def _normalize(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _tokenize(value: str) -> set[str]:
    return {
        token
        for token in value.split()
        if len(token) >= 3 and token not in {"and", "for", "the", "with"}
    }