from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil

from src.core.journal import append_review_decision, append_signal, append_usage_marker, archive_weekly_journal_files, archive_weekly_journal_files_by_retention, get_program_journal_archive_dir, get_program_journal_dir, load_latest_review_decisions
from src.core.journal import read_review_log, read_signals
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalClass, SignalReviewDecision, SignalUsageMarker


def test_append_signal_and_read_filters_by_date_and_workstream(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = Signal(
        id="sig-001",
        timestamp=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        source="ado/odata",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment item changed state.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"field": "State", "current": "Active"},
    )
    second = Signal(
        id="sig-002",
        timestamp=datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Target date slipped.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"field": "TargetDate", "prior": "2026-05-10", "current": "2026-05-17"},
    )
    third = Signal(
        id="sig-003",
        timestamp=datetime(2026, 5, 9, 11, 15, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="networking",
        entity_refs=("WI:2001",),
        text="Networking follow-up needed.",
        raw_ref="manual:1",
        confidence=Confidence.LOW,
        metadata=None,
    )

    partition_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)

    append_signal(first, programs_root=programs_root, partition_at=partition_at)
    append_signal(second, programs_root=programs_root, partition_at=partition_at)
    append_signal(third, programs_root=programs_root, partition_at=partition_at)

    all_signals = read_signals("acme", programs_root=programs_root)
    filtered_signals = read_signals(
        "acme",
        start=datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc),
        workstream_id="deployment_readiness",
        programs_root=programs_root,
    )

    assert tuple(signal.id for signal in all_signals) == ("sig-001", "sig-002", "sig-003")
    assert tuple(signal.id for signal in filtered_signals) == ("sig-002",)
    assert filtered_signals[0].metadata is not None
    assert filtered_signals[0].metadata["field"] == "TargetDate"
    assert filtered_signals[0].metadata["prior"] == "2026-05-10"
    assert filtered_signals[0].metadata["current"] == "2026-05-17"
    assert filtered_signals[0].metadata["signal_class"] == SignalClass.RISK.value


def test_append_signal_round_trips_gather_run_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stamped = Signal(
        id="sig-101",
        timestamp=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        source="ado/odata",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment item changed state.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"field": "State", "current": "Active"},
        gather_run_id="gather-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    unstamped = Signal(
        id="sig-102",
        timestamp=datetime(2026, 5, 5, 18, 5, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Manual note.",
        raw_ref="manual:1",
        confidence=Confidence.LOW,
        metadata=None,
    )

    append_signal(stamped, programs_root=programs_root)
    append_signal(unstamped, programs_root=programs_root)

    signals = read_signals("acme", programs_root=programs_root)
    by_id = {signal.id: signal for signal in signals}

    assert by_id["sig-101"].gather_run_id == "gather-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert by_id["sig-102"].gather_run_id is None


def test_review_log_round_trips_and_latest_decision_is_last_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    initial_review = SignalReviewDecision(
        signal_id="sig-001",
        decision="approved",
        reviewed_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        reviewed_by="maintainer",
        note=None,
    )
    usage_marker = SignalUsageMarker(
        signal_id="sig-001",
        issue_number=77,
        edition_id="acme_weekly",
        manifest_id="manifest-001",
        used_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
    )
    latest_review = SignalReviewDecision(
        signal_id="sig-001",
        decision="dismissed",
        reviewed_at=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
        reviewed_by="maintainer",
        note="Superseded by a newer ADO revision signal.",
    )

    append_review_decision("acme", initial_review, programs_root=programs_root)
    append_usage_marker("acme", usage_marker, programs_root=programs_root)
    append_review_decision("acme", latest_review, programs_root=programs_root)

    entries = read_review_log("acme", programs_root=programs_root)
    latest = load_latest_review_decisions("acme", programs_root=programs_root)

    assert len(entries) == 3
    assert isinstance(entries[0], SignalReviewDecision)
    assert isinstance(entries[1], SignalUsageMarker)
    assert isinstance(entries[2], SignalReviewDecision)
    assert latest["sig-001"].decision == "dismissed"
    assert latest["sig-001"].note == "Superseded by a newer ADO revision signal."


def test_archive_weekly_journal_files_moves_old_weeks_and_read_signals_still_sees_archived_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    older_timestamp = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    newer_timestamp = datetime(2025, 1, 21, 12, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="sig-old",
            timestamp=older_timestamp,
            source="manual",
            program_id="acme",
            workstream_id="ws",
            entity_refs=("WI:1",),
            text="Older signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=older_timestamp,
    )
    append_signal(
        Signal(
            id="sig-new",
            timestamp=newer_timestamp,
            source="manual",
            program_id="acme",
            workstream_id="ws",
            entity_refs=("WI:2",),
            text="Newer signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=newer_timestamp,
    )

    moved = archive_weekly_journal_files("acme", before_week="2025-W03", programs_root=programs_root)

    active_dir = get_program_journal_dir("acme", programs_root)
    archive_dir = get_program_journal_archive_dir("acme", programs_root)
    all_signals = read_signals("acme", programs_root=programs_root)

    assert len(moved) == 1
    assert moved[0] == archive_dir / "2025-W02.jsonl"
    assert not (active_dir / "2025-W02.jsonl").exists()
    assert (active_dir / "2025-W04.jsonl").exists()
    assert (archive_dir / "2025-W02.jsonl").exists()
    assert tuple(signal.id for signal in all_signals) == ("sig-old", "sig-new")


def test_archive_weekly_journal_files_by_retention_moves_only_fully_expired_weeks(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    mixed_manual_timestamp = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    mixed_ado_timestamp = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)
    eligible_manual_timestamp = datetime(2025, 1, 14, 12, 0, tzinfo=timezone.utc)
    retained_manual_timestamp = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="sig-mixed-manual",
            timestamp=mixed_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id="ws",
            entity_refs=("WI:1",),
            text="Mixed manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=mixed_manual_timestamp,
    )
    append_signal(
        Signal(
            id="sig-mixed-ado",
            timestamp=mixed_ado_timestamp,
            source="ado/revision",
            program_id="acme",
            workstream_id="ws",
            entity_refs=("WI:2",),
            text="Mixed ADO signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=mixed_ado_timestamp,
    )
    append_signal(
        Signal(
            id="sig-eligible-manual",
            timestamp=eligible_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id="ws",
            entity_refs=("WI:3",),
            text="Eligible manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=eligible_manual_timestamp,
    )
    append_signal(
        Signal(
            id="sig-retained-manual",
            timestamp=retained_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id="ws",
            entity_refs=("WI:4",),
            text="Retained manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=retained_manual_timestamp,
    )

    moved = archive_weekly_journal_files_by_retention(
        "acme",
        as_of=datetime(2025, 3, 15, 12, 0, tzinfo=timezone.utc),
        retention_days_by_source={"manual": 30},
        default_retention_days=365,
        programs_root=programs_root,
    )

    active_dir = get_program_journal_dir("acme", programs_root)
    archive_dir = get_program_journal_archive_dir("acme", programs_root)
    all_signals = read_signals("acme", programs_root=programs_root)

    assert moved == (archive_dir / "2025-W03.jsonl",)
    assert (active_dir / "2025-W02.jsonl").exists()
    assert not (active_dir / "2025-W03.jsonl").exists()
    assert (archive_dir / "2025-W03.jsonl").exists()
    assert (active_dir / "2025-W09.jsonl").exists()
    assert tuple(signal.id for signal in all_signals) == (
        "sig-mixed-manual",
        "sig-mixed-ado",
        "sig-eligible-manual",
        "sig-retained-manual",
    )


def test_read_signals_can_exclude_non_committed_gather_runs(tmp_path: Path) -> None:
    from src.core.gather_run_manifest import GatherRunManifest, GatherRunStatus, RequiredScopeStatus, create_staging_manifest

    programs_root = tmp_path / "programs"
    started = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    running = GatherRunManifest("gather-running", GatherRunStatus.RUNNING, "acme", "interactive", "test", 1, started, started, RequiredScopeStatus.FULL)
    create_staging_manifest(running, programs_root=programs_root)
    append_signal(Signal("signal-running", started, "ado", "acme", "ws", ("WI:1",), "uncommitted", None, Confidence.HIGH, gather_run_id=running.run_id), programs_root=programs_root, partition_at=started)
    append_signal(Signal("signal-legacy", started, "manual", "acme", "ws", ("WI:2",), "legacy", None, Confidence.HIGH), programs_root=programs_root, partition_at=started)

    assert tuple(signal.id for signal in read_signals("acme", programs_root=programs_root, require_committed_gather_run=True)) == ("signal-legacy",)
