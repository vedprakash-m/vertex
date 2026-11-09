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
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from tests.unit.test_metric_binding_probe_error_privacy import _PRIVATE, scenario


_SAFE = "Cannot parse selected metric-binding edition or program YAML; check syntax and values."


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("document", ["edition", "program", "workstreams", "scorecards"])
@pytest.mark.parametrize("surface", ["cli", "direct"])
def test_early_yaml_error_omits_raw_detail_and_chain(scenario, document: str, surface: str) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    paths = {
        "edition": editions_root / "demo_weekly.yaml",
        "program": programs_root / "demo" / "program.yaml",
        "workstreams": programs_root / "demo" / "workstreams.yaml",
        "scorecards": programs_root / "demo" / "scorecards.yaml",
    }
    path = paths[document]
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
def test_implicitly_discovered_edition_yaml_error_is_safe(scenario) -> None:
    _editions_root, programs_root, _db_root, store, calls, _source = scenario
    directory = programs_root / "demo" / "editions"
    directory.mkdir()
    path = directory / "demo_weekly.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    before = store.get_metric_source_binding("synthetic-binding")
    result = CliRunner().invoke(app, ["doctor", "--metric-bindings"])
    assert result.exit_code == 1
    assert type(result.exception) is ConfigError
    assert str(result.exception) == _SAFE
    assert result.exception.__cause__ is None and result.exception.__context__ is None
    formatted = "".join(traceback.format_exception(result.exception))
    assert _PRIVATE not in formatted
    assert str(path) not in formatted and path.as_posix() not in formatted
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_early_yaml_error_is_safe_before_all_output_formats(scenario, output_format: str) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--metric-bindings", "--format", output_format],
    )
    assert result.exit_code == 1
    assert type(result.exception) is ConfigError
    assert str(result.exception) == _SAFE
    assert result.exception.__cause__ is None and result.exception.__context__ is None


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_direct_edition_resolver_remains_unchanged(scenario) -> None:
    editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        resolve_edition("demo_weekly", editions_root=editions_root, programs_root=programs_root)
    assert isinstance(caught.value.__cause__, yaml.YAMLError)
    assert _PRIVATE in str(caught.value)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_unrelated_doctor_mode_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--kusto"])
    assert isinstance(result.exception, ConfigError)
    assert isinstance(result.exception.__cause__, yaml.YAMLError)
    assert _PRIVATE in str(result.exception)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_other_metric_binding_configuration_errors_remain_detailed(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert isinstance(result.exception, ConfigError)
    assert str(result.exception) == f"Expected mapping at top-level in {path}"
    assert result.exception.__cause__ is None


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_banner_yaml_error_is_not_a_resolution_error(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    error = ConfigError(f"{_PRIVATE} banner")
    error.__cause__ = yaml.YAMLError(f"{_PRIVATE} banner cause")

    def fail_banner(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(doctor, "_surface_alert_banner", fail_banner)
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert result.exception is error
    assert _PRIVATE in str(error) and isinstance(error.__cause__, yaml.YAMLError)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_encoding_failure_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_bytes(b"\xff")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--metric-bindings"])
    assert isinstance(result.exception, ConfigError)
    assert isinstance(result.exception.__cause__, UnicodeDecodeError)
    assert str(path) in str(result.exception)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_unexpected_resolution_error_propagates(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    error = RuntimeError(f"{_PRIVATE} unexpected")

    def fail_resolution(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(metric_binding_checks, "resolve_edition", fail_resolution)
    with pytest.raises(RuntimeError) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is error


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_early_yaml_context_is_clear_inside_an_existing_handler(scenario) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    original = RuntimeError(f"{_PRIVATE} active handler")
    try:
        raise original
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
@pytest.mark.parametrize(
    ("document", "explicit_edition"),
    [
        ("edition", True), ("program", True), ("workstreams", True),
        ("scorecards", True), ("edition", False),
    ],
)
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_early_yaml_traceback_omits_parser_detail(
    scenario, document: str, explicit_edition: bool, traceback_style: str, tmp_path: Path,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    nested_editions = programs_root / "demo" / "editions"
    nested_editions.mkdir()
    nested_edition = nested_editions / "demo_weekly.yaml"
    nested_edition.write_text((editions_root / "demo_weekly.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    path = {
        "edition": nested_edition,
        "program": programs_root / "demo" / "program.yaml",
        "workstreams": programs_root / "demo" / "workstreams.yaml",
        "scorecards": programs_root / "demo" / "scorecards.yaml",
    }[document]
    path.write_text(f"invalid: [{_PRIVATE}\n", encoding="utf-8")
    script = tmp_path / "synthetic_doctor_cli.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *args, **kwargs: False\n"
        "import cli\n"
        "cli._maybe_register_tier_decision_sink = lambda *args, **kwargs: None\n"
        "cli._maybe_run_scheduled_db_maintenance = lambda *args, **kwargs: None\n"
        "cli._maybe_run_session_catchup = lambda *args, **kwargs: None\n"
        + ("cli.app.pretty_exceptions_show_locals = True\n" if traceback_style == "rich-locals" else "")
        + (
            "sys.argv = ['vertex', 'doctor', '--edition', 'demo_weekly', '--metric-bindings']\n"
            if explicit_edition else
            "sys.argv = ['vertex', 'doctor', '--metric-bindings']\n"
        )
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
