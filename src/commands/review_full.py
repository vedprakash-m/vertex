from __future__ import annotations

import csv
import json
import logging
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import inspect
from io import StringIO
from pathlib import Path
from typing import Any, Callable

import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.anticipation_engine import anticipate_questions
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.provider import LLMProvider
from src.commands.doctor_checks.semantic_index_checks import semantic_index_enabled as _semantic_index_enabled
from src.commands.confirm import _deserialize_items, _load_draft_state
from src.commands.report import _ado_item_base_url, _artifact_url, _build_item_urls, _build_scorecard_data, _build_scorecard_packets
from src.commands.report_narratives import _active_workstream_blurbs
from src.commands.report import _build_v2_vitality_snapshot
from src.commands.report import _build_top_items, _build_workstream_data, _format_edition_title, _load_eta_forecasts, _load_guarded_review_evidence, _load_previous_snapshot, _visible_detail_section_ids, _write_output_text
from src.commands.report import _write_output_json
from src.core.action_tracker import assess_action_staleness, load_action_resolution_candidate_ids
from src.core.assumption_tracker import check_validation_due
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.attribution_engine import build_inline_citations, build_section_citations
from src.core.cascade_detector import detect_dependency_cascades
from src.core.claim_tracker import assess_claim_entries, load_open_claims, load_open_decision_asks
from src.core.config_loader import REPORTS_ROOT, ReportBundle, ScorecardSettings, load_bundle
from src.core.coverage_gap import CoverageGap, build_coverage_gaps, coverage_gap_confidence_label
from src.core.decision_register import assess_proposed_decision_staleness
from src.core.dependency_graph import load_inbound_cross_program_dependencies
from src.core.delta_engine import build_deltas
from src.core.edition_resolver import resolve_edition, get_program_output_dir, PROGRAMS_ROOT
from src.core.engms_content import summarize_engms_page
from src.core.evidence_engine import build_evidence
from src.core.exceptions import AuthError, QueryError
from src.core.forecast_engine import ETAForecast, build_forecast_assessment
from src.core.freshness_engine import build_freshness_report
from src.core.issue_projection import IssueProjection, build_issue_projection, issue_projection_confidence_label, issue_projection_source_label
from src.core.jinja_filters import build_anchor, delta_label, risk_label
from src.core.knowledge_store import load_program_knowledge, select_engms_pages
from src.core.lineage import build_lineage_lookup
from src.core.milestone_engine import (
    assess_milestone_health,
    build_critical_path,
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import Confidence, DeltaKind, DimensionRisk, EditionType, EvidencePacket, ItemDelta, ReviewSection, ReviewState, ReviewStatus
from src.core.models import ScorecardEvidencePacket, WorkItem
from src.core.models_v2 import ActionItem, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, DecisionAsk, DecisionEntry, DecisionStatus, Dependency, DependencyScheduleStatus, LeadershipReader, Milestone, MilestoneAssessment, MilestoneStatus, PersonDirectory, Signal
from src.core.narrative_store import load_narratives
from src.core.overrides_store import load_overrides
from src.core.program_fact_store import (
    load_program_facts,
    project_action_items,
    project_assumptions,
    project_decision_entries,
    project_dependencies,
    project_milestones,
    project_risk_entries,
)
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score
from src.core.review_status_store import load_review_status
from src.core.reviewer_renderer import ReviewerAnticipatedQuestion, ReviewerAttentionGapRow, ReviewerContextRow, ReviewerDeltaRow, ReviewerEvidenceRow, ReviewerInlineLink, ReviewerMilestoneTimelineRow, ReviewerOverrideRow, ReviewerRenderContext, ReviewerSignalThreadEvent, ReviewerSignalThreadRow, ReviewerTrackedEntryRow, ReviewerTrustRow, ReviewerVitalityRow, ReviewerWhyBlock, ReviewerWhyLine
from src.core.reviewer_renderer import ReviewerRenderer, ReviewerSectionData, ReviewerSimilarityBadge, ReviewerStatusChip
from src.core.semantic_index import find_archive_similarity_match
from src.core.signal_ranking import signal_source_family, sort_signals_for_ai_context
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.summary_store import load_summary
from src.core.telemetry_summary import build_approved_telemetry_summary
from src.core.feedback.trust_profile_store import build_trust_profile_snapshot
from src.core.trusted_baseline_store import load_trusted_baseline_issue
from src.core.trajectory_analyzer import DriftPattern
from src.core.vitality_reporting import vitality_settings_from_program
from src.core.vitality_scorer import aggregate_vitality
from src.core.view_models import WorkstreamData
from src.core.models_v2 import RiskEntry, RiskStatus
from src.m365.adaptive_card_renderer import AdaptiveCardRenderer
from src.m365.agency_bridge import AgencyBridge
from src.m365.enricher import M365Enricher
from src.m365.teams_webhook_client import TeamsWebhookClient


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewFullArtifacts:
    issue_number: int
    html_path: Path
    adaptive_card_paths: tuple[Path, ...] = ()
    posted_card_count: int = 0


@dataclass(frozen=True, slots=True)
class ReviewFullContextData:
    issue_number: int
    bundle: ReportBundle
    reviewer_context: ReviewerRenderContext
    items: tuple[WorkItem, ...]
    program_id: str | None


SectionReviewCardSender = Callable[[str, dict[str, Any]], None]


def _build_similarity_badge(
    *,
    edition_name: str,
    published_text: str | None,
    archive_root: Path,
    enabled: bool,
    min_similarity: float,
) -> ReviewerSimilarityBadge | None:
    if not enabled or published_text is None or not published_text.strip():
        return None
    match = find_archive_similarity_match(
        edition_name,
        published_text,
        archive_root=archive_root,
        min_similarity=min_similarity,
    )
    if match is None:
        return None
    if match.issue_number is None:
        return None
    return ReviewerSimilarityBadge(
        issue_number=match.issue_number,
        generated_at=match.generated_at,
        similarity=match.similarity,
        excerpt=match.excerpt,
        risk_level=match.risk_level,
    )

def _semantic_similarity_threshold(raw_program: dict[str, Any] | None) -> float:
    if not isinstance(raw_program, dict):
        return 0.92
    ai_config = raw_program.get("ai")
    if not isinstance(ai_config, dict):
        return 0.92
    raw_threshold = ai_config.get("semantic_similarity_threshold", 0.92)
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        return 0.92
    return max(0.0, min(1.0, threshold))
def review_full_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to render. Defaults to the active issue."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the reviewer HTML in the browser after rendering."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    post_adaptive_cards: bool = typer.Option(
        False,
        "--post-adaptive-cards",
        help="Post generated section review adaptive cards to Teams when a webhook is configured.",
    ),
) -> None:
    try:
        artifacts = generate_review_full(
            edition_name=edition,
            issue_number=issue,
            open_browser=open_browser,
            post_adaptive_cards=post_adaptive_cards,
        )
    except (AuthError, QueryError, typer.BadParameter) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    if format == "human":
        typer.echo(f"Leadership review view generated for Issue {artifacts.issue_number:03d}.")
        typer.echo(f"Reviewer HTML: {artifacts.html_path}")
        if artifacts.posted_card_count:
            typer.echo(f"Section review cards posted: {artifacts.posted_card_count}")
    else:
        typer.echo(render_review_full_output(edition, artifacts, format=format), nl=False)
    raise typer.Exit(code=0)


def render_review_full_output(edition: str, artifacts: ReviewFullArtifacts, *, format: str) -> str:
    payload: dict[str, Any] = {
        "adaptive_card_count": len(artifacts.adaptive_card_paths),
        "adaptive_card_paths": [str(path) for path in artifacts.adaptive_card_paths],
        "edition_name": edition,
        "html_path": str(artifacts.html_path),
        "issue_number": artifacts.issue_number,
        "posted_card_count": artifacts.posted_card_count,
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("entry_type", "edition_name", "issue_number", "html_path", "adaptive_card_count", "posted_card_count", "adaptive_card_path"))
        writer.writerow(
            (
                "summary",
                payload["edition_name"],
                payload["issue_number"],
                payload["html_path"],
                payload["adaptive_card_count"],
                payload["posted_card_count"],
                None,
            )
        )
        for adaptive_card_path in payload["adaptive_card_paths"]:
            writer.writerow(
                (
                    "adaptive_card",
                    payload["edition_name"],
                    payload["issue_number"],
                    payload["html_path"],
                    payload["adaptive_card_count"],
                    payload["posted_card_count"],
                    adaptive_card_path,
                )
            )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def generate_review_full(
    edition_name: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    m365_enricher: M365Enricher | None = None,
    open_browser: bool = False,
    post_adaptive_cards: bool = False,
) -> ReviewFullArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    review_data = prepare_review_full_context(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=resolved_reports_root,
        archive_root=archive_root,
        m365_enricher=m365_enricher,
    )
    reviewer_html = ReviewerRenderer(edition_name, reports_root=resolved_reports_root).render(review_data.reviewer_context)
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    reviewer_path = _write_output_text(
        get_program_output_dir(edition_name, programs_root=resolved_programs_root) / "review" / f"issue_{review_data.issue_number:03d}.html",
        reviewer_html,
    )
    card_sender: SectionReviewCardSender | None = None
    if post_adaptive_cards:
        teams_webhook_url = review_data.bundle.config.m365.teams_incoming_webhook_url
        if not teams_webhook_url:
            raise typer.BadParameter(
                "m365.teams_incoming_webhook_url must be configured in program.yaml before --post-adaptive-cards can be used."
            )
        card_sender = _build_section_review_teams_sender(teams_webhook_url)
    adaptive_card_paths = _write_section_review_adaptive_cards(
        bundle=review_data.bundle,
        edition_name=edition_name,
        issue_number=review_data.issue_number,
        sections=review_data.reviewer_context.sections,
        programs_root=resolved_programs_root,
        reviewer_html_path=reviewer_path,
        sender=card_sender,
    )
    if open_browser:
        webbrowser.open(reviewer_path.resolve().as_uri())
    return ReviewFullArtifacts(
        issue_number=review_data.issue_number,
        html_path=reviewer_path,
        adaptive_card_paths=adaptive_card_paths,
        posted_card_count=len(adaptive_card_paths) if card_sender is not None else 0,
    )


def prepare_review_full_context(
    edition_name: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    m365_enricher: M365Enricher | None = None,
) -> ReviewFullContextData:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    repo_root = resolved_reports_root.parent
    programs_root = programs_root if programs_root is not None else (repo_root / "programs")

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=programs_root,
    )
    resolved_issue_number, review_status = _load_review_full_context(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
    )
    draft_state = _load_draft_state(edition_name, resolved_issue_number, programs_root=programs_root)
    published_html_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{resolved_issue_number:03d}" / f"issue_{resolved_issue_number:03d}.html"
    if not published_html_path.exists():
        raise typer.BadParameter(
            f"Draft HTML not found at {published_html_path}. Run `vertex report --dry-run --edition {edition_name} --issue {resolved_issue_number}` first."
        )

    draft_items = tuple(draft_state.get("items", []))
    items = _deserialize_items(tuple(draft_items))
    data_as_of = datetime.fromisoformat(str(draft_state["ado_data_as_of"]))
    enrichments_by_item: dict[int, tuple] = {}
    if bundle.config.m365.enabled:
        try:
            enrichments_by_item = (m365_enricher or M365Enricher(AgencyBridge())).enrich_items(
                config=bundle.config.m365,
                program_context=bundle.program_context,
                items=items,
                as_of=data_as_of,
            )
        except Exception as exc:
            log.warning("M365 enrichment unavailable for review pane: %s", exc)
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=resolved_issue_number,
        programs_root=resolved_reports_root.parent / "programs",
    )
    previous_snapshot, previous_issue_number = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        archive_root=resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    evidence_window_start = data_as_of - timedelta(days=bundle.config.ado.date_window_days)
    evidence_by_item = {
        item.id: build_evidence(item, evidence_window_start, data_as_of, enrichments_by_item=enrichments_by_item)
        for item in items
    }
    deltas = build_deltas(
        current_items=items,
        previous_snapshot=previous_snapshot,
        issue_number=resolved_issue_number,
        previous_issue_number=previous_issue_number,
        evidence_by_item=evidence_by_item,
    )
    guarded_review_evidence = _load_guarded_review_evidence(
        edition_name=edition_name,
        bundle=bundle,
        items=items,
        as_of=data_as_of,
        previous_snapshot=previous_snapshot,
        reports_root=resolved_reports_root,
    )

    overrides_document = load_overrides(
        edition_name,
        reports_root=resolved_reports_root,
        issue_number=resolved_issue_number,
    )
    if overrides_document is None or overrides_document.issue_number != resolved_issue_number:
        raise typer.BadParameter(
            f"overrides.yaml is not initialized for Issue {resolved_issue_number:03d}. Run `vertex report --dry-run --edition {edition_name}` first."
        )

    scorecard_packets = _build_scorecard_packets(bundle, items, previous_snapshot)
    scorecards, _, _ = _build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    loaded_narratives = load_narratives(edition_name, resolved_issue_number, reports_root=resolved_reports_root)
    loaded_exec_summary_text = loaded_narratives.get("exec_summary.md", "").strip()
    draft_exec_summary_text = str(draft_state.get("exec_summary_text", "")).strip()
    exec_summary_text = loaded_exec_summary_text or draft_exec_summary_text
    top_items = _build_top_items(overrides_document, scorecards)
    visible_section_ids = _visible_detail_section_ids(
        bundle,
        overrides_document,
        edition_type=EditionType.from_string(bundle.config.edition.type),
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        deltas=deltas,
        top_items=top_items,
    )
    loaded_workstream_blurbs = _active_workstream_blurbs(loaded_narratives, visible_section_ids)
    raw_draft_workstream_blurbs = draft_state.get("workstream_blurbs", {})
    draft_workstream_blurbs = {
        str(section_id): str(blurb).strip()
        for section_id, blurb in raw_draft_workstream_blurbs.items()
        if str(section_id).strip() and str(blurb).strip()
    } if isinstance(raw_draft_workstream_blurbs, dict) else {}
    workstream_blurbs = {
        section_id: loaded_workstream_blurbs.get(section_id) or draft_workstream_blurbs.get(section_id, "")
        for section_id in visible_section_ids
        if loaded_workstream_blurbs.get(section_id) or draft_workstream_blurbs.get(section_id, "")
    }
    published_html = published_html_path.read_text(encoding="utf-8")
    persona_coverage = _load_persona_coverage_artifact(
        programs_root=programs_root,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
    )
    item_urls = _build_item_urls(bundle, items)
    resolved_v2 = resolve_edition(edition_name, programs_root=programs_root)
    program_facts = (
        load_program_facts(resolved_v2.program.id, db_root=repo_root, programs_root=programs_root)
        if resolved_v2 is not None
        else None
    )
    vitality_settings = vitality_settings_from_program(resolved_v2.raw_program) if resolved_v2 is not None else None
    owner_vitality_rows: tuple[ReviewerVitalityRow, ...] = ()
    if vitality_settings is not None and vitality_settings.reviewer_pane:
        owner_vitality_rows = _build_owner_vitality_rows(
            resolved_v2=resolved_v2,
            items=items,
            as_of=data_as_of,
            programs_root=programs_root,
        )
    coverage_gap_rows: tuple[ReviewerTrackedEntryRow, ...] = ()
    coverage_gap_window_days = bundle.config.ado.date_window_days
    if resolved_v2 is not None and resolved_v2.edition.altitude == "helicopter":
        coverage_gap_rows = tuple(
            ReviewerTrackedEntryRow(
                title=_format_coverage_gap_row_title(gap),
                detail=(f"Owner {gap.assigned_to}" if gap.assigned_to else "Owner unassigned"),
                summary=gap.title,
            )
            for gap in build_coverage_gaps(
                items,
                approved_signals=guarded_review_evidence.approved_signals,
                narratives=loaded_narratives,
                as_of=data_as_of,
            )
        )
    summary_lookup = _load_reviewer_summaries(resolved_v2=resolved_v2, programs_root=programs_root)
    anticipation_client = _build_default_anticipation_client(
        bundle=bundle,
        trace_context=_build_review_full_trace_context(
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            budget_usd=bundle.config.ai.budget_usd_per_run,
        ),
    )
    reviewer_signal_people_directory: tuple[PersonDirectory, ...] = ()
    reviewer_signal_source_confidence_order: tuple[str, ...] = ()
    if resolved_v2 is not None:
        reviewer_signal_knowledge = load_program_knowledge(resolved_v2.program.id, programs_root=programs_root)
        reviewer_signal_people_directory = reviewer_signal_knowledge.people_directory
        reviewer_signal_source_confidence_order = resolved_v2.program.source_confidence_order

    reviewer_context = _build_reviewer_context(
        bundle=bundle,
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        data_as_of=data_as_of,
        reports_root=resolved_reports_root,
        published_html=published_html,
        published_html_uri=_artifact_url(bundle, artifact_path=published_html_path),
        items=items,
        deltas=deltas,
        evidence_by_item=evidence_by_item,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        review_status=review_status,
        exec_summary_text=exec_summary_text,
        workstream_blurbs=workstream_blurbs,
        overrides_document=overrides_document,
        item_urls=item_urls,
        previous_snapshot=previous_snapshot,
        archive_root=resolved_archive_root,
        approved_signals=guarded_review_evidence.approved_signals,
        drift_patterns=guarded_review_evidence.drift_patterns,
        dependencies=(project_dependencies(program_facts) if program_facts is not None else ()),
        decisions=(project_decision_entries(program_facts) if program_facts is not None else ()),
        risks=(project_risk_entries(program_facts) if program_facts is not None else ()),
        active_actions=(
            tuple(
                action
                for action in project_action_items(program_facts)
                if action.status in {ActionStatus.PROPOSED, ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
            )
            if program_facts is not None
            else ()
        ),
        summary_lookup=summary_lookup,
        anticipation_client=anticipation_client,
        owner_vitality_rows=owner_vitality_rows,
        coverage_gap_rows=coverage_gap_rows,
        coverage_gap_window_days=coverage_gap_window_days,
        program_id=(resolved_v2.program.id if resolved_v2 is not None else None),
        raw_workstreams=(resolved_v2.raw_workstreams if resolved_v2 is not None else None),
        semantic_index_enabled=(_semantic_index_enabled(resolved_v2.raw_program) if resolved_v2 is not None else False),
        semantic_similarity_threshold=(
            _semantic_similarity_threshold(resolved_v2.raw_program) if resolved_v2 is not None else 0.92
        ),
        programs_root=programs_root,
        signal_people_directory=reviewer_signal_people_directory,
        signal_source_confidence_order=reviewer_signal_source_confidence_order,
        persona_coverage=persona_coverage,
    )
    return ReviewFullContextData(
        issue_number=resolved_issue_number,
        bundle=bundle,
        reviewer_context=reviewer_context,
        items=items,
        program_id=(resolved_v2.program.id if resolved_v2 is not None else None),
    )


def _write_section_review_adaptive_cards(
    *,
    bundle: ReportBundle,
    edition_name: str,
    issue_number: int,
    sections: tuple[ReviewerSectionData, ...],
    programs_root: Path = PROGRAMS_ROOT,
    reviewer_html_path: Path,
    sender: SectionReviewCardSender | None = None,
) -> tuple[Path, ...]:
    if not sections:
        return ()

    program_output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    renderer = AdaptiveCardRenderer()
    review_html_url = _artifact_url(bundle, output_root=program_output_dir, artifact_path=reviewer_html_path)
    card_paths: list[Path] = []
    for section in sections:
        filename = _sanitize_review_filename_component(section.section_id)
        card_path = program_output_dir / "review" / "adaptive_cards" / f"issue_{issue_number:03d}.{filename}.section_review.json"
        payload = renderer.render_section_review(
            edition_name=edition_name,
            issue_number=issue_number,
            section=section,
            review_html_url=review_html_url,
        )
        if sender is not None:
            sender(section.section_id, payload)
        card_paths.append(_write_output_json(card_path, payload))
    return tuple(card_paths)


def _build_section_review_teams_sender(webhook_url: str) -> SectionReviewCardSender:
    client = TeamsWebhookClient(webhook_url=webhook_url)

    def _sender(section_id: str, payload: dict[str, Any]) -> None:
        client.post_card(payload)

    return _sender


def _sanitize_review_filename_component(value: str) -> str:
    cleaned = [character.lower() if character.isalnum() else "_" for character in value.strip()]
    collapsed = "".join(cleaned).strip("_")
    return collapsed or "section"


def _load_review_full_context(
    *,
    edition_name: str,
    issue_number: int | None,
    reports_root: Path,
    archive_root: Path,
) -> tuple[int, ReviewStatus]:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    review_status = load_review_status(edition_name, reports_root=reports_root)
    if issue_number is None:
        if review_status is not None and review_status.issue_number > 0:
            resolved_issue_number = review_status.issue_number
        else:
            latest = find_latest_confirmed_entry(archive_index)
            resolved_issue_number = 1 if latest is None else latest.issue_number + 1
    else:
        resolved_issue_number = issue_number
    if review_status is None or review_status.issue_number != resolved_issue_number:
        raise typer.BadParameter(
            f"review_status.yaml is not initialized for Issue {resolved_issue_number:03d}. Run `vertex report --dry-run --edition {edition_name} --issue {resolved_issue_number}` first."
        )
    return resolved_issue_number, review_status


def _build_reviewer_context(
    *,
    bundle: ReportBundle,
    edition_name: str,
    issue_number: int,
    data_as_of: datetime,
    reports_root: Path,
    published_html: str,
    published_html_uri: str,
    items: tuple[WorkItem, ...],
    deltas,
    evidence_by_item: dict[int, EvidencePacket],
    scorecards: tuple,
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    review_status: ReviewStatus,
    exec_summary_text: str,
    workstream_blurbs: dict[str, str],
    overrides_document,
    item_urls: dict[int, str],
    previous_snapshot,
    archive_root: Path,
    approved_signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    dependencies,
    decisions,
    risks,
    active_actions,
    summary_lookup: dict[str, str],
    anticipation_client: LLMProvider | None,
    owner_vitality_rows: tuple[ReviewerVitalityRow, ...],
    coverage_gap_rows: tuple[ReviewerTrackedEntryRow, ...],
    coverage_gap_window_days: int,
    program_id: str | None,
    raw_workstreams: dict[str, Any] | None,
    semantic_index_enabled: bool,
    semantic_similarity_threshold: float,
    programs_root: Path,
    signal_people_directory: tuple[PersonDirectory, ...],
    signal_source_confidence_order: tuple[str, ...],
    persona_coverage: dict[str, Any] | None = None,
) -> ReviewerRenderContext:
    assigned_reviewers = _assigned_reviewers_by_section(bundle)
    review_lookup = {section.section_id: section for section in review_status.sections}
    item_lookup = {item.id: item for item in items}
    detail_sections = _build_workstream_data(
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
        evidence_by_item=evidence_by_item,
        item_urls=item_urls,
    )
    forecast = build_forecast_assessment(
        enabled=bundle.config.forecast_enabled,
        edition_name=edition_name,
        as_of=data_as_of,
        workstreams=detail_sections,
        deltas=deltas,
        archive_root=archive_root,
    )
    lineage_lookup = build_lineage_lookup(
        edition_name=edition_name,
        issue_number=issue_number,
        edition_type=EditionType.from_string(bundle.config.edition.type),
        workstreams=detail_sections,
        items=items,
        deltas=deltas,
        evidence_by_item=evidence_by_item,
        overrides_document=overrides_document,
        previous_snapshot=previous_snapshot,
        archive_root=archive_root,
        forecast=forecast,
    )
    anticipated_questions = tuple(
        ReviewerAnticipatedQuestion(
            reader=question.reader,
            question=question.question,
            suggested_response=question.suggested_response,
            confidence=question.confidence.value,
            evidence=question.evidence,
        )
        for question in anticipate_questions(
            readers=tuple(
                LeadershipReader(
                    name=reader.name,
                    role=reader.role,
                    cares_about=reader.cares_about,
                    prefers=reader.prefers,
                    pet_peeves=reader.pet_peeves,
                )
                for reader in (bundle.program_context.leadership_readers if bundle.program_context is not None else ())
            ),
            signals=approved_signals,
            drift_patterns=drift_patterns,
            summaries=summary_lookup,
            workstreams=detail_sections,
            dependencies=dependencies,
            client=anticipation_client,
        )
    )
    trust_editorial_rows: tuple[ReviewerTrustRow, ...] = ()
    trust_claim_extraction_rows: tuple[ReviewerTrustRow, ...] = ()
    trust_autonomy_rows: tuple[ReviewerTrustRow, ...] = ()
    trust_attention_gap_rows: tuple[ReviewerAttentionGapRow, ...] = ()
    if program_id is not None:
        trust_report = build_trust_profile_snapshot(
            program_id,
            programs_root=programs_root,
            as_of=data_as_of,
        )
        trust_editorial_rows = tuple(
            ReviewerTrustRow(
                label=row.label,
                summary=(
                    f"override={row.average_override_magnitude:.4f} | calibration={row.calibration_score:.4f} | samples={row.sample_count}"
                ),
                trust_level=row.trust_level,
                confidence_percent=round(row.calibration_score * 100),
            )
            for row in trust_report.editorial_rows
        )
        trust_claim_extraction_rows = tuple(
            ReviewerTrustRow(
                label=row.label,
                summary=(
                    f"agreement={row.agreement_rate:.4f} | avg_difference={row.average_difference_count:.2f} | samples={row.sample_count} | calibration_samples={row.calibration_sample_count}"
                ),
                trust_level=row.trust_level,
                confidence_percent=round(row.agreement_rate * 100),
            )
            for row in trust_report.claim_extraction_rows
        )
        trust_autonomy_rows = tuple(
            ReviewerTrustRow(
                label=row.label,
                summary=(
                    f"accepted={row.accepted_count}/{row.sample_count} ({round(row.acceptance_rate * 100):d}%) | level={row.latest_level}"
                ),
                trust_level=row.trust_level,
                confidence_percent=round(row.acceptance_rate * 100),
            )
            for row in trust_report.autonomy_rows
        )
        trust_attention_gap_rows = tuple(
            ReviewerAttentionGapRow(
                workstream_id=row.workstream_id,
                slip_modifier=row.slip_modifier,
                attention_weight=row.attention_weight,
                bridge_summary=row.bridge_summary,
            )
            for row in trust_report.attention_gap_rows
        )
    open_claim_rows = _build_open_claim_rows(
        program_id,
        items=items,
        as_of=data_as_of,
        programs_root=programs_root,
    )
    telemetry_rows = _build_telemetry_rows(approved_signals)
    cascade_rows = _build_cascade_rows(
        dependencies=dependencies,
        approved_signals=approved_signals,
        drift_patterns=drift_patterns,
        items=items,
        as_of=data_as_of,
        program_id=program_id,
        programs_root=programs_root,
        signal_window_days=bundle.config.ado.date_window_days,
    )
    dependency_lifecycle_rows = _build_dependency_lifecycle_rows(dependencies, program_id)
    signal_thread_rows = _build_signal_thread_rows(approved_signals)
    reference_doc_rows = _build_reference_doc_rows(
        program_id,
        workstream_ids=_review_workstream_ids(bundle=bundle, raw_workstreams=raw_workstreams),
        programs_root=programs_root,
    )
    risk_rows = _build_risk_rows(
        risks,
        as_of=data_as_of,
    )
    milestone_assessments = _build_milestone_assessments(
        program_id,
        dependencies=dependencies,
        items=items,
        as_of=data_as_of,
        programs_root=programs_root,
    )
    milestone_timeline_rows = _build_milestone_timeline_rows(milestone_assessments)
    milestone_rows = _build_milestone_rows(milestone_assessments)
    decision_rows = _build_decision_rows(
        decisions,
        as_of=data_as_of,
    )
    assumption_rows = _build_assumption_rows(
        program_id,
        as_of=data_as_of,
        programs_root=programs_root,
    )
    action_rows = _build_action_rows(
        active_actions,
        as_of=data_as_of,
        programs_root=programs_root,
    )
    eta_forecasts = _load_eta_forecasts(
        edition_name=edition_name,
        items=items,
        as_of=data_as_of,
        reports_root=reports_root,
    )
    issue_rows = _build_issue_rows(
        bundle=bundle,
        issue_number=issue_number,
        items=items,
        previous_snapshot=previous_snapshot,
        program_id=program_id,
        as_of=data_as_of,
        eta_forecasts=eta_forecasts,
        programs_root=programs_root,
        risk_entries=risks,
        active_actions=active_actions,
    )
    open_ask_rows = _build_open_ask_rows(
        program_id,
        programs_root=programs_root,
    )
    raci_entries = _build_raci_entries(raw_workstreams)

    status_chips: list[ReviewerStatusChip] = []
    sections: list[ReviewerSectionData] = []

    exec_section = _section_or_pending(review_lookup.get("exec_summary"), "exec_summary")
    status_chips.append(
        ReviewerStatusChip(
            section_id="exec_summary",
            label="Executive Summary",
            state=exec_section.state,
            reviewer=_resolved_reviewer(exec_section, assigned_reviewers),
        )
    )
    exec_citations = build_inline_citations(items, evidence_by_item, ado_base_url=_ado_item_base_url(bundle))
    sections.append(
        ReviewerSectionData(
            section_id="exec_summary",
            title="Executive Summary",
            published_text=exec_summary_text or "No executive summary narrative is stored for this issue.",
            state=exec_section.state,
            reviewer=_resolved_reviewer(exec_section, assigned_reviewers),
            note=exec_section.note,
            delta_rows=_build_section_delta_rows(deltas, set(item_lookup), item_lookup, item_urls),
            evidence_rows=_build_evidence_rows_from_citations(exec_citations, evidence_by_item),
            context_rows=_build_context_rows(
                approved_signals=approved_signals,
                drift_patterns=drift_patterns,
                item_ids=set(item_lookup),
                item_urls=item_urls,
                as_of=data_as_of,
                people_directory=signal_people_directory,
                source_confidence_order=signal_source_confidence_order,
            ),
            override_rows=_build_exec_summary_override_rows(scorecards, scorecard_packets),
            why_block=_build_why_block(lineage_lookup.claims_for_section("exec_summary"), issue_number),
            item_ids=tuple(sorted(item_lookup)),
            similarity_badge=_build_similarity_badge(
                edition_name=edition_name,
                published_text=exec_summary_text,
                archive_root=archive_root,
                enabled=semantic_index_enabled,
                min_similarity=semantic_similarity_threshold,
            ),
        )
    )
    for workstream in detail_sections:
        section_key = f"ws:{workstream.section_id}"
        workstream_section = _section_or_pending(review_lookup.get(section_key), section_key)
        status_chips.append(
            ReviewerStatusChip(
                section_id=section_key,
                label=workstream.title,
                state=workstream_section.state,
                reviewer=_resolved_reviewer(workstream_section, assigned_reviewers),
                raci_summary=_build_section_raci_summary(workstream.items, raci_entries),
            )
        )
        workstream_item_ids = {item.id for item in workstream.items}
        workstream_delta_rows = _build_section_delta_rows(deltas, workstream_item_ids, item_lookup, item_urls)
        sections.append(
            ReviewerSectionData(
                section_id=section_key,
                title=workstream.title,
                published_text=workstream.blurb or "No narrative is stored for this section.",
                state=workstream_section.state,
                reviewer=_resolved_reviewer(workstream_section, assigned_reviewers),
                note=workstream_section.note,
                delta_rows=workstream_delta_rows,
                evidence_rows=_build_workstream_evidence_rows(
                    workstream_items=workstream.items,
                    delta_rows=workstream_delta_rows,
                    citations=workstream.citations,
                    evidence_by_item=evidence_by_item,
                ),
                context_rows=_build_context_rows(
                    approved_signals=approved_signals,
                    drift_patterns=drift_patterns,
                    item_ids=workstream_item_ids,
                    item_urls=item_urls,
                    as_of=data_as_of,
                    people_directory=signal_people_directory,
                    source_confidence_order=signal_source_confidence_order,
                ),
                override_rows=_build_workstream_override_rows(workstream),
                why_block=_build_why_block(lineage_lookup.claims_for_section(workstream.section_id), issue_number),
                item_ids=tuple(sorted(workstream_item_ids)),
                similarity_badge=_build_similarity_badge(
                    edition_name=edition_name,
                    published_text=workstream.blurb,
                    archive_root=archive_root,
                    enabled=semantic_index_enabled,
                    min_similarity=semantic_similarity_threshold,
                ),
            )
        )

    return ReviewerRenderContext(
        title=_format_edition_title(bundle, issue_number, data_as_of),
        subtitle=f"Issue {issue_number:03d} leadership review",
        edition_name=edition_name,
        issue_number=issue_number,
        published_html=published_html,
        published_html_uri=published_html_uri,
        anticipated_questions=anticipated_questions,
        trust_editorial_rows=trust_editorial_rows,
        trust_claim_extraction_rows=trust_claim_extraction_rows,
        trust_autonomy_rows=trust_autonomy_rows,
        trust_attention_gap_rows=trust_attention_gap_rows,
        owner_vitality_rows=owner_vitality_rows,
        coverage_gap_rows=coverage_gap_rows,
        coverage_gap_window_days=coverage_gap_window_days,
        telemetry_rows=telemetry_rows,
        open_claim_rows=open_claim_rows,
        risk_rows=risk_rows,
        milestone_timeline_rows=milestone_timeline_rows,
        milestone_rows=milestone_rows,
        cascade_rows=cascade_rows,
        signal_thread_rows=signal_thread_rows,
        decision_rows=decision_rows,
        assumption_rows=assumption_rows,
        reference_doc_rows=reference_doc_rows,
        action_rows=action_rows,
        issue_rows=issue_rows,
        open_ask_rows=open_ask_rows,
        status_chips=tuple(status_chips),
        sections=tuple(sections),
        dependency_lifecycle_rows=dependency_lifecycle_rows,
        persona_coverage=persona_coverage,
    )


def _build_raci_entries(raw_workstreams: dict[str, Any] | None) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    if not isinstance(raw_workstreams, dict):
        return ()

    entries = raw_workstreams.get("workstreams")
    if not isinstance(entries, list):
        return ()

    summaries: list[tuple[str, tuple[str, ...], str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        workstream_id = entry.get("id")
        if not isinstance(workstream_id, str) or not workstream_id.strip():
            continue
        area_paths = tuple(
            path.strip()
            for path in entry.get("area_paths", [])
            if isinstance(path, str) and path.strip()
        )
        if not area_paths:
            continue
        raci = entry.get("raci")
        if not isinstance(raci, dict):
            continue

        parts: list[str] = []
        accountable = _normalize_raci_display_value(raci.get("accountable"))
        if accountable is not None:
            parts.append(f"A {accountable}")
        for label, field_name in (("R", "responsible"), ("C", "consulted"), ("I", "informed")):
            aliases = _normalize_raci_display_list(raci.get(field_name))
            if aliases:
                parts.append(f"{label} {', '.join(aliases)}")
        if parts:
            summaries.append((workstream_id.strip(), area_paths, "RACI: " + " | ".join(parts)))
    return tuple(summaries)

def _review_workstream_ids(*, bundle: ReportBundle, raw_workstreams: dict[str, Any] | None) -> tuple[str, ...]:
    ids: list[str] = []
    if isinstance(raw_workstreams, dict):
        raw_entries = raw_workstreams.get("workstreams")
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                if isinstance(entry, dict):
                    workstream_id = str(entry.get("id") or "").strip()
                    if workstream_id:
                        ids.append(workstream_id)
    if ids:
        return tuple(dict.fromkeys(ids))
    program_context = bundle.program_context
    if program_context is None:
        return ()
    return tuple(dict.fromkeys(workstream.name for workstream in program_context.workstreams if workstream.name))


def _build_section_raci_summary(
    items: tuple[WorkItem, ...],
    raci_entries: tuple[tuple[str, tuple[str, ...], str], ...],
) -> str | None:
    if not items or not raci_entries:
        return None

    matched: list[tuple[str, str]] = []
    seen: set[str] = set()
    for workstream_id, area_paths, summary in raci_entries:
        if workstream_id in seen:
            continue
        if any(any(_area_path_matches(item.area_path, area_path) for area_path in area_paths) for item in items):
            seen.add(workstream_id)
            matched.append((workstream_id, summary))

    if not matched:
        return None
    if len(matched) == 1:
        return matched[0][1]
    return "RACI: " + "; ".join(
        f"{workstream_id} [{summary.removeprefix('RACI: ')}]"
        for workstream_id, summary in matched
    )


def _area_path_matches(item_area_path: str, configured_area_path: str) -> bool:
    normalized_item = item_area_path.strip().lower()
    normalized_path = configured_area_path.strip().lower()
    return normalized_item == normalized_path or normalized_item.startswith(f"{normalized_path}\\")


def _normalize_raci_display_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() in {"unassigned", "none", "null"}:
        return None
    return normalized


def _normalize_raci_display_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in value:
        normalized = _normalize_raci_display_value(entry)
        if normalized is None:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return tuple(ordered)


def _build_open_claim_rows(
    program_id: str | None,
    *,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root: Path,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    return tuple(
        ReviewerTrackedEntryRow(
            title=f"{assessment.claim.id} · issue #{assessment.claim.issue_number} · {assessment.effective_status}",
            detail=_format_claim_detail(assessment.claim),
            summary=(assessment.claim.text if assessment.reason is None else f"{assessment.claim.text} {assessment.reason}"),
            anchor_id=_review_entity_anchor(assessment.claim.id),
        )
        for assessment in assess_claim_entries(
            load_open_claims(program_id, programs_root=programs_root),
            items=items,
            as_of=as_of,
        )
    )


def _build_telemetry_rows(approved_signals: tuple[Signal, ...]) -> tuple[ReviewerTrackedEntryRow, ...]:
    summary = build_approved_telemetry_summary(approved_signals)
    if summary is None:
        return ()
    telemetry_confidence = _reviewer_telemetry_confidence_label(approved_signals)
    detail = "ADO analytics and sprint signals from the local journal"
    if telemetry_confidence is not None:
        detail += f" | {telemetry_confidence} confidence"
    return (
        ReviewerTrackedEntryRow(
            title="Latest approved telemetry",
            detail=detail,
            summary=summary,
        ),
    )


def _reviewer_telemetry_confidence_label(approved_signals: tuple[Signal, ...]) -> str | None:
    telemetry_signals = [
        signal
        for signal in approved_signals
        if signal.source in {"ado/analytics", "ado/wiql", "ado/sprint", "ado/pipeline", "ado/pr"}
    ]
    if not telemetry_signals:
        return None
    confidence_order = {
        Confidence.HIGH: 3,
        Confidence.MEDIUM: 2,
        Confidence.LOW: 1,
        Confidence.NONE: 0,
    }
    strongest = max(telemetry_signals, key=lambda signal: confidence_order[signal.confidence]).confidence
    return strongest.value.lower()


def _build_open_ask_rows(
    program_id: str | None,
    *,
    programs_root: Path,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    return tuple(
        ReviewerTrackedEntryRow(
            title=f"{entry.id} · issue #{entry.issue_number}",
            detail=_format_ask_detail(entry),
            summary=entry.text,
            anchor_id=_review_entity_anchor(entry.id),
        )
        for entry in load_open_decision_asks(program_id, programs_root=programs_root)
    )


def _build_reference_doc_rows(
    program_id: str | None,
    *,
    workstream_ids: tuple[str, ...],
    programs_root: Path,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    pages = select_engms_pages(knowledge, program_id=program_id, workstream_ids=workstream_ids)[:5]
    return tuple(
        ReviewerTrackedEntryRow(
            title=page.title,
            detail=(f"Workstreams {', '.join(page.workstream_ids)}" if page.workstream_ids else None),
            summary=summarize_engms_page(page),
            href=page.url,
        )
        for page in pages
    )


def _build_risk_rows(
    risks: tuple[RiskEntry, ...],
    *,
    as_of: datetime,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if not risks:
        return ()
    stale_ids = {entry.id for entry in risks if assess_risk_staleness(entry, as_of.date())}
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_risk_row_title(entry, stale=entry.id in stale_ids),
            detail=_format_risk_row_detail(entry),
            summary=_format_risk_row_summary(entry),
            anchor_id=_review_entity_anchor(entry.id),
        )
        for entry in sorted(
            risks,
            key=lambda entry: (
                0 if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED} else 1,
                0 if entry.id in stale_ids else 1,
                -compute_risk_score(entry),
                entry.title.lower(),
            ),
        )
    )


def _build_milestone_assessments(
    program_id: str | None,
    *,
    dependencies: tuple[Dependency, ...],
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root: Path,
) -> tuple[tuple[Milestone, MilestoneAssessment, bool, str | None, str | None, str | None], ...]:
    if program_id is None:
        return ()
    milestones = project_milestones(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    )
    if not milestones:
        return ()
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    trajectories = {
        item_id: trajectory_store.read(program_id, item_id)
        for milestone in milestones
        for item_id in milestone.linked_work_item_ids
    }
    target_date_history = load_milestone_target_date_history_map(
        program_id,
        milestones,
        programs_root=programs_root,
    )
    critical_path_ids = {milestone.id for milestone in build_critical_path(milestones, dependencies)}
    assessments = tuple(
        assess_milestone_health(milestone, items, trajectories, as_of)
        for milestone in milestones
    )
    completion_date_history = load_milestone_completion_date_history_map(
        program_id,
        milestones,
        current_completion_dates={assessment.milestone_id: assessment.completion_date for assessment in assessments},
        programs_root=programs_root,
    )
    return tuple(
        (
            milestone,
            assessment,
            milestone.id in critical_path_ids,
            describe_milestone_schedule_variance(milestone, items, trajectories, as_of),
            summarize_milestone_target_date_history(
                target_date_history.get(milestone.id, ()),
                prefix="target history",
            ),
            summarize_milestone_completion_date_history(
                completion_date_history.get(milestone.id, ()),
                prefix="completion history",
            ),
        )
        for milestone, assessment in zip(milestones, assessments, strict=False)
    )


def _build_milestone_timeline_rows(
    assessed_rows: tuple[tuple[Milestone, MilestoneAssessment, bool, str | None, str | None, str | None], ...],
) -> tuple[ReviewerMilestoneTimelineRow, ...]:
    return tuple(
        ReviewerMilestoneTimelineRow(
            milestone_id=milestone.id,
            name=milestone.name,
            target_date_label=milestone.target_date.isoformat(),
            declared_status=milestone.status.value,
            computed_status=assessment.computed_health.value,
            critical_path=critical_path,
            schedule_summary=schedule_summary,
            target_history_summary=target_history_summary,
            completion_history_summary=completion_history_summary,
        )
        for milestone, assessment, critical_path, schedule_summary, target_history_summary, completion_history_summary in sorted(
            assessed_rows,
            key=lambda entry: (entry[0].target_date, entry[0].name.lower()),
        )
    )


def _build_milestone_rows(
    assessed_rows: tuple[tuple[Milestone, MilestoneAssessment, bool, str | None, str | None, str | None], ...],
) -> tuple[ReviewerTrackedEntryRow, ...]:
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_milestone_row_title(milestone, assessment, critical_path=critical_path),
            detail=_format_milestone_row_detail(
                milestone,
                assessment,
                schedule_summary=schedule_summary,
                target_history_summary=target_history_summary,
                completion_history_summary=completion_history_summary,
            ),
            summary=assessment.reasoning,
        )
        for milestone, assessment, critical_path, schedule_summary, target_history_summary, completion_history_summary in sorted(
            assessed_rows,
            key=lambda entry: (
                0 if entry[1].computed_health in {MilestoneStatus.AT_RISK, MilestoneStatus.MISSED} else 1,
                entry[0].target_date,
                entry[0].name.lower(),
            ),
        )
    )


def _build_dependency_lifecycle_rows(
    dependencies: tuple[Dependency, ...],
    program_id: str | None,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    rows: list[ReviewerTrackedEntryRow] = []
    for dep in dependencies:
        if dep.from_program_id != program_id:
            continue
        if dep.schedule_status is None and dep.planned_resolution_date is None:
            continue
        status_label = dep.schedule_status.value.replace("_", " ").title() if dep.schedule_status else None
        detail_parts: list[str] = []
        if status_label:
            detail_parts.append(f"Schedule: {status_label}")
        if dep.planned_resolution_date is not None:
            detail_parts.append(f"Target: {dep.planned_resolution_date.isoformat()}")
        rows.append(
            ReviewerTrackedEntryRow(
                title=f"{dep.from_program_id} \u2192 {dep.to_program_id}",
                summary=dep.risk_if_broken,
                detail=" | ".join(detail_parts) or None,
            )
        )
    return tuple(rows)


def _build_cascade_rows(
    *,
    dependencies: tuple[Dependency, ...],
    approved_signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    items: tuple[WorkItem, ...],
    as_of: datetime,
    program_id: str | None,
    programs_root: Path,
    signal_window_days: int,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    outbound_dependencies = tuple(
        dependency
        for dependency in dependencies
        if dependency.from_program_id == program_id and dependency.to_program_id != program_id
    )
    inbound_dependencies = load_inbound_cross_program_dependencies(program_id, programs_root=programs_root)
    if not outbound_dependencies and not inbound_dependencies:
        return ()
    cascades = list(
        detect_dependency_cascades(
            dependencies=outbound_dependencies,
            signals=approved_signals,
            drift_patterns=drift_patterns,
            items=items,
            scorecards=(),
            workstreams=(),
        )
    )
    signal_window_start = as_of - timedelta(days=signal_window_days)
    for source_program_id in sorted({dependency.from_program_id for dependency in inbound_dependencies}):
        source_signals = _load_approved_signals_for_program(
            source_program_id,
            start=signal_window_start,
            end=as_of,
            programs_root=programs_root,
        )
        if not source_signals:
            continue
        cascades.extend(
            detect_dependency_cascades(
                dependencies=tuple(
                    dependency
                    for dependency in inbound_dependencies
                    if dependency.from_program_id == source_program_id
                ),
                signals=source_signals,
                drift_patterns=(),
                items=(),
                scorecards=(),
                workstreams=(),
            )
        )
    return tuple(
        ReviewerTrackedEntryRow(
            title=f"{_normalize_cascade_label(cascade.source_item)} impacts {_normalize_cascade_label(cascade.target_item)}",
            detail=_format_cascade_row_detail(cascade),
            summary=cascade.trigger_detail,
        )
        for cascade in sorted(
            cascades,
            key=lambda entry: (
                entry.target_item.lower(),
                entry.source_item.lower(),
                entry.trigger_kind,
                entry.work_item_id or -1,
                entry.trigger_detail.lower(),
            ),
        )
    )


def _load_approved_signals_for_program(
    program_id: str,
    *,
    start: datetime,
    end: datetime,
    programs_root: Path,
) -> tuple[Signal, ...]:
    try:
        signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
        review_decisions = signal_store.read_reviews(program_id)
        approved_signal_ids = {
            signal_id
            for signal_id, decision in review_decisions.items()
            if decision.decision == "approved" and decision.reviewed_at <= end
        }
        if not approved_signal_ids:
            return ()
        return tuple(
            signal
            for signal in signal_store.read(program_id, start=start, end=end)
            if signal.id in approved_signal_ids
        )
    except ValueError as error:
        log.warning("Skipping inbound signal sweep for program %s: %s", program_id, error)
        return ()


def _build_signal_thread_rows(approved_signals: tuple[Signal, ...]) -> tuple[ReviewerSignalThreadRow, ...]:
    grouped: dict[str, list[Signal]] = {}
    for signal in approved_signals:
        if signal.thread_id is None:
            continue
        grouped.setdefault(signal.thread_id, []).append(signal)
    if not grouped:
        return ()

    ordered_groups = sorted(
        grouped.items(),
        key=lambda entry: max(signal.timestamp for signal in entry[1]),
        reverse=True,
    )
    return tuple(
        ReviewerSignalThreadRow(
            thread_id=thread_id,
            signal_count=len(group),
            detail=_format_signal_thread_detail(tuple(group)),
            events=tuple(
                ReviewerSignalThreadEvent(
                    timestamp_label=signal.timestamp.date().isoformat(),
                    source=signal.source,
                    confidence=signal.confidence.value.lower(),
                    text=signal.text,
                )
                for signal in sorted(group, key=lambda entry: entry.timestamp, reverse=True)
            ),
        )
        for thread_id, group in ordered_groups
    )


def _build_decision_rows(
    decisions: tuple[DecisionEntry, ...],
    *,
    as_of: datetime,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if not decisions:
        return ()
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_decision_row_title(entry, as_of=as_of),
            detail=_format_decision_row_detail(entry),
            summary=f"{entry.title}: {entry.decision}",
        )
        for entry in sorted(
            decisions,
            key=lambda entry: (
                0 if entry.status is DecisionStatus.PROPOSED else 1,
                0 if assess_proposed_decision_staleness(entry, as_of.date()) else 1,
                -(entry.decision_date.toordinal() if entry.decision_date is not None else 0),
                entry.title.lower(),
            ),
        )
    )


def _build_assumption_rows(
    program_id: str | None,
    *,
    as_of: datetime,
    programs_root: Path,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    assumptions = project_assumptions(
        load_program_facts(
            program_id,
            db_root=programs_root.parent,
            programs_root=programs_root,
            fact_types=("assumption.entry",),
        )
    )
    overdue_ids = {entry.id for entry in check_validation_due(assumptions, as_of.date())}
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_assumption_row_title(entry, overdue=entry.id in overdue_ids),
            detail=_format_assumption_row_detail(entry),
            summary=entry.text,
            anchor_id=_review_entity_anchor(entry.id),
        )
        for entry in sorted(
            assumptions,
            key=lambda entry: (
                0 if entry.id in overdue_ids else 1,
                0 if entry.status is AssumptionStatus.UNVALIDATED else 1,
                entry.validation_due or date.max,
                entry.identified_date,
                entry.text.lower(),
            ),
        )
    )


def _load_persona_coverage_artifact(
    *,
    programs_root: Path = PROGRAMS_ROOT,
    edition_name: str,
    issue_number: int,
) -> dict[str, Any] | None:
    path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.persona_signal_coverage.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _build_action_rows(
    active_actions: tuple[ActionItem, ...],
    *,
    as_of: datetime,
    programs_root: Path,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if not active_actions:
        return ()
    overdue_ids = {entry.id for entry in assess_action_staleness(active_actions, as_of.date())}
    program_id = active_actions[0].program_id
    resolution_candidate_ids = load_action_resolution_candidate_ids(
        program_id,
        active_actions,
        programs_root=programs_root,
    )
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_action_row_title(
                entry,
                overdue=entry.id in overdue_ids,
                resolution_candidate=entry.id in resolution_candidate_ids,
            ),
            detail=_format_action_row_detail(entry),
            summary=entry.text,
            anchor_id=_review_entity_anchor(entry.id),
        )
        for entry in sorted(
            active_actions,
            key=lambda entry: (
                entry.owner_alias.lower(),
                0 if entry.id in overdue_ids else 1,
                entry.due_date or date.max,
                entry.text.lower(),
            ),
        )
    )


def _build_issue_rows(
    *,
    bundle: ReportBundle,
    issue_number: int,
    items: tuple[WorkItem, ...],
    previous_snapshot,
    program_id: str | None,
    as_of: datetime,
    eta_forecasts: dict[int, ETAForecast],
    programs_root: Path,
    risk_entries: tuple[RiskEntry, ...],
    active_actions: tuple[ActionItem, ...],
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    freshness_report = build_freshness_report(
        current_items=items,
        issue_number=issue_number,
        as_of=as_of,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=previous_snapshot,
        previous_notification_state=None,
        program_context=bundle.program_context,
        workstream_narrative_history={},
    )
    open_asks = load_open_decision_asks(program_id, programs_root=programs_root)
    open_claims = load_open_claims(program_id, programs_root=programs_root)
    overdue_actions = assess_action_staleness(active_actions, as_of.date())
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    icm_signals = tuple(
        signal
        for signal in signal_store.read(
            program_id,
            start=as_of - timedelta(days=30),
            end=as_of,
        )
        if signal_source_family(signal.source) == "icm"
    )
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_issue_row_title(entry),
            detail=_format_issue_row_detail(entry, eta_forecasts=eta_forecasts),
            summary=entry.summary,
            href=entry.ado_url,
            links=_build_issue_row_links(entry),
        )
        for entry in build_issue_projection(
            items=items,
            freshness_report=freshness_report,
            icm_signals=icm_signals,
            open_asks=open_asks,
            overdue_actions=overdue_actions,
            open_claims=open_claims,
            risk_entries=risk_entries,
            ado_item_base_url=_ado_item_base_url(bundle),
        )
    )
def _format_decision_row_title(entry, *, as_of: datetime) -> str:
    labels = [entry.id, entry.status.value]
    if entry.status is DecisionStatus.PROPOSED:
        labels.append("stale" if assess_proposed_decision_staleness(entry, as_of.date()) else "current")
    return " · ".join(labels)


def _format_risk_row_title(entry: RiskEntry, *, stale: bool) -> str:
    labels = [entry.id, entry.status.value]
    if stale:
        labels.append("stale")
    return " · ".join(labels)


def _format_risk_row_detail(entry: RiskEntry) -> str:
    next_review = ((entry.last_reviewed_date or entry.identified_date) + timedelta(days=30)).isoformat()
    mitigation_due = entry.mitigation_due_date.isoformat() if entry.mitigation_due_date is not None else "-"
    correlations = _format_risk_correlations(entry)
    correlation_suffix = f" | Linked {correlations}" if correlations else ""
    return (
        f"Owner {entry.owner_alias} | {entry.category.value} | {entry.probability.value} x {entry.impact.value}"
        f" | Next review {next_review} | Mitigation due {mitigation_due}{correlation_suffix}"
    )


def _format_risk_row_summary(entry: RiskEntry) -> str:
    summary = f"{entry.title}: {entry.description}"
    if entry.mitigation_plan:
        summary += f" Mitigation: {entry.mitigation_plan}"
    return summary


def _format_risk_correlations(entry: RiskEntry) -> str:
    parts: list[str] = []
    if entry.linked_work_item_ids:
        parts.append(", ".join(f"WI:{work_item_id}" for work_item_id in entry.linked_work_item_ids))
    if entry.linked_milestone_ids:
        parts.append("milestone " + ", ".join(entry.linked_milestone_ids))
    if entry.linked_claim_ids:
        parts.append("claim " + ", ".join(entry.linked_claim_ids))
    if entry.linked_action_ids:
        parts.append("action " + ", ".join(entry.linked_action_ids))
    if entry.linked_workstream_ids:
        parts.append("workstream " + ", ".join(entry.linked_workstream_ids))
    return "; ".join(parts)


def _format_milestone_row_title(
    milestone: Milestone,
    assessment: MilestoneAssessment,
    *,
    critical_path: bool,
) -> str:
    labels = [
        milestone.id,
        f"declared {milestone.status.value}",
        f"computed {assessment.computed_health.value}",
        f"{assessment.confidence.value} confidence",
    ]
    if critical_path:
        labels.append("critical path")
    return " · ".join(labels)


def _format_milestone_row_detail(
    milestone: Milestone,
    assessment: MilestoneAssessment,
    *,
    schedule_summary: str | None = None,
    target_history_summary: str | None = None,
    completion_history_summary: str | None = None,
) -> str:
    parts = [
        f"Target {milestone.target_date.isoformat()}",
        f"Owner {milestone.owner_alias}",
        f"{assessment.confidence.value.title()} confidence",
    ]
    if milestone.linked_work_item_ids:
        parts.append("Linked " + ", ".join(f"WI:{work_item_id}" for work_item_id in milestone.linked_work_item_ids))
    if milestone.linked_workstream_ids:
        parts.append("Workstream " + ", ".join(milestone.linked_workstream_ids))
    if schedule_summary:
        parts.append(schedule_summary)
    if target_history_summary:
        parts.append(target_history_summary.title())
    if completion_history_summary:
        parts.append(completion_history_summary.title())
    return " | ".join(parts)


def _format_cascade_row_detail(cascade) -> str:
    trigger = cascade.trigger_kind
    if cascade.work_item_id is not None:
        trigger = f"{cascade.trigger_kind} on WI:{cascade.work_item_id}"
    parts = [f"Impact {cascade.impact}", f"Trigger {trigger}"]
    if cascade.confidence.value != "none":
        parts.append(f"Confidence {cascade.confidence.value}")
    return " | ".join(parts)


def _format_signal_thread_detail(signals: tuple[Signal, ...]) -> str:
    item_refs = tuple(
        dict.fromkeys(
            ref
            for signal in signals
            for ref in signal.entity_refs
            if ref.startswith("WI:")
        )
    )
    sources = tuple(dict.fromkeys(signal.source for signal in signals))
    start = min(signal.timestamp for signal in signals).date().isoformat()
    end = max(signal.timestamp for signal in signals).date().isoformat()
    parts = [f"Window {start} to {end}"]
    if item_refs:
        parts.append("Refs " + ", ".join(item_refs))
    parts.append("Sources " + ", ".join(sources))
    return " | ".join(parts)


def _signal_count_label(count: int) -> str:
    return f"{count} signal" if count == 1 else f"{count} signals"


def _normalize_cascade_label(value: str) -> str:
    if value.startswith("WI#"):
        return "WI:" + value[3:]
    return value


def _format_decision_row_detail(entry) -> str:
    parts = [f"Owner {entry.decided_by}", f"Date {entry.decision_date.isoformat() if entry.decision_date is not None else 'TBD'}"]
    if entry.workstream_id:
        parts.append(f"Workstream {entry.workstream_id}")
    return " | ".join(parts)


def _format_assumption_row_title(entry: Assumption, *, overdue: bool) -> str:
    labels = [entry.id, entry.status.value]
    if entry.status is AssumptionStatus.UNVALIDATED:
        labels.append("overdue" if overdue else "current")
    return " · ".join(labels)


def _format_assumption_row_detail(entry: Assumption) -> str:
    parts = []
    if entry.owner_alias:
        parts.append(f"Owner {entry.owner_alias}")
    if entry.validation_due is not None:
        parts.append(f"Due {entry.validation_due.isoformat()}")
    if entry.validation_method:
        parts.append(f"Method {entry.validation_method}")
    if entry.linked_milestone_id:
        parts.append(f"Milestone {entry.linked_milestone_id}")
    if entry.linked_risk_id:
        parts.append(f"Risk {entry.linked_risk_id}")
    if not parts:
        return "Assumption tracked from program register"
    return " | ".join(parts)


def _format_action_row_title(entry, *, overdue: bool, resolution_candidate: bool) -> str:
    labels = [entry.owner_alias, entry.id, entry.status.value]
    labels.append("overdue" if overdue else "current")
    if resolution_candidate:
        labels.append("candidate for resolution")
    return " · ".join(labels)


def _format_action_row_detail(entry) -> str:
    parts = [f"Due {entry.due_date.isoformat()}" if entry.due_date is not None else "Due -"]
    if entry.workstream_id:
        parts.append(f"Workstream {entry.workstream_id}")
    if entry.linked_work_item_ids:
        parts.append("Linked " + ", ".join(f"WI:{work_item_id}" for work_item_id in entry.linked_work_item_ids))
    if entry.linked_claim_id:
        parts.append(f"Claim {entry.linked_claim_id}")
    if entry.linked_risk_id:
        parts.append(f"Risk {entry.linked_risk_id}")
    return " | ".join(parts)


def _format_issue_row_title(entry: IssueProjection) -> str:
    labels = []
    if entry.work_item_id is not None:
        labels.append(f"WI:{entry.work_item_id}")
    labels.append(issue_projection_source_label(entry))
    labels.append(entry.severity)
    labels.append(issue_projection_confidence_label(entry))
    return " · ".join(labels)


def _format_coverage_gap_row_title(entry: CoverageGap) -> str:
    return f"WI:{entry.work_item_id} · {entry.state} · {coverage_gap_confidence_label(entry)}"


def _format_issue_row_detail(
    entry: IssueProjection,
    *,
    eta_forecasts: dict[int, ETAForecast],
) -> str:
    parts = []
    if entry.owner_alias:
        parts.append(f"Owner {entry.owner_alias}")
    if entry.workstream_id:
        parts.append(f"Workstream {entry.workstream_id}")
    forecast = eta_forecasts.get(entry.work_item_id) if entry.work_item_id is not None else None
    if forecast is not None and forecast.display_annotation is not None:
        parts.append(forecast.display_annotation)
    if entry.linked_entity_ids:
        parts.append(f"Linked {', '.join(entry.linked_entity_ids)}")
    return " | ".join(parts)


def _build_issue_row_links(entry: IssueProjection) -> tuple[ReviewerInlineLink, ...]:
    return tuple(
        ReviewerInlineLink(
            label=f"Jump to {entity_id}",
            href=f"#{_review_entity_anchor(entity_id)}",
        )
        for entity_id in entry.linked_entity_ids
        if _is_review_navigable_entity_id(entity_id)
    )


def _review_entity_anchor(entity_id: str) -> str:
    return build_anchor(f"review-{entity_id}")


def _is_review_navigable_entity_id(entity_id: str) -> bool:
    normalized = entity_id.strip().lower()
    return normalized.startswith(("ask-", "action-", "assumption-", "claim-", "risk-"))


def _format_claim_detail(claim: ClaimEntry) -> str:
    parts = []
    if claim.owner_alias:
        parts.append(f"Owner {claim.owner_alias}")
    if claim.due_date is not None:
        parts.append(f"Due {claim.due_date.isoformat()}")
    return " | ".join(parts) if parts else "Claim tracked from confirmed narrative"


def _format_ask_detail(entry: DecisionAsk) -> str:
    if entry.owner_alias:
        return f"Owner {entry.owner_alias}"
    return "Decision ask tracked from confirmed narrative"


def _build_owner_vitality_rows(
    *,
    resolved_v2,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root: Path,
) -> tuple[ReviewerVitalityRow, ...]:
    if resolved_v2 is None:
        return ()
    vitality_snapshot, _ = _build_v2_vitality_snapshot(
        resolved_v2=resolved_v2,
        items=items,
        as_of=as_of,
        programs_root=programs_root,
    )
    owner_aggregates = aggregate_vitality(vitality_snapshot.scores, scope_type="owner")
    return tuple(
        ReviewerVitalityRow(
            owner_alias=aggregate.scope_id,
            composite_score=aggregate.composite_score,
            fresh_items=aggregate.fresh_items,
            total_items=aggregate.total_items,
            leakage_events=aggregate.total_leakage,
        )
        for aggregate in owner_aggregates
    )


def _load_reviewer_summaries(*, resolved_v2, programs_root: Path) -> dict[str, str]:
    if resolved_v2 is None:
        return {}
    summaries: dict[str, str] = {}
    knowledge = load_program_knowledge(resolved_v2.program.id, programs_root=programs_root)
    for workstream in resolved_v2.workstreams:
        summary = load_summary(resolved_v2.program.id, workstream.id, programs_root=programs_root)
        summary_text = summary.text.strip() if summary is not None and summary.text.strip() else ""
        reference_pages = select_engms_pages(
            knowledge,
            program_id=resolved_v2.program.id,
            workstream_ids=(workstream.id,),
        )[:3]
        if reference_pages:
            reference_suffix = " ".join(
                f"Reference doc: {page.title} ({page.url}). {summarize_engms_page(page)}."
                for page in reference_pages
            )
            summary_text = f"{summary_text} {reference_suffix}".strip()
        if summary_text:
            summaries[workstream.id] = summary_text
    return summaries


def _build_anticipation_client(
    bundle: ReportBundle,
    *,
    trace_context: AITraceContext | None = None,
) -> LLMProvider | None:
    if get_ai_mode() == AIMode.DISABLED:
        return None
    if not bundle.config.ai.enabled:
        return None
    deployments = resolve_ai_deployments_for_feature(
        feature_name="anticipation_engine",
        primary_candidates=(bundle.config.ai.exec_summary_deployment, bundle.config.ai.blurb_deployment),
        backup_candidates=(bundle.config.ai.exec_summary_backup_deployment, bundle.config.ai.blurb_backup_deployment),
        primary_fallback_envs=("VERTEX_EXEC_DEPLOYMENT", "VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_EXEC_BACKUP_DEPLOYMENT", "VERTEX_AI_BACKUP_DEPLOYMENT"),
    )
    if not deployments:
        log.warning(
            "Anticipation AI unavailable for review pane; set VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT. "
            + LEGACY_DEPLOYMENT_ALIAS_NOTICE.capitalize()
        )
        return None
    try:
        # D-20: bind the trace context to the process-level ContextVar so
        # any nested helper that doesn't take an explicit `trace_context=`
        # arg still picks it up. The explicit kwarg below still wins, so
        # this is behavior-preserving.
        with use_trace_context(trace_context):
            return FallbackStructuredClient(
                deployments=deployments,
                temperature=bundle.config.ai.temperature or 0.0,
                budget_usd=bundle.config.ai.budget_usd_per_run,
                trace_context=trace_context,
            )
    except (AIClientError, RuntimeError) as error:
        log.warning("Anticipation AI unavailable for review pane; using deterministic fallback: %s", error)
        return None


def _build_review_full_trace_context(
    *,
    edition_name: str,
    issue_number: int,
    budget_usd: float,
    output_root: Path | None = None,
) -> AITraceContext:
    del output_root  # Legacy compatibility for older tests/callers.
    current_time = datetime.now(timezone.utc)
    return AITraceContext(
        edition=edition_name,
        run_id=f"{edition_name}:review_full:{issue_number:03d}:{current_time.strftime('%Y%m%dT%H%M%SZ')}",
        caller="src.commands.review_full.prepare_review_full_context",
        metadata={
            "edition_name": edition_name,
            "issue_number": issue_number,
            "task_type": "reviewer_anticipation",
            "run_budget_usd": budget_usd,
        },
    )


def _build_default_anticipation_client(
    *,
    bundle: ReportBundle,
    trace_context: AITraceContext,
) -> LLMProvider | None:
    if get_ai_mode() == AIMode.DISABLED:
        return None
    if "trace_context" in inspect.signature(_build_anticipation_client).parameters:
        return _build_anticipation_client(bundle, trace_context=trace_context)
    return _build_anticipation_client(bundle)


def _assigned_reviewers_by_section(bundle: ReportBundle) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for reviewer in bundle.review.reviewers:
        for section_id in reviewer.sections:
            assignments[section_id] = reviewer.name
    return assignments


def _resolved_reviewer(section: ReviewSection, assigned_reviewers: dict[str, str]) -> str | None:
    return section.reviewer or assigned_reviewers.get(section.section_id)


def _section_or_pending(section: ReviewSection | None, section_id: str) -> ReviewSection:
    if section is not None:
        return section
    return ReviewSection(
        section_id=section_id,
        state=ReviewState.PENDING,
        reviewer=None,
        note=None,
        updated_at=None,
    )


def _build_section_delta_rows(
    deltas,
    item_ids: set[int],
    item_lookup: dict[int, WorkItem],
    item_urls: dict[int, str],
) -> tuple[ReviewerDeltaRow, ...]:
    ordered_deltas = [
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_UP and delta.work_item_id in item_ids), key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.new_items if delta.work_item_id in item_ids), key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.eta_changes if delta.work_item_id in item_ids), key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_DOWN and delta.work_item_id in item_ids), key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.closed_items if delta.work_item_id in item_ids), key=lambda delta: delta.work_item_id),
    ]
    rows: list[ReviewerDeltaRow] = []
    for delta in ordered_deltas:
        item = item_lookup.get(delta.work_item_id)
        rows.append(
            ReviewerDeltaRow(
                label=_delta_row_label(delta.kind),
                title=item.title if item is not None else f"Work item {delta.work_item_id}",
                detail=_delta_row_detail(delta),
                ado_url=item_urls.get(delta.work_item_id),
            )
        )
    return tuple(rows)


def _build_evidence_rows_from_citations(
    citations,
    evidence_by_item: dict[int, EvidencePacket],
    max_rows: int = 5,
) -> tuple[ReviewerEvidenceRow, ...]:
    rows: list[ReviewerEvidenceRow] = []
    for citation in citations[:max_rows]:
        if citation.work_item_id is None:
            continue
        evidence = evidence_by_item.get(citation.work_item_id)
        if evidence is None:
            continue
        rows.append(
            ReviewerEvidenceRow(
                work_item_id=citation.work_item_id,
                title=citation.title,
                summary=evidence.summary_for_reviewer,
                ado_url=citation.ado_url,
            )
        )
    return tuple(rows)


def _build_workstream_evidence_rows(
    *,
    workstream_items: tuple[WorkItem, ...],
    delta_rows: tuple[ReviewerDeltaRow, ...],
    citations,
    evidence_by_item: dict[int, EvidencePacket],
) -> tuple[ReviewerEvidenceRow, ...]:
    item_citations = tuple(citation for citation in citations if citation.work_item_id is not None)
    cited_ids = {citation.work_item_id: citation for citation in item_citations}
    selected_ids: list[int] = []
    if delta_rows:
        for citation in item_citations:
            if citation.work_item_id in cited_ids:
                selected_ids.append(citation.work_item_id)
        selected_ids = selected_ids[:3]
    if not selected_ids:
        selected_ids = [citation.work_item_id for citation in item_citations[:3]]
    if not selected_ids:
        selected_ids = [item.id for item in workstream_items[:3]]

    rows: list[ReviewerEvidenceRow] = []
    seen_ids: set[int] = set()
    item_lookup = {item.id: item for item in workstream_items}
    for work_item_id in selected_ids:
        if work_item_id in seen_ids:
            continue
        seen_ids.add(work_item_id)
        item = item_lookup.get(work_item_id)
        evidence = evidence_by_item.get(work_item_id)
        if item is None or evidence is None:
            continue
        rows.append(
            ReviewerEvidenceRow(
                work_item_id=work_item_id,
                title=item.title,
                summary=evidence.summary_for_reviewer,
                ado_url=cited_ids[work_item_id].ado_url if work_item_id in cited_ids else None,
            )
        )
    return tuple(rows)


def _build_context_rows(
    *,
    approved_signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    item_ids: set[int],
    item_urls: dict[int, str],
    as_of: datetime,
    people_directory: tuple[PersonDirectory, ...],
    source_confidence_order: tuple[str, ...],
    max_rows: int = 6,
) -> tuple[ReviewerContextRow, ...]:
    rows: list[ReviewerContextRow] = []
    matching_patterns = [pattern for pattern in drift_patterns if pattern.work_item_id in item_ids]
    for pattern in matching_patterns[: max_rows // 2 or 1]:
        rows.append(
            ReviewerContextRow(
                label=f"Drift · {pattern.severity.title()} · {pattern.pattern.replace('_', ' ').title()}",
                summary=f"WI #{pattern.work_item_id}: {pattern.detail}",
                ado_url=item_urls.get(pattern.work_item_id),
            )
        )

    remaining = max_rows - len(rows)
    if remaining <= 0:
        return tuple(rows)

    matching_signals = sort_signals_for_ai_context(
        tuple(signal for signal in approved_signals if _matching_signal_item_id(signal, item_ids) is not None),
        people_directory=people_directory,
        as_of=as_of,
        source_confidence_order=source_confidence_order,
    )
    for signal in matching_signals[:remaining]:
        work_item_id = _matching_signal_item_id(signal, item_ids)
        rows.append(
            ReviewerContextRow(
                label=f"Signal · {signal.source} · {signal.confidence.value.title()}",
                summary=signal.text,
                ado_url=item_urls.get(work_item_id) if work_item_id is not None else None,
            )
        )
    return tuple(rows)


def _matching_signal_item_id(signal: Signal, item_ids: set[int]) -> int | None:
    if signal.metadata is not None:
        metadata_item_id = signal.metadata.get("work_item_id")
        if isinstance(metadata_item_id, int) and metadata_item_id in item_ids:
            return metadata_item_id
    for entity_ref in signal.entity_refs:
        if not entity_ref.startswith("WI:"):
            continue
        raw_id = entity_ref.split(":", 1)[1].strip()
        if raw_id.isdigit() and int(raw_id) in item_ids:
            return int(raw_id)
    return None


def _build_exec_summary_override_rows(
    scorecards: tuple,
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
) -> tuple[ReviewerOverrideRow, ...]:
    rows: list[ReviewerOverrideRow] = []
    for scorecard in scorecards:
        packet_map = scorecard_packets.get(scorecard.scorecard_name, {})
        for dimension in scorecard.dimensions:
            packet = packet_map.get(dimension.name)
            if packet is None:
                continue
            if packet.prior_confirmed_risk == dimension.risk:
                continue
            rows.append(
                ReviewerOverrideRow(
                    scorecard_name=scorecard.scorecard_name,
                    dimension_name=dimension.name,
                    current_risk=dimension.risk,
                    prior_risk=packet.prior_confirmed_risk,
                    summary=dimension.summary,
                    ado_query_url=packet.ado_query_url,
                )
            )
    return tuple(rows)


def _build_workstream_override_rows(workstream: WorkstreamData) -> tuple[ReviewerOverrideRow, ...]:
    if workstream.risk is None:
        return ()
    return (
        ReviewerOverrideRow(
            scorecard_name=workstream.scorecard_name or "Details",
            dimension_name=workstream.title,
            current_risk=workstream.risk,
            prior_risk=workstream.prior_risk,
            summary=workstream.summary or workstream.blurb,
            ado_query_url=workstream.ado_query_url,
        ),
    )


def _delta_row_label(kind: DeltaKind) -> str:
    if kind == DeltaKind.RISK_UP:
        return "Risk Up"
    if kind == DeltaKind.RISK_DOWN:
        return "Risk Down"
    if kind == DeltaKind.NEW:
        return "New"
    if kind == DeltaKind.CLOSED:
        return "Closed"
    if kind == DeltaKind.ETA_CHANGED:
        return "ETA Shift"
    return "Change"


def _delta_row_detail(delta: ItemDelta) -> str:
    if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
        return f"{risk_label(delta.old_risk)} → {risk_label(delta.new_risk)}"
    if delta.kind == DeltaKind.NEW:
        return risk_label(delta.new_risk)
    if delta.kind == DeltaKind.CLOSED:
        return "Done"
    if delta.kind == DeltaKind.ETA_CHANGED:
        return delta_label(delta.kind, delta.old_eta, delta.new_eta)
    return "—"


def _build_why_block(claims, issue_number: int) -> ReviewerWhyBlock | None:
    if not claims:
        return None
    primary_claim = claims[0]
    lines = []
    for claim in claims:
        if claim is not primary_claim:
            lines.append(ReviewerWhyLine(label=claim.title, value=claim.statement))
        lines.extend(
            ReviewerWhyLine(label=line.label if claim is primary_claim else f"{claim.title} {line.label}", value=line.value, href=line.href)
            for line in claim.lines
        )
    return ReviewerWhyBlock(
        summary=primary_claim.statement,
        confidence=primary_claim.confidence.value.upper(),
        evidence_command=f"vertex evidence --issue {issue_number} --section {primary_claim.section_id}",
        lines=tuple(lines),
    )
