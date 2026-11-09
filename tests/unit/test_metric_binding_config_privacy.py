from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor, metric
from src.commands.doctor_checks import metric_binding_checks
from src.core.exceptions import ConfigError, PersonaSchemaError
from src.core.failure_taxonomy import classify_exception, classify_message, safe_failure_suffix
from src.core.kusto_client import KustoColumn
from src.core.metric_binding_validator import validate_metric_source_binding
from src.core.yaml_utils import load_yaml_mapping
from tests.unit.test_metric_binding_probe_error_privacy import (
    _DEFINITION, _PATTERNS, _PRIVATE, _VALIDATED_AT, _binding, _expected, _raise, scenario,
)


@pytest.mark.parametrize("source_kind", ["kusto", "wiql"])
@pytest.mark.parametrize("error_type", [ConfigError, PersonaSchemaError])
@pytest.mark.parametrize("pattern", _PATTERNS)
def test_selected_probe_config_errors_use_safe_taxonomy_details(
    source_kind: str, error_type: type[ConfigError], pattern: str,
) -> None:
    binding = _binding(source_kind, "binding-403")
    original = error_type(f"{_PRIVATE} {pattern}")
    safe_probe = metric._privacy_safe_metric_binding_probe(_raise(original))
    with pytest.raises(ConfigError) as caught:
        safe_probe(binding)
    assert type(caught.value) is ConfigError
    assert caught.value is not original
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert str(caught.value) == _expected(original, source_kind)
    assert _PRIVATE not in str(caught.value)
    assert classify_exception(caught.value) == classify_exception(original)
    assert classify_message(f"{binding.binding_id}: {caught.value}") == classify_message(
        f"{binding.binding_id}: {original}"
    )


def test_direct_validator_keeps_original_config_error() -> None:
    error = ConfigError(f"{_PRIVATE} invalid configuration")
    with pytest.raises(ConfigError) as caught:
        validate_metric_source_binding(
            _binding(), metric_definitions={_DEFINITION.id: _DEFINITION},
            probe=_raise(error), validated_at=_VALIDATED_AT,
        )
    assert caught.value is error


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_actual_admin_program_yaml_error_has_no_path_or_parser_content(
    scenario, tmp_path: Path,
) -> None:
    _editions_root, programs_root, db_root, store, calls, _source = scenario
    program_path = programs_root / "demo" / "program.yaml"
    program_path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as original:
        load_yaml_mapping(program_path)
    result = CliRunner().invoke(app, [
        "admin", "metric", "validate", "--program", "demo",
        "--binding-id", "synthetic-binding", "--db-root", str(db_root),
        "--programs-root", str(programs_root),
    ])
    assert result.exit_code == 2, (result.output, result.exception)
    assert _PRIVATE not in result.output
    assert str(tmp_path) not in result.output
    assert f"Metric binding probe failed{safe_failure_suffix(str(original.value))}." in result.output
    assert "Check access, query and current iteration settings." in result.output
    assert calls == []
    binding = store.get_metric_source_binding("synthetic-binding")
    assert binding is not None and not binding.validated and binding.last_validated_at == _VALIDATED_AT


@pytest.mark.parametrize("scenario", ["kusto", "wiql"], indirect=True)
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_doctor_selected_probe_config_error_is_safe_in_every_format(
    scenario, output_format: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _editions_root, _programs_root, _db_root, store, calls, source_kind = scenario
    original = ConfigError(f"{_PRIVATE} 403")
    monkeypatch.setattr(
        metric_binding_checks, "_build_program_metric_binding_probe",
        lambda *_args, **_kwargs: _raise(original),
    )
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--metric-bindings", "--format", output_format],
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert _PRIVATE not in result.output
    assert f"synthetic-binding: {_expected(original, source_kind)}" in result.output
    assert calls == []
    binding = store.get_metric_source_binding("synthetic-binding")
    assert binding is not None and not binding.validated and binding.last_validated_at == _VALIDATED_AT
    if output_format == "json":
        checks = {row["label"]: row for row in json.loads(result.output)["checks"]}
        assert checks["Metric Bindings"]["status"] == "warn"
        assert checks["Binding Revalidation"]["status"] == "warn"
        assert checks["Metric Bindings"]["metadata"]["revalidation_failures"] == [{
            "binding_id": "synthetic-binding", "error": _expected(original, source_kind),
        }]
    elif output_format == "csv":
        checks = {row["label"]: row for row in csv.DictReader(result.output.splitlines())}
        assert checks["Metric Bindings"]["status"] == "warn"
        assert checks["Binding Revalidation"]["status"] == "warn"
        assert _PRIVATE not in checks["Metric Bindings"]["metadata_json"]
    else:
        assert "WARN Binding Revalidation" in result.output


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
@pytest.mark.parametrize("mode", ["doctor", "admin"])
def test_config_failure_keeps_successful_bindings_and_failure_count(
    scenario, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    store.upsert_metric_source_binding(_binding(binding_id="second-binding"))
    calls: list[str] = []

    def probe(binding):
        calls.append(binding.binding_id)
        if binding.binding_id == "synthetic-binding":
            raise ConfigError(f"{_PRIVATE} invalid config")
        return [{"Count": 1}], (KustoColumn("Count", "long"),)

    if mode == "doctor":
        report = doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
            metric_definitions={_DEFINITION.id: _DEFINITION}, metric_binding_probe=probe,
        )
        checks = {check.label: check for check in report.checks}
        failures = checks["Metric Bindings"].metadata["revalidation_failures"]
        assert len(failures) == 1 and failures[0]["binding_id"] == "synthetic-binding"
        assert _PRIVATE not in repr(report)
    else:
        monkeypatch.setattr(metric, "_live_metric_binding_probe", lambda: probe)
        result = CliRunner().invoke(app, [
            "admin", "metric", "validate", "--program", "demo", "--all",
            "--db-root", str(db_root), "--programs-root", str(programs_root),
        ])
        assert result.exit_code == 2, (result.output, result.exception)
        assert "Validated binding second-binding for metric demo.count." in result.output
        assert _PRIVATE not in result.output
    assert sorted(calls) == ["second-binding", "synthetic-binding"]
    failed = store.get_metric_source_binding("synthetic-binding")
    succeeded = store.get_metric_source_binding("second-binding")
    assert failed is not None and not failed.validated and failed.last_validated_at == _VALIDATED_AT
    assert succeeded is not None and succeeded.validated and succeeded.last_validated_at != _VALIDATED_AT
