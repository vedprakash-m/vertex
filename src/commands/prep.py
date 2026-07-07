from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import re
from typing import Any

import typer

from src.ai.anticipation_engine import anticipate_questions
from src.core.action_tracker import assess_action_staleness, load_action_resolution_candidate_ids
from src.core.assumption_tracker import check_validation_due
from src.commands.confirm import _deserialize_items, _load_draft_state
from src.commands.report import _ado_item_base_url, _build_item_urls, _build_scorecard_data, _build_scorecard_packets, _build_workstream_data, _format_dependency_cascade, _load_guarded_review_evidence, _load_previous_snapshot, _write_output_text
from src.commands.review_full import _build_anticipation_client, _load_review_full_context, _load_reviewer_summaries
from src.core.archive_store import ARCHIVE_ROOT
from src.core.cascade_detector import DependencyCascade, detect_dependency_cascades
from src.core.charter import normalize_charter_values
from src.core.claim_tracker import load_open_claims, load_open_decision_asks
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.dependency_graph import dependency_source_label, dependency_target_label
from src.core.dependency_scout import DependencyProposalStatus, load_dependency_proposals
from src.core.edition_resolver import resolve_edition, get_program_output_dir
from src.core.engms_content import summarize_engms_page
from src.core.exceptions import ConfigError
from src.core.freshness_engine import build_freshness_report
from src.core.issue_projection import IssueProjection, build_issue_projection, issue_projection_confidence_label, issue_projection_source_label
from src.core.knowledge_store import load_program_knowledge, select_engms_pages
from src.core.models import EditionType
from src.core.models_v2 import ActionItem, ActionStatus, Assumption, AssumptionStatus, Dependency, RiskStatus
from src.core.overrides_store import load_overrides
from src.core.program_fact_store import (
    load_program_facts,
    project_action_items,
    project_assumptions,
    project_dependencies,
    project_risk_entries,
)
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.scorecard_trends import ScorecardTrend, load_scorecard_trends
from src.core.signal_ranking import signal_source_family
from src.core.store_factory import build_signal_store_for_program_id
from src.core.telemetry_summary import build_approved_telemetry_summary
from src.core.trusted_baseline_store import load_trusted_baseline_issue


@dataclass(frozen=True, slots=True)
class PrepBriefArtifacts:
    issue_number: int
    markdown_path: Path


def prep_command(
    edition: str = typer.Option(..., "--edition", help="Edition name (e.g. myprogram_lt_deck)."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    try:
        artifacts = generate_prep_brief(edition_name=edition)
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    if format == "human":
        typer.echo(f"Prep brief generated for Issue {artifacts.issue_number:03d}.")
        typer.echo(f"Prep brief: {artifacts.markdown_path}")
    else:
        typer.echo(render_prep_output(edition, artifacts, format=format), nl=False)
    raise typer.Exit(code=0)


def render_prep_output(edition: str, artifacts: PrepBriefArtifacts, *, format: str) -> str:
    payload = {
        "edition_name": edition,
        "issue_number": artifacts.issue_number,
        "markdown_path": str(artifacts.markdown_path),
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("edition_name", "issue_number", "markdown_path"))
        writer.writerow((payload["edition_name"], payload["issue_number"], payload["markdown_path"]))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def generate_prep_brief(
    *,
    edition_name: str,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    as_of: datetime | None = None,
) -> PrepBriefArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    generated_at = as_of or datetime.now(timezone.utc)
    programs_root = resolved_reports_root.parent / "programs"

    resolved_v2 = resolve_edition(edition_name, programs_root=programs_root)
    if resolved_v2 is None or resolved_v2.edition.altitude != "satellite":
        raise typer.BadParameter("prep is only available for V2 satellite editions.")

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=programs_root,
    )
    issue_number, review_status = _load_review_full_context(
        edition_name=edition_name,
        issue_number=None,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
    )
    draft_state = _load_draft_state(edition_name, issue_number, programs_root=programs_root)
    items = _deserialize_items(tuple(draft_state.get("items", ())))
    draft_as_of = datetime.fromisoformat(str(draft_state["ado_data_as_of"]))
    exec_summary_text = str(draft_state.get("exec_summary_text") or "").strip()
    workstream_blurbs = {
        str(key): str(value)
        for key, value in (draft_state.get("workstream_blurbs") or {}).items()
        if str(value).strip()
    }

    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=issue_number,
        programs_root=programs_root,
    )
    previous_snapshot, _ = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    guarded_review_evidence = _load_guarded_review_evidence(
        edition_name=edition_name,
        bundle=bundle,
        items=items,
        as_of=draft_as_of,
        previous_snapshot=previous_snapshot,
        reports_root=resolved_reports_root,
    )
    overrides_document = load_overrides(
        edition_name,
        reports_root=resolved_reports_root,
        issue_number=issue_number,
    )
    if overrides_document is None or overrides_document.issue_number != issue_number:
        raise typer.BadParameter(
            f"overrides.yaml is not initialized for Issue {issue_number:03d}. Run `vertex draft --edition {edition_name}` first."
        )

    scorecard_packets = _build_scorecard_packets(bundle, items, previous_snapshot)
    scorecards, _, _ = _build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item={},
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    item_urls = _build_item_urls(bundle, items)
    program_facts = load_program_facts(resolved_v2.program.id, db_root=resolved_reports_root.parent, programs_root=programs_root)
    projected_dependencies = project_dependencies(program_facts)
    projected_risks = project_risk_entries(program_facts)
    projected_actions = project_action_items(program_facts)
    active_actions = tuple(
        action for action in projected_actions if action.status in {ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
    )
    workstreams = _build_workstream_data(
        issue_number=issue_number,
        bundle=bundle,
        edition_type=EditionType.from_string(bundle.config.edition.type),
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        workstream_blurbs=workstream_blurbs,
        dependency_cascades=(),
        review_status=review_status,
        evidence_by_item={},
        item_urls=item_urls,
    )
    dependencies = projected_dependencies
    summary_lookup = _load_reviewer_summaries(resolved_v2=resolved_v2, programs_root=programs_root)
    anticipation_client = _build_anticipation_client(bundle)
    anticipated_questions = anticipate_questions(
        readers=resolved_v2.program.leadership_readers,
        signals=guarded_review_evidence.approved_signals,
        drift_patterns=guarded_review_evidence.drift_patterns,
        summaries=summary_lookup,
        workstreams=workstreams,
        dependencies=dependencies,
        client=anticipation_client,
    )
    dependency_cascades = detect_dependency_cascades(
        dependencies=dependencies,
        signals=guarded_review_evidence.approved_signals,
        drift_patterns=guarded_review_evidence.drift_patterns,
        items=items,
        scorecards=resolved_v2.scorecards,
        workstreams=resolved_v2.workstreams,
    )
    open_decision_asks = load_open_decision_asks(resolved_v2.program.id, programs_root=programs_root)
    recent_signals = _load_recent_unincorporated_signals(
        program_id=resolved_v2.program.id,
        start=draft_as_of,
        end=generated_at,
        programs_root=programs_root,
    )
    scorecard_trends = _interesting_scorecard_trends(
        edition_name=edition_name,
        scorecards=scorecards,
        archive_root=resolved_archive_root,
    )

    markdown = _render_prep_brief(
        edition_name=edition_name,
        issue_number=issue_number,
        generated_at=generated_at,
        draft_as_of=draft_as_of,
        exec_summary_text=exec_summary_text,
        charter_context_lines=_build_charter_context_lines(resolved_v2.raw_program),
        telemetry_lines=_build_prep_telemetry_lines(guarded_review_evidence.approved_signals),
        risk_lines=_build_prep_risk_lines(
            risks=projected_risks,
            as_of=generated_at,
        ),
        issue_lines=_build_prep_issue_lines(
            issue_number=issue_number,
            bundle=bundle,
            items=items,
            approved_signals=guarded_review_evidence.approved_signals,
            as_of=draft_as_of,
            program_id=resolved_v2.program.id,
            programs_root=programs_root,
            active_actions=active_actions,
            risk_entries=projected_risks,
        ),
        action_lines=_build_prep_action_lines(
            active_actions=active_actions,
            as_of=generated_at,
            programs_root=programs_root,
        ),
        dependency_lines=_build_prep_dependency_lines(
            dependency_cascades,
            program_id=resolved_v2.program.id,
            programs_root=programs_root,
        ),
        dependency_diagram_lines=_build_prep_dependency_diagram_lines(
            program_id=resolved_v2.program.id,
            programs_root=programs_root,
        ),
        milestone_lines=_build_prep_milestone_lines(
            edition_name=edition_name,
            issue_number=issue_number,
            programs_root=programs_root,
        ),
        assumption_lines=_build_prep_assumption_lines(
            program_id=resolved_v2.program.id,
            as_of=generated_at,
            programs_root=programs_root,
        ),
        reference_doc_lines=_build_prep_reference_doc_lines(
            program_id=resolved_v2.program.id,
            workstream_ids=tuple(workstream.id for workstream in resolved_v2.workstreams),
            programs_root=programs_root,
        ),
        anticipated_questions=anticipated_questions,
        drift_patterns=guarded_review_evidence.drift_patterns,
        recent_signals=recent_signals,
        open_decision_asks=open_decision_asks,
        scorecard_trends=scorecard_trends,
    )
    path = _write_output_text(get_program_output_dir(edition_name, programs_root=programs_root) / "prep_brief.md", markdown)
    return PrepBriefArtifacts(issue_number=issue_number, markdown_path=path)


def _load_recent_unincorporated_signals(
    *,
    program_id: str,
    start: datetime,
    end: datetime,
    programs_root: Path,
) -> tuple:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    approved = []
    for signal in signal_store.read(program_id, start=start, end=end):
        decision = review_states.get(signal.id)
        if decision is None or decision.decision != "approved":
            continue
        if signal.timestamp <= start:
            continue
        approved.append(signal)
    approved.sort(key=lambda signal: signal.timestamp, reverse=True)
    return tuple(approved[:5])


def _interesting_scorecard_trends(*, edition_name: str, scorecards: tuple, archive_root: Path) -> tuple[tuple[str, str, ScorecardTrend], ...]:
    current_dimensions = {
        (scorecard.scorecard_name, dimension.name): dimension.risk
        for scorecard in scorecards
        for dimension in scorecard.dimensions
    }
    trends = load_scorecard_trends(edition_name, current_dimensions, archive_root=archive_root)
    interesting = [
        (scorecard_name, dimension_name, trend)
        for (scorecard_name, dimension_name), trend in trends.items()
        if trend.annotation is not None or trend.direction == "worsening"
    ]
    interesting.sort(key=lambda entry: (0 if entry[2].direction == "worsening" else 1, -entry[2].consecutive_high_count, entry[0], entry[1]))
    return tuple(interesting[:5])


def _build_prep_risk_lines(
    *,
    risks: tuple,
    as_of: datetime,
) -> tuple[str, ...]:
    risks = tuple(
        risk
        for risk in risks
        if risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )
    if not risks:
        return ("- No open risk-register entries are currently tracked.",)

    ordered_risks = sorted(
        risks,
        key=lambda risk: (
            0 if risk.status == RiskStatus.ESCALATED else 1,
            -compute_risk_score(risk),
            risk.title.lower(),
        ),
    )
    lines: list[str] = []
    for risk in ordered_risks[:5]:
        details = [risk.status.value.upper(), f"score {compute_risk_score(risk)}"]
        details.append("stale" if assess_risk_staleness(risk, as_of.date()) else "current")
        details.append(f"owner {risk.owner_alias}")
        if risk.mitigation_due_date is not None:
            details.append(f"mitigation due {risk.mitigation_due_date.isoformat()}")
        linked_refs: list[str] = []
        if risk.linked_workstream_ids:
            linked_refs.append(f"workstream {', '.join(risk.linked_workstream_ids)}")
        if risk.linked_milestone_ids:
            linked_refs.append(f"milestone {', '.join(risk.linked_milestone_ids)}")
        if risk.linked_work_item_ids:
            linked_refs.append(", ".join(f"WI:{work_item_id}" for work_item_id in risk.linked_work_item_ids))
        if linked_refs:
            details.append("linked " + " ; ".join(linked_refs))
        lines.append(f"- {risk.title} | {' | '.join(details)}")
    return tuple(lines)


def _build_prep_telemetry_lines(approved_signals: tuple) -> tuple[str, ...]:
    summary = build_approved_telemetry_summary(approved_signals)
    if summary is None:
        return ("- No approved analytics or sprint telemetry is currently available.",)
    return (f"- {summary}",)


def _build_prep_issue_lines(
    *,
    issue_number: int,
    bundle: Any,
    items: tuple,
    approved_signals: tuple,
    as_of: datetime,
    program_id: str,
    programs_root: Path,
    active_actions: tuple[ActionItem, ...],
    risk_entries: tuple,
) -> tuple[str, ...]:
    overdue_actions = assess_action_staleness(active_actions, as_of.date())
    freshness_report = build_freshness_report(
        current_items=items,
        issue_number=issue_number,
        as_of=as_of,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=None,
        previous_notification_state=None,
        program_context=bundle.program_context,
        workstream_narrative_history={},
    )
    issue_projections = build_issue_projection(
        items=items,
        freshness_report=freshness_report,
        icm_signals=tuple(signal for signal in approved_signals if signal_source_family(signal.source) == "icm"),
        open_asks=load_open_decision_asks(program_id, programs_root=programs_root),
        overdue_actions=overdue_actions,
        open_claims=load_open_claims(program_id, programs_root=programs_root),
        risk_entries=risk_entries,
        ado_item_base_url=_ado_item_base_url(bundle),
    )
    if not issue_projections:
        return ("- No active issues are currently projected from the latest draft context.",)
    return tuple(_format_prep_issue_line(entry) for entry in issue_projections[:5])


def _build_prep_action_lines(
    *,
    active_actions: tuple[ActionItem, ...],
    as_of: datetime,
    programs_root: Path,
) -> tuple[str, ...]:
    if not active_actions:
        return ("- No open action items are currently tracked.",)

    program_id = active_actions[0].program_id
    overdue_ids = {action.id for action in assess_action_staleness(active_actions, as_of.date())}
    resolution_candidate_ids = set(load_action_resolution_candidate_ids(program_id, active_actions, programs_root=programs_root))
    ordered_actions = sorted(
        active_actions,
        key=lambda action: (
            0 if action.id in overdue_ids else 1,
            action.due_date or datetime.max.date(),
            action.created_at,
            action.text.lower(),
        ),
    )
    return tuple(
        _format_prep_action_line(
            action,
            overdue=action.id in overdue_ids,
            resolution_candidate=action.id in resolution_candidate_ids,
        )
        for action in ordered_actions[:5]
    )


def _format_prep_action_line(action: ActionItem, *, overdue: bool, resolution_candidate: bool) -> str:
    details = [action.status.value.upper(), f"due {action.due_date.isoformat() if action.due_date is not None else '-'}"]
    details.append("overdue" if overdue else "current")
    details.append(f"owner {action.owner_alias}")
    if action.linked_work_item_ids:
        details.append(", ".join(f"WI:{work_item_id}" for work_item_id in action.linked_work_item_ids))
    if action.linked_risk_id:
        details.append(f"risk {action.linked_risk_id}")
    if resolution_candidate:
        details.append("candidate for resolution")
    return f"- {action.text} | {' | '.join(details)}"


def _format_prep_issue_line(entry: IssueProjection) -> str:
    details = [issue_projection_source_label(entry), entry.severity.upper(), issue_projection_confidence_label(entry)]
    if entry.owner_alias is not None:
        details.append(f"owner {entry.owner_alias}")
    if entry.workstream_id is not None:
        details.append(f"workstream {entry.workstream_id}")
    if entry.linked_entity_ids:
        details.append(f"linked {', '.join(entry.linked_entity_ids)}")
    if entry.ado_url is not None:
        details.append(f"[ADO]({entry.ado_url})")
    return f"- {entry.summary} | {' | '.join(details)}"


def _build_prep_dependency_lines(
    cascades: tuple[DependencyCascade, ...],
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[str, ...]:
    proposal_lines = tuple(
        _format_prep_dependency_proposal_line(proposal, program_id=program_id)
        for proposal in load_dependency_proposals(program_id, programs_root=programs_root)
        if proposal.status == DependencyProposalStatus.PROPOSED
    )
    if not cascades and not proposal_lines:
        return ("- No dependency cascades are currently detected from approved signals or drift.",)

    cascade_lines = tuple(
        f"- {message}"
        for message in tuple(dict.fromkeys(_format_dependency_cascade(cascade) for cascade in cascades))
    )
    combined_lines = tuple(dict.fromkeys((*proposal_lines, *cascade_lines)))
    return combined_lines[:5]


def _format_prep_dependency_proposal_line(proposal, *, program_id: str) -> str:
    return (
        f"- Proposed dependency {proposal.id}: {proposal.from_workstream_id}:{proposal.from_item_id} -> "
        f"{proposal.to_workstream_id}:{proposal.to_item_id} | {proposal.detection_method} | "
        f"{proposal.occurrence_count} signal(s) | {proposal.confidence.value.lower()} confidence | accept via vertex dependencies accept --program {program_id} --id {proposal.id}"
    )


def _build_prep_dependency_diagram_lines(
    *,
    program_id: str,
    programs_root: Path,
    max_hops: int = 3,
) -> tuple[str, ...]:
    reachable_dependencies = _load_reachable_cross_program_dependencies(
        program_id=program_id,
        programs_root=programs_root,
        max_hops=max_hops,
    )
    if not reachable_dependencies:
        return ()

    lines = ["```mermaid", "flowchart LR"]
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    for dependency in reachable_dependencies:
        source_label = _prep_dependency_diagram_label(
            dependency=dependency,
            endpoint="source",
            root_program_id=program_id,
        )
        target_label = _prep_dependency_diagram_label(
            dependency=dependency,
            endpoint="target",
            root_program_id=program_id,
        )
        source_node = _prep_mermaid_node_id(source_label)
        target_node = _prep_mermaid_node_id(target_label)
        if source_node not in seen_nodes:
            seen_nodes.add(source_node)
            lines.append(f'  {source_node}["{_escape_mermaid_label(source_label)}"]')
        if target_node not in seen_nodes:
            seen_nodes.add(target_node)
            lines.append(f'  {target_node}["{_escape_mermaid_label(target_label)}"]')
        edge_key = (source_node, target_node, dependency.id)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        lines.append(
            f"  {source_node} -->|{dependency.status.value.upper()} {dependency.dependency_type.value}| {target_node}"
        )
    lines.append("```")
    return tuple(lines)


def _load_reachable_cross_program_dependencies(
    *,
    program_id: str,
    programs_root: Path,
    max_hops: int,
) -> tuple[Dependency, ...]:
    if max_hops < 1:
        return ()

    adjacency: dict[str, tuple[Dependency, ...]] = {}
    for dependency in _load_all_cross_program_dependencies(programs_root=programs_root):
        adjacency[dependency.from_program_id] = (*adjacency.get(dependency.from_program_id, ()), dependency)

    reachable: list[Dependency] = []
    seen_dependency_ids: set[str] = set()
    seen_program_ids = {program_id}
    frontier: list[tuple[str, int]] = [(program_id, 0)]
    while frontier:
        current_program_id, depth = frontier.pop(0)
        if depth >= max_hops:
            continue
        for dependency in adjacency.get(current_program_id, ()):
            if dependency.id not in seen_dependency_ids:
                seen_dependency_ids.add(dependency.id)
                reachable.append(dependency)
            if dependency.to_program_id not in seen_program_ids:
                seen_program_ids.add(dependency.to_program_id)
                frontier.append((dependency.to_program_id, depth + 1))
    return tuple(reachable)


def _load_all_cross_program_dependencies(*, programs_root: Path) -> tuple[Dependency, ...]:
    dependencies: list[Dependency] = []
    for program_dir in sorted(programs_root.iterdir(), key=lambda entry: entry.name.lower()):
        if not program_dir.is_dir() or not (program_dir / "program.yaml").exists():
            continue
        try:
            loaded_dependencies = project_dependencies(
                load_program_facts(program_dir.name, db_root=programs_root.parent, programs_root=programs_root)
            )
        except ConfigError:
            continue
        dependencies.extend(
            dependency
            for dependency in loaded_dependencies
            if dependency.from_program_id != dependency.to_program_id
        )
    return tuple(dependencies)


def _prep_mermaid_node_id(label: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").lower() or "node"
    if normalized[0].isdigit():
        normalized = f"node_{normalized}"
    return normalized


def _prep_dependency_diagram_label(*, dependency: Dependency, endpoint: str, root_program_id: str) -> str:
    if endpoint == "source":
        label = dependency_source_label(dependency)
        endpoint_program_id = dependency.from_program_id
    else:
        label = dependency_target_label(dependency)
        endpoint_program_id = dependency.to_program_id
    if endpoint_program_id != root_program_id and not label.startswith(f"{endpoint_program_id}:"):
        return f"{endpoint_program_id}:{label}"
    return label


def _escape_mermaid_label(label: str) -> str:
    return label.replace('"', "'")


def _build_prep_milestone_lines(
    *,
    programs_root: Path,
    edition_name: str,
    issue_number: int,
) -> tuple[str, ...]:
    manifest_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    assessments = _load_manifest_milestone_assessments(manifest_path)
    if not assessments:
        return ("- No cached milestone assessments are available from the latest draft.",)

    ordered_assessments = sorted(
        assessments,
        key=lambda assessment: (
            0 if assessment.get("computed_health") in {"at_risk", "missed"} else 1,
            0 if assessment.get("critical_path") else 1,
            str(assessment.get("target_date") or "9999-12-31"),
            str(assessment.get("milestone_name") or assessment.get("milestone_id") or ""),
        ),
    )
    lines: list[str] = []
    for assessment in ordered_assessments[:5]:
        name = str(assessment.get("milestone_name") or assessment.get("milestone_id") or "unknown milestone")
        status = str(assessment.get("computed_health") or "unknown").replace("_", " ")
        details = [status]
        if assessment.get("critical_path"):
            details.append("critical path")
        target_date = assessment.get("target_date")
        if target_date is not None:
            details.append(f"target {target_date}")
        completion_date = assessment.get("completion_date")
        if completion_date is not None:
            details.append(f"completed {completion_date}")
        lines.append(f"- {name} | {' | '.join(details)}")
    return tuple(lines)


def _build_prep_assumption_lines(
    *,
    program_id: str,
    as_of: datetime,
    programs_root: Path,
) -> tuple[str, ...]:
    assumptions = tuple(
        entry
        for entry in project_assumptions(
            load_program_facts(
                program_id,
                db_root=programs_root.parent,
                programs_root=programs_root,
                fact_types=("assumption.entry",),
            )
        )
        if entry.status is AssumptionStatus.UNVALIDATED
    )
    if not assumptions:
        return ("- No open assumptions are currently tracked.",)

    overdue_ids = {entry.id for entry in check_validation_due(assumptions, as_of.date())}
    ordered_assumptions = sorted(
        assumptions,
        key=lambda entry: (
            0 if entry.id in overdue_ids else 1,
            entry.validation_due or datetime.max.date(),
            entry.identified_date,
            entry.text.lower(),
        ),
    )
    return tuple(
        _format_prep_assumption_line(entry, overdue=entry.id in overdue_ids)
        for entry in ordered_assumptions[:5]
    )


def _format_prep_assumption_line(entry: Assumption, *, overdue: bool) -> str:
    details = [entry.status.value.upper(), "overdue" if overdue else "current"]
    details.append(f"due {entry.validation_due.isoformat() if entry.validation_due is not None else '-'}")
    details.append(f"owner {entry.owner_alias or '-'}")
    if entry.validation_method:
        details.append(f"method {entry.validation_method}")
    if entry.linked_milestone_id:
        details.append(f"milestone {entry.linked_milestone_id}")
    if entry.linked_risk_id:
        details.append(f"risk {entry.linked_risk_id}")
    return f"- {entry.text} | {' | '.join(details)}"


def _load_manifest_milestone_assessments(manifest_path: Path) -> tuple[dict[str, Any], ...]:
    if not manifest_path.exists():
        return ()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return ()
    raw_assessments = metadata.get("milestone_assessments")
    if not isinstance(raw_assessments, list):
        return ()
    return tuple(entry for entry in raw_assessments if isinstance(entry, dict))


def _render_prep_brief(
    *,
    edition_name: str,
    issue_number: int,
    generated_at: datetime,
    draft_as_of: datetime,
    exec_summary_text: str,
    charter_context_lines: tuple[str, ...],
    telemetry_lines: tuple[str, ...],
    risk_lines: tuple[str, ...],
    issue_lines: tuple[str, ...],
    action_lines: tuple[str, ...],
    dependency_lines: tuple[str, ...],
    dependency_diagram_lines: tuple[str, ...],
    milestone_lines: tuple[str, ...],
    assumption_lines: tuple[str, ...],
    reference_doc_lines: tuple[str, ...],
    anticipated_questions: tuple,
    drift_patterns: tuple,
    recent_signals: tuple,
    open_decision_asks: tuple,
    scorecard_trends: tuple[tuple[str, str, ScorecardTrend], ...],
) -> str:
    lines = [
        f"# Prep Brief | {edition_name} | Issue {issue_number:03d}",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Latest draft data: {draft_as_of.isoformat()}",
        "",
        "## Latest Draft Summary",
        "",
        exec_summary_text or "No executive summary is present in the latest draft.",
        "",
    ]

    if charter_context_lines:
        lines.extend(("## Charter Context", ""))
        lines.extend(charter_context_lines)
        lines.extend(("",))

    lines.extend(("## Telemetry", ""))
    lines.extend(telemetry_lines)

    lines.extend(("## Open Risks", ""))
    lines.extend(risk_lines)

    lines.extend(("", "## Active Issues", ""))
    lines.extend(issue_lines)

    lines.extend(("", "## Open Actions", ""))
    lines.extend(action_lines)

    lines.extend(("", "## Dependency Cascades", ""))
    lines.extend(dependency_lines)
    if dependency_diagram_lines:
        lines.extend(("", "### Dependency Diagram", ""))
        lines.extend(dependency_diagram_lines)

    lines.extend(("", "## Milestone Health", ""))
    lines.extend(milestone_lines)

    lines.extend(("", "## Open Assumptions", ""))
    lines.extend(assumption_lines)

    lines.extend(("", "## Reference Docs", ""))
    lines.extend(reference_doc_lines)

    lines.extend(("## Anticipated Questions", ""))
    if anticipated_questions:
        for question in anticipated_questions:
            confidence_suffix = ""
            if getattr(question, "confidence", None) is not None:
                confidence_value = getattr(question.confidence, "value", str(question.confidence)).strip().lower()
                if confidence_value and confidence_value != "none":
                    confidence_suffix = f" | {confidence_value} confidence"
            lines.append(f"- {question.reader}: {question.question}{confidence_suffix}")
            if question.suggested_response.strip():
                lines.append(f"  Suggested response: {question.suggested_response}")
    else:
        lines.append("- No anticipated questions were generated.")

    lines.extend(("", "## Unresolved Drift Patterns", ""))
    if drift_patterns:
        for pattern in drift_patterns[:5]:
            lines.append(f"- WI:{pattern.work_item_id} [{pattern.pattern}/{pattern.severity}] {pattern.detail}")
    else:
        lines.append("- No unresolved drift patterns are currently detected.")

    lines.extend(("", "## Recent Unincorporated Signals", ""))
    if recent_signals:
        for signal in recent_signals:
            lines.append(f"- {signal.timestamp.isoformat()} [{signal.source}] {signal.text}")
    else:
        lines.append("- No new approved signals have appeared since the latest draft.")

    lines.extend(("", "## Open Decision Asks", ""))
    if open_decision_asks:
        for ask in open_decision_asks[:5]:
            owner = ask.owner_alias or "unassigned"
            lines.append(f"- {ask.ask_date.isoformat()} | owner={owner} | {ask.text}")
    else:
        lines.append("- No open decision asks are currently tracked.")

    lines.extend(("", "## Scorecard Trend Summary", ""))
    if scorecard_trends:
        for scorecard_name, dimension_name, trend in scorecard_trends:
            summary = trend.annotation or f"{trend.direction.title()} at {trend.current_risk.value.title()}."
            lines.append(f"- {scorecard_name} / {dimension_name}: {summary}")
    else:
        lines.append("- No notable scorecard trend shifts are active.")
    lines.append("")
    return "\n".join(lines)


def _build_prep_reference_doc_lines(
    *,
    program_id: str,
    workstream_ids: tuple[str, ...],
    programs_root: Path,
) -> tuple[str, ...]:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    pages = select_engms_pages(knowledge, program_id=program_id, workstream_ids=workstream_ids)[:5]
    if not pages:
        return ("- No authored eng.ms reference pages are currently cataloged.",)
    return tuple(
        f"- {page.title} | {page.url} | {summarize_engms_page(page)}"
        for page in pages
    )


def _build_charter_context_lines(raw_program: dict[str, object]) -> tuple[str, ...]:
    charter = raw_program.get("charter")
    if not isinstance(charter, dict):
        return ()

    lines: list[str] = []
    scope_statement = charter.get("scope_statement")
    if isinstance(scope_statement, str):
        normalized_scope = " ".join(scope_statement.strip().split())
        if normalized_scope:
            lines.append(f"Scope: {normalized_scope}")

    for key, label in (("success_criteria", "Success criterion"), ("constraints", "Constraint")):
        for value in normalize_charter_values(charter.get(key)):
            lines.append(f"- {label}: {value}")

    raw_stakeholders = charter.get("stakeholder_register")
    if isinstance(raw_stakeholders, list):
        for entry in raw_stakeholders:
            if not isinstance(entry, dict):
                continue
            alias = entry.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                continue
            role = entry.get("role") if isinstance(entry.get("role"), str) else "-"
            interest = entry.get("interest") if isinstance(entry.get("interest"), str) else "-"
            lines.append(
                f"- Stakeholder: {alias.strip()} | {role.strip() if isinstance(role, str) else '-'} | {interest.strip() if isinstance(interest, str) else '-'}"
            )

    return tuple(lines)
