from __future__ import annotations

from datetime import date
from datetime import datetime, timezone
import json
from pathlib import Path

from src.commands.watch_scan import watch_program_once
from src.core.catchup_runner import get_catchup_usage_log_path, maybe_catchup, render_cached_catchup_banner, render_catchup_banner, run_catchup, should_run_catchup
from src.core.catchup_scan import WatchPollResult, WatchSource
from src.core.catchup_state_store import load_catchup_state
from src.core.journal import read_review_log, read_signals
from src.core.models import Confidence, Revision, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, CatchupEvent, Program, Workstream


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