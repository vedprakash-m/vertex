from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
from io import StringIO
import json
from pathlib import Path
from typing import Callable, Literal

import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.summary_generator import RollingSummaryDraft, SummaryGenerator, SummaryGeneratorError
from src.core.edition_resolver import PROGRAMS_ROOT, _parse_program
from src.core.knowledge_store import load_program_knowledge
from src.core.models_v2 import PersonDirectory, Program, Signal, Workstream
from src.core.program_fact_store import load_program_facts, project_workstreams
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.signal_ranking import sort_signals_for_ai_context
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.summary_store import RollingSummary, get_summary_path, load_summary, save_summary
from src.core.trajectory_analyzer import DriftPattern, analyze_trajectories
from src.core.yaml_utils import load_yaml_mapping


SUMMARY_SIGNAL_WINDOW_DAYS = 90
SUMMARY_STALENESS_THRESHOLD_DAYS = 14

NowProvider = Callable[[], datetime]
SummaryBuilder = Callable[[Program], SummaryGenerator]


@dataclass(frozen=True, slots=True)
class WorkstreamSummaryResult:
    workstream_id: str
    path: Path
    status: Literal["written", "unchanged", "skipped"]
    signal_count: int
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class SummaryArtifacts:
    program_id: str
    results: tuple[WorkstreamSummaryResult, ...]
    warnings: tuple[str, ...]


def summarize_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    reset: bool = typer.Option(False, "--reset", help="Regenerate summaries from raw approved signals instead of incrementally."),
    workstream: str | None = typer.Option(None, "--workstream", help="Limit summary generation to a single workstream id."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    try:
        artifacts = summarize_program(program, reset=reset, target_workstream_id=workstream)
    except SummaryGeneratorError as error:
        raise typer.BadParameter(str(error)) from error

    written = sum(1 for result in artifacts.results if result.status == "written")
    unchanged = sum(1 for result in artifacts.results if result.status == "unchanged")
    skipped = sum(1 for result in artifacts.results if result.status == "skipped")

    if format == "human":
        typer.echo(
            f"Summaries for {artifacts.program_id}: {written} written, {unchanged} unchanged, {skipped} skipped."
        )
        for result in artifacts.results:
            typer.echo(f"- {result.workstream_id}: {result.status} ({result.path})")
        for warning in artifacts.warnings:
            typer.echo(f"Warning: {warning}")
    else:
        typer.echo(
            render_summarize_output(
                artifacts,
                written_count=written,
                unchanged_count=unchanged,
                skipped_count=skipped,
                format=format,
            ),
            nl=False,
        )
    raise typer.Exit(code=0)


def render_summarize_output(
    artifacts: SummaryArtifacts,
    *,
    written_count: int,
    unchanged_count: int,
    skipped_count: int,
    format: str,
) -> str:
    payload = {
        "program_id": artifacts.program_id,
        "written_count": written_count,
        "unchanged_count": unchanged_count,
        "skipped_count": skipped_count,
        "results": [
            {
                "workstream_id": result.workstream_id,
                "path": str(result.path),
                "status": result.status,
                "signal_count": result.signal_count,
                "warning": result.warning,
            }
            for result in artifacts.results
        ],
        "warnings": list(artifacts.warnings),
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            (
                "row_type",
                "program_id",
                "workstream_id",
                "status",
                "signal_count",
                "path",
                "warning",
                "written_count",
                "unchanged_count",
                "skipped_count",
            )
        )
        writer.writerow(
            (
                "summary",
                payload["program_id"],
                "",
                "",
                "",
                "",
                "",
                payload["written_count"],
                payload["unchanged_count"],
                payload["skipped_count"],
            )
        )
        for result in payload["results"]:  # type: ignore[attr-defined]
            writer.writerow(
                (
                    "result",
                    payload["program_id"],
                    result["workstream_id"],
                    result["status"],
                    result["signal_count"],
                    result["path"],
                    result["warning"] or "",
                    "",
                    "",
                    "",
                )
            )
        for warning in payload["warnings"]:  # type: ignore[attr-defined]
            writer.writerow(("warning", payload["program_id"], "", "", "", "", warning, "", "", ""))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def summarize_program(
    program_id: str,
    *,
    reset: bool = False,
    target_workstream_id: str | None = None,
    programs_root: Path | None = None,
    summary_builder: SummaryBuilder | None = None,
    now_provider: NowProvider | None = None,
) -> SummaryArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    current_time = (now_provider or _now_utc)()
    program, workstreams = _load_program_context(program_id, resolved_programs_root)
    knowledge = load_program_knowledge(program_id, programs_root=resolved_programs_root)
    target_workstreams = _select_workstreams(workstreams, target_workstream_id)
    approved_signals = _load_approved_signals(
        program_id,
        current_time=current_time,
        programs_root=resolved_programs_root,
    )
    if summary_builder is None:
        generator = _build_default_summary_generator(
            program=program,
            trace_context=_build_summary_trace_context(
                program_id=program_id,
                current_time=current_time,
                target_workstream_id=target_workstream_id,
                reset=reset,
                budget_usd=program.ai.budget_usd_per_run if program.ai is not None else 0.0,
            ),
        )
    else:
        generator = summary_builder(program)

    results: list[WorkstreamSummaryResult] = []
    warnings: list[str] = []
    for workstream in target_workstreams:
        prior_summary = load_summary(program_id, workstream.id, programs_root=resolved_programs_root)
        result = _summarize_workstream(
            program_id=program_id,
            program=program,
            workstream=workstream,
            current_time=current_time,
            approved_signals=approved_signals,
            prior_summary=prior_summary,
            reset=reset,
            people_directory=knowledge.people_directory,
            programs_root=resolved_programs_root,
            generator=generator,
        )
        results.append(result)
        if result.warning is not None:
            warnings.append(result.warning)

    return SummaryArtifacts(program_id=program_id, results=tuple(results), warnings=tuple(warnings))


def _summarize_workstream(
    *,
    program_id: str,
    program: Program,
    workstream: Workstream,
    current_time: datetime,
    approved_signals: tuple[Signal, ...],
    prior_summary: RollingSummary | None,
    reset: bool,
    people_directory: tuple[PersonDirectory, ...],
    programs_root: Path,
    generator: SummaryGenerator,
) -> WorkstreamSummaryResult:
    path = get_summary_path(program_id, workstream.id, programs_root)
    workstream_signals = tuple(signal for signal in approved_signals if workstream.id in signal.workstream_ids)

    if reset:
        candidate_signals = workstream_signals
        prior_text = None
        source_mode = "reset"
    elif prior_summary is not None:
        candidate_signals = tuple(signal for signal in workstream_signals if signal.timestamp > prior_summary.generated_at)
        if not candidate_signals:
            warning = _stale_summary_warning(workstream=workstream, current_time=current_time, prior_summary=prior_summary)
            return WorkstreamSummaryResult(
                workstream_id=workstream.id,
                path=path,
                status="unchanged",
                signal_count=0,
                warning=warning,
            )
        prior_text = prior_summary.text
        source_mode = "incremental"
    else:
        candidate_signals = workstream_signals
        prior_text = None
        source_mode = "reset"

    candidate_signals = sort_signals_for_ai_context(
        candidate_signals,
        people_directory=people_directory,
        as_of=current_time,
        source_confidence_order=program.source_confidence_order,
    )

    if not candidate_signals and not reset:
        warning = f"No approved signals available for workstream {workstream.id}; summary not written."
        return WorkstreamSummaryResult(
            workstream_id=workstream.id,
            path=path,
            status="skipped",
            signal_count=0,
            warning=warning,
        )

    if not candidate_signals and reset:
        summary_text = _reset_placeholder_summary(workstream)
        save_summary(
            program_id,
            RollingSummary(
                workstream_id=workstream.id,
                generated_at=current_time,
                prompt_version=None,
                source_mode=source_mode,
                signal_count=0,
                text=summary_text,
            ),
            programs_root=programs_root,
        )
        return WorkstreamSummaryResult(
            workstream_id=workstream.id,
            path=path,
            status="written",
            signal_count=0,
            warning=f"Reset summary for {workstream.id} with no approved signals in the last {SUMMARY_SIGNAL_WINDOW_DAYS} days.",
        )

    drift_patterns = _load_drift_patterns(
        program_id=program_id,
        signals=candidate_signals,
        current_time=current_time,
        programs_root=programs_root,
    )
    draft = generator.generate(
        program=program,
        workstream=workstream,
        prior_summary=prior_text,
        signals=candidate_signals,
        drift_patterns=drift_patterns,
        programs_root=programs_root,
    )
    if draft is None:
        warning = f"Summary generation returned no content for workstream {workstream.id}."
        return WorkstreamSummaryResult(
            workstream_id=workstream.id,
            path=path,
            status="skipped",
            signal_count=len(candidate_signals),
            warning=warning,
        )

    save_summary(
        program_id,
        RollingSummary(
            workstream_id=workstream.id,
            generated_at=current_time,
            prompt_version=draft.prompt_version,
            source_mode=source_mode,
            signal_count=len(candidate_signals),
            text=draft.text,
        ),
        programs_root=programs_root,
    )
    return WorkstreamSummaryResult(
        workstream_id=workstream.id,
        path=path,
        status="written",
        signal_count=len(candidate_signals),
    )


def _load_program_context(program_id: str, programs_root: Path) -> tuple[Program, tuple[Workstream, ...]]:
    program_dir = programs_root / program_id
    if not program_dir.exists():
        raise typer.BadParameter(f"Unknown program '{program_id}'.")
    raw_program = load_yaml_mapping(program_dir / "program.yaml")
    workstream_snapshot = load_program_facts(
        program_id,
        programs_root=programs_root,
        fact_types=("workstream.entry",),
    )
    return (
        _parse_program(raw_program, program_dir / "program.yaml"),
        project_workstreams(workstream_snapshot),
    )


def _select_workstreams(
    workstreams: tuple[Workstream, ...],
    target_workstream_id: str | None,
) -> tuple[Workstream, ...]:
    if target_workstream_id is None:
        return workstreams
    selected = tuple(workstream for workstream in workstreams if workstream.id == target_workstream_id)
    if not selected:
        raise typer.BadParameter(f"Unknown workstream '{target_workstream_id}'.")
    return selected


def _load_approved_signals(
    program_id: str,
    *,
    current_time: datetime,
    programs_root: Path,
) -> tuple[Signal, ...]:
    start = current_time - timedelta(days=SUMMARY_SIGNAL_WINDOW_DAYS)
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    signals = signal_store.read(program_id, start=start, end=current_time)
    review_states = signal_store.read_reviews(program_id)
    return tuple(
        signal
        for signal in signals
        if signal_is_approved_for_evidence(signal, review_states)
    )


def _load_drift_patterns(
    *,
    program_id: str,
    signals: tuple[Signal, ...],
    current_time: datetime,
    programs_root: Path,
) -> tuple[DriftPattern, ...]:
    work_item_ids = sorted(_work_item_ids_from_signals(signals))
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    trajectories = {
        work_item_id: trajectory_store.read(program_id, work_item_id)
        for work_item_id in work_item_ids
    }
    populated = {
        work_item_id: points
        for work_item_id, points in trajectories.items()
        if points
    }
    if not populated:
        return ()
    return analyze_trajectories(populated, as_of=current_time.date())


def _work_item_ids_from_signals(signals: tuple[Signal, ...]) -> set[int]:
    work_item_ids: set[int] = set()
    for signal in signals:
        for entry in signal.entity_refs:
            if not entry.startswith("WI:"):
                continue
            raw_id = entry.split(":", 1)[1].strip()
            if raw_id.isdigit():
                work_item_ids.add(int(raw_id))
    return work_item_ids


def _stale_summary_warning(
    *,
    workstream: Workstream,
    current_time: datetime,
    prior_summary: RollingSummary,
) -> str | None:
    age_days = max(0, (current_time - prior_summary.generated_at).days)
    if age_days <= SUMMARY_STALENESS_THRESHOLD_DAYS:
        return None
    return (
        f"Summary for {workstream.id} is {age_days} days old and no new approved signals were available."
    )


def _reset_placeholder_summary(workstream: Workstream) -> str:
    return "\n".join(
        [
            "## Current State",
            f"No approved signals were available recently for {workstream.name}.",
            "",
            "## New Since Last Summary",
            "No new reviewed evidence is available yet.",
            "",
            "## Risks And Watchouts",
            "Treat this workstream as lacking fresh reviewed context until new approved signals arrive.",
        ]
    )


def _build_summary_generator(
    program: Program,
    *,
    trace_context: AITraceContext | None = None,
) -> SummaryGenerator:
    # D-20: bind the trace context to the process-level ContextVar so any
    # nested helper that doesn't take an explicit `trace_context=` arg
    # (rate-limit scope, cost-guard construction, trace-file write path)
    # still picks it up. The explicit kwarg below still wins, so this is
    # behavior-preserving.
    with use_trace_context(trace_context):
        return SummaryGenerator.from_program(program, trace_context=trace_context)


def _build_summary_trace_context(
    *,
    program_id: str,
    current_time: datetime,
    target_workstream_id: str | None,
    reset: bool,
    budget_usd: float,
) -> AITraceContext:
    return AITraceContext(
        edition=program_id,
        run_id=_build_summary_trace_run_id(
            program_id=program_id,
            target_workstream_id=target_workstream_id,
            current_time=current_time,
        ),
        caller="src.commands.summarize.summarize_program",
        metadata={
            "program_id": program_id,
            "task_type": "rolling_summary",
            "target_workstream_id": target_workstream_id,
            "reset": reset,
            "run_budget_usd": budget_usd,
        },
    )


def _build_summary_trace_run_id(
    *,
    program_id: str,
    target_workstream_id: str | None,
    current_time: datetime,
) -> str:
    scope = target_workstream_id or "all"
    return f"{program_id}:summarize:{scope}:{current_time.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _build_default_summary_generator(*, program: Program, trace_context: AITraceContext) -> SummaryGenerator:
    if get_ai_mode() == AIMode.DISABLED:
        return SummaryGenerator(client=None)
    if "trace_context" in inspect.signature(_build_summary_generator).parameters:
        return _build_summary_generator(program, trace_context=trace_context)
    return _build_summary_generator(program)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)