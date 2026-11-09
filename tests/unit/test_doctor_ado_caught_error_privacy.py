from __future__ import annotations

import csv
import json
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor
from src.commands.doctor_checks import doctor_support_checks
from src.commands.doctor_checks.models import ADOProbeResult
from src.core.ado_client import ADOClient, ADO_RESOURCE
from src.core.exceptions import AuthError, QueryError, QueryTimeoutError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception, classify_message, safe_failure_suffix
from tests.unit.test_doctor_kusto_probe_result import configured_doctor, repo_root


_PRIVATE = "SYNTHETIC_ADO_PROVIDER_DETAIL"
_REAL_PROBE = doctor._probe_ado_access
_PATTERNS = tuple(pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns) + ("novel failure",)


def _bundle():
    return SimpleNamespace(config=SimpleNamespace(ado=SimpleNamespace(
        organization="synthetic-org",
        project="synthetic-project",
        api_timeout_seconds=3,
        date_window_days=7,
        area_paths=("Example\\Area",),
        work_item_types=("Task",),
        excluded_states=("Closed",),
    )))


@pytest.mark.parametrize("stage", ["constructor", "query", "credential"])
@pytest.mark.parametrize("error_type", [AuthError, QueryError, QueryTimeoutError])
@pytest.mark.parametrize("pattern", _PATTERNS)
def test_ado_probe_caught_errors_preserve_safe_classification(
    monkeypatch: pytest.MonkeyPatch, stage: str, error_type: type[Exception], pattern: str,
) -> None:
    error = error_type(f"{_PRIVATE} {pattern}")

    class Credential:
        def get_token(self, _resource):
            raise error

    class Client:
        def __init__(self, **_kwargs):
            if stage == "constructor":
                raise error
            self.auth_method = "synthetic"
            self._credential = Credential() if stage == "credential" else None

        def query_all(self, **_kwargs):
            if stage == "query":
                raise error
            return []

    monkeypatch.setattr(doctor_support_checks, "ADOClient", Client)
    result = doctor_support_checks.probe_ado_access(_bundle())
    original = f"ADO probe failed: {error}"
    assert result == ADOProbeResult(
        False, "unknown", None, None,
        f"ADO probe failed{safe_failure_suffix(original)}. Check access and retry.",
    )
    assert _PRIVATE not in result.detail
    assert classify_message(result.detail).category == classify_exception(original).category


def _simulate_response(monkeypatch: pytest.MonkeyPatch, status: int, calls: list[str]) -> None:
    monkeypatch.setattr(ADOClient, "_init_auth", lambda _self: None)

    def request(_self, method, url, **_kwargs):
        calls.append(f"{method} {url}")
        return SimpleNamespace(
            status_code=status,
            headers={"WWW-Authenticate": f"{_PRIVATE} 401"},
            text=f"{_PRIVATE} 403",
        )

    monkeypatch.setattr(ADOClient, "_request_with_progress", request)


@pytest.mark.parametrize("status", [401, 403])
def test_actual_ado_client_response_is_contained(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    calls: list[str] = []
    _simulate_response(monkeypatch, status, calls)
    result = doctor_support_checks.probe_ado_access(_bundle())
    assert result == ADOProbeResult(
        False, "unknown", None, None,
        f"ADO probe failed; matched pattern: '{status}'. Check access and retry.",
    )
    assert _PRIVATE not in result.detail and "synthetic-org" not in result.detail
    assert len(calls) == 1 and calls[0].startswith("GET ")


def test_successful_ado_probe_retains_details_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    resources: list[str] = []

    class Credential:
        def get_token(self, resource):
            resources.append(resource)
            return SimpleNamespace(expires_on=time.time() + 1800)

    def init_auth(client):
        client.auth_method = "synthetic"
        client._credential = Credential()

    monkeypatch.setattr(ADOClient, "_init_auth", init_auth)
    monkeypatch.setattr(
        ADOClient, "_request_with_progress",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=200, json=lambda: {"value": [{"WorkItemId": 1}]},
        ),
    )
    result = doctor_support_checks.probe_ado_access(_bundle())
    assert result.reachable and result.auth_method == "synthetic" and result.item_count == 1
    assert result.token_minutes_remaining is not None and 28 <= result.token_minutes_remaining <= 30
    assert result.detail == "synthetic-org/synthetic-project reachable (auth: synthetic, 1 sampled items in scope)"
    assert resources == [ADO_RESOURCE]


def test_unexpected_ado_probe_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ADOClient, "_init_auth", lambda _self: None)

    def fail(*_args, **_kwargs):
        raise ValueError("synthetic unexpected failure")

    monkeypatch.setattr(ADOClient, "_request_with_progress", fail)
    with pytest.raises(ValueError, match="synthetic unexpected failure"):
        doctor_support_checks.probe_ado_access(_bundle())


def test_injected_ado_probe_result_remains_unchanged(configured_doctor) -> None:
    reports, _calls = configured_doctor
    injected = ADOProbeResult(False, "custom", None, None, "synthetic injected detail")
    report = doctor.run_doctor(
        edition_name="acme_weekly", check_auth=True, reports_root=reports,
        ado_probe=lambda _bundle: injected,
    )
    check = next(check for check in report.checks if check.label == "ADO Access")
    assert check.status == "fail" and check.detail == injected.detail


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("mode", ["default", "check-auth"])
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_actual_ado_doctor_cli_omits_response_details(
    configured_doctor, isolated_cli_startup, monkeypatch: pytest.MonkeyPatch,
    status: int, mode: str, output_format: str,
) -> None:
    monkeypatch.setattr(doctor, "_probe_ado_access", _REAL_PROBE)
    calls: list[str] = []
    _simulate_response(monkeypatch, status, calls)
    args = ["doctor", "--edition", "acme_weekly", "--format", output_format]
    if mode == "check-auth":
        args.append("--check-auth")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, (result.output, result.exception)
    assert _PRIVATE not in result.output
    assert f"ADO probe failed; matched pattern: '{status}'. Check access and retry." in result.output
    assert len(calls) == 1
    if output_format == "json":
        checks = {row["label"]: row for row in json.loads(result.output)["checks"]}
        assert checks["ADO Access"]["status"] == "fail" and checks["Token"]["status"] == "fail"
    elif output_format == "csv":
        checks = {row["label"]: row for row in csv.DictReader(result.output.splitlines())}
        assert checks["ADO Access"]["status"] == "fail" and checks["Token"]["status"] == "fail"
    else:
        assert "FAIL ADO Access" in result.output and "FAIL Token" in result.output
