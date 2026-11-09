from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import traceback

import pytest
import yaml
from typer.testing import CliRunner

from cli import app
from src.commands import doctor
from src.commands.doctor_checks import metric_binding_checks
from src.commands.doctor_checks.models import DoctorReport
from src.core.exceptions import ConfigError
from src.core.metric_registry import load_metric_definition_map
from tests.unit.test_metric_binding_probe_error_privacy import _PRIVATE, scenario


_SAFE = "Cannot parse selected metric-binding registry YAML; check syntax and values."
_INVALID_YAML = (
    f"invalid: [{_PRIVATE}\n",
    f'invalid: "\\q{_PRIVATE}"\n',
    f'invalid: "\x01{_PRIVATE}"\n',
)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("surface", ["cli", "direct", "operator-gates"])
@pytest.mark.parametrize("content", _INVALID_YAML, ids=["parser", "scanner", "reader"])
def test_registry_yaml_error_omits_raw_detail_and_chain(
    scenario, surface: str, content: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text(content, encoding="utf-8")
    before = store.get_metric_source_binding("synthetic-binding")
    if surface == "direct":
        with pytest.raises(yaml.YAMLError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
        error = caught.value
    else:
        if surface == "operator-gates":
            monkeypatch.setattr(doctor, "_run_auth_doctor", lambda **_kwargs: DoctorReport("demo_weekly", ()))
            monkeypatch.setattr(doctor, "_run_channel_doctor", lambda **_kwargs: DoctorReport("demo_weekly", ()))
        flag = "--operator-gates" if surface == "operator-gates" else "--metric-bindings"
        result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", flag])
        assert result.exit_code == 1
        error = result.exception
    assert type(error) is yaml.YAMLError
    assert str(error) == _SAFE
    assert error.__cause__ is None and error.__context__ is None
    formatted = "".join(traceback.format_exception(error))
    assert _PRIVATE not in formatted
    assert str(path) not in formatted and path.as_posix() not in formatted
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_registry_yaml_error_is_safe_before_all_output_formats(scenario, output_format: str) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--metric-bindings", "--format", output_format],
    )
    assert result.exit_code == 1
    assert type(result.exception) is yaml.YAMLError
    assert str(result.exception) == _SAFE
    assert result.exception.__cause__ is None and result.exception.__context__ is None


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_registry_yaml_context_is_clear_inside_an_existing_handler(scenario) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    try:
        raise RuntimeError(f"{_PRIVATE} active handler")
    except RuntimeError:
        with pytest.raises(yaml.YAMLError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
    assert type(caught.value) is yaml.YAMLError
    assert str(caught.value) == _SAFE
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_direct_registry_loader_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    with pytest.raises(yaml.parser.ParserError) as caught:
        load_metric_definition_map(metrics_root=path.parent)
    assert _PRIVATE in str(caught.value)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_injected_metric_definitions_skip_registry_load(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")

    def fail_registry(*_args, **_kwargs):
        raise AssertionError("Injected definitions must skip registry loading")

    monkeypatch.setattr(metric_binding_checks, "load_metric_definition_map", fail_registry)
    before = store.get_metric_source_binding("synthetic-binding")
    report = doctor.run_doctor(
        edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
        programs_root=programs_root, reality_db_root=db_root, metric_definitions={},
        metric_binding_probe=lambda _binding: None, now=datetime(2000, 1, 2, tzinfo=timezone.utc),
    )
    assert any(check.label == "Metric Bindings" for check in report.checks)
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_registry_schema_error_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text("metrics: invalid\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert type(result.exception) is ConfigError
    assert str(path) in str(result.exception)
    assert result.exception.__cause__ is None


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_registry_encoding_failure_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_bytes(b"\xff")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert isinstance(result.exception, UnicodeDecodeError)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_registry_read_failure_remains_unchanged(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    failure = PermissionError(f"{_PRIVATE} read failure")
    read_text = Path.read_text

    def fail_registry_read(self, *args, **kwargs):
        if self == path:
            raise failure
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_registry_read)
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert result.exception is failure


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("error", [RuntimeError(f"{_PRIVATE} unexpected"), ConfigError(f"{_PRIVATE} config")])
def test_unexpected_registry_errors_propagate(scenario, error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    if isinstance(error, ConfigError):
        error.__cause__ = yaml.YAMLError(f"{_PRIVATE} chained")

    def fail_registry(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(metric_binding_checks, "load_metric_definition_map", fail_registry)
    with pytest.raises(type(error)) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is error


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_later_yaml_error_is_not_a_registry_load_error(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    error = yaml.YAMLError(f"{_PRIVATE} rollout")
    monkeypatch.setattr(metric_binding_checks, "load_kpi_queries", lambda *_args, **_kwargs: (object(),))

    def fail_rollout(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(metric_binding_checks, "_build_metric_rollout_status", fail_rollout)
    with pytest.raises(yaml.YAMLError) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is error


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_registry_yaml_traceback_omits_parser_detail(
    scenario, traceback_style: str, tmp_path: Path,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    path = programs_root.parent / "knowledge" / "metrics" / "demo.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    script = tmp_path / "synthetic_doctor_cli.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from pathlib import Path\n"
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *args, **kwargs: False\n"
        "import cli\n"
        "from src.commands import doctor\n"
        "cli._maybe_register_tier_decision_sink = lambda *args, **kwargs: None\n"
        "cli._maybe_run_scheduled_db_maintenance = lambda *args, **kwargs: None\n"
        "cli._maybe_run_session_catchup = lambda *args, **kwargs: None\n"
        f"doctor.EDITIONS_ROOT = Path({str(editions_root)!r})\n"
        f"doctor.PROGRAMS_ROOT = Path({str(programs_root)!r})\n"
        + ("cli.app.pretty_exceptions_show_locals = True\n" if traceback_style == "rich-locals" else "")
        + "sys.argv = ['vertex', 'doctor', '--edition', 'demo_weekly', '--metric-bindings']\n"
        + "cli.app()\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.upper().startswith((
            "VERTEX_", "ADO_", "AZURE_", "ICM_", "WORKIQ_", "GH_", "GITHUB_",
            "OPENAI_", "ANTHROPIC_", "AWS_", "KUSTO_",
        )):
            del environment[name]
    for name in (
        "APPDATA", "HOME", "LOCALAPPDATA", "TEMP", "TMP", "USERPROFILE",
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    ):
        environment[name] = str(tmp_path)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["VERTEX_DB_PATH"] = str(db_root)
    environment["VERTEX_PROGRAMS_ROOT"] = str(programs_root)
    if traceback_style == "standard":
        environment["TYPER_STANDARD_TRACEBACK"] = "1"
    else:
        environment.pop("TYPER_STANDARD_TRACEBACK", None)
        environment.pop("_TYPER_STANDARD_TRACEBACK", None)

    before = store.get_metric_source_binding("synthetic-binding")
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, env=environment,
        capture_output=True, timeout=90, check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == 1, output
    assert _SAFE in " ".join(output.split())
    assert _PRIVATE not in output
    assert str(path) not in output and path.as_posix() not in output
    if traceback_style == "rich-locals":
        assert "locals" in output.lower()
    assert store.get_metric_source_binding("synthetic-binding") == before
