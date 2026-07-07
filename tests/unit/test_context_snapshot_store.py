"""
Unit tests for src/core/context_snapshot_store.py — §22 E2 context snapshots.

Zone A only. Tests use tmp_path for all filesystem operations.
Covers: write_context_snapshot (path, file created, content), load_context_snapshot
        (round-trip, missing file returns None), and ContextSnapshot serialization.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.models_v2 import DecisionEntry, DecisionStatus
from src.core.context_snapshot_store import ContextSnapshot, load_context_snapshot, write_context_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_simple_snapshot(
    tmp_path: Path,
    *,
    program_id: str = "acme",
    edition_id: str = "acme_weekly",
    issue_number: int = 78,
) -> Path:
    """Write a snapshot with empty domain lists."""
    return write_context_snapshot(
        program_id,
        edition_id,
        issue_number,
        milestones=[],
        risks=[],
        workstreams=[],
        decisions=[],
        confirmed_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        plane1_change_count_since_prior=3,
        archive_root=tmp_path / "output",
    )


# ---------------------------------------------------------------------------
# write_context_snapshot
# ---------------------------------------------------------------------------

def test_write_creates_file(tmp_path: Path) -> None:
    path = _write_simple_snapshot(tmp_path)
    assert path.exists()


def test_write_creates_correct_filename(tmp_path: Path) -> None:
    path = _write_simple_snapshot(tmp_path, issue_number=7)
    assert path.name == "issue_007.context.json"


def test_write_file_is_valid_json(tmp_path: Path) -> None:
    path = _write_simple_snapshot(tmp_path)
    content = path.read_text(encoding="utf-8")
    data = json.loads(content)
    assert isinstance(data, dict)


def test_write_contains_required_fields(tmp_path: Path) -> None:
    path = _write_simple_snapshot(tmp_path, program_id="acme", edition_id="acme_weekly", issue_number=78)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["program_id"] == "acme"
    assert data["edition"] == "acme_weekly"
    assert data["issue_number"] == 78
    assert data["schema_version"] == "1.1"
    assert "confirmed_at" in data
    assert data["plane1_change_count_since_prior"] == 3


def test_write_returns_path_inside_archive(tmp_path: Path) -> None:
    archive = tmp_path / "output"
    path = write_context_snapshot(
        "acme", "acme_weekly", 10,
        milestones=[], risks=[], workstreams=[], decisions=[],
        confirmed_at=datetime.now(timezone.utc),
        plane1_change_count_since_prior=0,
        archive_root=archive,
    )
    assert archive in path.parents


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    """write_context_snapshot must create missing parent directories."""
    deep_archive = tmp_path / "a" / "b" / "c"
    path = write_context_snapshot(
        "acme", "acme_weekly", 1,
        milestones=[], risks=[], workstreams=[], decisions=[],
        confirmed_at=datetime.now(timezone.utc),
        plane1_change_count_since_prior=0,
        archive_root=deep_archive,
    )
    assert path.exists()


# ---------------------------------------------------------------------------
# load_context_snapshot
# ---------------------------------------------------------------------------

def test_load_returns_none_for_missing_snapshot(tmp_path: Path) -> None:
    result = load_context_snapshot(
        "acme", "acme_weekly", 99,
        archive_root=tmp_path / "output",
    )
    assert result is None


def test_load_round_trip(tmp_path: Path) -> None:
    _write_simple_snapshot(tmp_path, program_id="acme", edition_id="acme_weekly", issue_number=78)
    snap = load_context_snapshot(
        "acme", "acme_weekly", 78,
        archive_root=tmp_path / "output",
    )
    assert snap is not None
    assert snap.program_id == "acme"
    assert snap.edition == "acme_weekly"
    assert snap.issue_number == 78
    assert snap.plane1_change_count_since_prior == 3
    assert isinstance(snap.confirmed_at, datetime)


def test_load_preserves_empty_lists(tmp_path: Path) -> None:
    _write_simple_snapshot(tmp_path)
    snap = load_context_snapshot(
        "acme", "acme_weekly", 78,
        archive_root=tmp_path / "output",
    )
    assert snap is not None
    assert snap.milestones == ()
    assert snap.risks == ()
    assert snap.workstreams == ()
    assert snap.decisions == ()


def test_write_preserves_decision_history_fields(tmp_path: Path) -> None:
    decision = DecisionEntry(
        id="decision-1",
        program_id="acme",
        title="Promote demo gate",
        context="Context",
        decision="Proceed",
        rationale=None,
        alternatives_considered=(),
        decided_by="operator",
        decision_date=datetime(2026, 5, 19, tzinfo=timezone.utc).date(),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
    )
    _write_simple_snapshot(tmp_path)

    path = write_context_snapshot(
        "acme",
        "acme_weekly",
        79,
        milestones=[],
        risks=[],
        workstreams=[],
        decisions=[decision],
        confirmed_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
        plane1_change_count_since_prior=1,
        archive_root=tmp_path / "output",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.1"
    assert payload["decisions"] == [
        {
            "id": "decision-1",
            "title": "Promote demo gate",
            "decided_by": "operator",
            "decision_date": "2026-05-19",
            "status": "decided",
        }
    ]


def test_load_returns_none_for_invalid_json(tmp_path: Path) -> None:
    archive = tmp_path / "output"
    snap_dir = archive / "acme" / "archive" / "acme_weekly" / "context_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "issue_001.context.json").write_text("not valid json", encoding="utf-8")
    result = load_context_snapshot("acme", "acme_weekly", 1, archive_root=archive)
    assert result is None


# ---------------------------------------------------------------------------
# ContextSnapshot serialization
# ---------------------------------------------------------------------------

def test_context_snapshot_to_from_json_round_trip() -> None:
    now = datetime.now(timezone.utc)
    snap = ContextSnapshot(
        schema_version="1.0",
        issue_number=5,
        edition="acme_weekly",
        program_id="acme",
        confirmed_at=now,
        milestones=({"id": "ms1", "name": "M1"},),
        risks=(),
        workstreams=(),
        decisions=(),
        plane1_change_count_since_prior=2,
    )
    d = snap.to_json()
    restored = ContextSnapshot.from_json(d)
    assert restored.issue_number == snap.issue_number
    assert restored.edition == snap.edition
    assert restored.program_id == snap.program_id
    assert restored.plane1_change_count_since_prior == snap.plane1_change_count_since_prior
    assert len(restored.milestones) == 1
    assert restored.milestones[0]["id"] == "ms1"
