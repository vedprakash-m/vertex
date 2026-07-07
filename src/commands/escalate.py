from __future__ import annotations

from collections.abc import Callable
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import StringIO
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import typer
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

from src.commands import gather as gather_helpers
from src.commands import report as report_helpers
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, compute_prior_acceptance_rate
from src.core.ado_semantics import item_owner_alias
from src.core.archive_store import get_dimension_history
from src.core.claim_tracker import load_decision_asks, load_latest_claim_statuses, resolve_entry_status, touch_decision_ask
from src.core.config_loader import REPORTS_ROOT, load_bundle_with_mode
from src.core.edition_resolver import get_program_output_dir, load_program, resolve_edition, PROGRAMS_ROOT
from src.core.eml_writer import build_eml_bytes, write_eml
from src.core.evidence_engine import build_evidence
from src.core.incident_learning_synthesizer import IncidentRefPattern, build_incident_ref_patterns, normalize_incident_ref
from src.core.exceptions import AuthError, ConfigError, QueryError, StateError
from src.core.dependency_graph import dependency_target_label
from src.core.incident_journal_store import read_incident_entries
from src.core.journal import get_weekly_journal_path
from src.core.knowledge_store import load_program_knowledge
from src.core.leakage_detector import detect_leakage, load_approved_workiq_signals
from src.core.milestone_engine import (
    assess_milestone_health,
    describe_milestone_schedule_variance,
    load_milestone_completion_date_history_map,
    load_milestone_target_date_history_map,
    summarize_milestone_completion_date_history,
    summarize_milestone_target_date_history,
)
from src.core.models import Confidence, RiskLevel, ScorecardEvidencePacket, WorkItem
from src.core.models_v2 import DependencyStatus, IncidentEntry, LeadershipReader, Scorecard, Signal, SignalReviewDecision, Workstream
from src.core.overrides_store import load_overrides, merge_overrides
from src.core.program_fact_store import load_program_facts, project_dependencies, project_milestones
from src.core.policy_evaluator import check_cooldown, evaluate_rules, load_escalation_rules, record_cooldown
from src.core.sqlite_stores import get_program_sqlite_store_path
from src.core.signal_classification import classify_signal as _classify_signal
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id, resolve_storage_backend
from src.core.vitality_reporting import effective_vitality_exempt_aliases, vitality_settings_from_program
from src.core.vitality_scorer import aggregate_vitality, score_vitality
from src.m365.graph_send_client import GraphMailMessage, GraphSendClient


REPO_ROOT = Path(__file__).resolve().parents[2]

WorkItemLoader = report_helpers.WorkItemLoader
EscalationSender = Callable[["EscalationPreview"], str | None]


@dataclass(frozen=True, slots=True)
class EscalationPreview:
    rule_name: str
    dimension_name: str
    consecutive_high: int
    workstream_ids: tuple[str, ...]
    workstream_names: tuple[str, ...]
    recipients: tuple[str, ...]
    subject: str
    md_body: str
    html_body: str
    detail_label: str
    detail_text: str
    signal_text: str
    cooldown_key: str
    evidence_lines: tuple[str, ...] = ()
    trend_label: str | None = None
    risk_sparkline: str | None = None
    recommended_action: str | None = None
    vitality_composite: int | None = None
    stale_days: int | None = None
    milestone_id: str | None = None
    milestone_status: str | None = None
    milestone_days_to_target: int | None = None
    milestone_schedule_summary: str | None = None
    milestone_target_date_history_summary: str | None = None
    milestone_completion_date_history_summary: str | None = None
    decision_ask_id: str | None = None
    decision_ask_status: str | None = None
    decision_ask_age_days: int | None = None
    decision_ask_owner_alias: str | None = None
    decision_ask_entity_refs: tuple[str, ...] = ()
    incident_refs: tuple[str, ...] = ()
    incident_summary: str | None = None
    escalation_path_label: str | None = None
    escalation_guidance: str | None = None


@dataclass(frozen=True, slots=True)
class EscalationArtifacts:
    previews: tuple[EscalationPreview, ...]
    suppressed: tuple[str, ...]
    unresolved: tuple[str, ...]
    eml_paths: tuple[Path, ...] = ()
    signal_paths: tuple[Path, ...] = ()
    state_path: Path | None = None
    sent_count: int = 0


@dataclass(frozen=True, slots=True)
class DecisionAskEscalationPlan:
    edition_name: str
    decision_ask_id: str
    artifacts: EscalationArtifacts


@dataclass(frozen=True, slots=True)
class EscalationDependencyContext:
    escalation_path_label: str
    guidance: str


def escalate_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    decision_ask: str | None = typer.Option(None, "--decision-ask", help="Optional decision ask id to scope the preview to a single ask."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview escalation recipients and draft content without writing files."),
    channel: str = typer.Option("eml", "--channel", help="Delivery channel. 'eml' writes manual-send draft EMLs and 'email' sends live Graph mail when maturity_level >= 2."),
    rules: Path | None = typer.Option(None, "--rules", help="Optional path to escalation_rules.yaml."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if channel not in {"eml", "email"}:
        raise typer.BadParameter("Only '--channel eml' and '--channel email' are currently supported.")
    if format != "human" and not dry_run:
        raise typer.BadParameter("--format json/csv requires --dry-run for escalate output.")
    preview_rendered = False
    try:
        preview_artifacts = generate_escalations(
            edition_name=edition,
            decision_ask_id=(decision_ask.strip() if decision_ask is not None and decision_ask.strip() else None),
            dry_run=True,
            channel=channel,
            rules_path=rules,
            reports_root=REPORTS_ROOT,
        )
    except (AuthError, ConfigError, QueryError, StateError, typer.BadParameter) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)

    if not dry_run and channel == "email":
        resolved = resolve_edition(
            edition,
            editions_root=REPORTS_ROOT.parent / "editions",
            programs_root=REPORTS_ROOT.parent / "programs",
        )
        if resolved is None:
            typer.echo(f"Edition '{edition}' could not be resolved.")
            raise typer.Exit(code=2)
        if resolved.program.maturity_level < 2:
            typer.echo(
                f"Program '{resolved.program.id}' is at maturity level {resolved.program.maturity_level}. "
                "'vertex escalate --channel email' requires maturity_level >= 2."
            )
            raise typer.Exit(code=2)

    if dry_run:
        artifacts = preview_artifacts
    elif not preview_artifacts.previews:
        artifacts = preview_artifacts
    else:
        if format == "human":
            typer.echo(render_escalation_preview_plaintext(preview_artifacts))
            preview_rendered = True
        delivery_label = "live Graph email" if channel == "email" else "local EML draft"
        if not typer.confirm(
            f"Apply {len(preview_artifacts.previews)} escalation(s) for {edition} via {delivery_label}?",
            default=True,
        ):
            _record_declined_escalation_audits(
                edition_name=edition,
                artifacts=preview_artifacts,
                channel=channel,
                reports_root=REPORTS_ROOT,
            )
            if format == "human":
                typer.echo("Apply cancelled.")
            raise typer.Exit(code=1)
        try:
            artifacts = generate_escalations(
                edition_name=edition,
                decision_ask_id=(decision_ask.strip() if decision_ask is not None and decision_ask.strip() else None),
                dry_run=False,
                channel=channel,
                rules_path=rules,
                reports_root=REPORTS_ROOT,
            )
        except (AuthError, ConfigError, QueryError, StateError, typer.BadParameter) as error:
            typer.echo(str(error))
            raise typer.Exit(code=2)

    if format == "human":
        if not preview_rendered:
            typer.echo(render_escalation_preview_plaintext(artifacts))
        if dry_run:
            typer.echo("Dry run: no escalation drafts written.")
            raise typer.Exit(code=0)
        if not artifacts.previews:
            typer.echo("No escalation drafts written.")
            raise typer.Exit(code=0)
        for path in artifacts.eml_paths:
            typer.echo(f"EML: {path}")
        for path in artifacts.signal_paths:
            typer.echo(f"Signal: {path}")
        if artifacts.state_path is not None:
            typer.echo(f"Cooldown state: {artifacts.state_path}")
        if channel == "email":
            typer.echo(f"Sent {artifacts.sent_count} escalation email(s) via Graph.")
            raise typer.Exit(code=0)
        typer.echo(f"Wrote {len(artifacts.eml_paths)} escalation draft EML(s). Send manually via Outlook.")
    else:
        typer.echo(
            render_escalation_output(
                edition_name=edition,
                artifacts=artifacts,
                dry_run=dry_run,
                channel=channel,
                format=format,
            ),
            nl=False,
        )
    raise typer.Exit(code=0)


def generate_escalations(
    *,
    edition_name: str,
    decision_ask_id: str | None = None,
    dry_run: bool,
    channel: str = "eml",
    rules_path: Path | None = None,
    as_of: datetime | None = None,
    reports_root: Path = REPORTS_ROOT,
    work_item_loader: WorkItemLoader | None = None,
    sender: EscalationSender | None = None,
) -> EscalationArtifacts:
    programs_root = reports_root.parent / "programs"
    current_time = as_of or datetime.now(timezone.utc)

    load_result = load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        programs_root=programs_root,
    )
    if load_result.mode != "v2":
        raise typer.BadParameter("vertex escalate currently supports V2 editions only.")

    resolved = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    if resolved is None:
        raise typer.BadParameter(f"Edition '{edition_name}' could not be resolved.")

    bundle = load_result.bundle
    loader = work_item_loader or report_helpers._load_live_work_items
    items, _ado_calls = loader(bundle, current_time)
    evidence_window_start = current_time - timedelta(days=bundle.config.ado.date_window_days)
    evidence_by_item = {item.id: build_evidence(item, evidence_window_start, current_time) for item in items}
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    overrides_document, _ = merge_overrides(
        issue_number=0,
        expected_scorecards=expected_scorecards,
        existing=load_overrides(edition_name, reports_root=reports_root),
    )
    scorecard_packets = report_helpers._build_scorecard_packets(bundle, items, None)
    _scorecards, dimension_risks, _scorecard_deltas = report_helpers._build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=reports_root,
    )

    resolved_rules = load_escalation_rules(
        program_id=resolved.program.id,
        programs_root=programs_root,
        rules_path=rules_path,
    )
    if not resolved_rules:
        return EscalationArtifacts(previews=(), suppressed=(), unresolved=())

    state_path = programs_root / resolved.program.id / "escalation_state.json"
    knowledge = load_program_knowledge(resolved.program.id, programs_root=programs_root)
    workstream_names = {workstream.id: workstream.name for workstream in resolved.workstreams}
    dimension_workstream_ids = _scorecard_dimension_workstream_ids(resolved.scorecards)
    dependency_context_by_dimension = {
        dimension_name: _build_dimension_escalation_dependency_context(
            program_id=resolved.program.id,
            linked_workstream_ids=linked_workstream_ids,
            programs_root=programs_root,
        )
        for dimension_name, linked_workstream_ids in dimension_workstream_ids.items()
    }
    accountable_by_workstream = _accountable_aliases_by_workstream(resolved.workstreams)
    vitality_composite_by_workstream, stale_days_by_workstream = _build_workstream_vitality_context(
        program_id=resolved.program.id,
        items=items,
        workstreams=resolved.workstreams,
        raw_program=resolved.raw_program,
        knowledge=knowledge,
        as_of=current_time,
        programs_root=programs_root,
    )
    milestones = project_milestones(
        load_program_facts(
            resolved.program.id,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    )
    trajectory_store = build_trajectory_store_for_program_id(
        resolved.program.id,
        programs_root=programs_root,
    )
    milestone_trajectories = {
        work_item_id: trajectory_store.read(resolved.program.id, work_item_id)
        for milestone in milestones
        for work_item_id in milestone.linked_work_item_ids
    }
    milestone_assessments = {
        milestone.id: assess_milestone_health(
            milestone,
            items,
            milestone_trajectories,
            current_time,
        )
        for milestone in milestones
    }
    milestone_target_history_map = load_milestone_target_date_history_map(
        resolved.program.id,
        milestones,
        programs_root=programs_root,
    )
    milestone_completion_history_map = load_milestone_completion_date_history_map(
        resolved.program.id,
        milestones,
        current_completion_dates={
            milestone_id: assessment.completion_date
            for milestone_id, assessment in milestone_assessments.items()
            if assessment.completion_date is not None
        },
        programs_root=programs_root,
    )
    claim_statuses = load_latest_claim_statuses(resolved.program.id, programs_root=programs_root)
    decision_asks = load_decision_asks(resolved.program.id, programs_root=programs_root)
    incident_patterns = _build_incident_patterns(
        read_incident_entries(
            resolved.program.id,
            start=current_time - timedelta(days=14),
            end=current_time,
            programs_root=programs_root,
        )
    )
    if decision_ask_id is not None:
        decision_asks = tuple(ask for ask in decision_asks if ask.id == decision_ask_id)
        if not decision_asks:
            raise typer.BadParameter(f"Decision ask '{decision_ask_id}' was not found in {resolved.program.id}.")

    previews: list[EscalationPreview] = []
    suppressed: list[str] = []
    unresolved: list[str] = []
    if decision_ask_id is None:
        for dimension in dimension_risks:
            linked_workstream_ids = dimension_workstream_ids.get(dimension.name, ())
            scorecard_packet = _scorecard_evidence_packet(scorecard_packets, dimension.name)
            dimension_history = (*_dimension_history_levels(edition_name, dimension.name, archive_dir=resolved.paths.archive_dir), dimension.risk)
            risk_sparkline = dimension.risk_sparkline
            trend_label = dimension.trend_label
            if trend_label is None and scorecard_packet is not None:
                risk_sparkline, trend_label = report_helpers._derive_risk_sparkline(
                    dimension.risk,
                    scorecard_packet.prior_confirmed_risk,
                )
            if trend_label is None:
                risk_sparkline, trend_label = _history_risk_trend(dimension_history)
            vitality_composite = _linked_workstream_vitality_composite(
                linked_workstream_ids,
                vitality_composite_by_workstream,
            )
            stale_days = _linked_workstream_stale_days(
                linked_workstream_ids,
                stale_days_by_workstream,
            )
            context: dict[str, object] = {
                "consecutive_high": _consecutive_high_count(dimension_history),
            }
            if vitality_composite is not None:
                context["vitality_composite"] = vitality_composite
            if stale_days is not None:
                context["stale_days"] = stale_days
            for rule in resolved_rules:
                if rule.name not in evaluate_rules((rule,), context):
                    continue
                cooldown_key = _build_cooldown_key(rule.name, dimension.name, linked_workstream_ids)
                if not check_cooldown(state_path, cooldown_key, rule.cooldown_hours, as_of=current_time):
                    suppressed.append(
                        f"{rule.name}: {dimension.name} suppressed by cooldown ({rule.cooldown_hours}h)"
                    )
                    continue

                recipients = _resolve_recipients(
                    linked_workstream_ids=linked_workstream_ids,
                    raw_program=resolved.raw_program,
                    leadership_readers=resolved.program.leadership_readers,
                    accountable_by_workstream=accountable_by_workstream,
                    knowledge=knowledge,
                )
                if not recipients:
                    unresolved.append(
                        f"{rule.name}: {dimension.name} triggered but no recipient email could be resolved"
                    )
                    continue

                linked_workstream_names = tuple(
                    workstream_names[workstream_id]
                    for workstream_id in linked_workstream_ids
                    if workstream_id in workstream_names
                )
                previews.append(
                    _build_escalation_preview(
                        edition_name=edition_name,
                        edition_title=bundle.config.edition.name,
                        dimension_name=dimension.name,
                        dimension_summary=dimension.summary,
                        consecutive_high=context["consecutive_high"],  # type: ignore[arg-type]
                        rule_name=rule.name,
                        linked_workstream_ids=linked_workstream_ids,
                        linked_workstream_names=linked_workstream_names,
                        recipients=recipients,
                        evidence_lines=_scorecard_evidence_lines(scorecard_packet),
                        trend_label=trend_label,
                        risk_sparkline=risk_sparkline,
                        recommended_action=_build_dimension_escalation_recommended_action(
                            dependency_context_by_dimension.get(dimension.name)
                        ),
                        vitality_composite=vitality_composite,
                        stale_days=stale_days,
                        dependency_context=dependency_context_by_dimension.get(dimension.name),
                    )
                )

    if decision_ask_id is None:
        for milestone in milestones:
            assessment = milestone_assessments[milestone.id]
            vitality_composite = _linked_workstream_vitality_composite(
                milestone.linked_workstream_ids,
                vitality_composite_by_workstream,
            )
            stale_days = _linked_workstream_stale_days(
                milestone.linked_workstream_ids,
                stale_days_by_workstream,
            )
            context = {
                "milestone_status": assessment.computed_health.value,
                "milestone_days_to_target": (milestone.target_date - current_time.date()).days,
            }
            if vitality_composite is not None:
                context["vitality_composite"] = vitality_composite
            if stale_days is not None:
                context["stale_days"] = stale_days
            for rule in resolved_rules:
                if rule.name not in evaluate_rules((rule,), context):
                    continue
                cooldown_key = _build_cooldown_key(rule.name, milestone.id, milestone.linked_workstream_ids)
                if not check_cooldown(state_path, cooldown_key, rule.cooldown_hours, as_of=current_time):
                    suppressed.append(
                        f"{rule.name}: {milestone.name} suppressed by cooldown ({rule.cooldown_hours}h)"
                    )
                    continue

                recipients = _resolve_recipients(
                    linked_workstream_ids=milestone.linked_workstream_ids,
                    raw_program=resolved.raw_program,
                    leadership_readers=resolved.program.leadership_readers,
                    accountable_by_workstream=accountable_by_workstream,
                    knowledge=knowledge,
                )
                if not recipients:
                    unresolved.append(
                        f"{rule.name}: {milestone.name} triggered but no recipient email could be resolved"
                    )
                    continue

                linked_workstream_names = tuple(
                    workstream_names[workstream_id]
                    for workstream_id in milestone.linked_workstream_ids
                    if workstream_id in workstream_names
                )
                milestone_schedule_summary = describe_milestone_schedule_variance(
                    milestone,
                    items,
                    milestone_trajectories,
                    current_time,
                )
                milestone_target_date_history_summary = summarize_milestone_target_date_history(
                    milestone_target_history_map.get(milestone.id, ()),
                )
                milestone_completion_date_history_summary = summarize_milestone_completion_date_history(
                    milestone_completion_history_map.get(milestone.id, ()),
                )
                previews.append(
                    _build_milestone_escalation_preview(
                        edition_name=edition_name,
                        edition_title=bundle.config.edition.name,
                        milestone=milestone,
                        assessment=assessment,
                        rule_name=rule.name,
                        linked_workstream_names=linked_workstream_names,
                        recipients=recipients,
                        cooldown_key=cooldown_key,
                    vitality_composite=vitality_composite,
                    stale_days=stale_days,
                    milestone_days_to_target=context["milestone_days_to_target"],  # type: ignore[arg-type]
                    milestone_schedule_summary=milestone_schedule_summary,
                    milestone_target_date_history_summary=milestone_target_date_history_summary,
                    milestone_completion_date_history_summary=milestone_completion_date_history_summary,
                )
            )

    for ask in decision_asks:
        effective_status = resolve_entry_status(ask, claim_statuses)
        related_incident_patterns = _related_incident_patterns_for_ask(ask, incident_patterns)
        incident_refs = tuple(
            dict.fromkeys(
                incident_ref
                for pattern in related_incident_patterns
                for incident_ref in pattern.incident_refs
            )
        )
        linked_workstream_ids = _decision_ask_workstream_ids(
            ask.entity_refs,
            items,
            resolved.workstreams,
        )
        vitality_composite = _linked_workstream_vitality_composite(
            linked_workstream_ids,
            vitality_composite_by_workstream,
        )
        stale_days = _linked_workstream_stale_days(
            linked_workstream_ids,
            stale_days_by_workstream,
        )
        context = {
            "decision_ask_age_days": max(0, (current_time.date() - ask.ask_date).days),
            "decision_ask_status": effective_status,
        }
        if vitality_composite is not None:
            context["vitality_composite"] = vitality_composite
        if stale_days is not None:
            context["stale_days"] = stale_days
        for rule in resolved_rules:
            if rule.name not in evaluate_rules((rule,), context):
                continue
            cooldown_key = _build_cooldown_key(rule.name, ask.id, linked_workstream_ids)
            if not check_cooldown(state_path, cooldown_key, rule.cooldown_hours, as_of=current_time):
                suppressed.append(
                    f"{rule.name}: {ask.id} suppressed by cooldown ({rule.cooldown_hours}h)"
                )
                continue

            recipients = _resolve_recipients(
                linked_workstream_ids=linked_workstream_ids,
                raw_program=resolved.raw_program,
                leadership_readers=resolved.program.leadership_readers,
                accountable_by_workstream=accountable_by_workstream,
                knowledge=knowledge,
            )
            if not recipients:
                unresolved.append(
                    f"{rule.name}: {ask.id} triggered but no recipient email could be resolved"
                )
                continue

            linked_workstream_names = tuple(
                workstream_names[workstream_id]
                for workstream_id in linked_workstream_ids
                if workstream_id in workstream_names
            )
            previews.append(
                _build_decision_ask_escalation_preview(
                    edition_title=bundle.config.edition.name,
                    edition_name=edition_name,
                    ask=ask,
                    effective_status=effective_status,
                    ask_age_days=context["decision_ask_age_days"],  # type: ignore[arg-type]
                    linked_workstream_ids=linked_workstream_ids,
                    linked_workstream_names=linked_workstream_names,
                    recipients=recipients,
                    cooldown_key=cooldown_key,
                    vitality_composite=vitality_composite,
                    stale_days=stale_days,
                    rule_name=rule.name,
                    incident_refs=incident_refs,
                    incident_summary=(
                        _render_incident_pattern_evidence(related_incident_patterns[0])
                        if related_incident_patterns
                        else None
                    ),
                )
            )

    if dry_run or not previews:
        return EscalationArtifacts(
            previews=tuple(previews),
            suppressed=tuple(suppressed),
            unresolved=tuple(unresolved),
            state_path=state_path,
        )

    if channel == "email" and resolved.program.maturity_level < 2:
        raise ConfigError(
            f"Program '{resolved.program.id}' is at maturity level {resolved.program.maturity_level}. "
            "'vertex escalate --channel email' requires maturity_level >= 2."
        )

    eml_paths: list[Path] = []
    signal_paths: list[Path] = []
    sent_count = 0
    author_alias = _resolve_author_alias(bundle.config.author.email, bundle.config.author.display_name)
    email_sender = sender or _build_escalation_email_sender(author_email=bundle.config.author.email)
    for index, preview in enumerate(previews, start=1):
        signal_ref: str
        if channel == "email":
            message_ref = email_sender(preview)
            sent_count += 1
            signal_ref = message_ref or f"graph://mail/{preview.cooldown_key}"
        else:
            eml_paths.append(
                _write_escalation_eml(
                    edition_name=edition_name,
                    preview=preview,
                    generated_at=current_time,
                    programs_root=programs_root,
                    from_display_name=bundle.config.author.display_name,
                    from_email=bundle.config.author.email,
                    index=index,
                )
            )
            signal_ref = str(eml_paths[-1])
        signal_paths.append(
            _append_escalation_signal(
                program_id=resolved.program.id,
                preview=preview,
                signal_ref=signal_ref,
                current_time=current_time,
                programs_root=programs_root,
            )
        )
        record_cooldown(state_path, preview.cooldown_key, triggered_at=current_time)
        if preview.decision_ask_id is not None:
            touch_decision_ask(
                program_id=resolved.program.id,
                decision_ask_id=preview.decision_ask_id,
                updated_at=current_time,
                updated_by=bundle.config.author.display_name,
                note=f"Decision ask touched by escalation preview {preview.rule_name}.",
                programs_root=programs_root,
            )
        action_type = _escalation_action_type(preview)
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id=resolved.program.id,
                action_id=str(uuid4()),
                level="l3" if channel == "email" else "l2",
                author_alias=author_alias,
                subject_alias=preview.decision_ask_owner_alias,
                action_type=action_type,
                evidence_refs=_escalation_evidence_refs(preview),
                policy_rule=preview.rule_name,
                accepted=True,
                applied_at=current_time,
                blast_radius=_escalation_blast_radius(preview, channel=channel),
                rollback_mechanism=_escalation_rollback_mechanism(channel=channel),
                prior_acceptance_rate=compute_prior_acceptance_rate(
                    resolved.program.id,
                    action_type=action_type,
                    programs_root=programs_root,
                ),
            ),
            programs_root=programs_root,
        )

    return EscalationArtifacts(
        previews=tuple(previews),
        suppressed=tuple(suppressed),
        unresolved=tuple(unresolved),
        eml_paths=tuple(eml_paths),
        signal_paths=tuple(signal_paths),
        state_path=state_path,
        sent_count=sent_count,
    )


def plan_decision_ask_escalation(
    *,
    edition_name: str,
    decision_ask_id: str,
    reports_root: Path | None = None,
) -> DecisionAskEscalationPlan:
    resolved_reports_root = reports_root or REPORTS_ROOT
    artifacts = generate_escalations(
        edition_name=edition_name,
        decision_ask_id=decision_ask_id,
        dry_run=True,
        reports_root=resolved_reports_root,
    )
    if not artifacts.previews:
        raise StateError(
            f"Decision ask '{decision_ask_id}' no longer has an active escalation preview in {edition_name}."
        )
    return DecisionAskEscalationPlan(
        edition_name=edition_name,
        decision_ask_id=decision_ask_id,
        artifacts=artifacts,
    )


def apply_decision_ask_escalation(
    plan: DecisionAskEscalationPlan,
    *,
    reports_root: Path | None = None,
    generated_at: datetime | None = None,
) -> EscalationArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    artifacts = generate_escalations(
        edition_name=plan.edition_name,
        decision_ask_id=plan.decision_ask_id,
        dry_run=False,
        as_of=generated_at,
        reports_root=resolved_reports_root,
    )
    if not artifacts.previews:
        raise StateError(
            f"Decision ask '{plan.decision_ask_id}' no longer has an active escalation preview in {plan.edition_name}."
        )
    return artifacts


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


def _accountable_aliases_by_workstream(workstreams: tuple[Workstream, ...]) -> dict[str, str]:
    accountable_by_workstream: dict[str, str] = {}
    for workstream in workstreams:
        workstream_id = workstream.id.strip()
        if not workstream_id:
            continue
        normalized = _normalize_identity(workstream.accountable_owner)
        if normalized is None:
            continue
        accountable_by_workstream[workstream_id] = normalized
    return accountable_by_workstream


def _resolve_recipients(
    *,
    linked_workstream_ids: tuple[str, ...],
    raw_program: dict[str, Any],
    leadership_readers: tuple[LeadershipReader, ...],
    accountable_by_workstream: dict[str, str],
    knowledge,
) -> tuple[str, ...]:
    accountables = tuple(
        dict.fromkeys(
            accountable_by_workstream[workstream_id]
            for workstream_id in linked_workstream_ids
            if workstream_id in accountable_by_workstream
        )
    )
    resolved = _resolve_contact_values(accountables, knowledge)
    if resolved:
        return resolved

    escalation_recipients = raw_program.get("escalation_recipients")
    if isinstance(escalation_recipients, list):
        resolved = _resolve_contact_values(
            tuple(str(value).strip() for value in escalation_recipients if str(value).strip()),
            knowledge,
        )
        if resolved:
            return resolved

    return _resolve_contact_values(tuple(reader.name for reader in leadership_readers), knowledge)


def _resolve_contact_values(values: tuple[str, ...], knowledge) -> tuple[str, ...]:
    if not values:
        return ()
    contacts: dict[str, str] = {}
    for person in knowledge.people_directory:
        if person.email is None or not person.email.strip():
            continue
        email = person.email.strip().lower()
        contacts[email] = email
        if person.alias:
            contacts[person.alias.strip().lower()] = email
        if person.display_name:
            contacts[person.display_name.strip().lower()] = email

    resolved: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if not normalized:
            continue
        resolved_email = contacts.get(normalized)
        if resolved_email is None and "@" in normalized:
            resolved_email = normalized
        if resolved_email is not None:
            resolved.append(resolved_email)
    return tuple(dict.fromkeys(resolved))


def _build_workstream_vitality_context(
    *,
    program_id: str,
    items: tuple[WorkItem, ...],
    workstreams,
    raw_program: dict[str, Any],
    knowledge,
    as_of: datetime,
    programs_root: Path,
) -> tuple[dict[str, int], dict[str, int]]:
    settings = vitality_settings_from_program(raw_program)
    exempt_aliases = effective_vitality_exempt_aliases(settings, knowledge.people_directory)
    eligible_items = tuple(
        item
        for item in items
        if item_owner_alias(item) not in exempt_aliases
    )
    if not eligible_items:
        return {}, {}

    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=programs_root,
    )
    leakage = detect_leakage(
        eligible_items,
        load_approved_workiq_signals(
            program_id,
            as_of=as_of,
            programs_root=programs_root,
        ),
        trajectory_loader=lambda work_item_id: trajectory_store.read(
            program_id,
            work_item_id,
        ),
    )
    scores = score_vitality(
        eligible_items,
        as_of=as_of,
        workstream_resolver=lambda item: gather_helpers._resolve_workstream_id(item.area_path, workstreams),
        leakage=leakage,
        leakage_signal_threshold=settings.sparse_workiq_threshold,
    )
    workstream_aggregates = aggregate_vitality(
        scores,
        scope_type="workstream",
        leakage_signal_threshold=settings.sparse_workiq_threshold,
    )
    composite_by_workstream = {
        aggregate.scope_id: aggregate.composite_score
        for aggregate in workstream_aggregates
    }
    stale_days_by_workstream: dict[str, int] = {}
    for score in scores:
        if score.workstream_id is None:
            continue
        previous = stale_days_by_workstream.get(score.workstream_id)
        if previous is None or score.freshness_days > previous:
            stale_days_by_workstream[score.workstream_id] = score.freshness_days
    return composite_by_workstream, stale_days_by_workstream


def _linked_workstream_vitality_composite(
    linked_workstream_ids: tuple[str, ...],
    composite_by_workstream: dict[str, int],
) -> int | None:
    values = [
        composite_by_workstream[workstream_id]
        for workstream_id in linked_workstream_ids
        if workstream_id in composite_by_workstream
    ]
    if not values:
        return None
    return min(values)


def _linked_workstream_stale_days(
    linked_workstream_ids: tuple[str, ...],
    stale_days_by_workstream: dict[str, int],
) -> int | None:
    values = [
        stale_days_by_workstream[workstream_id]
        for workstream_id in linked_workstream_ids
        if workstream_id in stale_days_by_workstream
    ]
    if not values:
        return None
    return max(values)


def _build_escalation_preview(
    *,
    edition_name: str,
    edition_title: str,
    dimension_name: str,
    dimension_summary: str,
    consecutive_high: int,
    rule_name: str,
    linked_workstream_ids: tuple[str, ...],
    linked_workstream_names: tuple[str, ...],
    recipients: tuple[str, ...],
    evidence_lines: tuple[str, ...],
    trend_label: str | None,
    risk_sparkline: str | None,
    recommended_action: str,
    vitality_composite: int | None = None,
    stale_days: int | None = None,
    dependency_context: EscalationDependencyContext | None = None,
) -> EscalationPreview:
    workstream_label = ", ".join(linked_workstream_names or linked_workstream_ids) or "unmapped"
    subject = f"[Vertex] Escalation: {edition_title} - {dimension_name}"
    body_lines = [
        f"Vertex detected an escalation condition in {edition_name}.",
        "",
        f"Rule: {rule_name}",
        f"Dimension: {dimension_name}",
        f"Linked workstreams: {workstream_label}",
    ]
    if dependency_context is not None:
        body_lines.append(f"Escalation path: {dependency_context.escalation_path_label}")
    if consecutive_high > 0:
        body_lines.append(f"Consecutive High issues: {consecutive_high}")
    if vitality_composite is not None:
        body_lines.append(f"Lowest linked vitality composite: {vitality_composite}")
    if stale_days is not None:
        day_label = "day" if stale_days == 1 else "days"
        body_lines.append(f"Oldest linked stale item: {stale_days} {day_label}")
    body_lines.extend(
        (
            f"Current summary: {dimension_summary}",
            "",
            "Review and send this escalation manually via Outlook.",
        )
    )
    if dependency_context is not None:
        body_lines.append(dependency_context.guidance)
    md_body = "\n".join(body_lines)
    metric_fragments: list[str] = []
    if consecutive_high > 0:
        metric_fragments.append(f"{consecutive_high} consecutive High issues")
    if vitality_composite is not None:
        metric_fragments.append(f"vitality {vitality_composite}")
    if stale_days is not None:
        metric_fragments.append(f"stale {stale_days}d")
    if metric_fragments:
        signal_text = f"Escalation drafted for {dimension_name} ({rule_name}; {', '.join(metric_fragments)})."
    else:
        signal_text = f"Escalation drafted for {dimension_name} (rule {rule_name})."
    return _finalize_escalation_preview(
        edition_name=edition_name,
        preview=EscalationPreview(
            rule_name=rule_name,
            dimension_name=dimension_name,
            consecutive_high=consecutive_high,
            workstream_ids=linked_workstream_ids,
            workstream_names=linked_workstream_names,
            recipients=recipients,
            subject=subject,
            md_body=md_body,
            html_body="",
            detail_label="Current Summary",
            detail_text=dimension_summary,
            evidence_lines=evidence_lines,
            trend_label=trend_label,
            risk_sparkline=risk_sparkline,
            recommended_action=recommended_action,
            signal_text=signal_text,
            cooldown_key=_build_cooldown_key(rule_name, dimension_name, linked_workstream_ids),
            vitality_composite=vitality_composite,
            stale_days=stale_days,
            escalation_path_label=(dependency_context.escalation_path_label if dependency_context is not None else None),
            escalation_guidance=(dependency_context.guidance if dependency_context is not None else None),
        ),
    )


def _build_dimension_escalation_recommended_action(
    dependency_context: EscalationDependencyContext | None,
) -> str:
    base = (
        "Review whether this chronic dimension now needs a risk register entry, "
        "an explicit owner recovery plan, or leadership escalation."
    )
    if dependency_context is None:
        return base
    return f"{base} {dependency_context.guidance}"


def _build_dimension_escalation_dependency_context(
    *,
    program_id: str,
    linked_workstream_ids: tuple[str, ...],
    programs_root: Path,
) -> EscalationDependencyContext | None:
    if not linked_workstream_ids:
        return None
    relevant = tuple(
        dependency
        for dependency in project_dependencies(
            load_program_facts(
                program_id,
                programs_root=programs_root,
                fact_types=("dependency.link",),
            )
        )
        if dependency.status is DependencyStatus.ACTIVE
        and dependency.resolution_path
        and (
            dependency.from_workstream_id in linked_workstream_ids
            or dependency.to_workstream_id in linked_workstream_ids
        )
    )
    if not relevant:
        return None

    def _priority(resolution_path: str) -> int:
        normalized = resolution_path.strip().lower()
        if normalized == "external":
            return 0
        if normalized.startswith("cross_org"):
            return 1
        if normalized == "intra_storage":
            return 2
        return 3

    ranked = sorted(
        relevant,
        key=lambda dependency: (
            _priority(dependency.resolution_path or ""),
            dependency_target_label(dependency),
            dependency.id,
        ),
    )
    selected = ranked[0]
    selected_resolution_path = str(selected.resolution_path or "").strip().lower()
    matching_targets = tuple(
        dict.fromkeys(
            dependency_target_label(dependency)
            for dependency in ranked
            if str(dependency.resolution_path or "").strip().lower() == selected_resolution_path
        )
    )
    target_preview = ", ".join(matching_targets[:2]) if matching_targets else "the dependent lane"
    if len(matching_targets) > 2:
        target_preview = f"{target_preview}, +{len(matching_targets) - 2} more"
    if selected_resolution_path == "intra_storage":
        return EscalationDependencyContext(
            escalation_path_label="Internal dependency follow-up",
            guidance=f"This dependency pressure remains internal to Storage; route the follow-up through the Storage owner chain for {target_preview}.",
        )
    if selected_resolution_path.startswith("cross_org"):
        return EscalationDependencyContext(
            escalation_path_label="Cross-org dependency escalation",
            guidance=f"This dimension is exposed through cross-org dependency pressure on {target_preview}; consider adding the partner bridge and VP-level CC coverage before sending.",
        )
    if selected_resolution_path == "external":
        return EscalationDependencyContext(
            escalation_path_label="External dependency escalation",
            guidance=f"This dimension is exposed through an external dependency on {target_preview}; frame the note as a formal program dependency escalation before sending.",
        )
    return None


def _build_milestone_escalation_preview(
    *,
    edition_name: str,
    edition_title: str,
    milestone,
    assessment,
    rule_name: str,
    linked_workstream_names: tuple[str, ...],
    recipients: tuple[str, ...],
    cooldown_key: str,
    vitality_composite: int | None,
    stale_days: int | None,
    milestone_days_to_target: int,
    milestone_schedule_summary: str | None,
    milestone_target_date_history_summary: str | None,
    milestone_completion_date_history_summary: str | None,
) -> EscalationPreview:
    workstream_label = ", ".join(linked_workstream_names or milestone.linked_workstream_ids) or "unmapped"
    subject = f"[Vertex] Escalation: {edition_title} - {milestone.name}"
    body_lines = [
        f"Vertex detected an escalation condition in {edition_name}.",
        "",
        f"Rule: {rule_name}",
        f"Milestone: {milestone.name}",
        f"Linked workstreams: {workstream_label}",
        f"Milestone status: {assessment.computed_health.value}",
        f"Days to target: {milestone_days_to_target}",
    ]
    if milestone_schedule_summary is not None:
        body_lines.append(f"Milestone schedule: {milestone_schedule_summary}")
    if milestone_target_date_history_summary is not None:
        body_lines.append(milestone_target_date_history_summary)
    if milestone_completion_date_history_summary is not None:
        body_lines.append(milestone_completion_date_history_summary)
    if vitality_composite is not None:
        body_lines.append(f"Lowest linked vitality composite: {vitality_composite}")
    if stale_days is not None:
        day_label = "day" if stale_days == 1 else "days"
        body_lines.append(f"Oldest linked stale item: {stale_days} {day_label}")
    if assessment.blocked_criteria:
        body_lines.append(f"Blocked criteria: {assessment.blocked_criteria[0]}")
    body_lines.extend(
        (
            f"Assessment: {assessment.reasoning}",
            "",
            "Review and send this escalation manually via Outlook.",
        )
    )
    md_body = "\n".join(body_lines)
    metric_fragments = [
        f"status {assessment.computed_health.value}",
        f"days_to_target {milestone_days_to_target}",
    ]
    if vitality_composite is not None:
        metric_fragments.append(f"vitality {vitality_composite}")
    if stale_days is not None:
        metric_fragments.append(f"stale {stale_days}d")
    signal_text = f"Escalation drafted for milestone {milestone.name} ({rule_name}; {', '.join(metric_fragments)})."
    return _finalize_escalation_preview(
        edition_name=edition_name,
        preview=EscalationPreview(
            rule_name=rule_name,
            dimension_name=milestone.name,
            consecutive_high=0,
            workstream_ids=milestone.linked_workstream_ids,
            workstream_names=linked_workstream_names,
            recipients=recipients,
            subject=subject,
            md_body=md_body,
            html_body="",
            detail_label="Assessment",
            detail_text=assessment.reasoning,
            recommended_action=(
                "Review blocked exit criteria with the accountable owner, confirm the recovery date, "
                "and escalate dependencies that can no longer hold the target."
            ),
            signal_text=signal_text,
            cooldown_key=cooldown_key,
            vitality_composite=vitality_composite,
            stale_days=stale_days,
            milestone_id=milestone.id,
            milestone_status=assessment.computed_health.value,
            milestone_days_to_target=milestone_days_to_target,
            milestone_schedule_summary=milestone_schedule_summary,
            milestone_target_date_history_summary=milestone_target_date_history_summary,
            milestone_completion_date_history_summary=milestone_completion_date_history_summary,
        ),
    )


def _build_decision_ask_escalation_preview(
    *,
    edition_title: str,
    edition_name: str,
    ask,
    effective_status: str,
    ask_age_days: int,
    linked_workstream_ids: tuple[str, ...],
    linked_workstream_names: tuple[str, ...],
    recipients: tuple[str, ...],
    cooldown_key: str,
    vitality_composite: int | None,
    stale_days: int | None,
    rule_name: str,
    incident_refs: tuple[str, ...],
    incident_summary: str | None,
) -> EscalationPreview:
    workstream_label = ", ".join(linked_workstream_names or linked_workstream_ids) or "unmapped"
    subject = f"[Vertex] Escalation: {edition_title} - {ask.id}"
    body_lines = [
        f"Vertex detected an escalation condition in {edition_name}.",
        "",
        f"Rule: {rule_name}",
        f"Decision ask: {ask.id}",
        f"Status: {effective_status}",
        f"Age: {ask_age_days} days",
        f"Linked workstreams: {workstream_label}",
        f"Ask text: {ask.text}",
    ]
    if incident_summary:
        body_lines.append(f"Incident-linked: {incident_summary}")
    if vitality_composite is not None:
        body_lines.append(f"Lowest linked vitality composite: {vitality_composite}")
    if stale_days is not None:
        day_label = "day" if stale_days == 1 else "days"
        body_lines.append(f"Oldest linked stale item: {stale_days} {day_label}")
    body_lines.extend(
        (
            "",
            "Review and send this escalation manually via Outlook.",
        )
    )
    md_body = "\n".join(body_lines)
    metric_fragments = [
        f"status {effective_status}",
        f"age {ask_age_days}d",
    ]
    if vitality_composite is not None:
        metric_fragments.append(f"vitality {vitality_composite}")
    if stale_days is not None:
        metric_fragments.append(f"stale {stale_days}d")
    signal_text = f"Escalation drafted for decision ask {ask.id} ({rule_name}; {', '.join(metric_fragments)})."
    return _finalize_escalation_preview(
        edition_name=edition_name,
        preview=EscalationPreview(
            rule_name=rule_name,
            dimension_name=ask.id,
            consecutive_high=0,
            workstream_ids=linked_workstream_ids,
            workstream_names=linked_workstream_names,
            recipients=recipients,
            subject=subject,
            md_body=md_body,
            html_body="",
            detail_label="Ask Text",
            detail_text=ask.text,
            recommended_action=(
                "Drive owner follow-up to close the ask or restate it with a decision owner and date "
                "before the next publish cycle."
            ),
            signal_text=signal_text,
            cooldown_key=cooldown_key,
            vitality_composite=vitality_composite,
            stale_days=stale_days,
            decision_ask_id=ask.id,
            decision_ask_status=effective_status,
            decision_ask_age_days=ask_age_days,
            decision_ask_owner_alias=ask.owner_alias,
            decision_ask_entity_refs=ask.entity_refs,
            incident_refs=incident_refs,
            incident_summary=incident_summary,
        ),
    )


def _decision_ask_workstream_ids(
    entity_refs: tuple[str, ...],
    items: tuple[WorkItem, ...],
    workstreams,
) -> tuple[str, ...]:
    item_lookup = {item.id: item for item in items}
    linked_workstream_ids: list[str] = []
    for ref in entity_refs:
        if not ref.upper().startswith("WI:"):
            continue
        work_item_id = ref.split(":", 1)[1]
        if not work_item_id.isdigit():
            continue
        item = item_lookup.get(int(work_item_id))
        if item is None:
            continue
        workstream_id = gather_helpers._resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is not None:
            linked_workstream_ids.append(workstream_id)
    return tuple(dict.fromkeys(linked_workstream_ids))


def _scorecard_evidence_packet(
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    dimension_name: str,
) -> ScorecardEvidencePacket | None:
    for packet_map in scorecard_packets.values():
        packet = packet_map.get(dimension_name)
        if packet is not None:
            return packet
    return None


def _scorecard_evidence_lines(packet: ScorecardEvidencePacket | None) -> tuple[str, ...]:
    if packet is None:
        return ()
    evidence_lines = [f"Total items: {packet.total_items}"]
    if packet.blocked_count > 0:
        evidence_lines.append(f"Blocked items: {packet.blocked_count}")
    if packet.overdue_count > 0:
        evidence_lines.append(f"Overdue items: {packet.overdue_count}")
    if packet.stale_count > 0:
        evidence_lines.append(f"Stale items: {packet.stale_count}")
    if packet.unowned_count > 0:
        evidence_lines.append(f"Unowned items: {packet.unowned_count}")
    if packet.item_ids:
        sample_ids = ", ".join(f"WI:{work_item_id}" for work_item_id in packet.item_ids[:3])
        evidence_lines.append(f"Sample items: {sample_ids}")
    return tuple(evidence_lines)


def _history_risk_trend(history: tuple[RiskLevel, ...]) -> tuple[str | None, str | None]:
    visible_history = history[-4:]
    if not visible_history:
        return None, None
    sparkline = "".join(report_helpers._spark_char(level) for level in visible_history)
    if all(level == RiskLevel.HIGH for level in visible_history):
        return sparkline, "chronic high"
    if len(visible_history) >= 2:
        if report_helpers._risk_rank(visible_history[-1]) > report_helpers._risk_rank(visible_history[0]):
            return sparkline, "rising 4w"
        if report_helpers._risk_rank(visible_history[-1]) < report_helpers._risk_rank(visible_history[0]):
            return sparkline, "falling 4w"
    return sparkline, "stable"


def _finalize_escalation_preview(*, edition_name: str, preview: EscalationPreview) -> EscalationPreview:
    return replace(
        preview,
        html_body=_render_escalation_email_html(edition_name=edition_name, preview=preview),
    )


@lru_cache(maxsize=1)
def _escalation_template_environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(REPO_ROOT / "templates"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return environment


def _render_escalation_email_html(*, edition_name: str, preview: EscalationPreview) -> str:
    try:
        template = _escalation_template_environment().get_template("escalation.j2")
    except TemplateNotFound as error:
        raise StateError("Missing escalation template: templates/escalation.j2") from error
    return template.render(
        title=preview.subject,
        preheader=f"Escalation draft for {preview.dimension_name}",
        header_label="Vertex Escalation Draft",
        subtitle=f"{edition_name} | rule {preview.rule_name}",
        footer_text="Generated by Vertex. Review and send manually via Outlook.",
        show_footer=True,
        edition_name=edition_name,
        preview=preview,
        subject_label=_escalation_subject_label(preview),
        workstream_label=_escalation_workstream_label(preview),
        fact_rows=_escalation_fact_rows(preview),
    )


def _escalation_subject_label(preview: EscalationPreview) -> str:
    if preview.milestone_id is not None:
        return "Milestone"
    if preview.decision_ask_id is not None:
        return "Decision Ask"
    return "Dimension"


def _escalation_workstream_label(preview: EscalationPreview) -> str:
    values = preview.workstream_names or preview.workstream_ids
    return ", ".join(values) or "unmapped"


def _escalation_fact_rows(preview: EscalationPreview) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    if preview.consecutive_high > 0:
        rows.append(("Consecutive High", str(preview.consecutive_high)))
    if preview.milestone_status is not None:
        rows.append(("Milestone Status", preview.milestone_status))
    if preview.milestone_days_to_target is not None:
        rows.append(("Days to Target", str(preview.milestone_days_to_target)))
    if preview.milestone_schedule_summary is not None:
        rows.append(("Milestone Schedule", preview.milestone_schedule_summary))
    if preview.milestone_target_date_history_summary is not None:
        rows.append(
            (
                "Target History",
                preview.milestone_target_date_history_summary.removeprefix("Target history "),
            )
        )
    if preview.milestone_completion_date_history_summary is not None:
        rows.append(
            (
                "Completion History",
                preview.milestone_completion_date_history_summary.removeprefix("Completion history "),
            )
        )
    if preview.decision_ask_status is not None:
        rows.append(("Decision Ask Status", preview.decision_ask_status))
    if preview.decision_ask_age_days is not None:
        rows.append(("Decision Ask Age", f"{preview.decision_ask_age_days} days"))
    if preview.incident_summary is not None:
        rows.append(("Incident-linked", preview.incident_summary))
    if preview.escalation_path_label is not None:
        rows.append(("Escalation Path", preview.escalation_path_label))
    if preview.vitality_composite is not None:
        rows.append(("Vitality Composite", str(preview.vitality_composite)))
    if preview.stale_days is not None:
        rows.append(("Stale Days", f"{preview.stale_days} days"))
    return tuple(rows)


def _write_escalation_eml(
    *,
    edition_name: str,
    preview: EscalationPreview,
    generated_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
    from_display_name: str,
    from_email: str,
    index: int,
) -> Path:
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root) / "escalations"
    file_name = (
        f"{generated_at.strftime('%Y%m%dT%H%M%SZ')}.{index:02d}.{_slugify(preview.dimension_name)}.eml"
    )
    return write_eml(
        output_dir / file_name,
        eml_bytes=build_eml_bytes(
            to=preview.recipients,
            cc=(from_email,),
            subject=preview.subject,
            html_body=preview.html_body,
            text_body=preview.md_body,
            from_display_name=from_display_name,
            from_email=from_email,
            generated_at=generated_at,
        ),
    )


def _send_escalation_email(preview: EscalationPreview, *, author_email: str) -> str | None:
    cc = (author_email,) if author_email.strip() else ()
    GraphSendClient().send_mail(
        GraphMailMessage(
            to=preview.recipients,
            cc=cc,
            subject=preview.subject,
            html_body=preview.html_body,
        )
    )
    return None


def _build_escalation_email_sender(*, author_email: str) -> EscalationSender:
    return lambda preview: _send_escalation_email(preview, author_email=author_email)


def _append_escalation_signal(
    *,
    program_id: str,
    preview: EscalationPreview,
    signal_ref: str,
    current_time: datetime,
    programs_root: Path,
) -> Path:
    workstream_id = preview.workstream_ids[0] if preview.workstream_ids else None
    refs = tuple(f"workstream:{workstream_id}" for workstream_id in preview.workstream_ids)
    if preview.milestone_id is not None:
        refs = refs + (f"milestone:{preview.milestone_id}",)
    elif preview.decision_ask_id is not None:
        refs = refs + tuple(
            entity_ref
            for entity_ref in preview.decision_ask_entity_refs
            if entity_ref not in refs
        ) + tuple(incident_ref for incident_ref in preview.incident_refs if incident_ref not in refs) + (f"decision_ask:{preview.decision_ask_id}",)
    else:
        refs = refs + (f"scorecard:{preview.dimension_name}",)
    metadata: dict[str, object] = {
        "rule": preview.rule_name,
        "consecutive_high": preview.consecutive_high,
    }
    if preview.vitality_composite is not None:
        metadata["vitality_composite"] = preview.vitality_composite
    if preview.stale_days is not None:
        metadata["stale_days"] = preview.stale_days
    if preview.milestone_status is not None:
        metadata["milestone_status"] = preview.milestone_status
    if preview.milestone_days_to_target is not None:
        metadata["milestone_days_to_target"] = preview.milestone_days_to_target
    if preview.milestone_schedule_summary is not None:
        metadata["milestone_schedule_summary"] = preview.milestone_schedule_summary
    if preview.milestone_target_date_history_summary is not None:
        metadata["milestone_target_date_history_summary"] = preview.milestone_target_date_history_summary
    if preview.milestone_completion_date_history_summary is not None:
        metadata["milestone_completion_date_history_summary"] = preview.milestone_completion_date_history_summary
    if preview.decision_ask_status is not None:
        metadata["decision_ask_status"] = preview.decision_ask_status
    if preview.decision_ask_age_days is not None:
        metadata["decision_ask_age_days"] = preview.decision_ask_age_days
    if preview.incident_refs:
        metadata["incident_refs"] = list(preview.incident_refs)
    if preview.incident_summary is not None:
        metadata["incident_summary"] = preview.incident_summary
    if preview.escalation_path_label is not None:
        metadata["escalation_path_label"] = preview.escalation_path_label
    if preview.escalation_guidance is not None:
        metadata["escalation_guidance"] = preview.escalation_guidance
    signal = Signal(
        id=_build_escalation_signal_id(
            program_id=program_id,
            workstream_id=workstream_id,
            refs=refs,
            text=preview.signal_text,
            timestamp=current_time,
        ),
        timestamp=current_time,
        source="vertex/escalation",
        program_id=program_id,
        workstream_id=workstream_id,
        entity_refs=refs,
        text=preview.signal_text,
        raw_ref=signal_ref,
        confidence=Confidence.HIGH,
        metadata=metadata,
    )
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    signal_store.append(_classify_signal(signal))
    signal_store.append_review(
        program_id,
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=current_time,
            reviewed_by=_default_actor(),
            note="Escalation draft generated.",
        ),
    )

    program = load_program(program_id, programs_root=programs_root)
    if resolve_storage_backend(program.storage_backend if program is not None else None) == "sqlite":
        return get_program_sqlite_store_path(program_id, programs_root=programs_root)
    return get_weekly_journal_path(program_id, current_time, programs_root=programs_root)


def _build_escalation_signal_id(
    *,
    program_id: str,
    workstream_id: str | None,
    refs: tuple[str, ...],
    text: str,
    timestamp: datetime,
) -> str:
    payload = f"{program_id}|vertex/escalation|{workstream_id or ''}|{'|'.join(refs)}|{text.lower()}|{timestamp.isoformat()}"
    return str(uuid5(NAMESPACE_URL, payload))


def _dimension_history_levels(
    edition_name: str,
    dimension_name: str,
    *,
    archive_dir: Path,
) -> tuple[RiskLevel, ...]:
    history: list[RiskLevel] = []
    scorecard_history_path = archive_dir / "scorecards.json"
    if scorecard_history_path.exists():
        try:
            payload = json.loads(scorecard_history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("dimension", "")).strip().lower() != dimension_name.strip().lower():
                continue
            raw_risk = entry.get("risk")
            if not isinstance(raw_risk, str):
                continue
            try:
                history.append(RiskLevel.from_string(raw_risk))
            except ValueError:
                continue
    return tuple(history)


def _consecutive_high_count(history: tuple[RiskLevel, ...]) -> int:
    count = 0
    for risk in reversed(history):
        if risk != RiskLevel.HIGH:
            break
        count += 1
    return count


def _build_cooldown_key(rule_name: str, dimension_name: str, workstream_ids: tuple[str, ...]) -> str:
    suffix = _slugify(dimension_name)
    if workstream_ids:
        suffix = f"{suffix}:{','.join(workstream_ids)}"
    return f"{rule_name}:{suffix}"


def _resolve_author_alias(author_email: str | None, display_name: str | None) -> str:
    if author_email is not None and "@" in author_email:
        alias = author_email.split("@", 1)[0].strip()
        if alias:
            return alias
    if display_name is not None and display_name.strip():
        return display_name.strip()
    return "manual"


def _record_declined_escalation_audits(
    *,
    edition_name: str,
    artifacts: EscalationArtifacts,
    channel: str,
    reports_root: Path,
) -> None:
    programs_root = reports_root.parent / "programs"
    load_result = load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        programs_root=programs_root,
    )
    resolved = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    if resolved is None:
        raise typer.BadParameter(f"Edition '{edition_name}' could not be resolved.")
    author_alias = _resolve_author_alias(load_result.bundle.config.author.email, load_result.bundle.config.author.display_name)
    declined_at = datetime.now(timezone.utc)
    for preview in artifacts.previews:
        action_type = _escalation_action_type(preview)
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id=resolved.program.id,
                action_id=str(uuid4()),
                level="l3" if channel == "email" else "l2",
                author_alias=author_alias,
                subject_alias=preview.decision_ask_owner_alias,
                action_type=action_type,
                evidence_refs=_escalation_evidence_refs(preview),
                policy_rule=preview.rule_name,
                accepted=False,
                applied_at=declined_at,
                blast_radius=(
                    f"Escalation declined before any {'live email' if channel == 'email' else 'local EML draft'} "
                    f"was created for {len(preview.recipients)} recipient(s)."
                ),
                rollback_mechanism=(
                    "No rollback needed; escalation was not sent."
                    if channel == "email"
                    else "No rollback needed; escalation draft was not written."
                ),
                prior_acceptance_rate=compute_prior_acceptance_rate(
                    resolved.program.id,
                    action_type=action_type,
                    programs_root=programs_root,
                ),
            ),
            programs_root=programs_root,
        )


def _escalation_action_type(preview: EscalationPreview) -> str:
    if preview.decision_ask_id is not None:
        return "decision_ask_escalation"
    if preview.milestone_id is not None:
        return "milestone_escalation"
    return "dimension_escalation"


def _escalation_evidence_refs(preview: EscalationPreview) -> tuple[str, ...]:
    refs = list(preview.decision_ask_entity_refs)
    if preview.decision_ask_id is not None:
        refs.extend(preview.incident_refs)
        refs.append(f"decision_ask:{preview.decision_ask_id}")
    elif preview.milestone_id is not None:
        refs.append(f"milestone:{preview.milestone_id}")
    else:
        refs.append(f"dimension:{_slugify(preview.dimension_name)}")
    refs.extend(f"workstream:{workstream_id}" for workstream_id in preview.workstream_ids)
    return tuple(dict.fromkeys(refs))


def _escalation_blast_radius(preview: EscalationPreview, *, channel: str) -> str:
    delivery = "live email" if channel == "email" else "local EML draft"
    return f"1 {delivery} to {len(preview.recipients)} recipient(s)"


def _escalation_rollback_mechanism(*, channel: str) -> str:
    if channel == "email":
        return "Send a corrective follow-up and pause further escalations for this rule if the escalation was unnecessary."
    return "Delete the draft EML and ignore the generated escalation signal if the escalation is not needed."


def render_escalation_preview_plaintext(artifacts: EscalationArtifacts) -> str:
    lines = ["ESCALATE PREVIEW"]
    if not artifacts.previews:
        lines.append("No escalation rules triggered.")
    for index, preview in enumerate(artifacts.previews, start=1):
        lines.append(f"{index}. Rule: {preview.rule_name}")
        lines.append(f"   To: {', '.join(preview.recipients)}")
        if preview.milestone_id is not None:
            lines.append(f"   Milestone: {preview.dimension_name}")
            if preview.milestone_status is not None:
                lines.append(f"   Milestone Status: {preview.milestone_status}")
            if preview.milestone_days_to_target is not None:
                lines.append(f"   Days to Target: {preview.milestone_days_to_target}")
            if preview.milestone_schedule_summary is not None:
                lines.append(f"   Milestone Schedule: {preview.milestone_schedule_summary}")
            if preview.milestone_target_date_history_summary is not None:
                lines.append(
                    "   Target History: "
                    f"{preview.milestone_target_date_history_summary.removeprefix('Target history ')}"
                )
            if preview.milestone_completion_date_history_summary is not None:
                lines.append(
                    "   Completion History: "
                    f"{preview.milestone_completion_date_history_summary.removeprefix('Completion history ')}"
                )
        elif preview.decision_ask_id is not None:
            lines.append(f"   Decision Ask: {preview.dimension_name}")
            if preview.decision_ask_status is not None:
                lines.append(f"   Decision Ask Status: {preview.decision_ask_status}")
            if preview.decision_ask_age_days is not None:
                lines.append(f"   Decision Ask Age: {preview.decision_ask_age_days}")
            if preview.incident_summary is not None:
                lines.append(f"   Incident-linked: {preview.incident_summary}")
        else:
            lines.append(f"   Dimension: {preview.dimension_name}")
        if preview.workstream_names or preview.workstream_ids:
            label = ", ".join(preview.workstream_names or preview.workstream_ids)
            lines.append(f"   Workstreams: {label}")
        if preview.escalation_path_label is not None:
            lines.append(f"   Escalation Path: {preview.escalation_path_label}")
        if preview.consecutive_high > 0:
            lines.append(f"   Consecutive High: {preview.consecutive_high}")
        if preview.vitality_composite is not None:
            lines.append(f"   Vitality Composite: {preview.vitality_composite}")
        if preview.stale_days is not None:
            lines.append(f"   Stale Days: {preview.stale_days}")
        lines.append(f"   Subject: {preview.subject}")
    for message in artifacts.suppressed:
        lines.append(f"Suppressed: {message}")
    for message in artifacts.unresolved:
        lines.append(f"Unresolved: {message}")
    return "\n".join(lines)


def render_escalation_output(
    *,
    edition_name: str,
    artifacts: EscalationArtifacts,
    dry_run: bool,
    channel: str,
    format: str,
) -> str:
    payload = {
        "edition_name": edition_name,
        "channel": channel,
        "dry_run": dry_run,
        "preview_count": len(artifacts.previews),
        "suppressed_count": len(artifacts.suppressed),
        "unresolved_count": len(artifacts.unresolved),
        "state_path": str(artifacts.state_path) if artifacts.state_path is not None else None,
        "eml_paths": [str(path) for path in artifacts.eml_paths],
        "signal_paths": [str(path) for path in artifacts.signal_paths],
        "sent_count": artifacts.sent_count,
        "suppressed": list(artifacts.suppressed),
        "unresolved": list(artifacts.unresolved),
        "previews": [_serialize_escalation_preview(preview) for preview in artifacts.previews],
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            (
                "edition_name",
                "channel",
                "dry_run",
                "rule_name",
                "preview_type",
                "dimension_name",
                "recipients",
                "workstream_ids",
                "workstream_names",
                "consecutive_high",
                "vitality_composite",
                "stale_days",
                "milestone_id",
                "milestone_status",
                "milestone_days_to_target",
                "decision_ask_id",
                "decision_ask_status",
                "decision_ask_age_days",
                "incident_refs",
                "incident_summary",
                "escalation_path_label",
                "subject",
            )
        )
        for preview in payload["previews"]:  # type: ignore[attr-defined]
            writer.writerow(
                (
                    payload["edition_name"],
                    payload["channel"],
                    str(payload["dry_run"]).lower(),
                    preview["rule_name"],
                    preview["preview_type"],
                    preview["dimension_name"],
                    preview["recipients"],
                    preview["workstream_ids"],
                    preview["workstream_names"],
                    preview["consecutive_high"] if preview["consecutive_high"] is not None else "",
                    preview["vitality_composite"] if preview["vitality_composite"] is not None else "",
                    preview["stale_days"] if preview["stale_days"] is not None else "",
                    preview["milestone_id"] or "",
                    preview["milestone_status"] or "",
                    preview["milestone_days_to_target"] if preview["milestone_days_to_target"] is not None else "",
                    preview["decision_ask_id"] or "",
                    preview["decision_ask_status"] or "",
                    preview["decision_ask_age_days"] if preview["decision_ask_age_days"] is not None else "",
                    preview["incident_refs"],
                    preview["incident_summary"] or "",
                    preview["escalation_path_label"] or "",
                    preview["subject"],
                )
            )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _serialize_escalation_preview(preview: EscalationPreview) -> dict[str, object]:
    if preview.milestone_id is not None:
        preview_type = "milestone"
    elif preview.decision_ask_id is not None:
        preview_type = "decision_ask"
    else:
        preview_type = "dimension"
    return {
        "rule_name": preview.rule_name,
        "preview_type": preview_type,
        "dimension_name": preview.dimension_name,
        "recipients": ", ".join(preview.recipients),
        "workstream_ids": ", ".join(preview.workstream_ids),
        "workstream_names": ", ".join(preview.workstream_names),
        "consecutive_high": preview.consecutive_high,
        "vitality_composite": preview.vitality_composite,
        "stale_days": preview.stale_days,
        "milestone_id": preview.milestone_id,
        "milestone_status": preview.milestone_status,
        "milestone_days_to_target": preview.milestone_days_to_target,
        "decision_ask_id": preview.decision_ask_id,
        "decision_ask_status": preview.decision_ask_status,
        "decision_ask_age_days": preview.decision_ask_age_days,
        "incident_refs": "|".join(preview.incident_refs),
        "incident_summary": preview.incident_summary,
        "escalation_path_label": preview.escalation_path_label,
        "escalation_guidance": preview.escalation_guidance,
        "subject": preview.subject,
    }


def _build_incident_patterns(entries: tuple[IncidentEntry, ...]) -> tuple[IncidentRefPattern, ...]:
    return build_incident_ref_patterns(entries)


def _related_incident_patterns_for_ask(
    ask,
    patterns: tuple[IncidentRefPattern, ...],
) -> tuple[IncidentRefPattern, ...]:
    if not ask.entity_refs:
        return ()
    ask_refs = {normalize_incident_ref(ref) for ref in ask.entity_refs if normalize_incident_ref(ref)}
    if not ask_refs:
        return ()
    return tuple(pattern for pattern in patterns if pattern.ref in ask_refs)


def _render_incident_pattern_evidence(pattern: IncidentRefPattern) -> str:
    incident_refs = ", ".join(str(ref) for ref in pattern.incident_refs)
    if pattern.entry_count == 1:
        return f"{pattern.ref}: {pattern.summary_text}. Source: {incident_refs}. ({pattern.confidence.value.lower()} confidence)"
    return (
        f"{pattern.ref}: repeated across {pattern.entry_count} incident learnings. {pattern.summary_text}. "
        f"Source: {incident_refs}. ({pattern.confidence.value.lower()} confidence)"
    )


def _normalize_identity(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized or None


def _slugify(value: str) -> str:
    cleaned = [character.lower() if character.isalnum() else "_" for character in value.strip()]
    return "".join(cleaned).strip("_") or "escalation"


def _default_actor() -> str:
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "vertex").strip() or "vertex"
