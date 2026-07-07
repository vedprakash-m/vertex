"""
Unit tests for src/core/plane1_changelog.py — §22 E1 context versioning.

Zone A only. Tests use tmp_path for all filesystem operations.
Covers: append_plane1_changes, load_plane1_changes (with/without since filter),
        write/load_plane1_last_seen, Plane1ChangeRecord round-trip serialization,
        and append-only guarantee (no overwrites on double-append).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from src.core.plane1_changelog import (
    Plane1ChangeRecord,
    append_plane1_changes,
    build_plane1_snapshot,
    compute_plane1_changes,
    load_plane1_changes,
    load_plane1_last_seen,
    shadow_write_plane1_snapshot,
    write_plane1_last_seen,
)
from src.core.operation_trace import load_operation_trace
from src.core.models_v2 import (
    Assumption,
    AssumptionStatus,
    DecisionEntry,
    DecisionStatus,
    Milestone,
    MilestoneStatus,
    Workstream,
)
from src.core.program_fact_store import ProgramFactStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _programs_root(tmp_path: Path) -> Path:
    root = tmp_path / "programs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_record(
    program_id: str = "acme",
    entity_id: str = "ms1",
    entity_type: str = "milestone",
    field: str = "status",
    prior: str | None = "on_track",
    current: str | None = "at_risk",
    kind: str = "status_change",
    ts: datetime | None = None,
) -> Plane1ChangeRecord:
    return Plane1ChangeRecord(
        ts=ts or datetime.now(timezone.utc),
        program_id=program_id,
        gather_run_id="run-001",
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name="Milestone 1",
        field=field,
        prior=prior,
        current=current,
        kind=kind,
        linked_workstream_ids=("ws1",),
    )


# ---------------------------------------------------------------------------
# append_plane1_changes
# ---------------------------------------------------------------------------

def test_append_creates_changelog_file(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_plane1_changes("acme", [_make_record()], programs_root=pr)
    log = pr / "acme" / "changelog" / "plane1_changes.jsonl"
    assert log.exists()


def test_append_empty_list_is_noop(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_plane1_changes("acme", [], programs_root=pr)
    log = pr / "acme" / "changelog" / "plane1_changes.jsonl"
    assert not log.exists(), "Empty append should not create the file"


def test_append_is_append_only(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_plane1_changes("acme", [_make_record(entity_id="ms1")], programs_root=pr)
    append_plane1_changes("acme", [_make_record(entity_id="ms2")], programs_root=pr)
    records = load_plane1_changes("acme", programs_root=pr)
    assert len(records) == 2
    entity_ids = {r.entity_id for r in records}
    assert entity_ids == {"ms1", "ms2"}


# ---------------------------------------------------------------------------
# load_plane1_changes
# ---------------------------------------------------------------------------

def test_load_returns_empty_for_missing_file(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    records = load_plane1_changes("acme", programs_root=pr)
    assert records == []


def test_load_round_trips_all_fields(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    original = _make_record()
    append_plane1_changes("acme", [original], programs_root=pr)
    loaded = load_plane1_changes("acme", programs_root=pr)
    assert len(loaded) == 1
    r = loaded[0]
    assert r.program_id == original.program_id
    assert r.entity_type == original.entity_type
    assert r.entity_id == original.entity_id
    assert r.field == original.field
    assert r.prior == original.prior
    assert r.current == original.current
    assert r.kind == original.kind
    assert r.linked_workstream_ids == original.linked_workstream_ids
    assert r.record_type == "plane1_change"


def test_load_since_filter(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    old_time = datetime.now(timezone.utc) - timedelta(days=7)
    new_time = datetime.now(timezone.utc)
    append_plane1_changes("acme", [_make_record(ts=old_time)], programs_root=pr)
    append_plane1_changes("acme", [_make_record(ts=new_time)], programs_root=pr)

    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    recent = load_plane1_changes("acme", programs_root=pr, since=cutoff)
    assert len(recent) == 1
    assert recent[0].ts > cutoff


def test_load_multiple_records(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    records = [_make_record(entity_id=f"ms{i}") for i in range(5)]
    append_plane1_changes("acme", records, programs_root=pr)
    loaded = load_plane1_changes("acme", programs_root=pr)
    assert len(loaded) == 5


# ---------------------------------------------------------------------------
# write/load_plane1_last_seen
# ---------------------------------------------------------------------------

def test_write_last_seen_creates_file(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    snapshot = {"milestone/ms1": {"status": "on_track"}}
    write_plane1_last_seen("acme", snapshot, programs_root=pr)
    path = pr / "acme" / "changelog" / "plane1_last_seen.json"
    assert path.exists()


def test_load_last_seen_returns_none_if_missing(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    result = load_plane1_last_seen("acme", programs_root=pr)
    assert result is None


def test_write_load_last_seen_round_trip(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    snapshot = {
        "milestone/ms1": {"status": "on_track", "target_date": "2026-06-30"},
        "risk/r1": {"status": "open"},
    }
    write_plane1_last_seen("acme", snapshot, programs_root=pr)
    loaded = load_plane1_last_seen("acme", programs_root=pr)
    assert loaded is not None
    assert loaded == snapshot


def test_write_last_seen_overwrites(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    write_plane1_last_seen("acme", {"milestone/ms1": {"status": "on_track"}}, programs_root=pr)
    write_plane1_last_seen("acme", {"milestone/ms1": {"status": "at_risk"}}, programs_root=pr)
    loaded = load_plane1_last_seen("acme", programs_root=pr)
    assert loaded == {"milestone/ms1": {"status": "at_risk"}}


# ---------------------------------------------------------------------------
# Plane1ChangeRecord serialization
# ---------------------------------------------------------------------------

def test_plane1_change_record_to_from_json() -> None:
    record = _make_record()
    d = record.to_json()
    restored = Plane1ChangeRecord.from_json(d)
    assert restored.program_id == record.program_id
    assert restored.entity_id == record.entity_id
    assert restored.field == record.field
    assert restored.prior == record.prior
    assert restored.current == record.current
    assert restored.kind == record.kind
    assert restored.linked_workstream_ids == record.linked_workstream_ids
    assert restored.record_type == "plane1_change"


def test_shadow_write_plane1_snapshot_appends_only_on_material_change(tmp_path: Path) -> None:
    snapshot = {
        "milestone/ms1": {
            "status": "on_track",
            "_name": "Milestone 1",
            "_linked_workstream_ids": ("ws1",),
        }
    }
    shadow_write_plane1_snapshot(
        "acme",
        snapshot,
        recorded_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        db_root=tmp_path,
    )
    shadow_write_plane1_snapshot(
        "acme",
        snapshot,
        recorded_at=datetime(2026, 5, 31, 13, 0, tzinfo=timezone.utc),
        db_root=tmp_path,
    )
    shadow_write_plane1_snapshot(
        "acme",
        {
            "milestone/ms1": {
                "status": "at_risk",
                "_name": "Milestone 1",
                "_linked_workstream_ids": ("ws1",),
            }
        },
        recorded_at=datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc),
        db_root=tmp_path,
    )

    store = ProgramFactStore("acme", db_root=tmp_path)
    facts = store.snapshot(as_of=datetime(2026, 5, 31, 15, 0, tzinfo=timezone.utc)).facts

    assert len(facts) == 1
    assert facts[0].payload["value"] == "at_risk"
    with sqlite3.connect(store.db_path) as connection:
        revision_count = connection.execute(
            "SELECT COUNT(*) FROM program_fact_revisions WHERE fact_type = ?",
            ("plane1.milestone.status",),
        ).fetchone()[0]
    assert revision_count == 2


def test_shadow_write_plane1_snapshot_records_one_trace_link_per_call(tmp_path: Path) -> None:
    # ADF-W2.12: one trace link per snapshot write (not one per field-fact,
    # since a real snapshot can touch hundreds of entity/field pairs), no-op
    # when no correlation identity is threaded (the pre-existing default).
    snapshot = {
        "milestone/ms1": {"status": "on_track", "_name": "Milestone 1", "_linked_workstream_ids": ("ws1",)},
        "risk/r1": {"status": "open", "_name": "Risk 1", "_linked_workstream_ids": ("ws1",)},
    }
    shadow_write_plane1_snapshot(
        "acme",
        snapshot,
        recorded_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        db_root=tmp_path,
        correlation_id="gather-corr-3",
        programs_root=tmp_path,
    )

    trace = load_operation_trace("acme", "gather-corr-3", programs_root=tmp_path)
    assert trace is not None
    assert len(trace.fact_refs) == 1
    assert trace.fact_refs[0].startswith("plane1_snapshot:acme:2@")


def test_compute_plane1_changes_tracks_milestone_status_change() -> None:
    milestone = Milestone(
        id="ms1",
        program_id="acme",
        name="Launch readiness",
        target_date=datetime(2026, 6, 30, tzinfo=timezone.utc).date(),
        owner_alias="operator",
        status=MilestoneStatus.AT_RISK,
        exit_criteria=("Dry run complete",),
        linked_workstream_ids=("ws1",),
        linked_work_item_ids=(),
        notes=None,
    )
    last_seen = build_plane1_snapshot(
        [
            Milestone(
                id="ms1",
                program_id="acme",
                name="Launch readiness",
                target_date=datetime(2026, 6, 30, tzinfo=timezone.utc).date(),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Dry run complete",),
                linked_workstream_ids=("ws1",),
                linked_work_item_ids=(),
                notes=None,
            )
        ],
        [],
        [],
        [],
        [],
    )

    changes = compute_plane1_changes(
        "acme",
        [milestone],
        [],
        [],
        [],
        [],
        last_seen,
        "run-001",
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert [(change.entity_type, change.field, change.kind, change.prior, change.current) for change in changes] == [
        ("milestone", "status", "status_change", "on_track", "at_risk")
    ]


def test_compute_plane1_changes_ignores_metadata_roundtrip_noise(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    workstream = Workstream(
        id="ws1",
        name="Launch",
        dri_email="operator@example.com",
        current_blocker="Waiting on partner",
    )
    write_plane1_last_seen(
        "acme",
        build_plane1_snapshot([], [], [workstream], [], []),
        programs_root=programs_root,
    )
    round_tripped_last_seen = load_plane1_last_seen("acme", programs_root=programs_root)

    changes = compute_plane1_changes(
        "acme",
        [],
        [],
        [workstream],
        [],
        [],
        round_tripped_last_seen,
        "run-001",
        datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert changes == []
