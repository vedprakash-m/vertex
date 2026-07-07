from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_pipeline.support import build_captured_window
from src.core.models_v2 import IntegrationError, Signal
from src.core.source_models import IngestionRun, SourceKind


class IngestionRunRecorder(Protocol):
    def initialize(self) -> object: ...

    def record_ingestion_run(self, run: IngestionRun) -> None: ...


def build_signal_source_ingestion_run_id(program_id: str, source_ref: str, as_of: datetime) -> str:
    return str(uuid5(NAMESPACE_URL, f"{program_id}|signal_source|{source_ref}|{as_of.isoformat()}"))


def build_signal_ingestion_captured_window(signals: tuple[Signal, ...], source_names: tuple[str, ...]) -> str | None:
    matching_timestamps = sorted(
        signal.timestamp.astimezone(timezone.utc)
        for signal in signals
        if signal.source in source_names
    )
    if not matching_timestamps:
        return None
    return build_captured_window(matching_timestamps[0], matching_timestamps[-1])


def record_optional_source_ingestion_runs(
    program_id: str,
    *,
    as_of: datetime,
    include_workiq: bool,
    include_analytics: bool,
    include_sprints: bool,
    include_pipelines: bool,
    include_icm: bool,
    signals: tuple[Signal, ...],
    integration_error_details: tuple[IntegrationError, ...],
    store: IngestionRunRecorder,
) -> None:
    source_specs: list[tuple[str, tuple[str, ...]]] = [
        ("ado/revision", ("ado/revision",)),
        ("ado/comment", ("ado/comment",)),
        ("vertex/freshness", ("vertex/freshness",)),
        ("ado/dependency", ("ado/dependency",)),
    ]
    if include_workiq:
        source_specs.append(("workiq", ("workiq/email", "workiq/teams", "workiq/transcript")))
    if include_analytics:
        source_specs.append(("ado/analytics", ("ado/analytics", "ado/wiql")))
    if include_sprints:
        source_specs.append(("ado/sprint", ("ado/sprint",)))
    if include_pipelines:
        source_specs.append(("ado/pipeline", ("ado/pipeline",)))
        source_specs.append(("ado/pr", ("ado/pr",)))
    if include_icm:
        source_specs.append(("icm", ("icm",)))
    store.initialize()
    for source_ref, source_names in source_specs:
        error_message = next(
            (
                detail.message
                for detail in integration_error_details
                if detail.source.strip().lower() == source_ref
            ),
            None,
        )
        store.record_ingestion_run(
            IngestionRun(
                id=build_signal_source_ingestion_run_id(program_id, source_ref, as_of),
                program_id=program_id,
                source_kind=SourceKind.SIGNAL.value,
                source_ref=source_ref,
                binding_id=None,
                started_at=as_of,
                heartbeat_at=as_of,
                completed_at=as_of,
                status="failed" if error_message is not None else "success",
                expected_rows=None,
                metrics_observed=0,
                signals_written=sum(1 for signal in signals if signal.source in source_names),
                query_hash=None,
                captured_window=build_signal_ingestion_captured_window(signals, source_names),
                error_message=error_message,
            )
        )
