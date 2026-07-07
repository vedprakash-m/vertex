from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from itertools import combinations
from pathlib import Path
import portalocker
from typing import Any, cast

import yaml

from src.core.config_loader import PROGRAMS_ROOT
from src.core.dependency_graph import DependencyType, dependency_source_label, dependency_target_label
from src.core.models import Confidence, SnapshotItem
from src.core.models_v2 import Dependency, Signal, SignalReviewDecision, TrajectoryPoint, Workstream
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_workstream_id


class DependencyProposalStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class DependencyProposal:
    id: str
    program_id: str
    from_workstream_id: str
    to_workstream_id: str
    from_item_id: int
    to_item_id: int
    from_item_title: str
    to_item_title: str
    suggested_dependency_type: DependencyType
    rationale: str
    evidence_refs: tuple[str, ...]
    detection_method: str
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    confidence: Confidence = Confidence.MEDIUM
    status: DependencyProposalStatus = DependencyProposalStatus.PROPOSED


def dependency_proposal_confidence_label(proposal: DependencyProposal) -> str:
    return f"{proposal.confidence.value.lower()} confidence"


@dataclass(frozen=True, slots=True)
class _ResolvedItem:
    item_id: int
    title: str
    workstream_id: str
    owner_alias: str | None


@dataclass(slots=True)
class _ProposalAccumulator:
    left: _ResolvedItem
    right: _ResolvedItem
    occurrence_count: int = 0
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    evidence_refs: list[str] | None = None
    phrases: list[str] | None = None


def get_dependency_proposals_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "dependency_proposals.yaml"


def load_dependency_proposals(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[DependencyProposal, ...]:
    path = get_dependency_proposals_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_entries = payload.get("proposals") or ()
    if not isinstance(raw_entries, list):
        return ()
    proposals: list[DependencyProposal] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        proposals.append(_parse_dependency_proposal(program_id, raw_entry))
    return tuple(sorted(proposals, key=_proposal_sort_key))


def save_dependency_proposals(
    program_id: str,
    proposals: tuple[DependencyProposal, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
    timestamp: datetime | None = None,
) -> Path:
    path = get_dependency_proposals_path(program_id, programs_root=programs_root)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "updated_at": _ensure_utc(timestamp or _utc_now()).isoformat(),
        "proposals": [_serialize_dependency_proposal(proposal) for proposal in sorted(proposals, key=_proposal_sort_key)],
    }
    _write_atomic_yaml(path, payload)
    _append_feedback_audit(
        path.parent / "_audit.jsonl",
        {
            "ts": _ensure_utc(timestamp or _utc_now()).isoformat(),
            "module": "dependency_scout",
            "file": path.name,
            "evidence_hash": _hash_proposals(proposals),
        },
    )
    return path


def merge_dependency_proposals(
    existing: tuple[DependencyProposal, ...],
    generated: tuple[DependencyProposal, ...],
) -> tuple[DependencyProposal, ...]:
    existing_by_id = {proposal.id: proposal for proposal in existing}
    merged: dict[str, DependencyProposal] = {proposal.id: proposal for proposal in existing}
    for proposal in generated:
        previous = existing_by_id.get(proposal.id)
        if previous is None:
            merged[proposal.id] = proposal
            continue
        merged[proposal.id] = replace(proposal, status=previous.status)
    return tuple(sorted(merged.values(), key=_proposal_sort_key))


def update_dependency_proposal_status(
    proposals: tuple[DependencyProposal, ...],
    proposal_id: str,
    *,
    status: DependencyProposalStatus,
) -> tuple[DependencyProposal, ...]:
    updated: list[DependencyProposal] = []
    for proposal in proposals:
        if proposal.id == proposal_id:
            updated.append(replace(proposal, status=status))
        else:
            updated.append(proposal)
    return tuple(sorted(updated, key=_proposal_sort_key))


def scout_dependency_proposals(
    *,
    program_id: str,
    signals: tuple[Signal, ...],
    review_states: dict[str, SignalReviewDecision],
    snapshot_items: tuple[SnapshotItem, ...],
    workstreams: tuple[Workstream, ...],
    existing_dependencies: tuple[Dependency, ...],
    trajectories_by_item_id: dict[int, tuple[TrajectoryPoint, ...]] | None = None,
    as_of: datetime,
    lookback_days: int = 30,
    min_occurrences: int = 3,
    co_movement_window_days: int = 7,
    min_co_movements: int = 2,
) -> tuple[DependencyProposal, ...]:
    if min_occurrences < 1:
        return ()
    threshold = _ensure_utc(as_of) - timedelta(days=lookback_days)
    item_lookup = _build_item_lookup(snapshot_items, workstreams)
    if not item_lookup:
        return ()
    existing_pairs = _existing_dependency_pairs(existing_dependencies)
    accumulators: dict[tuple[int, int], _ProposalAccumulator] = {}
    comment_accumulators: dict[tuple[int, int], _ProposalAccumulator] = {}
    for signal in signals:
        if signal.program_id != program_id:
            continue
        if _ensure_utc(signal.timestamp) < threshold:
            continue
        if not signal_is_approved_for_evidence(signal, review_states):
            continue
        resolved_items = tuple(
            item_lookup[item_id]
            for item_id in _extract_work_item_ids(signal.entity_refs)
            if item_id in item_lookup
        )
        matched_phrases = _extract_dependency_phrases(signal.text)
        for left, right in combinations(resolved_items, 2):
            if left.workstream_id == right.workstream_id:
                continue
            canonical_left, canonical_right = _canonical_pair(left, right)
            pair_key = cast(tuple[int, int], tuple(sorted((canonical_left.item_id, canonical_right.item_id))))
            if pair_key in existing_pairs:
                continue
            if matched_phrases:
                comment_accumulator = comment_accumulators.get(pair_key)
                if comment_accumulator is None:
                    comment_accumulator = _ProposalAccumulator(
                        left=canonical_left,
                        right=canonical_right,
                        evidence_refs=[],
                        phrases=[],
                    )
                    comment_accumulators[pair_key] = comment_accumulator
                _record_accumulator_signal(comment_accumulator, signal.id, signal.timestamp)
                for phrase in matched_phrases:
                    if comment_accumulator.phrases is None:
                        comment_accumulator.phrases = []
                    if phrase not in comment_accumulator.phrases:
                        comment_accumulator.phrases.append(phrase)
            accumulator = accumulators.get(pair_key)
            if accumulator is None:
                accumulator = _ProposalAccumulator(
                    left=canonical_left,
                    right=canonical_right,
                    evidence_refs=[],
                )
                accumulators[pair_key] = accumulator
            _record_accumulator_signal(accumulator, signal.id, signal.timestamp)

    proposals: list[DependencyProposal] = []
    for accumulator in accumulators.values():
        if tuple(sorted((accumulator.left.item_id, accumulator.right.item_id))) in comment_accumulators:
            continue
        if accumulator.occurrence_count < min_occurrences:
            continue
        first_seen_at = accumulator.first_seen_at or threshold
        last_seen_at = accumulator.last_seen_at or threshold
        rationale = (
            f"{accumulator.left.title} (WI#{accumulator.left.item_id}) and "
            f"{accumulator.right.title} (WI#{accumulator.right.item_id}) were co-mentioned in "
            f"{accumulator.occurrence_count} approved signals during the last {lookback_days} days."
        )
        proposals.append(
            DependencyProposal(
                id=_build_dependency_proposal_id(
                    accumulator.left.item_id,
                    accumulator.right.item_id,
                    detection_method="co_mention",
                ),
                program_id=program_id,
                from_workstream_id=accumulator.left.workstream_id,
                to_workstream_id=accumulator.right.workstream_id,
                from_item_id=accumulator.left.item_id,
                to_item_id=accumulator.right.item_id,
                from_item_title=accumulator.left.title,
                to_item_title=accumulator.right.title,
                suggested_dependency_type=DependencyType.SHARES_RESOURCE,
                rationale=rationale,
                evidence_refs=tuple(sorted(accumulator.evidence_refs or [])),
                detection_method="co_mention",
                occurrence_count=accumulator.occurrence_count,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                confidence=Confidence.MEDIUM,
            )
        )

    proposals.extend(
        _build_comment_language_proposals(
            program_id=program_id,
            accumulators=tuple(comment_accumulators.values()),
            threshold=threshold,
        )
    )

    proposals.extend(
        _build_owner_overlap_proposals(
            program_id=program_id,
            item_lookup=item_lookup,
            trajectories_by_item_id=trajectories_by_item_id or {},
            existing_pairs=existing_pairs,
            as_of=_ensure_utc(as_of),
            lookback_days=lookback_days,
            co_movement_window_days=co_movement_window_days,
            min_co_movements=min_co_movements,
        )
    )
    proposals.extend(
        _build_eta_co_movement_proposals(
            program_id=program_id,
            item_lookup=item_lookup,
            trajectories_by_item_id=trajectories_by_item_id or {},
            existing_pairs=existing_pairs,
            as_of=_ensure_utc(as_of),
            lookback_days=lookback_days,
            co_movement_window_days=co_movement_window_days,
            min_co_movements=min_co_movements,
        )
    )
    return tuple(sorted(proposals, key=_proposal_sort_key))


def dependency_proposal_to_dependency(
    proposal: DependencyProposal,
    *,
    dependency_type: DependencyType | None = None,
    risk_if_broken: str | None = None,
    resolution_path: str | None = None,
) -> Dependency:
    return Dependency(
        id=_build_dependency_id(proposal),
        from_program_id=proposal.program_id,
        from_workstream_id=proposal.from_workstream_id,
        from_item_id=proposal.from_item_id,
        from_milestone_id=None,
        to_program_id=proposal.program_id,
        to_workstream_id=proposal.to_workstream_id,
        to_item_id=proposal.to_item_id,
        to_milestone_id=None,
        dependency_type=dependency_type or proposal.suggested_dependency_type,
        risk_if_broken=(risk_if_broken.strip() if risk_if_broken is not None and risk_if_broken.strip() else proposal.rationale),
        mitigation=None,
        status=_dependency_status_active(),
        owner_alias=None,
        resolution_path=(resolution_path.strip() if resolution_path is not None and resolution_path.strip() else None),
    )


def _dependency_status_active():
    from src.core.models_v2 import DependencyStatus

    return DependencyStatus.ACTIVE


def _build_item_lookup(
    snapshot_items: tuple[SnapshotItem, ...],
    workstreams: tuple[Workstream, ...],
) -> dict[int, _ResolvedItem]:
    resolved: dict[int, _ResolvedItem] = {}
    for item in snapshot_items:
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is None:
            continue
        resolved[item.id] = _ResolvedItem(
            item_id=item.id,
            title=item.title,
            workstream_id=workstream_id,
            owner_alias=_normalize_owner_alias(item.assigned_to),
        )
    return resolved


def _canonical_pair(left: _ResolvedItem, right: _ResolvedItem) -> tuple[_ResolvedItem, _ResolvedItem]:
    if (left.workstream_id, left.item_id) <= (right.workstream_id, right.item_id):
        return left, right
    return right, left


def _extract_work_item_ids(entity_refs: tuple[str, ...]) -> tuple[int, ...]:
    seen: set[int] = set()
    item_ids: list[int] = []
    for ref in entity_refs:
        digits = "".join(character for character in ref if character.isdigit())
        if not digits:
            continue
        item_id = int(digits)
        if item_id in seen:
            continue
        seen.add(item_id)
        item_ids.append(item_id)
    return tuple(sorted(item_ids))


def _existing_dependency_pairs(dependencies: tuple[Dependency, ...]) -> set[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    for dependency in dependencies:
        if dependency.from_item_id is not None and dependency.to_item_id is not None:
            pairs.add(cast(tuple[int, int], tuple(sorted((dependency.from_item_id, dependency.to_item_id)))))
    return pairs


def _build_dependency_proposal_id(left_item_id: int, right_item_id: int, *, detection_method: str) -> str:
    first_item_id, second_item_id = sorted((left_item_id, right_item_id))
    if detection_method == "co_mention":
        return f"dep-proposal-co-mention-{first_item_id}-{second_item_id}"
    method_token = detection_method.replace("_", "-")
    return f"dep-proposal-{method_token}-{first_item_id}-{second_item_id}"


def _build_eta_co_movement_proposals(
    *,
    program_id: str,
    item_lookup: dict[int, _ResolvedItem],
    trajectories_by_item_id: dict[int, tuple[TrajectoryPoint, ...]],
    existing_pairs: set[tuple[int, int]],
    as_of: datetime,
    lookback_days: int,
    co_movement_window_days: int,
    min_co_movements: int,
) -> tuple[DependencyProposal, ...]:
    if min_co_movements < 1 or not trajectories_by_item_id:
        return ()

    proposals: list[DependencyProposal] = []
    window_start = as_of.date() - timedelta(days=lookback_days)
    ordered_item_ids = sorted(item_lookup)
    for left_item_id, right_item_id in combinations(ordered_item_ids, 2):
        pair_key = (left_item_id, right_item_id)
        if pair_key in existing_pairs:
            continue
        left = item_lookup[left_item_id]
        right = item_lookup[right_item_id]
        if left.workstream_id == right.workstream_id:
            continue

        left_events = _extract_positive_target_shift_events(
            trajectories_by_item_id.get(left_item_id, ()),
            start=window_start,
        )
        right_events = _extract_positive_target_shift_events(
            trajectories_by_item_id.get(right_item_id, ()),
            start=window_start,
        )
        matches = _match_co_movement_events(
            left_events,
            right_events,
            window_days=co_movement_window_days,
        )
        if len(matches) < min_co_movements:
            continue

        canonical_left, canonical_right = _canonical_pair(left, right)
        if canonical_left.owner_alias is not None and canonical_left.owner_alias == canonical_right.owner_alias:
            continue
        ordered_matches = matches if canonical_left.item_id == left.item_id else tuple((right_date, left_date) for left_date, right_date in matches)
        evidence_refs = sorted(
            {
                f"trajectory:{canonical_left.item_id}:{left_date.isoformat()}"
                for left_date, _ in ordered_matches
            }
            | {
                f"trajectory:{canonical_right.item_id}:{right_date.isoformat()}"
                for _, right_date in ordered_matches
            }
        )
        first_seen_at = datetime.combine(min(min(left_date, right_date) for left_date, right_date in ordered_matches), datetime.min.time(), tzinfo=as_of.tzinfo)
        last_seen_at = datetime.combine(max(max(left_date, right_date) for left_date, right_date in ordered_matches), datetime.min.time(), tzinfo=as_of.tzinfo)
        proposals.append(
            DependencyProposal(
                id=_build_dependency_proposal_id(
                    canonical_left.item_id,
                    canonical_right.item_id,
                    detection_method="eta_co_movement",
                ),
                program_id=program_id,
                from_workstream_id=canonical_left.workstream_id,
                to_workstream_id=canonical_right.workstream_id,
                from_item_id=canonical_left.item_id,
                to_item_id=canonical_right.item_id,
                from_item_title=canonical_left.title,
                to_item_title=canonical_right.title,
                suggested_dependency_type=DependencyType.SHARES_RESOURCE,
                rationale=(
                    f"{canonical_left.title} (WI#{canonical_left.item_id}) and "
                    f"{canonical_right.title} (WI#{canonical_right.item_id}) slipped target dates in the same direction "
                    f"within {co_movement_window_days} days on {len(ordered_matches)} occasions during the last {lookback_days} days."
                ),
                evidence_refs=tuple(evidence_refs),
                detection_method="eta_co_movement",
                occurrence_count=len(ordered_matches),
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                confidence=Confidence.HIGH if len(ordered_matches) >= 3 else Confidence.MEDIUM,
            )
        )
    return tuple(proposals)


def _build_comment_language_proposals(
    *,
    program_id: str,
    accumulators: tuple[_ProposalAccumulator, ...],
    threshold: datetime,
) -> tuple[DependencyProposal, ...]:
    proposals: list[DependencyProposal] = []
    for accumulator in accumulators:
        first_seen_at = accumulator.first_seen_at or threshold
        last_seen_at = accumulator.last_seen_at or threshold
        phrases = tuple(sorted(accumulator.phrases or ()))
        phrase_text = ", ".join(phrases) if phrases else "dependency language"
        proposals.append(
            DependencyProposal(
                id=_build_dependency_proposal_id(
                    accumulator.left.item_id,
                    accumulator.right.item_id,
                    detection_method="comment_language",
                ),
                program_id=program_id,
                from_workstream_id=accumulator.left.workstream_id,
                to_workstream_id=accumulator.right.workstream_id,
                from_item_id=accumulator.left.item_id,
                to_item_id=accumulator.right.item_id,
                from_item_title=accumulator.left.title,
                to_item_title=accumulator.right.title,
                suggested_dependency_type=DependencyType.SHARES_RESOURCE,
                rationale=(
                    f"Approved signals mentioned {accumulator.left.title} (WI#{accumulator.left.item_id}) and "
                    f"{accumulator.right.title} (WI#{accumulator.right.item_id}) together with explicit dependency language "
                    f"({phrase_text}) {accumulator.occurrence_count} time(s)."
                ),
                evidence_refs=tuple(sorted(accumulator.evidence_refs or [])),
                detection_method="comment_language",
                occurrence_count=accumulator.occurrence_count,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                confidence=Confidence.HIGH if accumulator.occurrence_count >= 2 else Confidence.MEDIUM,
            )
        )
    return tuple(proposals)


def _build_owner_overlap_proposals(
    *,
    program_id: str,
    item_lookup: dict[int, _ResolvedItem],
    trajectories_by_item_id: dict[int, tuple[TrajectoryPoint, ...]],
    existing_pairs: set[tuple[int, int]],
    as_of: datetime,
    lookback_days: int,
    co_movement_window_days: int,
    min_co_movements: int,
) -> tuple[DependencyProposal, ...]:
    if min_co_movements < 1 or not trajectories_by_item_id:
        return ()

    proposals: list[DependencyProposal] = []
    window_start = as_of.date() - timedelta(days=lookback_days)
    ordered_item_ids = sorted(item_lookup)
    for left_item_id, right_item_id in combinations(ordered_item_ids, 2):
        pair_key = (left_item_id, right_item_id)
        if pair_key in existing_pairs:
            continue
        left = item_lookup[left_item_id]
        right = item_lookup[right_item_id]
        if left.workstream_id == right.workstream_id:
            continue
        if left.owner_alias is None or left.owner_alias != right.owner_alias:
            continue

        left_events = _extract_positive_target_shift_events(
            trajectories_by_item_id.get(left_item_id, ()),
            start=window_start,
        )
        right_events = _extract_positive_target_shift_events(
            trajectories_by_item_id.get(right_item_id, ()),
            start=window_start,
        )
        matches = _match_co_movement_events(
            left_events,
            right_events,
            window_days=co_movement_window_days,
        )
        if len(matches) < min_co_movements:
            continue

        canonical_left, canonical_right = _canonical_pair(left, right)
        ordered_matches = matches if canonical_left.item_id == left.item_id else tuple((right_date, left_date) for left_date, right_date in matches)
        evidence_refs = sorted(
            {
                f"trajectory:{canonical_left.item_id}:{left_date.isoformat()}"
                for left_date, _ in ordered_matches
            }
            | {
                f"trajectory:{canonical_right.item_id}:{right_date.isoformat()}"
                for _, right_date in ordered_matches
            }
            | {f"owner:{canonical_left.owner_alias}"}
        )
        first_seen_at = datetime.combine(min(min(left_date, right_date) for left_date, right_date in ordered_matches), datetime.min.time(), tzinfo=as_of.tzinfo)
        last_seen_at = datetime.combine(max(max(left_date, right_date) for left_date, right_date in ordered_matches), datetime.min.time(), tzinfo=as_of.tzinfo)
        proposals.append(
            DependencyProposal(
                id=_build_dependency_proposal_id(
                    canonical_left.item_id,
                    canonical_right.item_id,
                    detection_method="owner_overlap",
                ),
                program_id=program_id,
                from_workstream_id=canonical_left.workstream_id,
                to_workstream_id=canonical_right.workstream_id,
                from_item_id=canonical_left.item_id,
                to_item_id=canonical_right.item_id,
                from_item_title=canonical_left.title,
                to_item_title=canonical_right.title,
                suggested_dependency_type=DependencyType.SHARES_RESOURCE,
                rationale=(
                    f"{canonical_left.title} (WI#{canonical_left.item_id}) and "
                    f"{canonical_right.title} (WI#{canonical_right.item_id}) share owner {canonical_left.owner_alias} "
                    f"and slipped target dates within {co_movement_window_days} days on {len(ordered_matches)} occasions "
                    f"during the last {lookback_days} days."
                ),
                evidence_refs=tuple(evidence_refs),
                detection_method="owner_overlap",
                occurrence_count=len(ordered_matches),
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                confidence=Confidence.HIGH,
            )
        )
    return tuple(proposals)


def _extract_positive_target_shift_events(
    points: tuple[TrajectoryPoint, ...],
    *,
    start: date,
) -> tuple[date, ...]:
    ordered = tuple(sorted(points, key=lambda point: point.date))
    events: list[date] = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.date < start:
            continue
        if previous.target_date is None or current.target_date is None:
            continue
        if current.target_date > previous.target_date:
            events.append(current.date)
    return tuple(events)


def _match_co_movement_events(
    left_events: tuple[date, ...],
    right_events: tuple[date, ...],
    *,
    window_days: int,
) -> tuple[tuple[date, date], ...]:
    matches: list[tuple[date, date]] = []
    right_index = 0
    for left_date in left_events:
        while right_index < len(right_events) and (right_events[right_index] - left_date).days < -window_days:
            right_index += 1
        candidate_index = right_index
        while candidate_index < len(right_events):
            delta_days = abs((right_events[candidate_index] - left_date).days)
            if delta_days <= window_days:
                matches.append((left_date, right_events[candidate_index]))
                right_index = candidate_index + 1
                break
            if right_events[candidate_index] > left_date:
                break
            candidate_index += 1
    return tuple(matches)


def _build_dependency_id(proposal: DependencyProposal) -> str:
    left_item_id, right_item_id = sorted((proposal.from_item_id, proposal.to_item_id))
    return f"dep-scout-{left_item_id}-{right_item_id}"


def _serialize_dependency_proposal(proposal: DependencyProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "program_id": proposal.program_id,
        "from_workstream_id": proposal.from_workstream_id,
        "to_workstream_id": proposal.to_workstream_id,
        "from_item_id": proposal.from_item_id,
        "to_item_id": proposal.to_item_id,
        "from_item_title": proposal.from_item_title,
        "to_item_title": proposal.to_item_title,
        "suggested_dependency_type": proposal.suggested_dependency_type.value,
        "rationale": proposal.rationale,
        "evidence_refs": list(proposal.evidence_refs),
        "detection_method": proposal.detection_method,
        "occurrence_count": proposal.occurrence_count,
        "first_seen_at": proposal.first_seen_at.isoformat(),
        "last_seen_at": proposal.last_seen_at.isoformat(),
        "confidence": proposal.confidence.value,
        "status": proposal.status.value,
    }


def _parse_dependency_proposal(program_id: str, raw_entry: dict[str, Any]) -> DependencyProposal:
    return DependencyProposal(
        id=str(raw_entry.get("id") or "").strip(),
        program_id=str(raw_entry.get("program_id") or program_id).strip() or program_id,
        from_workstream_id=str(raw_entry.get("from_workstream_id") or "").strip(),
        to_workstream_id=str(raw_entry.get("to_workstream_id") or "").strip(),
        from_item_id=int(raw_entry.get("from_item_id") or 0),
        to_item_id=int(raw_entry.get("to_item_id") or 0),
        from_item_title=str(raw_entry.get("from_item_title") or "").strip(),
        to_item_title=str(raw_entry.get("to_item_title") or "").strip(),
        suggested_dependency_type=DependencyType.from_string(str(raw_entry.get("suggested_dependency_type") or "shares_resource")),
        rationale=str(raw_entry.get("rationale") or "").strip(),
        evidence_refs=tuple(str(entry) for entry in raw_entry.get("evidence_refs") or ()),
        detection_method=str(raw_entry.get("detection_method") or "co_mention").strip() or "co_mention",
        occurrence_count=int(raw_entry.get("occurrence_count") or 0),
        first_seen_at=_parse_datetime(raw_entry.get("first_seen_at")) or _utc_now(),
        last_seen_at=_parse_datetime(raw_entry.get("last_seen_at")) or _utc_now(),
        confidence=Confidence.from_string(str(raw_entry.get("confidence") or "medium")),
        status=_parse_status(raw_entry.get("status")),
    )


def _parse_status(value: object) -> DependencyProposalStatus:
    normalized = str(value or DependencyProposalStatus.PROPOSED.value).strip().lower()
    for status in DependencyProposalStatus:
        if status.value == normalized:
            return status
    return DependencyProposalStatus.PROPOSED


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _proposal_sort_key(proposal: DependencyProposal) -> tuple[int, str, str, int, int]:
    status_rank = {
        DependencyProposalStatus.PROPOSED: 0,
        DependencyProposalStatus.ACCEPTED: 1,
        DependencyProposalStatus.DISMISSED: 2,
    }
    return (
        status_rank.get(proposal.status, 99),
        proposal.from_workstream_id,
        proposal.to_workstream_id,
        proposal.from_item_id,
        proposal.to_item_id,
    )


def _hash_proposals(proposals: tuple[DependencyProposal, ...]) -> str:
    body = json.dumps([_serialize_dependency_proposal(proposal) for proposal in proposals], sort_keys=True).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _write_atomic_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _append_feedback_audit(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record_accumulator_signal(accumulator: _ProposalAccumulator, signal_id: str, timestamp: datetime) -> None:
    accumulator.occurrence_count += 1
    normalized_timestamp = _ensure_utc(timestamp)
    if accumulator.first_seen_at is None or normalized_timestamp < accumulator.first_seen_at:
        accumulator.first_seen_at = normalized_timestamp
    if accumulator.last_seen_at is None or normalized_timestamp > accumulator.last_seen_at:
        accumulator.last_seen_at = normalized_timestamp
    if accumulator.evidence_refs is None:
        accumulator.evidence_refs = []
    if signal_id not in accumulator.evidence_refs:
        accumulator.evidence_refs.append(signal_id)


def _extract_dependency_phrases(text: str) -> tuple[str, ...]:
    normalized = text.lower()
    phrases = tuple(
        phrase
        for phrase in ("blocked by", "waiting on", "depends on")
        if phrase in normalized
    )
    return tuple(dict.fromkeys(phrases))


def _normalize_owner_alias(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)