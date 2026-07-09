from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from src.commands.report_detail import _is_presented_scorecard_dimension, _slice_contract_map
from src.commands.report_output import _ado_saved_query_base_url
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index, read_scorecard_history
from src.core.communication_plan import load_communication_plan_entries
from src.core.config_loader import ReportBundle, ScorecardDimensionSettings, ScorecardSettings
from src.core.continuation_contract import load_inherited_scorecard_dimensions
from src.core.dependency_graph import compute_blast_radius, dependency_target_label
from src.core.exceptions import ConfigError
from src.core.jinja_filters import risk_label
from src.core.models import AttributionTier, Confidence, DimensionRisk, EvidencePacket, RiskLevel, ScorecardDelta, ScorecardEvidencePacket, Snapshot, WorkItem
from src.core.models_v2 import Dependency, Scorecard
from src.core.overrides_store import DimensionOverride, OverridesDocument
from src.core.program_reality import ProgramReality
from src.core.scorecard_engine import assign_dimension_items, build_scorecard
from src.core.scorecard_trends import load_scorecard_trends
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.view_models import ScorecardData


_DEPENDENCY_RISK_MAX_HOPS = 3


@dataclass(frozen=True, slots=True)
class _DependencyRiskUplift:
    risk: RiskLevel
    detail: str


def _build_scorecard_packets(
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    previous_snapshot: Snapshot | None,
    *,
    edition_name: str | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    trusted_issue_number: int | None = None,
    overrides_document: OverridesDocument | None = None,
) -> dict[str, dict[str, Any]]:
    packets: dict[str, dict[str, Any]] = {}
    slice_contracts = _slice_contract_map(bundle)
    for scorecard in _effective_scorecard_settings(
        bundle,
        edition_name=edition_name,
        archive_root=archive_root,
        trusted_issue_number=trusted_issue_number,
        overrides_document=overrides_document,
    ):
        packet_list = build_scorecard(
            items=items,
            dimensions=scorecard.dimensions,
            prev_confirmed=previous_snapshot,
            scorecard_name=scorecard.name,
            slice_contracts=slice_contracts,
            ado_query_base_url=_ado_saved_query_base_url(bundle),
            governance=overrides_document.governance if overrides_document is not None else None,
        )
        packets[scorecard.name] = {packet.dimension_name: packet for packet in packet_list}
    if edition_name is not None:
        current_dimensions = {
            (scorecard_name, dimension_name): packet.derived_risk
            for scorecard_name, packet_map in packets.items()
            for dimension_name, packet in packet_map.items()
        }
        trends = load_scorecard_trends(edition_name, current_dimensions, archive_root=archive_root)
        for (scorecard_name, dimension_name), trend in trends.items():
            packet = packets.get(scorecard_name, {}).get(dimension_name)
            if packet is None:
                continue
            packets[scorecard_name][dimension_name] = replace(packet, streak_count=trend.consecutive_high_count)
    return packets


def _build_scorecard_data(
    bundle: ReportBundle,
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
    scorecard_packets: dict[str, dict[str, Any]],
    overrides_document: OverridesDocument,
    *,
    edition_name: str | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    trusted_issue_number: int | None = None,
    program_id: str | None = None,
    programs_root: Path | None = None,
    raw_program: Mapping[str, object] | None = None,
    resolved_scorecards: tuple[Scorecard, ...] = (),
    derive_vector_label: Callable[[RiskLevel, RiskLevel | None, ScorecardEvidencePacket], str],
    derive_risk_sparkline: Callable[[RiskLevel, RiskLevel | None], tuple[str | None, str | None]],
    scorecard_delta_kind: Callable[[RiskLevel, RiskLevel], Any],
    risk_rank: Callable[[RiskLevel], int],
) -> tuple[tuple[ScorecardData, ...], tuple[DimensionRisk, ...], tuple[ScorecardDelta, ...]]:
    override_map = {
        scorecard.name: {dimension.name: dimension for dimension in scorecard.dimensions}
        for scorecard in overrides_document.scorecards
    }
    slice_contracts = _slice_contract_map(bundle)
    dependency_uplifts = _build_dependency_risk_uplifts(
        program_id=program_id,
        programs_root=programs_root,
        raw_program=raw_program,
        scorecards=resolved_scorecards,
        risk_rank=risk_rank,
    )
    scorecards: list[ScorecardData] = []
    all_dimensions: list[DimensionRisk] = []
    deltas: list[ScorecardDelta] = []

    for scorecard in _effective_scorecard_settings(
        bundle,
        edition_name=edition_name,
        archive_root=archive_root,
        trusted_issue_number=trusted_issue_number,
        overrides_document=overrides_document,
    ):
        dimension_models: list[DimensionRisk] = []
        packet_map = scorecard_packets.get(scorecard.name, {})
        for dimension in scorecard.dimensions:
            if not _is_presented_scorecard_dimension(dimension):
                continue
            packet = packet_map[dimension.name]
            override = override_map.get(scorecard.name, {}).get(dimension.name)
            if override is not None and override.hide_from_scorecard:
                continue
            effective_override = override if _is_effective_dimension_override(override) else None
            if effective_override is not None and effective_override.eta is not None:
                packet = replace(packet, author_target_date=effective_override.eta)
                packet_map[dimension.name] = packet
            risk = _resolve_dimension_risk(packet.derived_risk, effective_override)
            dependency_uplift = dependency_uplifts.get(dimension.name)
            dependency_uplift_applied = False
            if (
                dependency_uplift is not None
                and effective_override is None
                and risk_rank(dependency_uplift.risk) > risk_rank(risk)
            ):
                risk = dependency_uplift.risk
                dependency_uplift_applied = True
            matching_items = assign_dimension_items(
                items,
                dimension,
                slice_contract=slice_contracts.get((scorecard.name, dimension.name)),
            ).items
            evidence = _select_dimension_evidence(matching_items, evidence_by_item)
            summary = _resolve_dimension_summary(packet, effective_override)
            if packet.prior_confirmed_risk is not None and evidence.confidence == Confidence.NONE and effective_override is None:
                summary = "No current evidence. Retained from trusted baseline."
                if risk == RiskLevel.UNKNOWN:
                    risk = packet.prior_confirmed_risk
            if dependency_uplift_applied:
                assert dependency_uplift is not None
                summary = f"Dependency risk: {dependency_uplift.detail}. {summary}".strip()
            summary = _apply_scorecard_trend_annotation(summary, risk, packet, risk_rank=risk_rank)
            summary = _apply_dfd_scorecard_annotation(summary, packet)
            risk_sparkline, trend_label = derive_risk_sparkline(risk, packet.prior_confirmed_risk)
            model = DimensionRisk(
                name=dimension.name,
                risk=risk,
                summary=summary,
                evidence=evidence,
                display_name=(effective_override.label if effective_override is not None else None),
                derived_risk=packet.derived_risk,
                override_risk=(effective_override.risk if effective_override is not None else None),
                vector_label=derive_vector_label(risk, packet.prior_confirmed_risk, packet),
                risk_sparkline=risk_sparkline,
                trend_label=trend_label,
                note=(effective_override.note if effective_override is not None else None),
            )
            if (
                evidence.confidence == Confidence.NONE
                and effective_override is None
                and packet.prior_confirmed_risk is None
                and (edition_name is None or trusted_issue_number is None)
            ):
                continue
            dimension_models.append(model)
            all_dimensions.append(model)
            if (
                packet.prior_confirmed_risk is not None
                and packet.prior_confirmed_risk != risk
                and RiskLevel.UNKNOWN not in {packet.prior_confirmed_risk, risk}
            ):
                delta_kind = scorecard_delta_kind(packet.prior_confirmed_risk, risk)
                deltas.append(
                    ScorecardDelta(
                        dimension=dimension.name,
                        old_risk=packet.prior_confirmed_risk,
                        new_risk=risk,
                        delta_kind=delta_kind,
                        summary=summary,
                    )
                )
        scorecard_override = next((s for s in overrides_document.scorecards if s.name == scorecard.name), None)
        scorecards.append(
            ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=_order_dimensions_by_risk(tuple(dimension_models), bundle.config.scorecard_sort),
                footnote=scorecard_override.footnote if scorecard_override is not None else None,
            )
        )

    return tuple(scorecards), tuple(all_dimensions), tuple(deltas)


def _is_effective_dimension_override(override: DimensionOverride | None) -> bool:
    if override is None:
        return False
    return any(
        (
            override.risk is not None,
            bool((override.label or "").strip()),
            bool((override.note or "").strip()),
            bool((override.summary or "").strip()),
            override.eta is not None,
            override.hide_details,
            override.hide_from_scorecard,
        )
    )


def _effective_scorecard_settings(
    bundle: ReportBundle,
    *,
    edition_name: str | None,
    archive_root: Path,
    trusted_issue_number: int | None,
    overrides_document: OverridesDocument | None,
) -> tuple[ScorecardSettings, ...]:
    if edition_name is None or trusted_issue_number is None:
        return bundle.config.scorecards

    inherited_dimensions = load_inherited_scorecard_dimensions(
        edition_name,
        trusted_issue_number,
        archive_root=archive_root,
    )
    if not inherited_dimensions:
        return bundle.config.scorecards

    removed_dimensions = {
        (entry.scorecard_name, entry.dimension_name)
        for entry in (overrides_document.removed_dimensions if overrides_document is not None else ())
    }
    configured_dimension_names: set[tuple[str, str]] = {
        (scorecard.name, dimension.name)
        for scorecard in bundle.config.scorecards
        for dimension in scorecard.dimensions
    }
    inherited_by_scorecard: dict[str, set[str]] = {}
    for scorecard_name, dimension_name in inherited_dimensions:
        if (scorecard_name, dimension_name) not in configured_dimension_names:
            continue  # Dimension was removed from config; don't resurrect it via archive inheritance
        if (scorecard_name, dimension_name) in removed_dimensions:
            continue
        inherited_by_scorecard.setdefault(scorecard_name, set()).add(dimension_name)

    # Explicit per-issue scorecard overrides should participate in composition
    # even when they were not present in prior confirmed scorecard history.
    for override_scorecard in (overrides_document.scorecards if overrides_document is not None else ()):
        scorecard_name = override_scorecard.name
        dimension_names = inherited_by_scorecard.setdefault(scorecard_name, set())
        for override_dimension in override_scorecard.dimensions:
            dimension_name = override_dimension.name
            if (scorecard_name, dimension_name) in removed_dimensions:
                continue
            if _is_effective_dimension_override(override_dimension):
                dimension_names.add(dimension_name)
    if not inherited_by_scorecard:
        return ()

    configured_scorecards = {scorecard.name: scorecard for scorecard in bundle.config.scorecards}
    ordered_scorecard_names = [
        scorecard.name
        for scorecard in bundle.config.scorecards
        if scorecard.name in inherited_by_scorecard
    ]
    ordered_scorecard_names.extend(
        sorted(scorecard_name for scorecard_name in inherited_by_scorecard if scorecard_name not in configured_scorecards)
    )

    effective_scorecards: list[ScorecardSettings] = []
    for scorecard_name in ordered_scorecard_names:
        configured_scorecard = configured_scorecards.get(scorecard_name)
        configured_dimensions = {
            dimension.name: dimension
            for dimension in (configured_scorecard.dimensions if configured_scorecard is not None else ())
        }
        ordered_dimension_names = [
            dimension.name
            for dimension in (configured_scorecard.dimensions if configured_scorecard is not None else ())
            if dimension.name in inherited_by_scorecard[scorecard_name]
        ]
        ordered_dimension_names.extend(
            sorted(
                dimension_name
                for dimension_name in inherited_by_scorecard[scorecard_name]
                if dimension_name not in configured_dimensions
            )
        )
        if not ordered_dimension_names:
            continue
        effective_scorecards.append(
            ScorecardSettings(
                name=scorecard_name,
                dimensions=tuple(
                    configured_dimensions.get(
                        dimension_name,
                        ScorecardDimensionSettings(name=dimension_name, description=None, ado_filter=""),
                    )
                    for dimension_name in ordered_dimension_names
                ),
            )
        )
    return tuple(effective_scorecards)


def _build_dependency_risk_uplifts(
    *,
    program_id: str | None,
    programs_root: Path | None,
    raw_program: Mapping[str, object] | None,
    scorecards: tuple[Scorecard, ...],
    risk_rank: Callable[[RiskLevel], int],
) -> dict[str, _DependencyRiskUplift]:
    if (
        program_id is None
        or programs_root is None
        or not _include_dependency_risk(raw_program)
        or not scorecards
    ):
        return {}

    dimension_workstream_ids = _scorecard_dimension_workstream_ids(scorecards)
    if not dimension_workstream_ids:
        return {}

    dependency_network = _load_reachable_dependency_network(program_id, programs_root=programs_root)
    if not dependency_network:
        return {}

    foreign_program_risk: dict[str, RiskLevel | None] = {}
    uplifts: dict[str, _DependencyRiskUplift] = {}
    for dimension_name, workstream_ids in dimension_workstream_ids.items():
        for workstream_id in workstream_ids:
            for dependency in compute_blast_radius(
                workstream_id,
                dependency_network,
                max_hops=_DEPENDENCY_RISK_MAX_HOPS,
            ):
                counterpart_program_id = dependency.to_program_id.strip()
                if not counterpart_program_id or counterpart_program_id == program_id:
                    continue

                if counterpart_program_id not in foreign_program_risk:
                    foreign_program_risk[counterpart_program_id] = _load_latest_program_overall_risk(
                        counterpart_program_id,
                        programs_root=programs_root,
                    )
                counterpart_risk = foreign_program_risk[counterpart_program_id]
                if counterpart_risk is None:
                    continue

                detail = (
                    f"depends on {dependency_target_label(dependency)}, and {counterpart_program_id}'s "
                    f"latest confirmed issue is {counterpart_risk.name}"
                )
                existing = uplifts.get(dimension_name)
                if existing is not None and risk_rank(existing.risk) >= risk_rank(counterpart_risk):
                    continue
                uplifts[dimension_name] = _DependencyRiskUplift(risk=counterpart_risk, detail=detail)
    return uplifts


def _load_reachable_dependency_network(program_id: str, *, programs_root: Path) -> tuple[Dependency, ...]:
    queue: deque[str] = deque((program_id,))
    visited_program_ids: set[str] = set()
    seen_dependency_keys: set[tuple[str, str]] = set()
    dependencies: list[Dependency] = []

    while queue:
        current_program_id = queue.popleft()
        if current_program_id in visited_program_ids:
            continue
        visited_program_ids.add(current_program_id)

        try:
            # Deliberately not consolidated: this BFS discovers additional
            # program_ids as it traverses the graph, so correctness requires one
            # ProgramReality load per newly visited program. The .record strip is
            # also intentional: blast-radius computation consumes only structural
            # dependency fields (program ids, resolution path), never FactAssessment
            # metadata — truth-level signals are surfaced in the newsletter's
            # structured tables, not in this cross-program risk-uplift graph.
            loaded_dependencies = tuple(
                a.record for a in ProgramReality.load(
                    current_program_id,
                    programs_root=programs_root,
                ).dependencies()
            )
        except ConfigError:
            continue

        for dependency in loaded_dependencies:
            dependency_key = (dependency.from_program_id, dependency.id)
            if dependency_key not in seen_dependency_keys:
                seen_dependency_keys.add(dependency_key)
                dependencies.append(dependency)
            if dependency.to_program_id != current_program_id and dependency.to_program_id not in visited_program_ids:
                queue.append(dependency.to_program_id)

    return tuple(dependencies)


def _include_dependency_risk(raw_program: Mapping[str, object] | None) -> bool:
    if raw_program is None:
        return False
    scorecard_block = raw_program.get("scorecard")
    if not isinstance(scorecard_block, Mapping):
        return False
    return bool(scorecard_block.get("include_dependency_risk", False))


def _scorecard_dimension_workstream_ids(scorecards: tuple[Scorecard, ...]) -> dict[str, tuple[str, ...]]:
    workstream_ids_by_dimension: dict[str, list[str]] = {}
    for scorecard in scorecards:
        for dimension in scorecard.dimensions:
            workstream_id = dimension.workstream_id.strip()
            if not workstream_id:
                continue
            workstream_ids_by_dimension.setdefault(dimension.name, []).append(workstream_id)
    return {
        name: tuple(dict.fromkeys(workstream_ids))
        for name, workstream_ids in workstream_ids_by_dimension.items()
    }


def _load_latest_program_overall_risk(program_id: str, *, programs_root: Path) -> RiskLevel | None:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return None
    try:
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(program_document, dict):
        return None

    archive_root = programs_root / program_id / "archive"
    primary_edition = _select_primary_program_edition(program_document, archive_root)
    if primary_edition is None:
        return None

    latest_confirmed = find_latest_confirmed_entry(read_archive_index(primary_edition, archive_root=archive_root))
    if latest_confirmed is None:
        return None

    current_risks = [
        RiskLevel.from_string(str(entry.get("risk") or ""))
        for entry in read_scorecard_history(primary_edition, archive_root=archive_root)
        if _scorecard_history_issue_number(entry.get("issue_number")) == latest_confirmed.issue_number
        and str(entry.get("risk") or "").strip()
    ]
    if not current_risks:
        return None
    return max(current_risks, key=_foreign_program_risk_rank)


def _select_primary_program_edition(program_document: dict[str, object], archive_root: Path) -> str | None:
    communication_plan_entries = tuple(load_communication_plan_entries(program_document))
    if not archive_root.exists():
        return communication_plan_entries[0].edition if communication_plan_entries else None

    confirmed_editions = {
        edition_dir.name
        for edition_dir in archive_root.iterdir()
        if edition_dir.is_dir()
        and find_latest_confirmed_entry(read_archive_index(edition_dir.name, archive_root=archive_root)) is not None
    }
    if not confirmed_editions:
        return communication_plan_entries[0].edition if communication_plan_entries else None

    for entry in communication_plan_entries:
        if entry.edition in confirmed_editions:
            return entry.edition
    return min(confirmed_editions)


def _scorecard_history_issue_number(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _foreign_program_risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.UNKNOWN: 0,
        RiskLevel.DONE: 1,
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 3,
        RiskLevel.HIGH: 4,
    }[level]


def _resolve_dimension_risk(
    derived_risk: RiskLevel,
    override: DimensionOverride | None,
) -> RiskLevel:
    if override is not None and override.risk is not None:
        return override.risk
    return derived_risk


def _resolve_dimension_summary(packet: Any, override: DimensionOverride | None) -> str:
    if override is not None and override.summary is not None and override.summary.strip():
        return override.summary.strip()
    if packet.total_items == 0:
        return "No in-scope items matched the current filter."
    fragments = [f"{packet.total_items} items tracked"]
    if packet.stale_count:
        fragments.append(f"{packet.stale_count} stale")
    if packet.overdue_count:
        fragments.append(f"{packet.overdue_count} overdue")
    if packet.blocked_count:
        fragments.append(f"{packet.blocked_count} blocked")
    if packet.unowned_count:
        fragments.append(f"{packet.unowned_count} unowned")
    return "; ".join(fragments) + "."


def _is_blocked_dimension(dimension: DimensionRisk) -> bool:
    return dimension.risk == RiskLevel.HIGH and dimension.note is not None and "DFD" in dimension.note


def _dimension_sort_rank(dimension: DimensionRisk) -> int:
    if _is_blocked_dimension(dimension):
        return 5
    return {
        RiskLevel.HIGH: 4,
        RiskLevel.MEDIUM: 3,
        RiskLevel.LOW: 2,
        RiskLevel.UNKNOWN: 1,
        RiskLevel.DONE: 0,
    }.get(dimension.risk, 0)


def _order_dimensions_by_risk(
    dimensions: tuple[DimensionRisk, ...],
    sort_mode: str,
) -> tuple[DimensionRisk, ...]:
    if sort_mode == "fixed":
        return dimensions
    indexed = list(enumerate(dimensions))
    indexed.sort(key=lambda entry: (-_dimension_sort_rank(entry[1]), entry[0]))
    return tuple(dimension for _index, dimension in indexed)


def _apply_scorecard_trend_annotation(
    summary: str,
    risk: RiskLevel,
    packet: ScorecardEvidencePacket,
    *,
    risk_rank: Callable[[RiskLevel], int],
) -> str:
    annotation: str | None = None
    if risk == RiskLevel.HIGH and packet.streak_count >= 3:
        annotation = f"High for {packet.streak_count} consecutive issues."
    elif (
        packet.prior_confirmed_risk is not None
        and packet.prior_confirmed_risk not in {RiskLevel.UNKNOWN, risk}
        and risk != RiskLevel.UNKNOWN
    ):
        if risk_rank(risk) < risk_rank(packet.prior_confirmed_risk):
            annotation = f"Improved from {risk_label(packet.prior_confirmed_risk)} to {risk_label(risk)}."
        elif risk_rank(risk) > risk_rank(packet.prior_confirmed_risk):
            annotation = f"Worsened from {risk_label(packet.prior_confirmed_risk)} to {risk_label(risk)}."
    if annotation is None:
        return summary
    return f"{annotation} {summary}".strip()


def _apply_dfd_scorecard_annotation(summary: str, packet: ScorecardEvidencePacket) -> str:
    """Prefix DFD proximity and LT escalation annotations onto a dimension summary.

    The annotations originate in ``scorecard_engine.build_scorecard`` (governance-aware).
    """
    badges = [badge for badge in (packet.dfd_annotation, packet.escalation_badge) if badge]
    if not badges:
        return summary
    prefix = " ".join(badges)
    return f"{prefix} {summary}".strip() if summary else prefix


def _select_dimension_evidence(
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
) -> EvidencePacket:
    if not items:
        return EvidencePacket(
            work_item_id=0,
            revisions=(),
            comments=(),
            enrichments=(),
            confidence=Confidence.NONE,
            tier=AttributionTier.TIER3,
            summary_for_reviewer="No evidence in selected window.",
        )
    ranked_items = sorted(
        items,
        key=lambda item: (_confidence_rank(evidence_by_item.get(item.id)), item.id),
        reverse=True,
    )
    selected = ranked_items[0]
    return evidence_by_item.get(
        selected.id,
        EvidencePacket(
            work_item_id=selected.id,
            revisions=(),
            comments=(),
            enrichments=(),
            confidence=Confidence.NONE,
            tier=AttributionTier.TIER3,
            summary_for_reviewer="No evidence in selected window.",
        ),
    )


def _confidence_rank(evidence: EvidencePacket | None) -> int:
    if evidence is None:
        return -1
    ranking = {
        Confidence.HIGH: 3,
        Confidence.MEDIUM: 2,
        Confidence.LOW: 1,
        Confidence.NONE: 0,
    }
    return ranking[evidence.confidence]