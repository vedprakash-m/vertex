from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

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
    evaluate_scheduled_gather_readiness,
    latest_gather_attempt_at,
    load_scheduled_acquisition_policy,
    resolve_scheduled_process_principal,
)
from src.core.gather_schedule_monitor import require_scheduled_gather_authorized
from src.core.gather_schedule_monitor import ScheduledGatherAuthorizationError
from src.core.exceptions import ConfigError


def _manifest(*, run_id: str, attempted_at: datetime) -> GatherRunManifest:
    return GatherRunManifest(
        run_id=run_id,
        status=GatherRunStatus.RUNNING,
        program_id="samplefleet",
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
    _schedule_policy(programs_root)
    checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)

    status = evaluate_scheduled_gather_attempt(
        "samplefleet",
        now=checked_at,
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )

    assert status.missed_attempt is True
    assert status.last_attempt_at is None
    alerts = read_alerts("samplefleet", programs_root=programs_root)
    assert len(alerts) == 1
    assert alerts[0].category == MISSED_ATTEMPT_CATEGORY
    assert alerts[0].occurrence_count == 1
    assert alerts[0].context == {
        "last_attempt_at": None,
        "max_attempt_age_seconds": int(DEFAULT_MAX_ATTEMPT_AGE.total_seconds()),
    }


def test_failed_attempt_counts_for_deadline_and_resolves_open_alert(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _schedule_policy(programs_root)
    checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    evaluate_scheduled_gather_attempt(
        "samplefleet",
        now=checked_at,
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )

    attempted_at = checked_at - timedelta(minutes=5)
    _fail_attempt(programs_root=programs_root, attempted_at=attempted_at, run_id="failed-recent")
    status = evaluate_scheduled_gather_attempt(
        "samplefleet",
        now=checked_at,
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )

    assert status.missed_attempt is False
    assert status.last_attempt_at == attempted_at
    assert latest_gather_attempt_at("samplefleet", programs_root=programs_root) == attempted_at
    assert read_alerts("samplefleet", programs_root=programs_root) == ()
    resolved = read_alerts("samplefleet", programs_root=programs_root, include_resolved=True)
    assert resolved[0].resolved_at == checked_at


def test_exact_26_hour_boundary_is_missed_and_recent_committed_attempt_is_current(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _schedule_policy(programs_root)
    checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    _commit_attempt(
        programs_root=programs_root,
        attempted_at=checked_at - DEFAULT_MAX_ATTEMPT_AGE,
        run_id="committed-boundary",
    )

    overdue = evaluate_scheduled_gather_attempt(
        "samplefleet",
        now=checked_at,
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )
    assert overdue.missed_attempt is True

    _commit_attempt(
        programs_root=programs_root,
        attempted_at=checked_at - timedelta(seconds=1),
        run_id="committed-recent",
    )
    recovered = evaluate_scheduled_gather_attempt(
        "samplefleet",
        now=checked_at,
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )
    assert recovered.missed_attempt is False
    assert recovered.last_attempt_at == checked_at - timedelta(seconds=1)


def test_rejects_naive_clock_and_nonpositive_deadline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_scheduled_gather_attempt(
            "samplefleet", now=datetime(2026, 7, 21, 10, 0), programs_root=tmp_path
        )
    with pytest.raises(ValueError, match="positive"):
        evaluate_scheduled_gather_attempt(
            "samplefleet", now=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
            programs_root=tmp_path,
            max_attempt_age=timedelta(0),
        )


def _schedule_policy(programs_root: Path, *, principal: str = "vertex-scheduler") -> None:
    program_root = programs_root / "samplefleet"
    program_root.mkdir(parents=True, exist_ok=True)
    (program_root / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0",
                "id": "samplefleet",
                "name": "Sample Fleet",
                "scheduled_acquisition": {
                    "mode": "read_only",
                    "authorized_principal": principal,
                    "approval_ref": "owner:scheduled-read-only:2026-07",
                    "alert_delivery_ref": "monitor:vertex-scheduled-gather",
                },
            }
        ),
        encoding="utf-8",
    )


def test_scheduled_readiness_exposes_bounded_recovery_and_exact_principal(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    _schedule_policy(programs_root)

    readiness = evaluate_scheduled_gather_readiness(
        "samplefleet",
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )

    assert readiness.ready is True
    assert readiness.principal_authorized is True
    assert readiness.overlap_bounded_seconds == 300
    assert readiness.crash_resume == "quarantine-abandoned-then-retry"
    assert readiness.missed_job_resume == "retry-on-next-authorized-start"
    assert readiness.alert_ready is True
    assert readiness.blockers == ()


def test_unauthorized_monitor_is_fail_closed_and_does_not_write_or_scan(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _schedule_policy(programs_root)

    status = evaluate_scheduled_gather_attempt(
        "samplefleet",
        now=datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
        principal_resolver=lambda: "different-principal",
        programs_root=programs_root,
    )

    assert status.missed_attempt is None
    assert status.readiness.ready is False
    assert status.readiness.principal_authorized is False
    assert read_alerts("samplefleet", programs_root=programs_root) == ()
    assert not (programs_root / "samplefleet" / "runtime" / "gather_runs").exists()


def test_missing_owner_authorization_cannot_be_counted_complete(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    readiness = evaluate_scheduled_gather_readiness(
        "samplefleet",
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=programs_root,
    )

    assert readiness.ready is False
    assert readiness.principal_authorized is False
    assert "scheduled principal authorization missing" in readiness.blockers


def test_authorization_guard_refuses_before_gather_runtime(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    with pytest.raises(ScheduledGatherAuthorizationError, match="refused before acquisition"):
        require_scheduled_gather_authorized(
            "samplefleet",
            principal_resolver=lambda: "",
            programs_root=programs_root,
        )

    assert not programs_root.exists()


def test_principal_comes_from_resolver_not_configuration_or_caller_string(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    _schedule_policy(programs_root, principal="os-bound-scheduler")
    calls = 0

    def resolve_principal() -> str:
        nonlocal calls
        calls += 1
        return "os-bound-scheduler"

    readiness = require_scheduled_gather_authorized(
        "samplefleet",
        programs_root=programs_root,
        principal_resolver=resolve_principal,
    )

    assert readiness.ready is True
    assert calls == 1


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("- item\n", "must be a mapping"),
        ("scheduled_acquisition: []\n", "must be a mapping"),
    ],
)
def test_scheduled_policy_rejects_non_mapping_shapes(
    tmp_path: Path,
    document: str,
    message: str,
) -> None:
    program_root = tmp_path / "sample"
    program_root.mkdir()
    (program_root / "program.yaml").write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_scheduled_acquisition_policy("sample", programs_root=tmp_path)


def test_scheduled_policy_surfaces_unreadable_yaml(tmp_path: Path) -> None:
    program_root = tmp_path / "sample"
    program_root.mkdir()
    (program_root / "program.yaml").write_text("scheduled_acquisition: [\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Could not load"):
        load_scheduled_acquisition_policy("sample", programs_root=tmp_path)

    assert load_scheduled_acquisition_policy("missing", programs_root=tmp_path) is None


def test_scheduled_readiness_reports_every_missing_authorization(
    tmp_path: Path,
) -> None:
    program_root = tmp_path / "sample"
    program_root.mkdir()
    (program_root / "program.yaml").write_text(
        yaml.safe_dump({
            "scheduled_acquisition": {
                "mode": "write",
                "authorized_principal": "expected",
                "approval_ref": "",
                "alert_delivery_ref": "",
            },
        }),
        encoding="utf-8",
    )

    readiness = evaluate_scheduled_gather_readiness(
        "sample",
        principal_resolver=lambda: (_ for _ in ()).throw(RuntimeError("not available")),
        programs_root=tmp_path,
    )

    assert readiness.ready is False
    assert "scheduled principal authorization missing" in readiness.blockers
    assert "owner approval reference missing" in readiness.blockers
    assert "scheduled acquisition is not explicitly read-only" in readiness.blockers
    assert "alert delivery authorization missing" in readiness.blockers


def test_scheduled_policy_treats_non_string_fields_as_missing(tmp_path: Path) -> None:
    program_root = tmp_path / "sample"
    program_root.mkdir()
    (program_root / "program.yaml").write_text(
        yaml.safe_dump({
            "scheduled_acquisition": {
                "mode": 7,
                "authorized_principal": None,
                "approval_ref": False,
                "alert_delivery_ref": [],
            },
        }),
        encoding="utf-8",
    )

    policy = load_scheduled_acquisition_policy("sample", programs_root=tmp_path)

    assert policy is not None
    assert (policy.mode, policy.authorized_principal, policy.approval_ref, policy.alert_delivery_ref) == (
        "", "", "", "",
    )


def test_windows_principal_resolution_is_os_bound_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import gather_schedule_monitor

    windows_os = SimpleNamespace(name="nt", environ={})
    monkeypatch.setattr(gather_schedule_monitor, "os", windows_os)
    with pytest.raises(RuntimeError, match="SystemRoot|could not be resolved"):
        resolve_scheduled_process_principal()

    windows_os.environ["SystemRoot"] = r"C:\Windows"

    class Completed:
        returncode = 0
        stdout = "domain\\scheduler\n"

    monkeypatch.setattr(gather_schedule_monitor.subprocess, "run", lambda *args, **kwargs: Completed())
    assert resolve_scheduled_process_principal() == "domain\\scheduler"

    Completed.returncode = 1
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_scheduled_process_principal()


def test_posix_principal_resolution_rejects_provider_and_result_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import gather_schedule_monitor

    monkeypatch.setattr(gather_schedule_monitor, "os", SimpleNamespace(name="posix", environ={}))

    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(gather_schedule_monitor.subprocess, "run", lambda *args, **kwargs: Failed())
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_scheduled_process_principal()

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(gather_schedule_monitor.subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_scheduled_process_principal()
