from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Any, Callable

from src.ai.provider import LLMProvider
from src.ai.prompt_registry import load_prompt
from src.ai.tiered_router import route_through_tiers
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.ban_list_validator import PolicyProfile
from src.core.assumption_tracker import check_validation_due
from src.core.charter import CharterSuccessCriterion, DimensionMaxRiskMetric, ItemCountMaxMetric, normalize_charter_values, parse_charter_success_criteria
from src.core.claim_tracker import assess_claim_entries, load_claim_entries, load_latest_claim_statuses
from src.core.incident_learning_synthesizer import build_incident_class_patterns, build_incident_ref_patterns
from src.core.models_v2 import IncidentEntry
from src.ai._pipeline import process_generated_text
from src.core.incident_journal_store import read_incident_entries
from src.core.jinja_filters import risk_label
from src.core.models import AttributionTier, Confidence, ConfirmedDimension, DeltaKind, DimensionRisk, EvidencePacket, RiskLevel, ScorecardDelta, ScorecardEvidencePacket, Snapshot, SnapshotItem, WorkItem
from src.core.models_v2 import Assumption, AssumptionStatus
from src.core.program_fact_store import load_current_assumptions
from src.core.program_reality import FactAssessment, ProgramReality
from src.core.stages.sor_gated_load import sor_gated_family_load
from src.core.truth_levels import TruthLevel
from src.core.snapshot_store import read_snapshot
from src.core.view_models import AssumptionLifecycleRow, AssumptionLifecycleSummary, CharterReviewRow, CharterReviewSummary, IncidentLearningRow, IncidentLearningSummary, RetrospectiveIntelligenceRow, RetrospectiveIntelligenceSummary, ScorecardData

log = logging.getLogger(__name__)

_ALLOW_LEGACY_ASSUMPTION_ROLLBACK_ENV = "VERTEX_REPORT_ALLOW_LEGACY_ASSUMPTION_ROLLBACK"


def _load_lookback_snapshots(
    *,
    archive_index: Any,
    archive_root: Path,
    as_of: datetime,
    lookback_range: int | None,
    lookback_days: int | None,
) -> tuple[Snapshot, ...]:
    confirmed_entries = sorted(
        (entry for entry in archive_index.issues if entry.kind == "confirmed" and entry.snapshot_path),
        key=lambda entry: entry.generated_at,
    )
    if lookback_range is not None:
        selected_entries = confirmed_entries[-lookback_range:]
    elif lookback_days is not None and lookback_days > 0:
        window_start = as_of - timedelta(days=lookback_days)
        selected_entries = [
            entry
            for entry in confirmed_entries
            if window_start <= entry.generated_at <= as_of
        ]
    else:
        selected_entries = confirmed_entries[-2:]
    snapshots: list[Snapshot] = []
    for entry in selected_entries:
        snapshot_path = Path(entry.snapshot_path)
        if snapshot_path.exists():
            snapshots.append(read_snapshot(snapshot_path))
    return tuple(snapshots)


def _build_lookback_items(
    snapshots: tuple[Snapshot, ...],
    *,
    fetched_at: datetime,
) -> tuple[WorkItem, ...]:
    latest_snapshot = snapshots[-1]
    latest_item_lookup = {item.id: item for item in latest_snapshot.items}
    lookback_items: dict[int, WorkItem] = {}
    for item_id, history in _collect_lookback_item_histories(snapshots).items():
        latest_known_item = history[-1]
        if item_id in latest_item_lookup:
            lookback_items[item_id] = _snapshot_item_to_work_item(
                latest_item_lookup[item_id],
                fetched_at=fetched_at,
            )
            continue
        lookback_items[item_id] = _snapshot_item_to_work_item(
            latest_known_item,
            fetched_at=fetched_at,
            state_override="Closed",
            risk_override=RiskLevel.DONE,
        )
    return tuple(sorted(lookback_items.values(), key=lambda item: item.id))


def _collect_lookback_item_histories(
    snapshots: tuple[Snapshot, ...],
) -> dict[int, list[SnapshotItem]]:
    histories: dict[int, list[SnapshotItem]] = {}
    for snapshot in snapshots:
        for item in snapshot.items:
            histories.setdefault(item.id, []).append(item)
    return histories


def _snapshot_item_to_work_item(
    item: SnapshotItem,
    *,
    fetched_at: datetime,
    state_override: str | None = None,
    risk_override: RiskLevel | None = None,
) -> WorkItem:
    return WorkItem(
        id=item.id,
        type=item.type,
        title=item.title,
        state=state_override or item.state,
        assigned_to=item.assigned_to,
        assigned_to_email=None,
        area_path=item.area_path,
        iteration_path="Lookback",
        target_date=item.target_date,
        risk_level=risk_override or item.risk_level,
        tags=list(item.tags),
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=fetched_at,
    )


def _build_lookback_scorecard_data(
    snapshots: tuple[Snapshot, ...],
    *,
    scorecard_delta_kind: Callable[[RiskLevel, RiskLevel], DeltaKind],
) -> tuple[
    tuple[ScorecardData, ...],
    tuple[DimensionRisk, ...],
    tuple[ScorecardDelta, ...],
    dict[str, dict[str, ScorecardDelta]],
    dict[str, dict[str, ScorecardEvidencePacket]],
    dict[str, str],
]:
    latest_snapshot = snapshots[-1]
    history_by_dimension: dict[tuple[str, str], list[ConfirmedDimension]] = {}
    for snapshot in snapshots:
        for dimension in snapshot.scorecards:
            history_by_dimension.setdefault((dimension.scorecard_name, dimension.name), []).append(dimension)

    scorecard_order: list[str] = []
    scorecards_by_name: dict[str, list[DimensionRisk]] = {}
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]] = {}
    grouped_deltas: dict[str, dict[str, ScorecardDelta]] = {}
    scorecard_urls: dict[str, str] = {}
    all_dimensions: list[DimensionRisk] = []
    deltas: list[ScorecardDelta] = []

    for latest_dimension in latest_snapshot.scorecards:
        scorecard_name = latest_dimension.scorecard_name
        if scorecard_name not in scorecards_by_name:
            scorecard_order.append(scorecard_name)
            scorecards_by_name[scorecard_name] = []
        history = history_by_dimension.get((scorecard_name, latest_dimension.name), [latest_dimension])
        prior_dimension = history[0]
        trend_summary = " -> ".join(risk_label(entry.risk) for entry in history)
        summary = f"Trend across archived issues: {trend_summary}."
        model = DimensionRisk(
            name=latest_dimension.name,
            risk=latest_dimension.risk,
            summary=summary,
            evidence=_build_lookback_evidence(
                work_item_id=latest_snapshot.issue_number,
                summary=summary,
            ),
            derived_risk=latest_dimension.risk,
        )
        scorecards_by_name[scorecard_name].append(model)
        all_dimensions.append(model)
        scorecard_packets.setdefault(scorecard_name, {})[latest_dimension.name] = ScorecardEvidencePacket(
            dimension_name=latest_dimension.name,
            dimension_description=summary,
            total_items=latest_dimension.item_count,
            items_by_risk={latest_dimension.risk.value: latest_dimension.item_count},
            stale_items=(),
            stale_count=0,
            overdue_items=(),
            overdue_count=0,
            blocked_items=(),
            blocked_count=0,
            unowned_items=(),
            unowned_count=0,
            high_activity_items=(),
            prior_confirmed_risk=prior_dimension.risk,
            author_risk=latest_dimension.risk,
            derived_risk=latest_dimension.risk,
            ado_query_url=latest_dimension.ado_query_url,
            item_links=(),
        )
        if latest_dimension.ado_query_url and scorecard_name not in scorecard_urls:
            scorecard_urls[scorecard_name] = latest_dimension.ado_query_url
        if prior_dimension.risk != latest_dimension.risk and RiskLevel.UNKNOWN not in {prior_dimension.risk, latest_dimension.risk}:
            delta = ScorecardDelta(
                dimension=latest_dimension.name,
                old_risk=prior_dimension.risk,
                new_risk=latest_dimension.risk,
                delta_kind=scorecard_delta_kind(prior_dimension.risk, latest_dimension.risk),
                summary=summary,
            )
            deltas.append(delta)
            grouped_deltas.setdefault(scorecard_name, {})[latest_dimension.name] = delta

    scorecards = tuple(
        ScorecardData(scorecard_name=scorecard_name, dimensions=tuple(scorecards_by_name[scorecard_name]))
        for scorecard_name in scorecard_order
    )
    return (
        scorecards,
        tuple(all_dimensions),
        tuple(deltas),
        grouped_deltas,
        scorecard_packets,
        scorecard_urls,
    )


def _build_lookback_exec_summary(
    *,
    snapshots: tuple[Snapshot, ...],
    items: tuple[WorkItem, ...],
    deltas: Any,
    scorecard_deltas: tuple[ScorecardDelta, ...],
) -> str:
    baseline_snapshot = snapshots[0]
    latest_snapshot = snapshots[-1]
    histories = _collect_lookback_item_histories(snapshots)
    baseline_ids = {item.id for item in baseline_snapshot.items}
    latest_ids = {item.id for item in latest_snapshot.items}
    current_lookup = {item.id: item for item in items}
    persistent_risk_titles = [
        current_lookup[item_id].title
        for item_id in sorted(baseline_ids & latest_ids)
        if current_lookup[item_id].risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}
    ]
    resolved_titles = [
        current_lookup[item_id].title
        for item_id in sorted(set(histories) - latest_ids)
        if item_id in current_lookup
    ]
    unresolved_escalations: list[str] = []
    resolved_escalations: list[str] = []
    chronic_drifters: list[str] = []
    for item_id, history in histories.items():
        escalated_to_high = any(
            previous.risk_level != RiskLevel.HIGH and current.risk_level == RiskLevel.HIGH
            for previous, current in zip(history, history[1:])
        )
        if escalated_to_high:
            title = current_lookup[item_id].title if item_id in current_lookup else history[-1].title
            if item_id in latest_ids and current_lookup[item_id].risk_level == RiskLevel.HIGH:
                unresolved_escalations.append(title)
            else:
                resolved_escalations.append(title)

        slip_count = sum(
            1
            for previous, current in zip(history, history[1:])
            if previous.target_date is not None
            and current.target_date is not None
            and current.target_date > previous.target_date
        )
        if slip_count >= 3:
            chronic_drifters.append(current_lookup[item_id].title if item_id in current_lookup else history[-1].title)

    worsened_workstreams = sum(1 for delta in scorecard_deltas if delta.delta_kind == DeltaKind.RISK_UP)
    improved_workstreams = sum(1 for delta in scorecard_deltas if delta.delta_kind == DeltaKind.RISK_DOWN)
    summary = (
        f"Retrospective across issues {baseline_snapshot.issue_number:03d}-{latest_snapshot.issue_number:03d}: "
        f"{len(histories)} items tracked, {len(resolved_titles)} resolved, {len(deltas.new_items)} newly introduced by the latest snapshot, "
        f"{len(unresolved_escalations)} unresolved high-risk escalations, and {len(chronic_drifters)} chronic drifters."
    )
    summary += f" Workstream risk movements: {worsened_workstreams} worsened and {improved_workstreams} improved."
    if unresolved_escalations:
        summary += f" Unresolved escalations: {', '.join(unresolved_escalations[:3])}."
    if resolved_escalations:
        summary += f" Resolved escalations: {', '.join(resolved_escalations[:3])}."
    if chronic_drifters:
        summary += f" Chronic drifters: {', '.join(chronic_drifters[:3])}."
    if persistent_risk_titles:
        summary += f" Persistent risks: {', '.join(persistent_risk_titles[:3])}."
    if resolved_titles:
        summary += f" Resolved items: {', '.join(resolved_titles[:3])}."
    return summary


def _build_lookback_retrospective_intelligence(
    *,
    program_id: str,
    edition_id: str,
    programs_root: Path,
    snapshots: tuple[Snapshot, ...],
    items: tuple[WorkItem, ...],
    scorecard_deltas: tuple[ScorecardDelta, ...],
    charter_review: CharterReviewSummary | None = None,
) -> RetrospectiveIntelligenceSummary | None:
    if not snapshots:
        return None

    chronic_rows: list[RetrospectiveIntelligenceRow] = []
    recovered_rows: list[RetrospectiveIntelligenceRow] = []
    drift_rows: list[RetrospectiveIntelligenceRow] = []

    dimension_histories: dict[tuple[str, str], list[tuple[int, RiskLevel]]] = {}
    for snapshot in snapshots:
        for dimension in snapshot.scorecards:
            key = (dimension.scorecard_name, dimension.name)
            dimension_histories.setdefault(key, []).append((snapshot.issue_number, dimension.risk))

    for (scorecard_name, dimension_name), history in sorted(
        dimension_histories.items(),
        key=lambda entry: (entry[0][0].lower(), entry[0][1].lower()),
    ):
        high_count = sum(1 for _, risk in history if risk is RiskLevel.HIGH)
        if high_count * 2 <= len(history):
            continue

        latest_issue_number, latest_risk = history[-1]
        trend_summary = " -> ".join(risk_label(risk) for _, risk in history)
        detail = (
            f"High in {high_count} of {len(history)} issues; latest risk {risk_label(latest_risk)} "
            f"in Issue {latest_issue_number:03d}; trend {trend_summary}."
        )
        row = RetrospectiveIntelligenceRow(
            category="Recovered chronic issue"
            if latest_risk in {RiskLevel.LOW, RiskLevel.DONE}
            else "Chronic workstream",
            title=f"{dimension_name} ({scorecard_name})",
            detail=detail,
        )
        if latest_risk in {RiskLevel.LOW, RiskLevel.DONE}:
            recovered_rows.append(row)
        else:
            chronic_rows.append(row)

    current_lookup = {item.id: item for item in items}
    for item_id, item_history in sorted(_collect_lookback_item_histories(snapshots).items()):
        slip_count = sum(
            1
            for previous, current in zip(item_history, item_history[1:])
            if previous.target_date is not None
            and current.target_date is not None
            and current.target_date > previous.target_date
        )
        if slip_count < 3:
            continue

        latest_item = item_history[-1]
        current_item = current_lookup.get(item_id)
        target_label = latest_item.target_date.isoformat() if latest_item.target_date is not None else "none"
        drift_rows.append(
            RetrospectiveIntelligenceRow(
                category="Recurring drift",
                title=current_item.title if current_item is not None else latest_item.title,
                detail=(
                    f"Target slipped {slip_count} times; latest target {target_label}; "
                    f"latest risk {risk_label(latest_item.risk_level)}; latest state {latest_item.state}."
                ),
            )
        )

    claim_rows = _build_lookback_claim_accuracy_rows(
        program_id=program_id,
        edition_id=edition_id,
        programs_root=programs_root,
        snapshots=snapshots,
        items=items,
    )
    charter_rows = _build_lookback_charter_evaluation_rows(charter_review)

    rows = tuple((*chronic_rows, *recovered_rows, *drift_rows, *claim_rows, *charter_rows))
    if not rows:
        return None

    return RetrospectiveIntelligenceSummary(
        chronic_workstream_count=len(chronic_rows),
        recovered_workstream_count=len(recovered_rows),
        recurring_drift_count=len(drift_rows),
        worsened_workstream_count=sum(1 for delta in scorecard_deltas if delta.delta_kind == DeltaKind.RISK_UP),
        improved_workstream_count=sum(1 for delta in scorecard_deltas if delta.delta_kind == DeltaKind.RISK_DOWN),
        claim_accuracy_signal_count=len(claim_rows),
        charter_evaluation_signal_count=len(charter_rows),
        rows=rows,
    )


def _build_lookback_incident_learning_summary(
    *,
    program_id: str,
    programs_root: Path,
    snapshots: tuple[Snapshot, ...],
) -> IncidentLearningSummary | None:
    if not program_id or not snapshots:
        return None

    window_start = snapshots[0].ado_data_as_of.date()
    window_end = snapshots[-1].ado_data_as_of.date()
    incident_entries: list[IncidentEntry] = []
    rows: list[IncidentLearningRow] = []
    attributed_incident_count = 0
    for entry in read_incident_entries(program_id, programs_root=programs_root):
        observed_date = entry.observed_at.date()
        if observed_date < window_start or observed_date > window_end:
            continue
        incident_entries.append(entry)
        if entry.ado_entity_refs:
            attributed_incident_count += 1
        detail_parts = [f"observed {observed_date.isoformat()}"]
        if entry.severity is not None:
            detail_parts.append(f"sev {entry.severity}")
        if entry.owning_team:
            detail_parts.append(f"team {entry.owning_team}")
        if entry.ado_entity_refs:
            detail_parts.append(f"refs {', '.join(entry.ado_entity_refs)}")
        detail_parts.append(entry.belief_change_summary)
        detail_parts.append(f"{entry.confidence.value.lower()} confidence")
        rows.append(
            IncidentLearningRow(
                title=f"IcM {entry.incident_id}",
                detail=" | ".join(detail_parts),
                attributed=bool(entry.ado_entity_refs),
            )
        )

    if not rows:
        return None

    rows.sort(key=lambda row: (not row.attributed, row.title))
    return IncidentLearningSummary(
        window_start=window_start,
        window_end=window_end,
        incident_count=len(rows),
        attributed_incident_count=attributed_incident_count,
        rows=tuple(rows),
        attributed_patterns=_build_incident_attributed_patterns(incident_entries),
    )


def build_lookback_ban_list_inputs(
    *,
    html_body: str,
    markdown_body: str,
    exec_summary_text: str,
    incident_learning: IncidentLearningSummary | None,
) -> tuple[dict[str, str], dict[str, PolicyProfile]]:
    rendered_strings = {
        "html": _strip_incident_learning_html(html_body),
        "markdown": _strip_incident_learning_markdown(markdown_body),
        "exec_summary": exec_summary_text,
    }
    location_profiles: dict[str, PolicyProfile] = {}
    if incident_learning is None:
        return rendered_strings, location_profiles

    for index, pattern in enumerate(incident_learning.attributed_patterns, start=1):
        location = f"incident_learning:pattern:{index}"
        rendered_strings[location] = pattern
        location_profiles[location] = PolicyProfile.RETROSPECTIVE
    for index, row in enumerate(incident_learning.rows, start=1):
        location = f"incident_learning:row:{index}"
        rendered_strings[location] = f"{row.title} - {row.detail}"
        if row.attributed:
            location_profiles[location] = PolicyProfile.RETROSPECTIVE
    return rendered_strings, location_profiles


def _build_incident_attributed_patterns(entries: list[IncidentEntry]) -> tuple[str, ...]:
    references: dict[str, list[IncidentEntry]] = {}
    for entry in entries:
        refs = tuple(dict.fromkeys(entry.ado_entity_refs))
        if not refs:
            continue
        for ref in refs:
            references.setdefault(ref, []).append(entry)

    patterns: list[str] = []
    for class_pattern in build_incident_class_patterns(tuple(entries)):
        incident_refs = ", ".join(class_pattern.incident_refs)
        linked_refs = f" Refs: {', '.join(class_pattern.linked_refs)}." if class_pattern.linked_refs else ""
        patterns.append(
            f"incident class {class_pattern.class_label} recurred across {class_pattern.entry_count} incident learnings ({incident_refs}): "
            f"{class_pattern.summary_text}{linked_refs} ({class_pattern.confidence.value.lower()} confidence)"
        )
    for ref_pattern in build_incident_ref_patterns(tuple(entries)):
        incident_refs = ", ".join(ref_pattern.incident_refs)
        if ref_pattern.entry_count == 1:
            patterns.append(
                f"{ref_pattern.ref} was implicated in {incident_refs}: {ref_pattern.summary_text} "
                f"({ref_pattern.confidence.value.lower()} confidence)"
            )
        else:
            patterns.append(
                f"{ref_pattern.ref} recurred across {ref_pattern.entry_count} incident learnings ({incident_refs}): {ref_pattern.summary_text} "
                f"({ref_pattern.confidence.value.lower()} confidence)"
            )
    return tuple(patterns[:3])


def _strip_incident_learning_html(html_body: str) -> str:
    return re.sub(
        r"<table id=\"incident-learnings\".*?</table>",
        "",
        html_body,
        flags=re.DOTALL,
    )


def _strip_incident_learning_markdown(markdown_body: str) -> str:
    return re.sub(
        r"\n## Incident Learnings\n.*?(?=\n## |\Z)",
        "\n",
        markdown_body,
        flags=re.DOTALL,
    )


_LOOKBACK_RETROSPECTIVE_FEATURE = "lookback_retrospective"
_LOOKBACK_RETROSPECTIVE_PROMPT_VERSION = "lookback_retrospective.v1"


def _build_lookback_ai_retrospective_rows(
    *,
    client: LLMProvider,
    retrospective_intelligence: RetrospectiveIntelligenceSummary,
    snapshots: tuple[Snapshot, ...],
    program_id: str = "",
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[RetrospectiveIntelligenceRow, ...]:
    if not retrospective_intelligence.rows or not snapshots:
        return ()

    issue_window = f"{snapshots[0].issue_number:03d}-{snapshots[-1].issue_number:03d}"
    user_lines = [
        f"Issue window: {issue_window}",
        "Deterministic retrospective summary:",
        (
            "- Counts: "
            f"chronic={retrospective_intelligence.chronic_workstream_count}, "
            f"recovered={retrospective_intelligence.recovered_workstream_count}, "
            f"recurring_drift={retrospective_intelligence.recurring_drift_count}, "
            f"worsened={retrospective_intelligence.worsened_workstream_count}, "
            f"improved={retrospective_intelligence.improved_workstream_count}, "
            f"claim_accuracy={retrospective_intelligence.claim_accuracy_signal_count}, "
            f"charter_evaluation={retrospective_intelligence.charter_evaluation_signal_count}"
        ),
        "- Evidence rows:",
    ]
    user_lines.extend(
        f"  - [{row.category}] {row.title}: {row.detail}"
        for row in retrospective_intelligence.rows[:12]
    )
    user_lines.append(
        "Return up to 3 grounded synthesis insights that connect multiple evidence rows. "
        "Do not invent facts, people, workstreams, issue numbers, or causes not present above."
    )

    def _parse_response(payload: dict[str, Any]) -> tuple[RetrospectiveIntelligenceRow, ...]:
        insights = payload.get("insights")
        if not isinstance(insights, list):
            return ()

        rows: list[RetrospectiveIntelligenceRow] = []
        for raw_insight in insights[:3]:
            if not isinstance(raw_insight, dict):
                continue
            title = str(raw_insight.get("title") or "").strip()
            detail = str(raw_insight.get("detail") or "").strip()
            if not title or not detail:
                continue
            category = str(raw_insight.get("category") or "AI synthesis").strip() or "AI synthesis"
            safe_category = process_generated_text(category).text
            safe_title = process_generated_text(title).text
            safe_detail = process_generated_text(detail).text
            if not safe_title or not safe_detail:
                continue
            rows.append(
                RetrospectiveIntelligenceRow(
                    category=(safe_category or "AI synthesis")[:120],
                    title=safe_title[:120],
                    detail=safe_detail[:320],
                )
            )
        return tuple(rows)

    user_prompt = "\n".join(user_lines)
    outcome = route_through_tiers(
        _LOOKBACK_RETROSPECTIVE_FEATURE,
        deterministic_fn=None,
        local_fn=None,
        frontier_fn=lambda: _run_ai_route(
            client,
            system_prompt=load_prompt(_LOOKBACK_RETROSPECTIVE_PROMPT_VERSION),
            user_prompt=user_prompt,
            parse_response=_parse_response,
            program_id=program_id,
            programs_root=programs_root,
        ),
        policy=load_ai_feature_policy(_LOOKBACK_RETROSPECTIVE_FEATURE),
    )
    return outcome.value or ()


def _run_ai_route(
    client: LLMProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    parse_response: Callable[[dict[str, Any]], tuple[RetrospectiveIntelligenceRow, ...]],
    program_id: str,
    programs_root: Path,
) -> tuple[RetrospectiveIntelligenceRow, ...]:
    """specs/backlog.md BL-C2: lookback_retrospective is production-
    classified (its output is appended directly into the rendered
    lookback report with no further gate). Bounds-checks the raw response
    through AISchemaGateway, then reuses the caller's own `parse_response`
    (already real semantic validation: title/detail non-empty, length caps,
    the text-safety pipeline) rather than a separate validator class.

    Preserves this feature's existing lenient contract exactly: any
    rejection degrades to zero insights (never blocks lookback report
    generation, matching this call site's caller in `assemble_stage.py`,
    which already treats retrospective AI synthesis as best-effort) --
    the one behavior change is that a non-dict response, which previously
    would have raised an uncaught `AttributeError` from `payload.get(...)`
    (not in `assemble_stage.py`'s caught-exception list), now degrades to
    zero insights like every other rejection instead of a latent crash
    risk.
    """
    ai_run_id = new_ai_run_id()

    def _lifecycle(state: AIRunState) -> None:
        record_ai_run_lifecycle(
            program_id=program_id,
            ai_run_id=ai_run_id,
            feature=_LOOKBACK_RETROSPECTIVE_FEATURE,
            state=state,
            prompt_version=_LOOKBACK_RETROSPECTIVE_PROMPT_VERSION,
            policy_version=_LOOKBACK_RETROSPECTIVE_PROMPT_VERSION,
            programs_root=programs_root,
        )

    def _discard(terminal: ReleaseTerminal, reason: str) -> None:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=0,
            programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)
    _lifecycle(AIRunState.REQUESTED)
    try:
        raw = client.structured(
            system_prompt,
            user_prompt,
            parser=lambda payload: payload,
            max_tokens=load_ai_feature_policy(_LOOKBACK_RETROSPECTIVE_FEATURE).max_tokens,
            prompt_version=_LOOKBACK_RETROSPECTIVE_PROMPT_VERSION,
        )
    except Exception as error:
        _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
        raise
    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")
        return ()

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")
        return ()
    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    rows = parse_response(raw)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason=f"passed AISchemaGateway bounds; {len(rows)} insight row(s) survived semantic filtering",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    return rows


def _build_lookback_claim_accuracy_rows(
    *,
    program_id: str,
    edition_id: str,
    programs_root: Path,
    snapshots: tuple[Snapshot, ...],
    items: tuple[WorkItem, ...],
) -> tuple[RetrospectiveIntelligenceRow, ...]:
    if not program_id.strip():
        return ()

    window_start = snapshots[0].ado_data_as_of.date()
    window_end = snapshots[-1].ado_data_as_of.date()
    latest_statuses = load_latest_claim_statuses(program_id, programs_root)
    relevant_claims = tuple(
        entry
        for entry in load_claim_entries(program_id, programs_root)
        if entry.edition_id == edition_id and window_start <= entry.claim_date <= window_end
    )
    if not relevant_claims:
        return ()

    assessments = assess_claim_entries(
        relevant_claims,
        items=items,
        as_of=snapshots[-1].ado_data_as_of,
        latest_statuses=latest_statuses,
    )
    rows: list[RetrospectiveIntelligenceRow] = []
    for assessment in assessments:
        status = assessment.effective_status
        if status == "open":
            continue
        if status in {"met", "resolved"}:
            category = "Claim follow-through"
        elif status in {"contradicted", "stale", "deferred"}:
            category = "Claim accuracy concern"
        else:
            category = "Claim status"

        detail_parts = [
            f"Issue {assessment.claim.issue_number:03d}",
            f"status {status.replace('_', ' ')}",
        ]
        if assessment.claim.due_date is not None:
            detail_parts.append(f"due {assessment.claim.due_date.isoformat()}")
        if assessment.reason:
            detail_parts.append(assessment.reason)
        rows.append(
            RetrospectiveIntelligenceRow(
                category=category,
                title=assessment.claim.text,
                detail=" | ".join(detail_parts),
            )
        )
    return tuple(rows)


def _build_lookback_charter_evaluation_rows(
    charter_review: CharterReviewSummary | None,
) -> tuple[RetrospectiveIntelligenceRow, ...]:
    if charter_review is None:
        return ()

    rows: list[RetrospectiveIntelligenceRow] = []
    for row in charter_review.rows:
        if row.status == "MET":
            category = "Charter criterion met"
        elif row.status == "NOT MET":
            category = "Charter criterion missed"
        else:
            continue
        rows.append(
            RetrospectiveIntelligenceRow(
                category=category,
                title=row.detail,
                detail=row.evidence or "Archive-backed evaluation completed.",
            )
        )
    return tuple(rows)


def _build_lookback_assumption_lifecycle(
    *,
    program_id: str,
    snapshots: tuple[Snapshot, ...],
    as_of: datetime,
    programs_root: Path,
    edition_name: str | None = None,
    archive_root: Path | None = None,
    load_program_reality: Callable[..., ProgramReality] | None = None,
) -> AssumptionLifecycleSummary | None:
    assumptions, assumption_assessments, _, _ = _load_lookback_assumptions(
        program_id=program_id,
        as_of=as_of,
        programs_root=programs_root,
        edition_name=edition_name,
        archive_root=archive_root,
        load_program_reality=load_program_reality,
    )
    if not assumptions:
        return None

    window_start = snapshots[0].ado_data_as_of.date()
    window_end = snapshots[-1].ado_data_as_of.date()
    relevant_entries = tuple(
        entry
        for entry in assumptions
        if _assumption_is_in_lookback_window(entry, window_start=window_start, window_end=window_end)
    )
    if not relevant_entries:
        return None

    overdue_ids = {entry.id for entry in check_validation_due(assumptions, as_of.date())}
    render_warning: str | None = None
    if not assumption_assessments:
        rows = _build_lookback_assumption_rows_from_records(
            _sort_lookback_assumption_rows_legacy(
                relevant_entries,
                overdue_ids=overdue_ids,
            ),
            overdue_ids=overdue_ids,
        )
        includes_unconfirmed_sources = False
    else:
        try:
            rows = _build_lookback_assumption_rows_from_assessments(
                _sort_lookback_assessment_rows(
                    assumption_assessments,
                    overdue_ids=overdue_ids,
                    window_start=window_start,
                    window_end=window_end,
                ),
                overdue_ids=overdue_ids,
            )
            includes_unconfirmed_sources = any(
                row.evidence_truth_level == TruthLevel.RAW_OBSERVED.value
                for row in rows
            )
        except Exception:  # noqa: BLE001 - visible fallback is the contract for render-shaping failures
            log.critical(
                "Assumption lifecycle render fallback activated for program %s",
                program_id,
                exc_info=True,
            )
            rows = _build_lookback_assumption_rows_from_records(
                _sort_lookback_assumption_rows_legacy(
                    relevant_entries,
                    overdue_ids=overdue_ids,
                ),
                overdue_ids=overdue_ids,
            )
            includes_unconfirmed_sources = False
            render_warning = (
                "This section is temporarily rendering from the older assumption source "
                "because live assumption metadata rendering failed."
            )
    return AssumptionLifecycleSummary(
        window_start=window_start,
        window_end=window_end,
        identified_count=sum(1 for entry in assumptions if window_start <= entry.identified_date <= window_end),
        confirmed_count=sum(
            1
            for entry in assumptions
            if entry.status is AssumptionStatus.CONFIRMED
            and entry.resolved_date is not None
            and window_start <= entry.resolved_date <= window_end
        ),
        invalidated_count=sum(
            1
            for entry in assumptions
            if entry.status is AssumptionStatus.INVALIDATED
            and entry.resolved_date is not None
            and window_start <= entry.resolved_date <= window_end
        ),
        still_open_count=sum(
            1
            for entry in assumptions
            if entry.status is AssumptionStatus.UNVALIDATED and window_start <= entry.identified_date <= window_end
        ),
        rows=rows,
        includes_unconfirmed_sources=includes_unconfirmed_sources,
        render_warning=render_warning,
    )


def _load_lookback_assumptions(
    *,
    program_id: str,
    as_of: datetime,
    programs_root: Path,
    edition_name: str | None = None,
    archive_root: Path | None = None,
    load_program_reality: Callable[..., ProgramReality] | None = None,
) -> tuple[
    tuple[Assumption, ...],
    tuple[FactAssessment, ...] | None,
    tuple[str, ...],
    dict[str, dict[str, str | None]] | None,
]:
    return sor_gated_family_load(
        program_id=program_id,
        family="judgment",
        programs_root=programs_root,
        reality_accessor=lambda reality: reality.assumptions(),
        legacy_loader=lambda: load_current_assumptions(program_id, programs_root=programs_root),
        allow_legacy_rollback_env=_ALLOW_LEGACY_ASSUMPTION_ROLLBACK_ENV,
        cross_check_label="assumption",
        load_program_reality=load_program_reality,
        as_of=as_of,
        edition_name=edition_name,
        archive_root=archive_root,
    )


def _build_lookback_assumption_rows_from_assessments(
    assessments: tuple[FactAssessment, ...],
    *,
    overdue_ids: set[str],
) -> tuple[AssumptionLifecycleRow, ...]:
    return tuple(
        _build_lookback_assumption_row_from_assessment(
            assessment,
            overdue=assessment.record.id in overdue_ids,
        )
        for assessment in assessments
    )


def _build_lookback_assumption_rows_from_records(
    assumptions: tuple[Assumption, ...],
    *,
    overdue_ids: set[str],
) -> tuple[AssumptionLifecycleRow, ...]:
    return tuple(
        _build_lookback_assumption_row_from_record(
            assumption,
            overdue=assumption.id in overdue_ids,
        )
        for assumption in assumptions
    )


def _build_lookback_assumption_row_from_assessment(
    assessment: FactAssessment,
    *,
    overdue: bool,
) -> AssumptionLifecycleRow:
    return AssumptionLifecycleRow(
        title=assessment.record.text,
        detail=_format_lookback_assumption_detail(
            assessment.record,
            overdue=overdue,
        ),
        evidence_truth_level=_assessment_truth_level(assessment),
        evidence_disputed=assessment.disputed,
        evidence_stale=assessment.stale,
    )


def _build_lookback_assumption_row_from_record(
    assumption: Assumption,
    *,
    overdue: bool,
) -> AssumptionLifecycleRow:
    return AssumptionLifecycleRow(
        title=assumption.text,
        detail=_format_lookback_assumption_detail(
            assumption,
            overdue=overdue,
        ),
    )


def _sort_lookback_assumption_rows_legacy(
    assumptions: tuple[Assumption, ...],
    *,
    overdue_ids: set[str],
) -> tuple[Assumption, ...]:
    return tuple(
        sorted(
            assumptions,
            key=lambda assumption: (
                0 if assumption.id in overdue_ids else 1,
                0 if assumption.status is AssumptionStatus.UNVALIDATED else 1,
                assumption.validation_due or date.max,
                assumption.identified_date,
                assumption.text.lower(),
            ),
        )
    )


def _sort_lookback_assessment_rows(
    assessments: tuple[FactAssessment, ...],
    *,
    overdue_ids: set[str],
    window_start: date,
    window_end: date,
) -> tuple[FactAssessment, ...]:
    return tuple(
        assessment
        for assessment in sorted(
            assessments,
            key=lambda assessment: (
                0 if assessment.record.id in overdue_ids else 1,
                0 if assessment.record.status is AssumptionStatus.UNVALIDATED else 1,
                assessment.record.validation_due or date.max,
                assessment.record.identified_date,
                assessment.record.text.lower(),
            ),
        )
        if _assumption_is_in_lookback_window(
            assessment.record,
            window_start=window_start,
            window_end=window_end,
        )
    )


def _assessment_truth_level(assessment: FactAssessment) -> str:
    return assessment.truth_level.value


def _build_lookback_charter_review(
    *,
    raw_program: dict[str, object],
    snapshots: tuple[Snapshot, ...],
    risk_rank: Callable[[RiskLevel], int],
) -> CharterReviewSummary | None:
    charter = raw_program.get("charter")
    if not isinstance(charter, dict):
        return None

    scope_statement = charter.get("scope_statement")
    normalized_scope = None
    if isinstance(scope_statement, str):
        collapsed_scope = " ".join(scope_statement.strip().split())
        if collapsed_scope:
            normalized_scope = collapsed_scope

    success_criteria = parse_charter_success_criteria(charter.get("success_criteria"))
    constraints = normalize_charter_values(charter.get("constraints"))
    if normalized_scope is None and not success_criteria and not constraints:
        return None

    rows: list[CharterReviewRow] = []
    evaluated_success_criteria_count = 0
    met_success_criteria_count = 0
    not_met_success_criteria_count = 0
    manual_review_success_criteria_count = 0

    for criterion in success_criteria:
        status: str | None = None
        evidence: str | None = None
        if criterion.is_structured:
            status, evidence = _evaluate_lookback_charter_criterion(criterion, snapshots, risk_rank=risk_rank)
            if status == "MET":
                evaluated_success_criteria_count += 1
                met_success_criteria_count += 1
            elif status == "NOT MET":
                evaluated_success_criteria_count += 1
                not_met_success_criteria_count += 1
            elif status == "MANUAL REVIEW":
                manual_review_success_criteria_count += 1
        rows.append(
            CharterReviewRow(
                title="Success criterion",
                detail=criterion.text,
                status=status,
                evidence=evidence,
            )
        )

    rows.extend(CharterReviewRow(title="Constraint", detail=value) for value in constraints)

    return CharterReviewSummary(
        scope_statement=normalized_scope,
        success_criteria_count=len(success_criteria),
        constraint_count=len(constraints),
        evaluated_success_criteria_count=evaluated_success_criteria_count,
        met_success_criteria_count=met_success_criteria_count,
        not_met_success_criteria_count=not_met_success_criteria_count,
        manual_review_success_criteria_count=manual_review_success_criteria_count,
        rows=tuple(rows),
    )


def _evaluate_lookback_charter_criterion(
    criterion: CharterSuccessCriterion,
    snapshots: tuple[Snapshot, ...],
    *,
    risk_rank: Callable[[RiskLevel], int],
) -> tuple[str, str]:
    if criterion.metric is None:
        return "MANUAL REVIEW", criterion.evaluation_note or "No deterministic archive metric authored."
    if isinstance(criterion.metric, DimensionMaxRiskMetric):
        return _evaluate_dimension_max_risk_criterion(criterion.metric, snapshots, risk_rank=risk_rank)
    if isinstance(criterion.metric, ItemCountMaxMetric):
        return _evaluate_item_count_max_criterion(criterion.metric, snapshots)
    return "MANUAL REVIEW", "Unsupported deterministic archive metric definition."


def _evaluate_dimension_max_risk_criterion(
    metric: DimensionMaxRiskMetric,
    snapshots: tuple[Snapshot, ...],
    *,
    risk_rank: Callable[[RiskLevel], int],
) -> tuple[str, str]:
    history: list[tuple[int, ConfirmedDimension]] = []
    expected_scorecard = metric.scorecard_name.strip().lower()
    expected_dimension = metric.dimension_name.strip().lower()
    for snapshot in snapshots:
        for dimension in snapshot.scorecards:
            if (
                dimension.scorecard_name.strip().lower() == expected_scorecard
                and dimension.name.strip().lower() == expected_dimension
            ):
                history.append((snapshot.issue_number, dimension))
                break
    if not history:
        return (
            "MANUAL REVIEW",
            f"No archived scorecard dimension matched {metric.scorecard_name} / {metric.dimension_name}.",
        )

    latest_issue_number, latest_dimension = history[-1]
    target_label = risk_label(metric.max_risk)
    trend_summary = " -> ".join(risk_label(entry.risk) for _, entry in history)
    status = "MET" if risk_rank(latest_dimension.risk) <= risk_rank(metric.max_risk) else "NOT MET"
    return (
        status,
        f"Latest confirmed risk: {risk_label(latest_dimension.risk)} in Issue {latest_issue_number:03d}; target <= {target_label}; window trend: {trend_summary}.",
    )


def _evaluate_item_count_max_criterion(
    metric: ItemCountMaxMetric,
    snapshots: tuple[Snapshot, ...],
) -> tuple[str, str]:
    latest_snapshot = snapshots[-1]
    matching_items = tuple(
        item
        for item in latest_snapshot.items
        if _snapshot_item_matches_item_count_metric(item, metric)
    )
    status = "MET" if len(matching_items) <= metric.max_count else "NOT MET"
    filter_summary = _format_item_count_metric_filter_summary(metric)
    return (
        status,
        f"Issue {latest_snapshot.issue_number:03d} matching item count: {len(matching_items)} ({filter_summary}; target <= {metric.max_count}).",
    )


def _snapshot_item_matches_item_count_metric(item: SnapshotItem, metric: ItemCountMaxMetric) -> bool:
    if metric.states and _normalize_item_count_metric_token(item.state) not in metric.states:
        return False
    if metric.work_item_types and _normalize_item_count_metric_token(item.type) not in metric.work_item_types:
        return False
    normalized_area_path = _normalize_item_count_metric_token(item.area_path)
    if metric.area_path_prefixes and not any(
        normalized_area_path.startswith(prefix) for prefix in metric.area_path_prefixes
    ):
        return False
    if metric.risk_levels and item.risk_level not in metric.risk_levels:
        return False
    if metric.tags:
        normalized_tags = {_normalize_item_count_metric_token(tag) for tag in item.tags}
        if not any(tag in normalized_tags for tag in metric.tags):
            return False
    return True


def _format_item_count_metric_filter_summary(metric: ItemCountMaxMetric) -> str:
    parts = [f"states: {', '.join(metric.states) if metric.states else 'any'}"]
    if metric.work_item_types:
        parts.append(f"work item types: {', '.join(metric.work_item_types)}")
    if metric.area_path_prefixes:
        parts.append(f"area path prefixes: {', '.join(metric.area_path_prefixes)}")
    if metric.risk_levels:
        parts.append(f"risk levels: {', '.join(risk_label(level) for level in metric.risk_levels)}")
    if metric.tags:
        parts.append(f"tags: {', '.join(metric.tags)}")
    return " | ".join(parts)


def _normalize_item_count_metric_token(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _assumption_is_in_lookback_window(
    entry: Assumption,
    *,
    window_start: date,
    window_end: date,
) -> bool:
    if window_start <= entry.identified_date <= window_end:
        return True
    return entry.resolved_date is not None and window_start <= entry.resolved_date <= window_end


def _sort_lookback_assumptions(
    entries: tuple[Assumption, ...],
    *,
    window_start: date,
    window_end: date,
) -> tuple[Assumption, ...]:
    def _status_rank(entry: Assumption) -> int:
        if entry.status is AssumptionStatus.INVALIDATED:
            return 0
        if entry.status is AssumptionStatus.CONFIRMED:
            return 1
        return 2

    def _sort_key(entry: Assumption) -> tuple[int, int, int, str]:
        resolved_in_window = entry.resolved_date is not None and window_start <= entry.resolved_date <= window_end
        event_date = entry.resolved_date or entry.identified_date
        return (
            0 if resolved_in_window else 1,
            _status_rank(entry),
            -event_date.toordinal(),
            entry.text.lower(),
        )

    return tuple(sorted(entries, key=_sort_key))


def _format_lookback_assumption_detail(entry: Assumption, *, overdue: bool) -> str:
    details = [entry.status.value.upper(), f"identified {entry.identified_date.isoformat()}"]
    if entry.status is AssumptionStatus.CONFIRMED and entry.resolved_date is not None:
        details.append(f"confirmed {entry.resolved_date.isoformat()}")
    elif entry.status is AssumptionStatus.INVALIDATED and entry.resolved_date is not None:
        details.append(f"invalidated {entry.resolved_date.isoformat()}")
    elif entry.validation_due is not None:
        details.append(f"due {entry.validation_due.isoformat()}")
        details.append("overdue" if overdue else "open")
    else:
        details.append("overdue" if overdue else "open")
    if entry.owner_alias is not None:
        details.append(f"owner {entry.owner_alias}")
    if entry.linked_milestone_id is not None:
        details.append(f"milestone {entry.linked_milestone_id}")
    if entry.linked_risk_id is not None:
        details.append(f"risk {entry.linked_risk_id}")
    return " | ".join(details)


def _build_lookback_evidence(*, work_item_id: int, summary: str) -> EvidencePacket:
    return EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.NONE,
        tier=AttributionTier.TIER3,
        summary_for_reviewer=summary,
    )
