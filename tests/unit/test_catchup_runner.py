from __future__ import annotations

from dataclasses import replace
from datetime import date
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands import gather
from src.commands.gather_workiq_helpers import WorkIQQueryPlan
from src.commands.watch_scan import watch_program_once
from src.core.catchup_runner import _build_catchup_signal, get_catchup_usage_log_path, maybe_catchup, render_cached_catchup_banner, render_catchup_banner, run_catchup, should_run_catchup
from src.core.catchup_scan import WatchPollResult, WatchSource
from src.core.catchup_state_store import load_catchup_state
from src.core.feedback.catchup_classifier import build_catchup_events
from src.core.journal import read_review_log, read_signals
from src.core.models import Confidence, Revision, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, CatchupEvent, M365Config, Program, ReviewPolicy, Signal, Workstream
from src.core.signal_dedup import dedupe_signals
from src.core.store_factory import build_program_signal_store


def test_run_catchup_persists_state_and_renders_banner(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    def _scan(program_id: str, **_: object) -> WatchPollResult:
        return WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=7,
            discovered_signals=3,
            new_signals=2,
            auto_reviews_written=2,
            trajectory_updates=1,
            ado_calls=4,
            new_signal_summaries=("ADO#1234 target date changed from Jun 15 to Jun 22.",),
            catchup_events=(
                CatchupEvent(
                    event_id="evt-1",
                    program_id=program_id,
                    detected_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
                    kind="eta_slip",
                    work_item_id=1234,
                    workstream_id="deployment",
                    summary="ADO#1234 target date changed from Jun 15 to Jun 22.",
                    severity="warn",
                    salience_score=0.8,
                    confidence=Confidence.HIGH,
                    signal_id="sig-1",
                ),
            ),
        )

    result = run_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    assert result.state_path.exists()
    assert "Catchup" in render_catchup_banner(result)
    assert "ADO#1234 target date changed from Jun 15 to Jun 22." in render_catchup_banner(result)
    assert "2 new signal(s)" in render_cached_catchup_banner("acme", programs_root=programs_root)
    assert "ADO#1234 target date changed from Jun 15 to Jun 22." in render_cached_catchup_banner("acme", programs_root=programs_root)
    assert should_run_catchup(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 19, 18, 10, tzinfo=timezone.utc),
    ) is False
    state = load_catchup_state("acme", programs_root=programs_root)
    assert state is not None
    assert state.last_result is not None
    assert state.last_result.catchup_events[0].kind == "eta_slip"


def test_maybe_catchup_skips_when_cursor_is_fresh(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    def _scan(program_id: str, **_: object) -> WatchPollResult:
        return WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=7,
            discovered_signals=3,
            new_signals=2,
            auto_reviews_written=2,
            trajectory_updates=1,
            ado_calls=4,
            new_signal_summaries=(),
        )

    first = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )
    second = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 5, tzinfo=timezone.utc),
    )

    assert first is not None
    assert second is None


def test_maybe_catchup_honors_program_config_disable_and_interval(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "catchup:",
                "  enabled: false",
                "  catchup_interval_minutes: 120",
            )
        ),
        encoding="utf-8",
    )

    def _disabled_scan(program_id: str, **_: object) -> WatchPollResult:
        raise AssertionError(f"catchup scan should not run for {program_id}")

    skipped = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_disabled_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    assert skipped is None

    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "catchup:",
                "  enabled: true",
                "  catchup_interval_minutes: 120",
            )
        ),
        encoding="utf-8",
    )

    def _scan(program_id: str, **_: object) -> WatchPollResult:
        return WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=1,
            discovered_signals=0,
            new_signals=0,
            auto_reviews_written=0,
            trajectory_updates=0,
            ado_calls=1,
            new_signal_summaries=(),
        )

    first = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )
    second = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 30, tzinfo=timezone.utc),
    )
    third = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 20, 5, tzinfo=timezone.utc),
    )

    assert first is not None
    assert second is None
    assert third is not None


def test_run_catchup_passes_workiq_limits_from_program_config(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "catchup:",
                "  workiq_timeout_seconds: 17",
                "  workiq_total_budget_seconds: 43",
            )
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def _scan(program_id: str, **kwargs: object) -> WatchPollResult:
        captured.update(kwargs)
        return WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=1,
            discovered_signals=0,
            new_signals=0,
            auto_reviews_written=0,
            trajectory_updates=0,
            ado_calls=1,
            new_signal_summaries=(),
        )

    run_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        sources=(WatchSource.WORKIQ,),
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    assert captured["workiq_timeout_seconds"] == 17
    assert captured["workiq_total_budget_seconds"] == 43


def test_run_catchup_leaves_new_signals_pending_review(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=("acme",),
        custom_fields={},
        revisions=(
            Revision(
                work_item_id=1234,
                rev_number=7,
                changed_by="priya@example.com",
                changed_by_email="priya@example.com",
                changed_date=datetime(2026, 5, 19, 17, 0, 30, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            ),
        ),
        comments=(),
        fetched_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    def _scan(program_id: str, **kwargs: object) -> WatchPollResult:
        return watch_program_once(
            program_id,
            loader=lambda program, workstreams, as_of, since: ((item,), 4),
            program_context=(program, workstreams),
            **kwargs,
        )

    result = run_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)

    assert result.result.new_signals == 1
    assert result.result.auto_reviews_written == 0
    assert result.result.new_signal_summaries == ("ADO#1234 state changed from Proposed to Active.",)
    assert len(signals) == 1
    assert signals[0].source == "vertex/catchup"
    assert signals[0].metadata is not None
    assert signals[0].metadata["catchup_origin"] == "ado/revision"
    assert reviews == ()


@pytest.mark.parametrize("include_valid", [False, True])
@pytest.mark.parametrize("use_event_builder", [False, True])
@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
def test_run_catchup_preserves_distinct_workiq_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_valid: bool, use_event_builder: bool,
    storage_backend: str,
) -> None:
    programs_root = tmp_path / "programs"
    as_of = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture", maturity_level=2,
        ado=ADOConfig(
            organization="fixture", project="fixture", area_paths=(), work_item_types=("Feature",),
            excluded_states=(), date_window_days=14, api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
        storage_backend=storage_backend,
    )
    plans = tuple(
        WorkIQQueryPlan(
            query_name=f"feedback_search:private-lane-{index}", question="Synthetic query",
            structured_window_start=date(2026, 10, 20), structured_window_end=date(2026, 10, 24),
            structured_result_limit=8,
        )
        for index in range(2)
    )
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_: plans)
    bridges = []

    class Bridge:
        def __init__(self) -> None:
            records = [
                None,
                {"id": "private-time", "subject": "Private subject", "receivedDateTime": "invalid"},
                {"receivedDateTime": "2026-10-24T09:00:00Z"},
            ]
            if include_valid:
                records.append({
                    "id": "valid-message", "subject": "Synthetic status",
                    "receivedDateTime": "2026-10-24T09:00:00Z",
                })
            self.responses = iter(({"emails": records}, {"response": "Private malformed response"}))
            self.calls = 0
            bridges.append(self)

        def probe(self):
            return SimpleNamespace(available=True, has_workiq=True)

        def ask_workiq(self, *args, **kwargs):
            self.calls += 1
            return next(self.responses)

    def scan(program_id: str, **kwargs: object) -> WatchPollResult:
        return watch_program_once(
            program_id, program_context=(program, ()), full_loader=lambda *args: ((), 0),
            bridge_provider=Bridge, **kwargs,
        )

    event_builder = build_catchup_events if use_event_builder else None
    result = run_catchup(
        "fixture", since_hours=24, sources=(WatchSource.WORKIQ,), programs_root=programs_root,
        scan_func=scan, as_of=as_of, event_builder=event_builder,
    )
    store = build_program_signal_store(program, programs_root=programs_root)
    stored = store.read("fixture")
    diagnostics = tuple(signal for signal in stored if (signal.metadata or {}).get("integration_source") == "workiq")
    expected_count = 4 + int(include_valid)
    assert result.result.discovered_signals == result.result.new_signals == len(stored) == expected_count
    assert len(diagnostics) == 4
    assert len({signal.id for signal in stored}) == expected_count
    assert all(signal.source == "vertex/catchup" and signal.review_policy is ReviewPolicy.PENDING for signal in stored)
    assert all(signal.confidence is Confidence.NONE for signal in diagnostics)
    assert {signal.text for signal in diagnostics} == {
        "WorkIQ: 1 inspected record occurrence(s) quarantined for missing or invalid source time.",
        "WorkIQ: 1 inspected record occurrence(s) quarantined for missing or invalid source identity.",
        "WorkIQ: 1 inspected record occurrence(s) quarantined for malformed or unsafe content.",
        "WorkIQ: structured discovery response unavailable or invalid.",
    }
    assert len(result.result.new_signal_summaries) == 3
    if use_event_builder:
        by_id = {event.signal_id: event for event in result.result.catchup_events}
        assert {by_id[signal.id].summary for signal in diagnostics} == {signal.text for signal in diagnostics}
        assert all(by_id[signal.id].confidence is Confidence.NONE for signal in diagnostics)
    banner = render_catchup_banner(result)
    cached = render_cached_catchup_banner("fixture", programs_root=programs_root)
    assert "WorkIQ:" in banner and "WorkIQ:" in cached
    for private in ("private-time", "private-lane", "Private subject", "Private malformed response"):
        assert private not in repr(diagnostics) + banner + cached
    assert store.read_reviews("fixture") == {}
    assert bridges[0].calls == 2

    repeated = run_catchup(
        "fixture", since_hours=24, sources=(WatchSource.WORKIQ,), programs_root=programs_root,
        scan_func=scan, as_of=as_of + timedelta(minutes=1), event_builder=event_builder,
    )
    assert repeated.result.new_signals == 0
    assert len(store.read("fixture")) == expected_count
    assert bridges[1].calls == 2


def test_run_catchup_hides_unstructured_query_names_from_diagnostic_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    as_of = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture", maturity_level=2,
        ado=ADOConfig(
            organization="fixture", project="fixture", area_paths=(), work_item_types=("Feature",),
            excluded_states=(), date_window_days=14, api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
    )
    monkeypatch.setattr(
        gather, "_build_workiq_query_plans",
        lambda **_: (WorkIQQueryPlan(query_name="feedback_search:private-lane", question="Synthetic query"),),
    )

    class Bridge:
        def probe(self):
            return SimpleNamespace(available=True, has_workiq=True)

        def ask_workiq(self, *args, **kwargs):
            return {"emails": [{"subject": "Private record", "receivedDateTime": "2026-10-24T09:00:00Z"}]}

    def scan(program_id: str, **kwargs: object) -> WatchPollResult:
        return watch_program_once(
            program_id, program_context=(program, ()), full_loader=lambda *args: ((), 0),
            bridge_provider=Bridge, **kwargs,
        )

    result = run_catchup(
        "fixture", since_hours=24, sources=(WatchSource.WORKIQ,), programs_root=programs_root,
        scan_func=scan, as_of=as_of, event_builder=build_catchup_events,
    )
    signal, = read_signals("fixture", programs_root=programs_root)
    assert signal.metadata is not None
    assert "private-lane" in signal.metadata["error"]
    assert signal.text == "WorkIQ: 1 inspected record occurrence(s) quarantined."
    assert result.result.catchup_events[0].summary == signal.text
    for output in (result.result.new_signal_summaries[0], render_catchup_banner(result),
                   render_cached_catchup_banner("fixture", programs_root=programs_root)):
        assert "private-lane" not in output
        assert "Private record" not in output
        assert "ADO#?" not in output


@pytest.mark.parametrize("storage_backend", ["file", "sqlite"])
@pytest.mark.parametrize("mixed", [False, True])
def test_run_catchup_preserves_workiq_budget_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage_backend: str, mixed: bool,
) -> None:
    programs_root = tmp_path / "programs"
    as_of = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture", maturity_level=2,
        ado=ADOConfig(
            organization="fixture", project="fixture", area_paths=(), work_item_types=("Feature",),
            excluded_states=(), date_window_days=14, api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
        storage_backend=storage_backend,
    )
    plans = tuple(
        WorkIQQueryPlan(
            query_name=f"feedback_search:private-lane-{index}", question="Private query",
            structured_window_start=date(2026, 10, 20), structured_window_end=date(2026, 10, 24),
            structured_result_limit=8,
        )
        for index in range(3 if mixed else 2)
    )
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_: plans)
    clock = [0.0]
    monkeypatch.setattr(gather, "monotonic", lambda: clock[0])
    bridges = []

    class Bridge:
        def __init__(self) -> None:
            self.calls = 0
            bridges.append(self)

        def probe(self):
            return SimpleNamespace(available=True, has_workiq=True)

        def ask_workiq(self, *args, **kwargs):
            self.calls += 1
            if not mixed or self.calls == 2:
                clock[0] = 1000.0
            if not mixed:
                return {"emails": []}
            if self.calls == 2:
                return {"response": "Private malformed response"}
            return {"emails": [
                {"id": "valid-message", "subject": "Synthetic status", "receivedDateTime": "2026-10-24T09:00:00Z"},
                {"id": "private-record", "subject": "Private subject", "receivedDateTime": "invalid"},
            ]}

    def scan(program_id: str, **kwargs: object) -> WatchPollResult:
        clock[0] = 0.0
        return watch_program_once(
            program_id, program_context=(program, ()), full_loader=lambda *args: ((), 0),
            bridge_provider=Bridge, **kwargs,
        )

    result = run_catchup(
        "fixture", since_hours=24, sources=(WatchSource.WORKIQ,), programs_root=programs_root,
        scan_func=scan, as_of=as_of, event_builder=build_catchup_events,
    )
    store = build_program_signal_store(program, programs_root=programs_root)
    stored = store.read("fixture")
    diagnostic, = (signal for signal in stored if (signal.metadata or {}).get("workiq_plan_budget_exhausted") is True)
    expected_count = 4 if mixed else 1
    assert result.result.discovered_signals == result.result.new_signals == len(stored) == expected_count
    assert diagnostic.source == "vertex/catchup"
    assert diagnostic.review_policy is ReviewPolicy.PENDING
    assert diagnostic.confidence is Confidence.NONE
    assert diagnostic.text == "WorkIQ: query-plan time budget exhausted; at least one plan was not attempted."
    event = next(event for event in result.result.catchup_events if event.signal_id == diagnostic.id)
    assert event.summary == diagnostic.text
    assert event.work_item_id is None
    assert diagnostic.text in render_catchup_banner(result)
    assert diagnostic.text in render_cached_catchup_banner("fixture", programs_root=programs_root)
    assert store.read_reviews("fixture") == {}
    assert bridges[0].calls == (2 if mixed else 1)
    for private in ("private-lane", "Private query", "private-record", "Private subject", "Private malformed response"):
        assert private not in repr(diagnostic) + render_catchup_banner(result)

    repeated = run_catchup(
        "fixture", since_hours=24, sources=(WatchSource.WORKIQ,), programs_root=programs_root,
        scan_func=scan, as_of=as_of + timedelta(minutes=1), event_builder=build_catchup_events,
    )
    assert repeated.result.new_signals == 0
    assert len(store.read("fixture")) == expected_count


def test_catchup_budget_diagnostic_is_private_and_distinct_from_discovery() -> None:
    signal = Signal(
        id="budget", timestamp=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
        source="system", program_id="fixture", workstream_id=None, entity_refs=(),
        text="Private raw content", raw_ref="system:workiq:synthetic", confidence=Confidence.NONE,
        metadata={
            "integration_source": "workiq", "workiq_plan_budget_exhausted": True,
            "error": "Private untrusted metadata", "field": "TargetDate", "work_item_id": 1234,
        },
    )
    budget = _build_catchup_signal(signal)
    discovery = _build_catchup_signal(replace(
        signal, id="discovery", metadata={
            "integration_source": "workiq", "workiq_structured_response_unusable": True,
            "error": "Private untrusted metadata",
        },
    ))

    assert len(dedupe_signals((budget, discovery))) == 2
    assert budget.text == "WorkIQ: query-plan time budget exhausted; at least one plan was not attempted."
    event = build_catchup_events((budget,))[0]
    assert event.summary == budget.text
    assert event.work_item_id is None
    assert "Private" not in budget.text + event.summary


def test_watch_leaves_workiq_budget_diagnostic_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    as_of = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture", maturity_level=2,
        ado=ADOConfig(
            organization="fixture", project="fixture", area_paths=(), work_item_types=("Feature",),
            excluded_states=(), date_window_days=14, api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
    )
    plans = tuple(WorkIQQueryPlan(query_name=f"feedback_search:{i}", question="Synthetic query") for i in range(2))
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_: plans)
    clock = [0.0]
    monkeypatch.setattr(gather, "monotonic", lambda: clock[0])

    class Bridge:
        def probe(self):
            return SimpleNamespace(available=True, has_workiq=True)

        def ask_workiq(self, *args, **kwargs):
            clock[0] = 5.0
            return {"emails": []}

    result = watch_program_once(
        "fixture", since=as_of - timedelta(hours=24), as_of=as_of, sources=(WatchSource.WORKIQ,),
        programs_root=programs_root, program_context=(program, ()), full_loader=lambda *args: ((), 0),
        bridge_provider=Bridge, auto_approve_signals=True, workiq_total_budget_seconds=5,
    )
    signal, = read_signals("fixture", programs_root=programs_root)
    assert result.discovered_signals == result.new_signals == 1
    assert result.auto_reviews_written == 0
    assert signal.source == "system"
    assert (signal.metadata or {}).get("workiq_plan_budget_exhausted") is True
    assert read_review_log("fixture", programs_root=programs_root) == ()


def test_run_catchup_logs_truncation_and_renders_banner_note(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    def _scan(program_id: str, **_: object) -> WatchPollResult:
        return WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=500,
            discovered_signals=500,
            new_signals=3,
            auto_reviews_written=0,
            trajectory_updates=0,
            ado_calls=4,
            new_signal_summaries=("ADO#1234 target date changed from Jun 15 to Jun 22.",),
            total_changed_items=650,
        )

    result = run_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    log_path = get_catchup_usage_log_path("acme", programs_root=programs_root)
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    banner = render_catchup_banner(result)
    cached_banner = render_cached_catchup_banner("acme", programs_root=programs_root)

    assert entries[-1]["event"] == "catchup_truncated"
    assert entries[-1]["processed_changes"] == 500
    assert entries[-1]["total_returned"] == 650
    assert "Truncated: 500 of 650 changes" in banner
    assert "run 'vertex gather' for full refresh" in banner
    assert "Truncated: 500 of 650 changes" in cached_banner


def test_maybe_catchup_logs_failure_and_does_not_raise(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    def _scan(program_id: str, **_: object) -> WatchPollResult:
        raise RuntimeError("simulated catchup failure")

    result = maybe_catchup(
        "acme",
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    log_path = get_catchup_usage_log_path("acme", programs_root=programs_root)
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert result is None
    assert entries[-1]["event"] == "catchup_failed"
    assert entries[-1]["reason"] == "simulated catchup failure"


def test_run_catchup_passes_selected_sources_and_persists_source_label(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    captured_sources: list[tuple[WatchSource, ...]] = []

    def _scan(program_id: str, **kwargs: object) -> WatchPollResult:
        captured_sources.append(kwargs["sources"])
        return WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=7,
            discovered_signals=0,
            new_signals=0,
            auto_reviews_written=0,
            trajectory_updates=0,
            ado_calls=4,
            new_signal_summaries=(),
        )

    run_catchup(
        "acme",
        sources=(WatchSource.ADO, WatchSource.WORKIQ),
        programs_root=programs_root,
        scan_func=_scan,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
    )

    state = load_catchup_state("acme", programs_root=programs_root)

    assert captured_sources == [(WatchSource.ADO, WatchSource.WORKIQ)]
    assert state is not None
    assert state.last_catchup_source == "ado+workiq"