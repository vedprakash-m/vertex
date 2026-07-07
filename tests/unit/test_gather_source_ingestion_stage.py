"""Direct coverage for the extracted source-ingestion stage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.commands.gather_pipeline import source_ingestion_stage
from src.core.models import Confidence
from src.core.models_v2 import IntegrationError, Signal
from src.core.source_models import IngestionRun


@dataclass
class _FakeStore:
    initialized: bool = False
    runs: list[IngestionRun] | None = None

    def __post_init__(self) -> None:
        if self.runs is None:
            self.runs = []

    def initialize(self) -> None:
        self.initialized = True

    def record_ingestion_run(self, run: IngestionRun) -> None:
        assert self.runs is not None
        self.runs.append(run)


def _signal(source: str, timestamp: datetime) -> Signal:
    return Signal(
        id=f"{source}-{timestamp.isoformat()}",
        timestamp=timestamp,
        source=source,
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1",),
        text=source,
        raw_ref=source,
        confidence=Confidence.HIGH,
    )


def test_build_signal_ingestion_captured_window_uses_utc_bounds() -> None:
    window = source_ingestion_stage.build_signal_ingestion_captured_window(
        (
            _signal("workiq/email", datetime(2026, 5, 10, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
            _signal("workiq/email", datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)),
        ),
        ("workiq/email",),
    )

    assert window == "2026-05-10T04:30:00+00:00/2026-05-10T08:00:00+00:00"


def test_record_optional_source_ingestion_runs_marks_matching_workiq_error_failed() -> None:
    store = _FakeStore()
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    source_ingestion_stage.record_optional_source_ingestion_runs(
        "acme",
        as_of=as_of,
        include_workiq=True,
        include_analytics=False,
        include_sprints=False,
        include_pipelines=False,
        include_icm=False,
        signals=(_signal("workiq/email", datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)),),
        integration_error_details=(
            IntegrationError(source="workiq", stage="discovery", retryable=False, message="WorkIQ unavailable"),
        ),
        store=store,
    )

    assert store.initialized is True
    assert store.runs is not None
    workiq_run = next(run for run in store.runs if run.source_ref == "workiq")
    assert workiq_run.status == "failed"
    assert workiq_run.error_message == "WorkIQ unavailable"


def test_record_optional_source_ingestion_runs_preserves_analytics_error_mismatch_behavior() -> None:
    store = _FakeStore()

    source_ingestion_stage.record_optional_source_ingestion_runs(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        include_workiq=False,
        include_analytics=True,
        include_sprints=False,
        include_pipelines=False,
        include_icm=False,
        signals=(_signal("ado/analytics", datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)),),
        integration_error_details=(
            IntegrationError(source="analytics", stage="discovery", retryable=False, message="Analytics unavailable"),
        ),
        store=store,
    )

    assert store.runs is not None
    analytics_run = next(run for run in store.runs if run.source_ref == "ado/analytics")
    assert analytics_run.status == "success"
    assert analytics_run.error_message is None


def test_record_optional_source_ingestion_runs_adds_pipeline_and_pr_entries() -> None:
    store = _FakeStore()

    source_ingestion_stage.record_optional_source_ingestion_runs(
        "acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        include_workiq=False,
        include_analytics=False,
        include_sprints=False,
        include_pipelines=True,
        include_icm=False,
        signals=(
            _signal("ado/pipeline", datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)),
            _signal("ado/pr", datetime(2026, 5, 10, 7, 5, tzinfo=timezone.utc)),
        ),
        integration_error_details=(),
        store=store,
    )

    assert store.runs is not None
    assert [run.source_ref for run in store.runs] == [
        "ado/revision",
        "ado/comment",
        "vertex/freshness",
        "ado/dependency",
        "ado/pipeline",
        "ado/pr",
    ]
