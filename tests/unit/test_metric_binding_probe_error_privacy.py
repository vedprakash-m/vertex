from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor, metric
from src.commands.doctor_checks.models import DoctorReport
from src.core.ado_client import ADOClient
from src.core.exceptions import QueryError, QueryTimeoutError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception, classify_message, safe_failure_suffix
from src.core.kusto_client import KustoClient, KustoColumn
from src.core.metric_binding_validator import validate_metric_source_binding
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricSourceBinding
from src.core.reality_store import RealityStore
from tests.unit.test_commands_doctor import _seed_kb_layout


_PRIVATE = "SYNTHETIC_METRIC_PROVIDER_DETAIL"
_PATTERNS = tuple(pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns) + ("novel failure",)
_VALIDATED_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)
_DEFINITION = MetricDefinition(
    id="demo.count", title="Count", unit="count", aggregation=MetricAggregation.LAST,
)


def _binding(source_kind: str = "kusto", binding_id: str = "synthetic-binding") -> MetricSourceBinding:
    return MetricSourceBinding(
        binding_id=binding_id,
        metric_id=_DEFINITION.id,
        program_id="demo",
        source_kind=source_kind,
        cluster="https://synthetic.example.invalid",
        database="SyntheticDb",
        kql_template=(
            "Synthetic | summarize Count=count()" if source_kind == "kusto"
            else "SELECT [System.Id] FROM WorkItems"
        ),
        result_column="Count",
        validated=True,
        last_validated_at=_VALIDATED_AT,
    )


def _raise(error: Exception):
    def fail(_binding):
        raise error
    return fail


def _expected(error: Exception, source_kind: str) -> str:
    guidance = (
        "Check access, query and current iteration settings."
        if source_kind == "wiql" else "Check access and query settings."
    )
    return f"Metric binding probe failed{safe_failure_suffix(str(error))}. {guidance}"


@pytest.mark.parametrize("source_kind", ["kusto", "wiql"])
@pytest.mark.parametrize("error_type", [QueryError, QueryTimeoutError, ValueError])
@pytest.mark.parametrize("pattern", _PATTERNS)
def test_selected_probe_errors_have_only_fixed_taxonomy_details(
    source_kind: str, error_type: type[Exception], pattern: str,
) -> None:
    binding = _binding(source_kind, "binding-403")
    error = error_type(f"{_PRIVATE} {pattern}")
    safe_probe = metric._privacy_safe_metric_binding_probe(_raise(error))
    with pytest.raises(error_type) as caught:
        safe_probe(binding)
    assert str(caught.value) == _expected(error, source_kind)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _PRIVATE not in str(caught.value)
    assert classify_exception(caught.value).category == classify_exception(error).category
    assert classify_message(f"{binding.binding_id}: {caught.value}").category == classify_message(
        f"{binding.binding_id}: {error}"
    ).category


@pytest.mark.parametrize("error", [
    RuntimeError("synthetic unexpected problem"),
])
def test_other_probe_errors_propagate_unchanged(error: Exception) -> None:
    safe_probe = metric._privacy_safe_metric_binding_probe(_raise(error))
    with pytest.raises(type(error)) as caught:
        safe_probe(_binding())
    assert caught.value is error


def test_direct_validator_api_preserves_probe_exception() -> None:
    error = QueryTimeoutError(f"{_PRIVATE} timeout")
    with pytest.raises(QueryTimeoutError) as caught:
        validate_metric_source_binding(
            _binding(), metric_definitions={_DEFINITION.id: _DEFINITION},
            probe=_raise(error), validated_at=datetime.now(timezone.utc),
        )
    assert caught.value is error


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "demo.yaml").write_text(
        "metrics:\n"
        "  - id: demo.count\n"
        "    title: Count\n"
        "    unit: count\n"
        "    aggregation: last\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(metric, "METRICS_ROOT", metrics_root)
    calls: list[tuple[str, str]] = []

    def execute(database, kql, **_kwargs):
        calls.append((database, kql))
        raise RuntimeError(_PRIVATE)

    monkeypatch.setattr(
        KustoClient, "_get_or_create_client", lambda _self, _cluster: SimpleNamespace(execute=execute),
    )
    monkeypatch.setattr(KustoClient, "_build_request_properties", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ADOClient, "_init_auth", lambda _self: None)

    def ado_request(_self, method, url, **_kwargs):
        calls.append((method, url))
        if request.param == "wiql-malformed":
            return SimpleNamespace(
                status_code=200, headers={}, json=lambda: {"workItems": [{"id": _PRIVATE}]},
            )
        return SimpleNamespace(status_code=403, headers={}, text=_PRIVATE)

    monkeypatch.setattr(ADOClient, "_request_with_progress", ado_request)
    db_root = tmp_path / "vertex-db"
    store = RealityStore("demo", db_root=db_root)
    store.initialize()
    store.upsert_metric_source_binding(_binding("kusto" if request.param == "kusto" else "wiql"))
    return editions_root, programs_root, db_root, store, calls, request.param


@pytest.mark.parametrize("scenario", ["kusto", "wiql", "wiql-malformed"], indirect=True)
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_actual_doctor_formats_contain_selected_probe_errors(
    scenario, output_format: str,
) -> None:
    _editions_root, _programs_root, _db_root, store, calls, source = scenario
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--metric-bindings", "--format", output_format],
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert _PRIVATE not in result.output
    assert "synthetic-binding: Metric binding probe failed" in result.output
    if source == "wiql":
        assert "matched pattern: '403'" in result.output
    assert len(calls) == 1
    updated = store.get_metric_source_binding("synthetic-binding")
    assert updated is not None and not updated.validated and updated.last_validated_at == _VALIDATED_AT
    if output_format == "json":
        checks = {row["label"]: row for row in json.loads(result.output)["checks"]}
        assert checks["Metric Bindings"]["status"] == "warn"
        assert checks["Binding Revalidation"]["status"] == "warn"
        assert checks["Metric Bindings"]["metadata"]["revalidation_failures"][0]["binding_id"] == "synthetic-binding"
        assert _PRIVATE not in json.dumps(checks["Metric Bindings"]["metadata"])
    elif output_format == "csv":
        checks = {row["label"]: row for row in csv.DictReader(result.output.splitlines())}
        assert checks["Metric Bindings"]["status"] == "warn"
        assert checks["Binding Revalidation"]["status"] == "warn"
        assert _PRIVATE not in checks["Metric Bindings"]["metadata_json"]
    else:
        assert "WARN Binding Revalidation" in result.output


@pytest.mark.parametrize("scenario", ["kusto", "wiql", "wiql-malformed"], indirect=True)
def test_actual_admin_validation_contains_selected_probe_errors(scenario) -> None:
    _editions_root, programs_root, db_root, store, calls, source = scenario
    result = CliRunner().invoke(app, [
        "admin", "metric", "validate", "--program", "demo",
        "--binding-id", "synthetic-binding", "--db-root", str(db_root),
        "--programs-root", str(programs_root),
    ])
    assert result.exit_code == 2, (result.output, result.exception)
    assert _PRIVATE not in result.output
    assert "synthetic-binding: Metric binding probe failed" in result.output
    if source == "wiql":
        assert "matched pattern: '403'" in result.output
    assert len(calls) == 1
    updated = store.get_metric_source_binding("synthetic-binding")
    assert updated is not None and not updated.validated and updated.last_validated_at == _VALIDATED_AT


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_missing_iteration_uses_general_guidance(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    original = store.get_metric_source_binding("synthetic-binding")
    assert original is not None
    missing_iteration = replace(
        original,
        kql_template=(
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.IterationPath] = '{current_iteration_path}'"
        ),
    )
    store.upsert_metric_source_binding(missing_iteration)
    monkeypatch.setattr(
        ADOClient, "_request_with_progress",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, json=lambda: {"value": []}),
    )
    report = doctor.run_doctor(
        edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
        programs_root=programs_root, reality_db_root=db_root,
        metric_definitions={_DEFINITION.id: _DEFINITION},
    )
    checks = {check.label: check for check in report.checks}
    assert "Current iteration path unavailable" not in checks["Binding Revalidation"].detail
    assert "Check access, query and current iteration settings." in checks["Binding Revalidation"].detail
    store.upsert_metric_source_binding(missing_iteration)
    result = CliRunner().invoke(app, [
        "admin", "metric", "validate", "--program", "demo",
        "--binding-id", "synthetic-binding", "--db-root", str(db_root),
        "--programs-root", str(programs_root),
    ])
    assert result.exit_code == 2, (result.output, result.exception)
    assert "Current iteration path unavailable" not in result.output
    assert "Check access, query and current iteration settings." in result.output


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
def test_validator_schema_and_field_errors_remain_detailed(scenario) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    report = doctor.run_doctor(
        edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
        programs_root=programs_root, reality_db_root=db_root,
        metric_definitions={_DEFINITION.id: _DEFINITION},
        metric_binding_probe=lambda _binding: ([], (KustoColumn("Other", "long"),)),
    )
    checks = {check.label: check for check in report.checks}
    expected = "result_column 'Count' was not returned for binding synthetic-binding."
    assert expected in checks["Binding Revalidation"].detail
    assert checks["Metric Bindings"].metadata["revalidation_failures"][0]["error"] == expected

    original = store.get_metric_source_binding("synthetic-binding")
    assert original is not None
    store.upsert_metric_source_binding(replace(original, validated=True, result_column=None))
    report = doctor.run_doctor(
        edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
        programs_root=programs_root, reality_db_root=db_root,
        metric_definitions={_DEFINITION.id: _DEFINITION},
        metric_binding_probe=lambda _binding: pytest.fail("invalid fields must not reach the probe"),
    )
    checks = {check.label: check for check in report.checks}
    assert "Binding synthetic-binding is missing result_column." in checks["Binding Revalidation"].detail


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
def test_injected_doctor_probe_failure_and_success_preserve_state_and_iteration(scenario) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    store.upsert_metric_source_binding(_binding(binding_id="second-binding"))
    calls: list[str] = []

    def probe(binding):
        calls.append(binding.binding_id)
        if binding.binding_id == "synthetic-binding":
            raise QueryError(f"{_PRIVATE} 403")
        return [{"Count": 1}], (KustoColumn("Count", "long"),)

    report = doctor.run_doctor(
        edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
        programs_root=programs_root, reality_db_root=db_root,
        metric_definitions={_DEFINITION.id: _DEFINITION}, metric_binding_probe=probe,
    )
    checks = {check.label: check for check in report.checks}
    failures = checks["Metric Bindings"].metadata["revalidation_failures"]
    assert sorted(calls) == ["second-binding", "synthetic-binding"]
    assert len(failures) == 1 and failures[0]["binding_id"] == "synthetic-binding"
    assert failures[0]["error"] == _expected(QueryError(f"{_PRIVATE} 403"), "kusto")
    assert _PRIVATE not in checks["Binding Revalidation"].detail
    failed = store.get_metric_source_binding("synthetic-binding")
    succeeded = store.get_metric_source_binding("second-binding")
    assert failed is not None and not failed.validated and failed.last_validated_at == _VALIDATED_AT
    assert succeeded is not None and succeeded.validated and succeeded.last_validated_at != _VALIDATED_AT
    assert succeeded.last_validated_kql_hash


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
def test_admin_all_continues_after_failure(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    _editions_root, programs_root, db_root, store, _calls, _source = scenario
    store.upsert_metric_source_binding(_binding(binding_id="second-binding"))
    calls: list[str] = []

    def probe(binding):
        calls.append(binding.binding_id)
        if binding.binding_id == "synthetic-binding":
            raise QueryTimeoutError(f"{_PRIVATE} query timeout")
        return [{"Count": 1}], (KustoColumn("Count", "long"),)

    monkeypatch.setattr(metric, "_live_metric_binding_probe", lambda: probe)
    result = CliRunner().invoke(app, [
        "admin", "metric", "validate", "--program", "demo", "--all",
        "--db-root", str(db_root), "--programs-root", str(programs_root),
    ])
    assert result.exit_code == 2, (result.output, result.exception)
    assert _PRIVATE not in result.output
    assert "Validated binding second-binding for metric demo.count." in result.output
    assert "synthetic-binding: Metric binding probe failed" in result.output
    assert set(calls) == {"synthetic-binding", "second-binding"} and len(calls) == 2
    failed = store.get_metric_source_binding("synthetic-binding")
    succeeded = store.get_metric_source_binding("second-binding")
    assert failed is not None and not failed.validated
    assert succeeded is not None and succeeded.validated and succeeded.last_validated_at != _VALIDATED_AT


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
@pytest.mark.parametrize("error_type", [RuntimeError])
def test_unhandled_doctor_probe_errors_do_not_change_binding(scenario, error_type: type[Exception]) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    error = error_type("synthetic uncaught failure")
    with pytest.raises(error_type) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
            metric_definitions={_DEFINITION.id: _DEFINITION}, metric_binding_probe=_raise(error),
        )
    assert caught.value is error
    original = store.get_metric_source_binding("synthetic-binding")
    assert original is not None and original.validated and original.last_validated_at == _VALIDATED_AT


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
def test_operator_gate_receives_safe_metric_failure(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    def empty_report(**_kwargs):
        return DoctorReport(edition="demo_weekly", checks=())

    monkeypatch.setattr(doctor, "_run_auth_doctor", empty_report)
    monkeypatch.setattr(doctor, "_run_channel_doctor", empty_report)
    report = doctor.run_doctor(
        edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
        programs_root=programs_root, reports_root=programs_root.parent / "reports",
        archive_root=programs_root.parent / "archive", reality_db_root=db_root,
        metric_definitions={_DEFINITION.id: _DEFINITION},
        metric_binding_probe=_raise(QueryError(f"{_PRIVATE} 403")),
    )
    checks = {check.label: check for check in report.checks}
    gate = checks["Gate:Kusto Validation"]
    assert gate.metadata["metric_bindings"]["status"] == "warn"
    assert _PRIVATE not in repr(report)
    failed = store.get_metric_source_binding("synthetic-binding")
    assert failed is not None and not failed.validated
