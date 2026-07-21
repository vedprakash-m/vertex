from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.alerts import read_alerts
from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    commit_staging_run,
    create_staging_manifest,
    fail_staging_run,
)
from src.core.gather_schedule_monitor import (
    DEFAULT_MAX_ATTEMPT_AGE,
    MISSED_ATTEMPT_CATEGORY,
    evaluate_scheduled_gather_attempt,
    latest_gather_attempt_at,
)


def _manifest(*, run_id: str, attempted_at: datetime) -> GatherRunManifest:
    return GatherRunManifest(
        run_id=run_id,
        status=GatherRunStatus.RUNNING,
        program_id="armada",
        actor_identity_type="scheduled",
        lease_owner="test-scheduler",
        lease_fencing_token=1,
        started_at=attempted_at,
        scope_as_of=attempted_at,
        required_scope_status=RequiredScopeStatus.PARTIAL,
    )


def _commit_attempt(*, programs_root: Path, attempted_at: datetime, run_id: str) -> None:
    manifest = _manifest(run_id=run_id, attempted_at=attempted_at)
    create_staging_manifest(manifest, programs_root=programs_root)
    commit_staging_run(manifest, finished_at=attempted_at, programs_root=programs_root)


def _fail_attempt(*, programs_root: Path, attempted_at: datetime, run_id: str) -> None:
    manifest = _manifest(run_id=run_id, attempted_at=attempted_at)
    create_staging_manifest(manifest, programs_root=programs_root)
    fail_staging_run(manifest, finished_at=attempted_at, programs_root=programs_root)


def test_no_attempt_opens_stable_missed_attempt_alert(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)

    status = evaluate_scheduled_gather_attempt(
        "armada", now=checked_at, programs_root=programs_root
    )

    assert status.missed_attempt is True
    assert status.last_attempt_at is None
    alerts = read_alerts("armada", programs_root=programs_root)
    assert len(alerts) == 1
    assert alerts[0].category == MISSED_ATTEMPT_CATEGORY
    assert alerts[0].occurrence_count == 1
    assert alerts[0].context == {
        "last_attempt_at": None,
        "max_attempt_age_seconds": int(DEFAULT_MAX_ATTEMPT_AGE.total_seconds()),
    }


def test_failed_attempt_counts_for_deadline_and_resolves_open_alert(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    evaluate_scheduled_gather_attempt("armada", now=checked_at, programs_root=programs_root)

    attempted_at = checked_at - timedelta(minutes=5)
    _fail_attempt(programs_root=programs_root, attempted_at=attempted_at, run_id="failed-recent")
    status = evaluate_scheduled_gather_attempt(
        "armada", now=checked_at, programs_root=programs_root
    )

    assert status.missed_attempt is False
    assert status.last_attempt_at == attempted_at
    assert latest_gather_attempt_at("armada", programs_root=programs_root) == attempted_at
    assert read_alerts("armada", programs_root=programs_root) == ()
    resolved = read_alerts("armada", programs_root=programs_root, include_resolved=True)
    assert resolved[0].resolved_at == checked_at


def test_exact_26_hour_boundary_is_missed_and_recent_committed_attempt_is_current(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    _commit_attempt(
        programs_root=programs_root,
        attempted_at=checked_at - DEFAULT_MAX_ATTEMPT_AGE,
        run_id="committed-boundary",
    )

    overdue = evaluate_scheduled_gather_attempt("armada", now=checked_at, programs_root=programs_root)
    assert overdue.missed_attempt is True

    _commit_attempt(
        programs_root=programs_root,
        attempted_at=checked_at - timedelta(seconds=1),
        run_id="committed-recent",
    )
    recovered = evaluate_scheduled_gather_attempt("armada", now=checked_at, programs_root=programs_root)
    assert recovered.missed_attempt is False
    assert recovered.last_attempt_at == checked_at - timedelta(seconds=1)


def test_rejects_naive_clock_and_nonpositive_deadline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_scheduled_gather_attempt(
            "armada", now=datetime(2026, 7, 21, 10, 0), programs_root=tmp_path
        )
    with pytest.raises(ValueError, match="positive"):
        evaluate_scheduled_gather_attempt(
            "armada", now=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
            programs_root=tmp_path,
            max_attempt_age=timedelta(0),
        )
