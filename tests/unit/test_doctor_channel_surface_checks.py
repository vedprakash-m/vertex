from __future__ import annotations

from types import SimpleNamespace

from src.commands.doctor_checks.channel_surface_checks import ado_pr_coverage_check, channel_last_error, uil_registry_check


def test_channel_last_error_ignores_outdated_kusto_target_failures() -> None:
    error = channel_last_error(
        "kusto",
        {"last_error": "Kusto pre-flight failed for oldcluster/olddb due to auth."},
        current_kusto_targets=("newcluster/newdb",),
    )

    assert error is None


def test_ado_pr_coverage_check_warns_when_all_workstreams_are_missing_repository_ids() -> None:
    check = ado_pr_coverage_check(
        (
            SimpleNamespace(id="acme", ado_repository_ids=()),
            SimpleNamespace(id="dd_on_pf", ado_repository_ids=()),
        )
    )

    assert check.status == "warn"
    assert "no workstreams declare ado_repository_ids" in check.detail
    assert check.metadata is not None
    assert check.metadata["missing_workstream_ids"] == ["acme", "dd_on_pf"]


def test_uil_registry_check_warns_when_scope_health_is_degraded() -> None:
    check = uil_registry_check(
        "teams",
        {
            "uil_enabled": True,
            "uil_health": "ok",
            "uil_registry_size": 2,
            "uil_last_discovery_at": "2026-05-20T00:00:00+00:00",
            "uil_scope_health": {"alpha": "ok", "beta": "warn"},
        },
    )

    assert check is not None
    assert check.status == "warn"
    assert "beta=warn" in check.detail
