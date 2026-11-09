from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor
from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.exceptions import AuthError, QueryError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception, classify_message
from tests.unit.test_doctor_kusto_probe_result import (
    _GOOD,
    _access,
    _query,
    configured_doctor,
    repo_root,
)


_PRIVATE = "SYNTHETIC_PROBE_SECRET"
_PREFIX = "Kusto access probe failed"
_PATTERNS = tuple(pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns) + ("novel failure",)


@pytest.mark.parametrize("error_type", [AuthError, QueryError])
@pytest.mark.parametrize("pattern", _PATTERNS)
@pytest.mark.parametrize("probe_source", ["injected", "builder"])
def test_caught_probe_error_uses_safe_taxonomy(
    tmp_path: Path, error_type: type[Exception], pattern: str, probe_source: str,
) -> None:
    error = error_type(f"{_PRIVATE} {pattern}")
    calls: list[object] = []

    def fail_probe(query):
        calls.append(query.id)
        raise error

    def fail_builder(**kwargs):
        calls.append(kwargs)
        raise error

    kwargs = {"probe": fail_probe} if probe_source == "injected" else {"builder": fail_builder}
    queries = (_query("one", _GOOD, "HealthyDb"), _query("two", _GOOD, "HealthyDb"))
    check = _access(tmp_path, queries, **kwargs)
    assert check.status == "fail"
    assert check.detail.startswith(_PREFIX)
    assert check.detail.endswith(". Check access and retry.")
    assert _PRIVATE not in check.detail
    assert classify_message(check.detail).category == classify_exception(error).category
    assert calls == (["one"] if probe_source == "injected" else [{"log_failures": False}])
    assert check.metadata is not None
    assert check.metadata["query_count"] == 2
    assert check.metadata["cluster_targets"] == ["https://healthy.example.invalid/HealthyDb"]


def test_unexpected_builder_error_still_propagates(tmp_path: Path) -> None:
    def fail(**_kwargs):
        raise ValueError("synthetic unexpected failure")

    with pytest.raises(ValueError, match="synthetic unexpected failure"):
        _access(tmp_path, (_query("one", _GOOD, "HealthyDb"),), builder=fail)


def _isolate_other_gates(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_actual_doctor_and_operator_gate_do_not_expose_caught_error(
    configured_doctor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports, _calls = configured_doctor
    _isolate_other_gates(monkeypatch)

    def fail(_query):
        raise QueryError(f"{_PRIVATE} 403")

    report = doctor.run_doctor(
        edition_name="acme_weekly", check_auth=True, reports_root=reports, kusto_probe=fail,
    )
    gate_report = doctor.run_doctor(
        edition_name="acme_weekly", operator_gates=True, reports_root=reports, kusto_probe=fail,
    )
    access = next(check for check in report.checks if check.label == "Kusto Access")
    gate = next(check for check in gate_report.checks if check.label == "Gate:Kusto Validation")
    assert access.status == "fail" and _PRIVATE not in access.detail
    assert classify_message(access.detail).category == classify_exception(QueryError("403")).category
    assert gate.status == "fail" and _PRIVATE not in gate.detail
    assert gate.metadata is not None
    assert gate.metadata["kusto_access"]["detail"] == access.detail
    assert gate.metadata["action_category"] == "auth-admin-required"
    assert report.overall == "UNHEALTHY" and gate_report.overall == "UNHEALTHY"


@pytest.mark.parametrize("mode,label", [
    ("--check-auth", "Kusto Access"),
    ("--operator-gates", "Gate:Kusto Validation"),
])
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_actual_doctor_cli_omits_caught_builder_error(
    configured_doctor, isolated_cli_startup, monkeypatch: pytest.MonkeyPatch,
    mode: str, label: str, output_format: str,
) -> None:
    _isolate_other_gates(monkeypatch)

    def fail(**_kwargs):
        raise QueryError(f"{_PRIVATE} 403")

    monkeypatch.setattr(doctor, "build_live_kusto_query_probe", fail)
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "acme_weekly", mode, "--format", output_format],
    )
    assert result.exit_code == 1, result.output
    assert _PRIVATE not in result.output
    assert "matched pattern: '403'" in result.output
    if output_format == "json":
        payload = json.loads(result.output)
        assert next(row for row in payload["checks"] if row["label"] == label)["status"] == "fail"
    elif output_format == "csv":
        assert next(row for row in csv.DictReader(result.output.splitlines()) if row["label"] == label)["status"] == "fail"
    else:
        assert f"FAIL {label}" in result.output
