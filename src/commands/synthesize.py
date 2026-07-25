from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import inspect
import json
import os
from pathlib import Path
import re
from typing import Callable

import click
import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.client import AIClient, AIClientError
from src.ai.deployment_fallback import LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.synthesizer import SynthesizedProposalDraft, SynthesizerError, WorkstreamSynthesizer, build_synthesizer_from_client
from src.core.action_tracker import PROGRAMS_ROOT
from src.core.analytics_store import load_contradiction_state
from src.core.ai_proposal_store import (
    append_ai_proposal,
    build_ai_proposal_id,
    expire_stale_ai_proposals,
    load_ai_proposals,
    supersede_pending_ai_proposals,
)
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import EDITIONS_ROOT, _parse_program, resolve_edition
from src.core.grounding_validator import validate_synthesis_grounding
from src.core.knowledge_store import load_program_knowledge
from src.core.models import Confidence
from src.core.models_v2 import AIProposal, AIProposalStatus, ActionStatus, ContradictionPacket, Program, RiskStatus, Signal, Workstream
from src.core.program_fact_store import load_current_workstreams, load_program_facts, project_action_items, project_risk_entries
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.signal_ranking import sort_signals_for_ai_context
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.trajectory_analyzer import DriftPattern, analyze_trajectories
from src.core.yaml_utils import load_yaml_mapping


SYNTHESIS_SIGNAL_WINDOW_DAYS = 30
_ITEM_REF_RE = re.compile(r"\bWI[:#\s-]?(\d+)\b", re.IGNORECASE)

NowProvider = Callable[[], datetime]
SynthesizerBuilder = Callable[[Program], WorkstreamSynthesizer]


@dataclass(frozen=True, slots=True)
class SynthesizeResult:
    program_id: str
    workstream_id: str
    proposal: AIProposal
    proposal_path: Path
    prompt_version: str
    superseded_count: int
    invalid_evidence_refs: tuple[str, ...]
    flagged_for_review: bool
    signal_count: int
    drift_pattern_count: int
    risk_count: int
    action_count: int


class SynthesisDisabledError(SynthesizerError):
    """Raised when synthesize is invoked while AI is disabled for the command."""


class _FallbackWorkstreamSynthesizer:
    def __init__(
        self,
        *,
        deployments: tuple[str, ...],
        temperature: float,
        budget_usd: float,
        requests_per_minute: int | None = None,
        trace_context: AITraceContext | None = None,
    ) -> None:
        self._deployments = deployments
        self._temperature = temperature
        self._budget_usd = budget_usd
        self._requests_per_minute = requests_per_minute
        self._trace_context = trace_context

    def generate(
        self,
        *,
        program: Program,
        workstream: Workstream,
        signals: tuple[Signal, ...],
        drift_patterns: tuple[DriftPattern, ...],
        open_risks=(),
        open_actions=(),
        contradictions: tuple[ContradictionPacket, ...] = (),
        programs_root: Path = PROGRAMS_ROOT,
    ) -> SynthesizedProposalDraft | None:
        last_error: Exception | None = None
        for deployment in self._deployments:
            try:
                client_kwargs = {
                    "deployment": deployment,
                    "temperature": self._temperature,
                    "budget_usd": self._budget_usd,
                    "trace_context": self._trace_context,
                }
                if self._requests_per_minute is not None and _ai_client_supports_parameter("requests_per_minute"):
                    client_kwargs["requests_per_minute"] = self._requests_per_minute
                client = AIClient(**client_kwargs)  # type: ignore[arg-type]
                return build_synthesizer_from_client(client).generate(
                    program=program,
                    workstream=workstream,
                    signals=signals,
                    drift_patterns=drift_patterns,
                    open_risks=open_risks,
                    open_actions=open_actions,
                    contradictions=contradictions,
                    programs_root=programs_root,
                )
            except (AIClientError, RuntimeError, SynthesizerError) as error:
                last_error = error
                continue

        if last_error is not None:
            raise SynthesizerError(str(last_error)) from last_error
        raise SynthesizerError("No synthesis-capable Azure OpenAI deployment is configured for this program.")


def _ai_client_supports_parameter(parameter_name: str) -> bool:
    try:
        parameters = inspect.signature(AIClient).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == parameter_name
        for parameter in parameters
    )


def synthesize_command(
    workstream: str = typer.Option(..., "--workstream", help="Workstream id to synthesize."),
    program: str | None = typer.Option(None, "--program", help="Program id override."),
    edition: str | None = typer.Option(None, "--edition", help="Edition id used to resolve the program."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias for supersession records."),
    window_days: int = typer.Option(SYNTHESIS_SIGNAL_WINDOW_DAYS, "--window-days", min=7, help="Approved-signal lookback window."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    try:
        result = synthesize_workstream(
            workstream_id=workstream,
            program_id=program,
            edition_id=edition,
            reviewer=reviewer,
            window_days=window_days,
        )
    except SynthesisDisabledError as error:
        typer.echo(str(error))
        raise click.exceptions.Exit(code=0)
    except SynthesizerError as error:
        raise typer.BadParameter(str(error)) from error

    if format == "human":
        typer.echo(f"Synthesis proposal stored for {result.program_id}/{result.workstream_id}.")
        typer.echo(f"Proposal id:      {result.proposal.id}")
        typer.echo(f"Proposed risk:    {result.proposal.synthesis.proposed_risk.value}")
        typer.echo(f"Confidence:       {result.proposal.synthesis.confidence.value}")
        typer.echo(f"Prompt version:   {result.prompt_version}")
        typer.echo(f"Signals analyzed: {result.signal_count}")
        typer.echo(f"Drift patterns:   {result.drift_pattern_count}")
        typer.echo(f"Active risks:     {result.risk_count}")
        typer.echo(f"Active actions:   {result.action_count}")
        typer.echo(f"Superseded:       {result.superseded_count}")
        typer.echo(f"Stored at:        {result.proposal_path}")
        if result.invalid_evidence_refs:
            typer.echo(f"Warning: dropped invalid evidence refs: {', '.join(result.invalid_evidence_refs)}")
        if result.flagged_for_review:
            typer.echo("Warning: more than half of the AI evidence refs were invalid; confidence was downgraded to low.")
    else:
        typer.echo(render_synthesize_output(result, format=format), nl=False)
    raise typer.Exit(code=0)


def render_synthesize_output(result: SynthesizeResult, *, format: str) -> str:
    payload = {
        "program_id": result.program_id,
        "workstream_id": result.workstream_id,
        "proposal_id": result.proposal.id,
        "proposed_risk": result.proposal.synthesis.proposed_risk.value,
        "confidence": result.proposal.synthesis.confidence.value,
        "prompt_version": result.prompt_version,
        "signal_count": result.signal_count,
        "drift_pattern_count": result.drift_pattern_count,
        "risk_count": result.risk_count,
        "action_count": result.action_count,
        "superseded_count": result.superseded_count,
        "proposal_path": str(result.proposal_path),
        "invalid_evidence_refs": list(result.invalid_evidence_refs),
        "flagged_for_review": result.flagged_for_review,
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            (
                "program_id",
                "workstream_id",
                "proposal_id",
                "proposed_risk",
                "confidence",
                "prompt_version",
                "signal_count",
                "drift_pattern_count",
                "risk_count",
                "action_count",
                "superseded_count",
                "proposal_path",
                "invalid_evidence_refs",
                "flagged_for_review",
            )
        )
        writer.writerow(
            (
                payload["program_id"],
                payload["workstream_id"],
                payload["proposal_id"],
                payload["proposed_risk"],
                payload["confidence"],
                payload["prompt_version"],
                payload["signal_count"],
                payload["drift_pattern_count"],
                payload["risk_count"],
                payload["action_count"],
                payload["superseded_count"],
                payload["proposal_path"],
                ";".join(payload["invalid_evidence_refs"]),  # type: ignore[arg-type]
                payload["flagged_for_review"],
            )
        )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def synthesize_workstream(
    *,
    workstream_id: str,
    program_id: str | None = None,
    edition_id: str | None = None,
    reviewer: str | None = None,
    window_days: int = SYNTHESIS_SIGNAL_WINDOW_DAYS,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    synthesizer_builder: SynthesizerBuilder | None = None,
    now_provider: NowProvider | None = None,
) -> SynthesizeResult:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    resolved_editions_root = editions_root or EDITIONS_ROOT
    current_time = (now_provider or _now_utc)()
    if get_ai_mode() == AIMode.DISABLED:
        raise SynthesisDisabledError(
            f"AI synthesis is disabled by --no-ai / AIMode.DISABLED; no proposal generated for workstream '{workstream_id}'."
        )
    resolved_program_id = _resolve_program_id(
        requested_program_id=program_id,
        edition_id=edition_id,
        workstream_id=workstream_id,
        programs_root=resolved_programs_root,
        editions_root=resolved_editions_root,
    )
    proposal_context = _resolve_proposal_context(
        edition_id=edition_id,
        programs_root=resolved_programs_root,
        editions_root=resolved_editions_root,
    )
    program, workstream = _load_program_context(resolved_program_id, workstream_id=workstream_id, programs_root=resolved_programs_root)

    approved_signals = _load_approved_signals(
        resolved_program_id,
        current_time=current_time,
        window_days=window_days,
        programs_root=resolved_programs_root,
    )
    knowledge = load_program_knowledge(resolved_program_id, programs_root=resolved_programs_root)
    workstream_signals = sort_signals_for_ai_context(
        tuple(signal for signal in approved_signals if workstream.id in signal.workstream_ids),
        people_directory=knowledge.people_directory,
        as_of=current_time,
        source_confidence_order=program.source_confidence_order,
    )
    if not workstream_signals:
        raise SynthesizerError(f"No approved signals found for workstream '{workstream.id}' in program '{resolved_program_id}'.")

    trajectories = _load_workstream_trajectories(
        resolved_program_id,
        signals=workstream_signals,
        current_time=current_time,
        programs_root=resolved_programs_root,
        window_days=window_days,
    )
    drift_patterns = analyze_trajectories(trajectories, window_days=window_days, as_of=current_time.date())
    work_item_ids = tuple(sorted(trajectories))
    open_risks = _load_open_risks(
        resolved_program_id,
        workstream_id=workstream.id,
        work_item_ids=work_item_ids,
        programs_root=resolved_programs_root,
    )
    open_actions = _load_open_actions(
        resolved_program_id,
        workstream_id=workstream.id,
        work_item_ids=work_item_ids,
        programs_root=resolved_programs_root,
    )
    contradictions = _load_workstream_contradictions(
        resolved_program_id,
        work_item_ids=work_item_ids,
        programs_root=resolved_programs_root,
    )

    if synthesizer_builder is None:
        synthesizer = _build_default_synthesizer(
            program=program,
            trace_context=_build_synthesis_trace_context(
                program_id=resolved_program_id,
                edition_id=(proposal_context.edition_id if proposal_context is not None else None),
                workstream_id=workstream.id,
                current_time=current_time,
                budget_usd=program.ai.budget_usd_per_run if program.ai is not None else 0.0,
            ),
        )
    else:
        synthesizer = synthesizer_builder(program)
    draft = synthesizer.generate(
        program=program,
        workstream=workstream,
        signals=workstream_signals,
        drift_patterns=drift_patterns,
        open_risks=open_risks,
        open_actions=open_actions,
        contradictions=contradictions,
        programs_root=resolved_programs_root,
    )
    if draft is None:
        raise SynthesizerError(f"No synthesis draft was produced for workstream '{workstream.id}'.")

    grounding = validate_synthesis_grounding(draft.synthesis, signals=workstream_signals)
    resolved_reviewer = _default_reviewer_identity(reviewer)
    # D-30: garbage-collect stale pending proposals (TTL 14d) on each synthesis
    # run so the central proposal store cannot accumulate unboundedly.
    expire_stale_ai_proposals(
        resolved_program_id,
        resolved_at=current_time,
        programs_root=resolved_programs_root,
    )
    superseded = supersede_pending_ai_proposals(
        resolved_program_id,
        workstream_id=workstream.id,
        resolved_by=resolved_reviewer,
        resolved_at=current_time,
        programs_root=resolved_programs_root,
    )
    proposal_created_at = _next_unique_proposal_timestamp(
        resolved_program_id,
        workstream_id=workstream.id,
        candidate=current_time,
        programs_root=resolved_programs_root,
    )
    proposal = AIProposal(
        id=build_ai_proposal_id(resolved_program_id, workstream_id=workstream.id, created_at=proposal_created_at),
        workstream_id=workstream.id,
        synthesis=grounding.synthesis,
        status=AIProposalStatus.PENDING,
        created_at=proposal_created_at,
        resolved_at=None,
        resolved_by=None,
        edition_id=(proposal_context.edition_id if proposal_context is not None else None),
        issue_number=(proposal_context.issue_number if proposal_context is not None else None),
    )
    proposal_path = append_ai_proposal(resolved_program_id, proposal, programs_root=resolved_programs_root)
    return SynthesizeResult(
        program_id=resolved_program_id,
        workstream_id=workstream.id,
        proposal=proposal,
        proposal_path=proposal_path,
        prompt_version=draft.prompt_version,
        superseded_count=len(superseded),
        invalid_evidence_refs=grounding.invalid_evidence_refs,
        flagged_for_review=grounding.flagged_for_review,
        signal_count=len(workstream_signals),
        drift_pattern_count=len(drift_patterns),
        risk_count=len(open_risks),
        action_count=len(open_actions),
    )


def _resolve_program_id(
    *,
    requested_program_id: str | None,
    edition_id: str | None,
    workstream_id: str,
    programs_root: Path,
    editions_root: Path,
) -> str:
    if requested_program_id is not None and requested_program_id.strip():
        return requested_program_id.strip()
    if edition_id is not None and edition_id.strip():
        resolved = resolve_edition(edition_id.strip(), editions_root=editions_root, programs_root=programs_root)
        if resolved is None:
            raise SynthesizerError(f"Unknown edition '{edition_id}'.")
        return resolved.paths.program_id

    matches: list[str] = []
    for program_dir in sorted(path for path in programs_root.iterdir() if path.is_dir()):
        workstreams = load_current_workstreams(program_dir.name, programs_root=programs_root)
        if any(workstream.id == workstream_id for workstream in workstreams):
            matches.append(program_dir.name)
    if not matches:
        raise SynthesizerError(f"Could not resolve a program for workstream '{workstream_id}'.")
    if len(matches) > 1:
        raise SynthesizerError(
            f"Workstream '{workstream_id}' exists in multiple programs ({', '.join(matches)}). Provide --program or --edition."
        )
    return matches[0]


@dataclass(frozen=True, slots=True)
class _ProposalContext:
    edition_id: str
    issue_number: int


def _resolve_proposal_context(
    *,
    edition_id: str | None,
    programs_root: Path,
    editions_root: Path,
) -> _ProposalContext | None:
    if edition_id is None or not edition_id.strip():
        return None
    resolved = resolve_edition(
        edition_id.strip(),
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return None

    latest_manifest_issue = _load_latest_manifest_issue_number(resolved.paths.publications_dir)
    if latest_manifest_issue is not None:
        return _ProposalContext(edition_id=resolved.paths.edition_id, issue_number=latest_manifest_issue)

    latest_confirmed = find_latest_confirmed_entry(read_archive_index(resolved.paths.edition_id, archive_root=resolved.paths.archive_dir.parent.parent))
    if latest_confirmed is None:
        return _ProposalContext(edition_id=resolved.paths.edition_id, issue_number=1)
    return _ProposalContext(edition_id=resolved.paths.edition_id, issue_number=latest_confirmed.issue_number + 1)


def _load_latest_manifest_issue_number(output_dir: Path) -> int | None:
    latest_issue_number: int | None = None
    for path in output_dir.glob("issue_*/issue_*.manifest.json"):
        match = re.fullmatch(r"issue_(\d{3})\.manifest\.json", path.name)
        if match is None:
            continue
        issue_number = int(match.group(1))
        if latest_issue_number is None or issue_number > latest_issue_number:
            latest_issue_number = issue_number
    return latest_issue_number


def _load_program_context(
    program_id: str,
    *,
    workstream_id: str,
    programs_root: Path,
) -> tuple[Program, Workstream]:
    program_dir = programs_root / program_id
    if not program_dir.exists():
        raise SynthesizerError(f"Unknown program '{program_id}'.")
    program = _parse_program(load_yaml_mapping(program_dir / "program.yaml"), program_dir / "program.yaml")
    workstreams = tuple(load_current_workstreams(program_id, programs_root=programs_root))
    matching = tuple(workstream for workstream in workstreams if workstream.id == workstream_id)
    if not matching:
        raise SynthesizerError(f"Unknown workstream '{workstream_id}' in program '{program_id}'.")
    return program, matching[0]


def _load_approved_signals(
    program_id: str,
    *,
    current_time: datetime,
    window_days: int,
    programs_root: Path,
) -> tuple[Signal, ...]:
    start = current_time - timedelta(days=window_days)
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    return tuple(
        signal
        for signal in signal_store.read(program_id, start=start, end=current_time)
        if signal_is_approved_for_evidence(signal, review_states)
    )


def _load_workstream_trajectories(
    program_id: str,
    *,
    signals: tuple[Signal, ...],
    current_time: datetime,
    programs_root: Path,
    window_days: int,
) -> dict[int, tuple]:
    start_date = current_time.date() - timedelta(days=window_days)
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    trajectories = {}
    for work_item_id in _signal_work_item_ids(signals):
        history = trajectory_store.read(
            program_id,
            work_item_id,
            start=start_date,
            end=current_time.date(),
        )
        if history:
            trajectories[work_item_id] = history
    return trajectories


def _load_open_risks(
    program_id: str,
    *,
    workstream_id: str,
    work_item_ids: tuple[int, ...],
    programs_root: Path,
):
    work_item_id_set = set(work_item_ids)
    risk_entries = project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
        )
    )
    return tuple(
        risk
        for risk in risk_entries
        if risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
        and (
            workstream_id in risk.linked_workstream_ids
            or bool(work_item_id_set.intersection(risk.linked_work_item_ids))
        )
    )


def _load_open_actions(
    program_id: str,
    *,
    workstream_id: str,
    work_item_ids: tuple[int, ...],
    programs_root: Path,
):
    work_item_id_set = set(work_item_ids)
    action_items = project_action_items(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("action.item",),
        )
    )
    return tuple(
        action
        for action in action_items
        if action.status not in {ActionStatus.DONE, ActionStatus.CANCELLED}
        and (
            action.workstream_id == workstream_id
            or bool(work_item_id_set.intersection(action.linked_work_item_ids))
        )
    )


def _load_workstream_contradictions(
    program_id: str,
    *,
    work_item_ids: tuple[int, ...],
    programs_root: Path,
) -> tuple[ContradictionPacket, ...]:
    if not work_item_ids:
        return ()
    work_item_id_set = set(work_item_ids)
    return tuple(
        packet
        for packet in load_contradiction_state(program_id, programs_root=programs_root)
        if packet.work_item_id in work_item_id_set
    )


def _signal_work_item_ids(signals: tuple[Signal, ...]) -> tuple[int, ...]:
    work_item_ids: set[int] = set()
    for signal in signals:
        for ref in signal.entity_refs:
            match = _ITEM_REF_RE.search(ref)
            if match is not None:
                work_item_ids.add(int(match.group(1)))
    return tuple(sorted(work_item_ids))


def _build_synthesizer(
    program: Program,
    *,
    trace_context: AITraceContext | None = None,
) -> WorkstreamSynthesizer:
    if program.ai is None:
        raise SynthesizerError("Program AI configuration is missing.")

    deployments = _resolve_synthesis_deployments(program)
    if not deployments:
        raise SynthesizerError(
            "No synthesis-capable Azure OpenAI deployment is configured for this program. "
            "Set VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT; "
            f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE}"
        )

    from typing import cast

    # D-20: bind the trace context to the process-level ContextVar so any
    # nested helper (rate-limit scope, cost-guard construction, trace-file
    # write path) that doesn't take an explicit `trace_context=` arg still
    # picks it up. The explicit kwarg below still wins, so this is
    # behavior-preserving.
    with use_trace_context(trace_context):
        return cast("WorkstreamSynthesizer", _FallbackWorkstreamSynthesizer(
            deployments=deployments,
            temperature=program.ai.temperature or 0.2,
            budget_usd=program.ai.budget_usd_per_run,
            requests_per_minute=program.ai.requests_per_minute,
            trace_context=trace_context,
        ))


def _build_synthesis_trace_context(
    *,
    program_id: str,
    edition_id: str | None,
    workstream_id: str,
    current_time: datetime,
    budget_usd: float,
) -> AITraceContext:
    scope = edition_id or program_id
    return AITraceContext(
        edition=scope,
        run_id=_build_synthesis_trace_run_id(
            scope=scope,
            workstream_id=workstream_id,
            current_time=current_time,
        ),
        caller="src.commands.synthesize.synthesize_workstream",
        metadata={
            "program_id": program_id,
            "edition_id": edition_id,
            "workstream_id": workstream_id,
            "task_type": "workstream_synthesis",
            "run_budget_usd": budget_usd,
        },
    )


def _build_synthesis_trace_run_id(*, scope: str, workstream_id: str, current_time: datetime) -> str:
    return f"{scope}:synthesize:{workstream_id}:{current_time.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _build_default_synthesizer(*, program: Program, trace_context: AITraceContext) -> WorkstreamSynthesizer:
    if get_ai_mode() == AIMode.DISABLED:
        raise SynthesisDisabledError("AI synthesis is disabled by --no-ai / AIMode.DISABLED.")
    if "trace_context" in inspect.signature(_build_synthesizer).parameters:
        return _build_synthesizer(program, trace_context=trace_context)
    return _build_synthesizer(program)


def _resolve_synthesis_deployments(program: Program) -> tuple[str, ...]:
    if program.ai is None:
        return ()
    return resolve_ai_deployments_for_feature(
        feature_name="synthesizer",
        primary_candidates=(program.ai.exec_summary_deployment, program.ai.blurb_deployment),
        backup_candidates=(program.ai.exec_summary_backup_deployment, program.ai.blurb_backup_deployment),
        primary_fallback_envs=("VERTEX_EXEC_DEPLOYMENT", "VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_EXEC_BACKUP_DEPLOYMENT", "VERTEX_AI_BACKUP_DEPLOYMENT"),
    )


def _default_reviewer_identity(reviewer: str | None) -> str:
    if reviewer is not None and reviewer.strip():
        return reviewer.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _next_unique_proposal_timestamp(
    program_id: str,
    *,
    workstream_id: str,
    candidate: datetime,
    programs_root: Path,
) -> datetime:
    existing_ids = {proposal.id for proposal in load_ai_proposals(program_id, programs_root=programs_root)}
    resolved = candidate
    while build_ai_proposal_id(program_id, workstream_id=workstream_id, created_at=resolved) in existing_ids:
        resolved += timedelta(microseconds=1)
    return resolved


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)
