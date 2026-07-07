from __future__ import annotations

from datetime import date
from datetime import datetime, timezone
from pathlib import Path

from src.commands.watch_scan import watch_program_once
from src.core.catchup_scan import WatchSource
from src.core.journal import read_signals
from src.core.models import Revision, RiskLevel, WorkItem
from src.core.models import Confidence
from src.core.models_v2 import CatchupEvent, Signal
from src.core.models_v2 import ADOConfig, Program, Workstream


def test_watch_program_once_caps_changed_items_when_requested(tmp_path: Path) -> None:
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
    fetched_at = datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc)
    items = tuple(
        WorkItem(
            id=1000 + index,
            type="Feature",
            title=f"Ramp checkpoint {index}",
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
                    work_item_id=1000 + index,
                    rev_number=7,
                    changed_by="priya@example.com",
                    changed_by_email="priya@example.com",
                    changed_date=datetime(2026, 5, 19, 17, 0, 30, tzinfo=timezone.utc),
                    fields_changed={"System.State": ("Proposed", "Active")},
                ),
            ),
            comments=(),
            fetched_at=fetched_at,
        )
        for index in range(501)
    )

    result = watch_program_once(
        "acme",
        since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        as_of=fetched_at,
        sources=(WatchSource.ADO,),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: (items, 4),
        program_context=(program, workstreams),
        auto_approve_signals=False,
        max_changed_items=500,
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert result.scanned_items == 500
    assert result.discovered_signals == 500
    assert result.new_signals == 500
    assert result.total_changed_items == 501
    assert len(signals) == 500


def test_watch_program_once_uses_custom_summary_builder(tmp_path: Path) -> None:
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
    fetched_at = datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc)
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
        fetched_at=fetched_at,
    )

    result = watch_program_once(
        "acme",
        since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        as_of=fetched_at,
        sources=(WatchSource.ADO,),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: ((item,), 4),
        program_context=(program, workstreams),
        auto_approve_signals=False,
        summary_builder=lambda signals: tuple(f"classified:{signal.text}" for signal in signals[:1]),
    )

    assert result.new_signal_summaries == ("classified:ADO#1234 state changed from Proposed to Active.",)


def test_watch_program_once_persists_typed_catchup_events_from_event_builder(tmp_path: Path) -> None:
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
    fetched_at = datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc)
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
        fetched_at=fetched_at,
    )

    result = watch_program_once(
        "acme",
        since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        as_of=fetched_at,
        sources=(WatchSource.ADO,),
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: ((item,), 4),
        program_context=(program, workstreams),
        auto_approve_signals=False,
        event_builder=lambda signals: (
            CatchupEvent(
                event_id="evt-1",
                program_id="acme",
                detected_at=fetched_at,
                kind="state_change",
                work_item_id=1234,
                workstream_id="acme",
                summary="State change: ADO#1234 moved from Proposed to Active.",
                severity="info",
                salience_score=0.5,
                confidence=Confidence.HIGH,
                signal_id=signals[0].id,
            ),
        ),
    )

    assert result.catchup_events[0].kind == "state_change"
    assert result.new_signal_summaries == ("State change: ADO#1234 moved from Proposed to Active.",)


def test_watch_program_once_passes_workiq_limits_to_signal_builder(monkeypatch, tmp_path: Path) -> None:
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
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "src.commands.watch_scan.gather._build_workiq_signals",
        lambda **kwargs: (captured.update(kwargs) or ()),
    )

    result = watch_program_once(
        "acme",
        since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        sources=(WatchSource.WORKIQ,),
        programs_root=programs_root,
        full_loader=lambda program, workstreams, as_of, since: ((), 2),
        program_context=(program, workstreams),
        workiq_timeout_seconds=19,
        workiq_total_budget_seconds=55,
    )

    assert result.discovered_signals == 0
    assert captured["timeout_seconds"] == 19
    assert captured["total_budget_seconds"] == 55