from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor
from src.commands.doctor_checks.auth_checks import kusto_auth_check
from src.commands.doctor_checks.kusto_checks import (
    kusto_target_labels,
    summarize_kusto_targets,
    validate_kusto_query_definitions,
)
from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck, DoctorReport
from src.core.exceptions import AuthError, QueryError
from src.core.kusto_client import KustoClient, build_live_kusto_query_probe
from src.core.models_v2 import KustoQuery
from src.m365.agency_bridge import AgencyCapabilities
from tests.support.doctor_test_setup import doctor_source_root as repo_root
from tests.support.report_test_setup import stage_v2_report_workspace


_PRIVATE = "synthetic-provider-private-detail"
_GOOD = "https://healthy.example.invalid/"
_BAD = "https://bad.example.invalid/"


def _query(identifier: str, cluster: str, database: str) -> KustoQuery:
    return KustoQuery(
        id=identifier,
        cluster=cluster,
        database=database,
        kql="Synthetic | take 1",
        section="Synthetic",
        render_as="table",
        confidence="high",
        validated=True,
        refresh_on_gather=True,
    )


def _access(
    tmp_path: Path,
    queries: tuple[KustoQuery, ...],
    *,
    enabled: bool = True,
    probe=None,
    builder=build_live_kusto_query_probe,
    validate=validate_kusto_query_definitions,
) -> DoctorCheck:
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="acme"),
        program=SimpleNamespace(
            id="acme", ado=None, kusto=SimpleNamespace(enabled=enabled, queries=()),
        ),
    )
    result = kusto_auth_check(
        edition_name="acme_weekly",
        editions_root=tmp_path,
        programs_root=tmp_path,
        kusto_probe=probe,
        resolve_edition_fn=lambda *_args, **_kwargs: resolved,
        load_doctor_kusto_queries_fn=lambda *_args, **_kwargs: queries,
        kusto_target_labels_fn=kusto_target_labels,
        validate_kusto_query_definitions_fn=validate,
        summarize_kusto_targets_fn=summarize_kusto_targets,
        build_live_kusto_query_probe_fn=builder,
    )
    assert result is not None
    return result


@pytest.mark.parametrize("error_type", [AuthError, QueryError])
@pytest.mark.parametrize("failed_count", [1, 2, 3])
def test_actual_probe_failed_targets_are_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error_type: type[Exception], failed_count: int,
) -> None:
    failed = tuple(
        _query(f"failed-{index}", f"https://bad{index}.example.invalid/", f"Db{index}")
        for index in range(failed_count)
    )
    healthy = _query("healthy", _GOOD, "HealthyDb")
    queries = (healthy, *failed, failed[0])
    calls: list[tuple[str, str]] = []

    def execute(_self, cluster, database, kql, **kwargs):
        calls.append((cluster, database))
        assert kql == ".show version"
        assert kwargs == {"timeout": 30, "max_retries": 1}
        if cluster != _GOOD:
            raise error_type(_PRIVATE)
        return [], ()

    monkeypatch.setattr(KustoClient, "execute", execute)
    check = _access(tmp_path, queries)

    assert len(calls) == failed_count + 1
    assert check.status == "fail"
    assert check.detail.startswith(f"Kusto auth probe failed for {failed_count} cluster/database target")
    assert "https://bad0.example.invalid/Db0" in check.detail
    assert "https://healthy.example.invalid/HealthyDb" not in check.detail
    assert _PRIVATE not in check.detail
    assert _PRIVATE not in caplog.text
    if failed_count == 3:
        assert "+1 more" in check.detail
    assert check.metadata is not None
    assert check.metadata["query_count"] == len(queries)
    assert check.metadata["cluster_targets"] == list(kusto_target_labels(queries))


def test_actual_probe_success_retains_targets_and_deduplicates_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = (_query("one", _GOOD, "HealthyDb"), _query("two", _GOOD, "HealthyDb"))
    calls: list[tuple[str, str]] = []

    def execute(_self, cluster, database, kql, **kwargs):
        calls.append((cluster, database))
        return [], ()

    monkeypatch.setattr(KustoClient, "execute", execute)
    check = _access(tmp_path, queries)
    assert check.status == "ok"
    assert check.detail == "Kusto auth probe succeeded for 1 cluster/database target (https://healthy.example.invalid/HealthyDb)."
    assert calls == [(_GOOD, "HealthyDb")]


def test_failed_target_labels_use_existing_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = (
        _query("one", _BAD, "BadDb"),
        _query("two", _BAD.rstrip("/"), "BadDb"),
    )
    calls: list[str] = []

    def execute(_self, cluster, _database, _kql, **_kwargs):
        calls.append(cluster)
        raise QueryError(_PRIVATE)

    monkeypatch.setattr(KustoClient, "execute", execute)
    check = _access(tmp_path, queries)
    assert check.status == "fail"
    assert check.detail.startswith("Kusto auth probe failed for 1 cluster/database target (https://bad.example.invalid/BadDb).")
    assert calls == [_BAD, _BAD.rstrip("/")]


def test_disabled_and_invalid_definitions_do_not_probe(tmp_path: Path) -> None:
    queries = (_query("one", _GOOD, "HealthyDb"),)

    def unexpected_builder(**_kwargs):
        pytest.fail("The probe must not run")

    disabled = _access(tmp_path, queries, enabled=False, builder=unexpected_builder)
    invalid = _access(tmp_path, queries, builder=unexpected_builder, validate=lambda _queries: ("invalid definition",))
    empty = _access(tmp_path, (), builder=unexpected_builder)
    assert disabled.status == "warn"
    assert invalid.status == "fail" and invalid.detail == "invalid definition"
    assert empty.status == "warn"


@pytest.mark.parametrize("error_type", [AuthError, QueryError])
def test_injected_probe_failure_remains_explicit(
    tmp_path: Path, error_type: type[Exception],
) -> None:
    def fail(_query):
        raise error_type("synthetic injected failure")

    check = _access(tmp_path, (_query("one", _GOOD, "HealthyDb"),), probe=fail)
    assert check.status == "fail"
    assert check.detail == "Kusto access probe failed. Check access and retry."


def test_injected_probe_success_and_unexpected_failure_are_unchanged(tmp_path: Path) -> None:
    queries = (_query("one", _GOOD, "HealthyDb"), _query("two", _GOOD, "HealthyDb"))
    calls: list[str] = []

    def unexpected_builder(**_kwargs):
        pytest.fail("An injected probe must be used")

    check = _access(tmp_path, queries, probe=lambda query: calls.append(query.id), builder=unexpected_builder)
    assert check.status == "ok"
    assert calls == ["one", "two"]

    def unexpected(_query):
        raise ValueError("synthetic unexpected failure")

    with pytest.raises(ValueError, match="synthetic unexpected failure"):
        _access(tmp_path, queries, probe=unexpected)


def test_unexpected_default_probe_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args, **_kwargs):
        raise ValueError("synthetic unexpected failure")

    monkeypatch.setattr(KustoClient, "execute", unexpected)
    with pytest.raises(ValueError, match="synthetic unexpected failure"):
        _access(tmp_path, (_query("one", _GOOD, "HealthyDb"),))


@pytest.fixture
def configured_doctor(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[tuple[str, str]]]:
    reports = stage_v2_report_workspace(repo_root, tmp_path)
    queries = (
        _query("healthy", _GOOD, "HealthyDb"),
        _query("failed", _BAD, "BadDb"),
    )
    monkeypatch.setattr(doctor, "_load_doctor_kusto_queries_impl", lambda *_args, **_kwargs: queries)
    monkeypatch.setattr(doctor, "REPORTS_ROOT", reports)
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", reports.parent / "programs")
    monkeypatch.setattr(doctor, "find_spec", lambda _name: object())
    monkeypatch.setenv("GRAPH_TENANT_ID", "synthetic-tenant")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "synthetic-client")
    monkeypatch.setattr(
        doctor, "_probe_ado_access",
        lambda _bundle: ADOProbeResult(True, "synthetic", 0, 47, "Synthetic ADO probe."),
    )

    class ReadyAgency:
        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(available=True, has_workiq=True, has_bluebird=True, has_ado=True, has_icm=True, tier="msft")

    monkeypatch.setattr(doctor, "AgencyBridge", ReadyAgency)
    calls: list[tuple[str, str]] = []

    def execute(_self, cluster, database, kql, **kwargs):
        calls.append((cluster, database))
        if cluster == _BAD:
            raise QueryError(_PRIVATE)
        return [], ()

    monkeypatch.setattr(KustoClient, "execute", execute)
    return reports, calls


def test_actual_doctor_reports_failed_probe(configured_doctor) -> None:
    reports, calls = configured_doctor
    report = doctor.run_doctor(edition_name="acme_weekly", check_auth=True, reports_root=reports)
    checks = {check.label: check for check in report.checks}
    assert checks["ADO Access"].status == "ok"
    assert checks["Kusto Access"].status == "fail"
    assert report.overall == "UNHEALTHY"
    assert calls == [(_GOOD, "HealthyDb"), (_BAD, "BadDb")]


def test_actual_doctor_reports_successful_default_probe(
    configured_doctor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports, _calls = configured_doctor
    monkeypatch.setattr(KustoClient, "execute", lambda *_args, **_kwargs: ([], ()))
    report = doctor.run_doctor(edition_name="acme_weekly", check_auth=True, reports_root=reports)
    access = next(check for check in report.checks if check.label == "Kusto Access")
    assert access.status == "ok"
    assert report.overall == "HEALTHY"


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_actual_doctor_cli_reports_failed_probe(
    configured_doctor, isolated_cli_startup, output_format: str,
) -> None:
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "acme_weekly", "--check-auth", "--format", output_format],
    )
    assert result.exit_code == 1
    assert _PRIVATE not in result.output
    if output_format == "human":
        assert "FAIL Kusto Access" in result.output
        assert "Overall: UNHEALTHY" in result.output
    elif output_format == "json":
        payload = json.loads(result.output)
        assert payload["overall"] == "UNHEALTHY"
        assert next(row for row in payload["checks"] if row["label"] == "Kusto Access")["status"] == "fail"
    else:
        rows = list(csv.DictReader(result.output.splitlines()))
        assert next(row for row in rows if row["label"] == "Kusto Access")["status"] == "fail"


@pytest.mark.parametrize("failed", [True, False])
def test_actual_operator_gate_uses_access_result(
    configured_doctor, monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    reports, _calls = configured_doctor
    if not failed:
        monkeypatch.setattr(KustoClient, "execute", lambda *_args, **_kwargs: ([], ()))
    empty = DoctorReport("acme_weekly", ())
    metric = DoctorReport(
        "acme_weekly",
        (
            DoctorCheck("Metric Bindings", "ok", "Synthetic bindings."),
            DoctorCheck("Metric Rollout", "ok", "Synthetic rollout."),
        ),
    )
    monkeypatch.setattr(doctor, "_run_channel_doctor", lambda **_kwargs: empty)
    monkeypatch.setattr(doctor, "_run_metric_binding_doctor", lambda **_kwargs: metric)
    monkeypatch.setattr(doctor, "_run_checkpoint_doctor", lambda **_kwargs: empty)
    report = doctor.run_doctor(edition_name="acme_weekly", operator_gates=True, reports_root=reports)
    gate = next(check for check in report.checks if check.label == "Gate:Kusto Validation")
    assert gate.status == ("fail" if failed else "ok")
    assert gate.metadata is not None
    assert gate.metadata["action_category"] == ("auth-admin-required" if failed else None)
    assert gate.metadata["kusto_access"]["status"] == ("fail" if failed else "ok")
