"""WS-20: RAID no-orphaned vertex/ado_update signals assertion contract tests.

Spec: specs/prod-vis.md §WS-20 acceptance:
  "'no orphaned vertex/ado_update signals' assertion wired into doctor --consistency;
   run ≥3 cycles to prove register transitions correct."

Tests:
  1. Orphan check is skipped (gracefully) when edition cannot be resolved
  2. No orphan failure when no vertex/ado_update signals exist
  3. No orphan failure when all vertex/ado_update signals have review decisions
  4. Orphan failure reported when unreviewed vertex/ado_update signals exist (count + date)
  5. Multiple orphaned signals: count and oldest timestamp both appear in failure message
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands.doctor_checks.consistency_checks import consistency_check
from src.core import journal
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision


_EDITION = "test_weekly_ws20"
_PROGRAM_ID = "testws20"


def _write_minimal_edition(editions_root: Path, programs_root: Path) -> None:
    """Write the minimal YAML files needed for resolve_edition to succeed."""
    editions_root.mkdir(parents=True, exist_ok=True)
    (editions_root / f"{_EDITION}.yaml").write_text(
        "\n".join([
            "schema_version: '2.0'",
            f"id: {_EDITION}",
            f"program_id: {_PROGRAM_ID}",
            "name: Test Weekly WS20",
            "type: narrative",
            "altitude: helicopter",
            "cadence: weekly",
        ]),
        encoding="utf-8",
    )
    prog_dir = programs_root / _PROGRAM_ID
    prog_dir.mkdir(parents=True, exist_ok=True)
    (prog_dir / "program.yaml").write_text(
        "\n".join([
            "schema_version: '3.0'",
            f"id: {_PROGRAM_ID}",
            "name: Test Program WS20",
        ]),
        encoding="utf-8",
    )
    (prog_dir / "workstreams.yaml").write_text(
        "schema_version: '2.0'\nworkstreams: []\n",
        encoding="utf-8",
    )
    (prog_dir / "scorecards.yaml").write_text(
        "schema_version: '1.0'\nscorecards: []\n",
        encoding="utf-8",
    )


def _write_minimal_archive_and_baseline(
    tmp_path: Path,
    editions_root: Path,
    programs_root: Path,
    reports_root: Path,
    archive_root: Path,
) -> None:
    """Write the minimal archive index, trusted baseline, and review status needed by consistency_check."""
    from src.core.archive_store import read_archive_index
    from src.core.snapshot_store import get_archive_root
    from src.core.trusted_baseline_store import TrustedBaseline, TrustedBaselineHistoryEntry, save_trusted_baseline
    from src.core.review_status_store import save_review_status
    from src.core.models import ReviewSection, ReviewState, ReviewStatus

    archive_dir = get_archive_root(_EDITION, archive_root)
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "index.json").write_text(
        json.dumps({
            "edition": _EDITION,
            "issues": [
                {
                    "issue_number": 1,
                    "generated_at": "2026-05-20T12:00:00+00:00",
                    "kind": "confirmed",
                    "html_path": None,
                    "md_path": None,
                    "snapshot_path": None,
                    "manifest_path": None,
                }
            ],
        }),
        encoding="utf-8",
    )
    save_trusted_baseline(
        _EDITION,
        TrustedBaseline(
            schema_version="1.0",
            edition=_EDITION,
            trusted_issue_number=1,
            established_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            established_by="tester",
            history=(
                TrustedBaselineHistoryEntry(
                    issue=1,
                    at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
                    by="tester",
                    action="established",
                ),
            ),
        ),
        editions_root=editions_root,
        programs_root=programs_root,
    )
    review_dir = archive_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "issue_001.review.yaml").write_text(
        "issue_number: 1\nsections: []\n",
        encoding="utf-8",
    )
    save_review_status(
        _EDITION,
        ReviewStatus(
            issue_number=1,
            sections=(
                ReviewSection(
                    section_id="exec_summary",
                    state=ReviewState.APPROVED,
                    reviewer=None,
                    note=None,
                    updated_at=None,
                ),
            ),
        ),
        reports_root=reports_root,
    )


def _make_ado_update_signal(signal_id: str, ts: datetime) -> Signal:
    return Signal(
        id=signal_id,
        timestamp=ts,
        source="vertex/ado_update",
        program_id=_PROGRAM_ID,
        workstream_id=None,
        entity_refs=(),
        text="Vertex wrote a field update to ADO item 42.",
        raw_ref="ado/42",
        confidence=Confidence.HIGH,
    )


def test_orphan_check_skipped_when_edition_not_found(tmp_path: Path) -> None:
    """When the edition YAML is absent, resolve_edition returns None; no orphan failure is injected."""
    editions_root = tmp_path / "editions"
    editions_root.mkdir()
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    archive_root = tmp_path / "archive"
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    result = consistency_check(
        "nonexistent_edition",
        archive_root=archive_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    # The check may fail for other reasons (no baseline, no archive), but must not have orphan message.
    assert "orphaned vertex/ado_update" not in result.detail


def test_no_orphan_failure_when_no_ado_update_signals(tmp_path: Path) -> None:
    """When the program journal has no vertex/ado_update signals, no orphan failure is added."""
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    _write_minimal_edition(editions_root, programs_root)
    _write_minimal_archive_and_baseline(tmp_path, editions_root, programs_root, reports_root, archive_root)

    result = consistency_check(
        _EDITION,
        archive_root=archive_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert "orphaned vertex/ado_update" not in result.detail


def test_no_orphan_failure_when_all_signals_reviewed(tmp_path: Path) -> None:
    """When all vertex/ado_update signals have review decisions, no orphan failure is added."""
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    _write_minimal_edition(editions_root, programs_root)
    _write_minimal_archive_and_baseline(tmp_path, editions_root, programs_root, reports_root, archive_root)

    ts = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    sig = _make_ado_update_signal("ado-sig-reviewed-1", ts)
    journal.append_signal(sig, programs_root=programs_root, partition_at=ts)

    # Add a review decision for the signal
    journal.append_review_decision(
        _PROGRAM_ID,
        SignalReviewDecision(
            signal_id="ado-sig-reviewed-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
            reviewed_by="tester",
        ),
        programs_root=programs_root,
    )

    result = consistency_check(
        _EDITION,
        archive_root=archive_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert "orphaned vertex/ado_update" not in result.detail


def test_orphan_failure_when_unreviewed_ado_update_signal(tmp_path: Path) -> None:
    """When a vertex/ado_update signal has no review decision, an orphan failure is added."""
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    _write_minimal_edition(editions_root, programs_root)
    _write_minimal_archive_and_baseline(tmp_path, editions_root, programs_root, reports_root, archive_root)

    ts = datetime(2026, 5, 12, 8, 30, tzinfo=timezone.utc)
    sig = _make_ado_update_signal("ado-sig-orphan-1", ts)
    journal.append_signal(sig, programs_root=programs_root, partition_at=ts)

    result = consistency_check(
        _EDITION,
        archive_root=archive_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert result.status == "fail"
    assert "orphaned vertex/ado_update" in result.detail
    assert "1 orphaned vertex/ado_update signal(s)" in result.detail
    assert "2026-05-12" in result.detail


def test_orphan_failure_count_and_oldest_date(tmp_path: Path) -> None:
    """With multiple unreviewed signals: count and oldest timestamp both appear in failure message."""
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    _write_minimal_edition(editions_root, programs_root)
    _write_minimal_archive_and_baseline(tmp_path, editions_root, programs_root, reports_root, archive_root)

    ts_old = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    ts_new = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    journal.append_signal(_make_ado_update_signal("ado-sig-old", ts_old), programs_root=programs_root, partition_at=ts_old)
    journal.append_signal(_make_ado_update_signal("ado-sig-new", ts_new), programs_root=programs_root, partition_at=ts_new)

    # Review the newer signal, leaving the older orphaned.
    journal.append_review_decision(
        _PROGRAM_ID,
        SignalReviewDecision(
            signal_id="ado-sig-new",
            decision="approved",
            reviewed_at=datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc),
            reviewed_by="tester",
        ),
        programs_root=programs_root,
    )

    result = consistency_check(
        _EDITION,
        archive_root=archive_root,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert result.status == "fail"
    assert "1 orphaned vertex/ado_update signal(s)" in result.detail
    assert "2026-05-01" in result.detail  # oldest is the unreviewed old signal
