from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.core import gather_schedule_monitor
from src.core.alerts import read_alerts
from src.core.exceptions import ConfigError
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
    (
        ("[]\n", "must be a mapping"),
        ("schema_version: '3.0'\nscheduled_acquisition: []\n", "must be a mapping"),
        ("schema_version: '3.0'\n", None),
    ),
)
def test_scheduled_policy_rejects_malformed_shapes_and_allows_absent_policy(
    tmp_path: Path, document: str, message: str | None,
) -> None:
    program_root = tmp_path / "samplefleet"
    program_root.mkdir()
    (program_root / "program.yaml").write_text(document, encoding="utf-8")

    if message is None:
        assert load_scheduled_acquisition_policy("samplefleet", programs_root=tmp_path) is None
    else:
        with pytest.raises(ConfigError, match=message):
            load_scheduled_acquisition_policy("samplefleet", programs_root=tmp_path)


def test_scheduled_policy_contains_read_error_without_caller_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_root = tmp_path / "samplefleet"
    program_root.mkdir()
    policy_path = program_root / "program.yaml"
    policy_path.write_text("schema_version: '3.0'\n", encoding="utf-8")

    def unreadable(*_args, **_kwargs):
        raise OSError("private parser path")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ConfigError, match="Could not load scheduled-acquisition policy"):
        load_scheduled_acquisition_policy("samplefleet", programs_root=tmp_path)


def test_readiness_reports_all_missing_policy_authorizations(
    tmp_path: Path,
) -> None:
    program_root = tmp_path / "samplefleet"
    program_root.mkdir()
    (program_root / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "scheduled_acquisition": {
                    "mode": "write",
                    "authorized_principal": 7,
                    "approval_ref": 8,
                    "alert_delivery_ref": False,
                }
            }
        ),
        encoding="utf-8",
    )

    readiness = evaluate_scheduled_gather_readiness(
        "samplefleet",
        principal_resolver=lambda: "vertex-scheduler",
        programs_root=tmp_path,
    )

    assert readiness.ready is False
    assert readiness.blockers == (
        "scheduled principal authorization missing",
        "owner approval reference missing",
        "scheduled acquisition is not explicitly read-only",
        "alert delivery authorization missing",
    )


def test_principal_resolver_failure_is_fail_closed(tmp_path: Path) -> None:
    _schedule_policy(tmp_path)

    def unavailable() -> str:
        raise OSError("identity backend unavailable")

    readiness = evaluate_scheduled_gather_readiness(
        "samplefleet",
        principal_resolver=unavailable,
        programs_root=tmp_path,
    )

    assert readiness.principal_authorized is False
    assert readiness.ready is False
    assert "scheduled principal authorization missing" in readiness.blockers


@pytest.mark.parametrize(
    ("platform", "system_root", "result"),
    (
        ("nt", "WindowsRoot", SimpleNamespace(returncode=0, stdout="DOMAIN\\Scheduler\n")),
        ("nt", None, None),
        ("posix", None, SimpleNamespace(returncode=0, stdout="scheduler\n")),
    ),
)
def test_process_principal_resolution_uses_os_bound_identity(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    system_root: str | None,
    result: SimpleNamespace | None,
) -> None:
    fake_os = SimpleNamespace(name=platform, environ={})
    if system_root is not None:
        fake_os.environ["SystemRoot"] = system_root
    monkeypatch.setattr(gather_schedule_monitor, "os", fake_os)
    if result is None:
        with pytest.raises(RuntimeError, match="could not be resolved"):
            resolve_scheduled_process_principal()
        return
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 5
        assert kwargs["capture_output"] is True
        return result

    monkeypatch.setattr(gather_schedule_monitor.subprocess, "run", run)
    assert resolve_scheduled_process_principal() == result.stdout.strip()
    assert Path(calls[0][0]).name == ("whoami.exe" if platform == "nt" else "id")


@pytest.mark.parametrize("platform", ("nt", "posix"))
def test_process_principal_resolution_rejects_failed_or_empty_commands(
    monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    monkeypatch.setattr(
        gather_schedule_monitor,
        "os",
        SimpleNamespace(
            name=platform,
            environ={"SystemRoot": "WindowsRoot"} if platform == "nt" else {},
        ),
    )
    monkeypatch.setattr(
        gather_schedule_monitor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_scheduled_process_principal()


def test_posix_process_principal_resolution_contains_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gather_schedule_monitor,
        "os",
        SimpleNamespace(name="posix", environ={}),
    )
    monkeypatch.setattr(
        gather_schedule_monitor.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private")),
    )
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_scheduled_process_principal()
