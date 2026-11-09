from __future__ import annotations

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
from src.core.kusto_query_loader import load_kpi_queries
from tests.unit.test_metric_binding_probe_error_privacy import _PRIVATE, scenario


_SAFE = "Cannot parse selected metric-binding edition or program YAML; check syntax and values."


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("surface", ["cli", "direct"])
def test_kpi_yaml_error_omits_raw_detail_and_chain(scenario, surface: str) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    path = programs_root / "demo" / "kpis.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    before = store.get_metric_source_binding("synthetic-binding")
    if surface == "cli":
        result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
        assert result.exit_code == 1
        error = result.exception
    else:
        with pytest.raises(ConfigError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
        error = caught.value
    assert type(error) is ConfigError
    assert str(error) == _SAFE
    assert error.__cause__ is None and error.__context__ is None
    formatted = "".join(traceback.format_exception(error))
    assert _PRIVATE not in formatted
    assert str(path) not in formatted and path.as_posix() not in formatted
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_kpi_yaml_error_is_safe_before_all_output_formats(scenario, output_format: str) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    (programs_root / "demo" / "kpis.yaml").write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--metric-bindings", "--format", output_format],
    )
    assert result.exit_code == 1
    assert type(result.exception) is ConfigError
    assert str(result.exception) == _SAFE
    assert result.exception.__cause__ is None and result.exception.__context__ is None


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_kpi_yaml_context_is_clear_inside_an_existing_handler(scenario) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    (programs_root / "demo" / "kpis.yaml").write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    try:
        raise RuntimeError(f"{_PRIVATE} active handler")
    except RuntimeError:
        with pytest.raises(ConfigError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
    assert str(caught.value) == _SAFE
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_operator_gates_reaches_metric_binding_kpi_boundary(
    scenario, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _editions_root, programs_root, _db_root, store, calls, _source = scenario
    (programs_root / "demo" / "kpis.yaml").write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_run_auth_doctor", lambda **_kwargs: DoctorReport("demo_weekly", ()))
    monkeypatch.setattr(doctor, "_run_channel_doctor", lambda **_kwargs: DoctorReport("demo_weekly", ()))
    before = store.get_metric_source_binding("synthetic-binding")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
    assert result.exit_code == 1
    assert type(result.exception) is ConfigError
    assert str(result.exception) == _SAFE
    assert result.exception.__cause__ is None and result.exception.__context__ is None
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_direct_kpi_loader_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "kpis.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_kpi_queries("demo", programs_root=programs_root)
    assert isinstance(caught.value.__cause__, yaml.YAMLError)
    assert _PRIVATE in str(caught.value) and str(path) in str(caught.value)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_unrelated_kusto_doctor_mode_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    (programs_root / "demo" / "kpis.yaml").write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--kusto"])
    assert result.exit_code == 1
    assert _PRIVATE in result.output


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_non_yaml_kpi_configuration_error_remains_detailed(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "kpis.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert isinstance(result.exception, ConfigError)
    assert str(result.exception) == f"Expected mapping at top-level in {path}"
    assert result.exception.__cause__ is None


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_kpi_encoding_failure_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "kpis.yaml"
    path.write_bytes(b"\xff")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert isinstance(result.exception, ConfigError)
    assert isinstance(result.exception.__cause__, UnicodeDecodeError)
    assert str(path) in str(result.exception)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_kpi_read_failure_remains_unchanged(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "kpis.yaml"
    path.write_text("kpis: []\n", encoding="utf-8")
    failure = PermissionError(f"{_PRIVATE} read failure")
    read_text = Path.read_text

    def fail_kpi_read(self, *args, **kwargs):
        if self == path:
            raise failure
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_kpi_read)
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert isinstance(result.exception, ConfigError)
    assert result.exception.__cause__ is failure
    assert _PRIVATE in str(result.exception) and str(path) in str(result.exception)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_rollout_yaml_caused_error_is_not_a_query_load_error(
    scenario, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    error = ConfigError(f"{_PRIVATE} rollout")
    error.__cause__ = yaml.YAMLError(f"{_PRIVATE} rollout cause")
    monkeypatch.setattr(metric_binding_checks, "load_kpi_queries", lambda *_args, **_kwargs: (object(),))

    def fail_rollout(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(metric_binding_checks, "_build_metric_rollout_status", fail_rollout)
    with pytest.raises(ConfigError) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is error
    assert _PRIVATE in str(error) and isinstance(error.__cause__, yaml.YAMLError)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_unexpected_kpi_load_error_propagates(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    error = RuntimeError(f"{_PRIVATE} unexpected")

    def fail_load(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(metric_binding_checks, "load_kpi_queries", fail_load)
    with pytest.raises(RuntimeError) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is error


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_kpi_yaml_traceback_omits_parser_detail(
    scenario, traceback_style: str, tmp_path: Path,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    path = programs_root / "demo" / "kpis.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
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
