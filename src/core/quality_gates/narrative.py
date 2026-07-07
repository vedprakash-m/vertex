"""Narrative-focused phase-1b gates extracted from ``src/core/quality_gates``."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from src.core.ado_reconcile import build_ado_reconcile_report
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.claim_tracker import load_open_claims
from src.core.config_loader import NarrativeProgramContext
from src.core.contradiction_engine import build_contradiction_packets
from src.core.models import DeltaKind, DeltaSet, DimensionRisk, RiskLevel, WorkItem
from src.core.models_v2 import Scorecard, Signal, Workstream
from src.core.narrative_store import load_archived_narratives
from src.core.overrides_store import OverridesDocument
from src.core.quality_gates.models import GateEvaluation

_WI_REF_PATTERN = re.compile(r"\bWI:(\d+)\b", re.IGNORECASE)
_NEXT_ACTION_RE = re.compile(
    r"\b(next step|next best action|follow up|follow-up|need to|needs to|must|confirm|resolve|close|mitigate|escalate|deliver|ship|land|track|owner|due\s+20\d{2}-\d{2}-\d{2}|by\s+20\d{2}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)


def evaluate_material_change_narrative_gate(
    *,
    items: tuple[WorkItem, ...],
    deltas: DeltaSet | None,
    edition_name: str | None,
    issue_number: int | None,
    workstream_blurbs: Mapping[str, str] | None,
    program_context: NarrativeProgramContext | None,
    archive_root: Path,
) -> GateEvaluation:
    if (
        deltas is None
        or edition_name is None
        or issue_number is None
        or program_context is None
        or not workstream_blurbs
    ):
        return GateEvaluation("QG-10", True, "Material-change narrative gate passed.", 2, forceable=True)

    previous_entry = find_latest_confirmed_entry(
        read_archive_index(edition_name, archive_root=archive_root),
        before_issue_number=issue_number,
    )
    if previous_entry is None:
        return GateEvaluation("QG-10", True, "Material-change narrative gate passed.", 2, forceable=True)

    archived_narratives = load_archived_narratives(edition_name, previous_entry.issue_number, archive_root=archive_root)
    if not archived_narratives:
        return GateEvaluation("QG-10", True, "Material-change narrative gate passed.", 2, forceable=True)

    items_by_id = {item.id: item for item in items}
    affected_workstreams: set[str] = set()
    material_deltas = tuple(
        delta
        for delta in (*deltas.risk_changes, *deltas.eta_changes)
        if delta.kind in {DeltaKind.RISK_UP, DeltaKind.ETA_CHANGED}
    )
    for delta in material_deltas:
        item = items_by_id.get(delta.work_item_id)
        if item is None:
            continue
        workstream_name = _matching_workstream_name(item, program_context)
        if workstream_name is None:
            continue
        section_id = _build_section_id(workstream_name)
        current_blurb = _current_workstream_blurb(workstream_blurbs, workstream_name, section_id)
        previous_blurb = _previous_workstream_blurb(archived_narratives, section_id)
        if not current_blurb or not previous_blurb:
            continue
        if _normalize_comparable_text(current_blurb) == _normalize_comparable_text(previous_blurb):
            affected_workstreams.add(workstream_name)

    if not affected_workstreams:
        return GateEvaluation("QG-10", True, "Material-change narrative gate passed.", 2, forceable=True)
    return GateEvaluation(
        "QG-10",
        False,
        "Narrative unchanged for workstreams with material changes since the last confirmed issue: "
        + ", ".join(sorted(affected_workstreams)),
        2,
        forceable=True,
    )


def evaluate_claim_contradiction_gate(
    *,
    items: tuple[WorkItem, ...],
    program_id: str | None,
    program_maturity_level: int,
    workstreams: tuple[Workstream, ...],
    programs_root: Path,
) -> GateEvaluation:
    forceable = _forceable_below_level(program_maturity_level, threshold=2)
    if program_id is None or not items or not workstreams:
        return GateEvaluation("QG-11", True, "Claim contradiction gate passed.", 2, forceable=forceable)

    discrepancies = tuple(
        discrepancy
        for discrepancy in build_ado_reconcile_report(
            program_id=program_id,
            items=items,
            workstreams=workstreams,
            scorecards=(),
            overrides_document=None,
            open_claims=load_open_claims(program_id, programs_root=programs_root),
        ).discrepancies
        if discrepancy.kind in {"claim_eta", "workstream_area"}
    )
    if not discrepancies:
        return GateEvaluation("QG-11", True, "Claim contradiction gate passed.", 2, forceable=forceable)

    labels = ", ".join(
        f"{discrepancy.context} (WI:{discrepancy.work_item_id})"
        for discrepancy in discrepancies[:5]
    )
    if len(discrepancies) > 5:
        labels = f"{labels}, and {len(discrepancies) - 5} more"
    return GateEvaluation(
        "QG-11",
        False,
        f"Open claims contradicted by current ADO state without acknowledgment: {labels}",
        2,
        forceable=forceable,
    )


def evaluate_contradiction_narrative_gate(
    *,
    items: tuple[WorkItem, ...],
    approved_signals: tuple[Signal, ...],
    workstream_blurbs: Mapping[str, str] | None,
    narratives: Mapping[str, str] | Iterable[str],
    as_of: datetime,
    program_id: str | None,
    workstreams: tuple[Workstream, ...],
    programs_root: Path,
) -> GateEvaluation:
    if program_id is None or not items or not workstreams:
        return GateEvaluation("QG-17", True, "Contradiction narrative gate passed.", 3, forceable=True)

    contradiction_packets = build_contradiction_packets(
        items=items,
        claims=load_open_claims(program_id, programs_root=programs_root),
        signals=approved_signals,
        workstreams=workstreams,
        as_of=as_of,
    )
    if not contradiction_packets:
        return GateEvaluation("QG-17", True, "Contradiction narrative gate passed.", 3, forceable=True)

    workstreams_by_id = {workstream.id: workstream for workstream in workstreams}
    failing_workstreams: list[str] = []
    for workstream_id, packets in _group_contradictions_by_workstream(contradiction_packets).items():
        if len(packets) < 2:
            continue
        workstream = workstreams_by_id.get(workstream_id)
        if workstream is None:
            continue
        contradicted_item_ids = tuple(packet.work_item_id for packet in packets)
        narrative_refs = _extract_narrative_work_item_ids(
            _workstream_narrative_text(
                workstream=workstream,
                workstream_blurbs=workstream_blurbs or {},
                narratives=narratives,
            )
        )
        if set(contradicted_item_ids).intersection(narrative_refs):
            continue
        failing_workstreams.append(
            f"{workstream.name} ({_preview_work_item_ids(list(contradicted_item_ids))})"
        )

    if not failing_workstreams:
        return GateEvaluation("QG-17", True, "Contradiction narrative gate passed.", 3, forceable=True)
    return GateEvaluation(
        "QG-17",
        False,
        "Contradiction exists between ADO and approved signals or open claims for multiple work items without any narrative acknowledgment: "
        + "; ".join(failing_workstreams),
        3,
        forceable=True,
    )


def evaluate_high_risk_next_action_gate(
    *,
    dimension_risks: tuple[DimensionRisk, ...],
    overrides_document: OverridesDocument | None,
    workstream_blurbs: Mapping[str, str] | None,
    scorecards: tuple[Scorecard, ...],
    workstreams: tuple[Workstream, ...],
) -> GateEvaluation:
    high_dimensions = tuple(dimension for dimension in dimension_risks if dimension.risk == RiskLevel.HIGH)
    if not high_dimensions:
        return GateEvaluation("QG-14", True, "High-risk dimensions have next-best-action coverage.", 2, forceable=True)

    dimension_workstream_ids = _dimension_workstream_ids(scorecards)
    workstream_names = {workstream.id: workstream.name for workstream in workstreams}
    failing_dimensions: list[str] = []

    for dimension in high_dimensions:
        override_texts = _dimension_override_texts(overrides_document, dimension.name)
        if any(_contains_next_best_action(text) for text in override_texts):
            continue

        linked_workstream_names = tuple(
            workstream_names[workstream_id]
            for workstream_id in dimension_workstream_ids.get(dimension.name, ())
            if workstream_id in workstream_names
        )
        if any(
            _contains_next_best_action(
                _current_workstream_blurb(workstream_blurbs or {}, workstream_name, _build_section_id(workstream_name))
            )
            for workstream_name in linked_workstream_names
        ):
            continue
        failing_dimensions.append(dimension.name)

    if not failing_dimensions:
        return GateEvaluation("QG-14", True, "High-risk dimensions have next-best-action coverage.", 2, forceable=True)
    return GateEvaluation(
        "QG-14",
        False,
        "High-risk dimensions missing a next-best action in overrides or narratives: " + ", ".join(sorted(failing_dimensions)),
        2,
        forceable=True,
    )


def _forceable_below_level(program_maturity_level: int, *, threshold: int) -> bool:
    return program_maturity_level < threshold


def _group_contradictions_by_workstream(packets: tuple[Any, ...]) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for packet in packets:
        if not getattr(packet, "workstream_id", None):
            continue
        grouped.setdefault(packet.workstream_id, []).append(packet)
    return {workstream_id: tuple(entries) for workstream_id, entries in grouped.items()}


def _workstream_narrative_text(
    *,
    workstream: Workstream,
    workstream_blurbs: Mapping[str, str],
    narratives: Mapping[str, str] | Iterable[str],
) -> str:
    section_id = _build_section_id(workstream.name)
    blurb = _current_workstream_blurb(workstream_blurbs, workstream.name, section_id)
    if blurb:
        return blurb
    if isinstance(narratives, Mapping):
        return (
            narratives.get(f"ws_{section_id}.md")
            or narratives.get(f"chapter_{section_id}.md")
            or narratives.get(section_id)
            or ""
        )
    return "\n".join(str(entry) for entry in narratives)


def _extract_narrative_work_item_ids(text: str) -> set[int]:
    return {int(match.group(1)) for match in _WI_REF_PATTERN.finditer(text)}


def _preview_work_item_ids(item_ids: list[int]) -> str:
    preview = ", ".join(f"WI:{work_item_id}" for work_item_id in item_ids[:5])
    if len(item_ids) > 5:
        preview = f"{preview}, and {len(item_ids) - 5} more"
    return preview


def _matching_workstream_name(item: WorkItem, program_context: NarrativeProgramContext) -> str | None:
    normalized_area_path = _normalize_area_path(item.area_path)
    matched_name: str | None = None
    matched_prefix_length = -1
    for workstream in program_context.workstreams:
        for area_path in workstream.area_paths:
            normalized_prefix = _normalize_area_path(area_path)
            if not normalized_prefix:
                continue
            if normalized_area_path == normalized_prefix or normalized_area_path.startswith(f"{normalized_prefix}\\"):
                if len(normalized_prefix) > matched_prefix_length:
                    matched_name = workstream.name
                    matched_prefix_length = len(normalized_prefix)
    return matched_name


def _normalize_area_path(value: str) -> str:
    return value.strip().replace("/", "\\").rstrip("\\").lower()


def _build_section_id(value: str | None) -> str:
    if not value:
        return "section"
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return normalized.strip("-") or "section"


def _current_workstream_blurb(workstream_blurbs: Mapping[str, str], workstream_name: str, section_id: str) -> str:
    return (workstream_blurbs.get(section_id) or workstream_blurbs.get(workstream_name) or "").strip()


def _previous_workstream_blurb(archived_narratives: Mapping[str, str], section_id: str) -> str:
    return (
        archived_narratives.get(f"ws_{section_id}.md")
        or archived_narratives.get(f"chapter_{section_id}.md")
        or ""
    ).strip()


def _normalize_comparable_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _dimension_override_texts(overrides_document: OverridesDocument | None, dimension_name: str) -> tuple[str, ...]:
    if overrides_document is None:
        return ()
    texts: list[str] = []
    for scorecard in overrides_document.scorecards:
        for dimension in scorecard.dimensions:
            if dimension.name != dimension_name:
                continue
            if dimension.note:
                texts.append(dimension.note)
            if dimension.summary:
                texts.append(dimension.summary)
    return tuple(texts)


def _contains_next_best_action(value: str) -> bool:
    normalized = value.strip()
    if len(normalized) < 12:
        return False
    return _NEXT_ACTION_RE.search(normalized) is not None


def _dimension_workstream_ids(scorecards: tuple[Scorecard, ...]) -> dict[str, tuple[str, ...]]:
    mapping: dict[str, set[str]] = {}
    for scorecard in scorecards:
        for dimension in scorecard.dimensions:
            mapping.setdefault(dimension.name, set()).add(dimension.workstream_id)
    return {name: tuple(sorted(workstream_ids)) for name, workstream_ids in mapping.items()}
