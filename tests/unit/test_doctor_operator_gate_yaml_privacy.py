from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import traceback
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

with patch("dotenv.load_dotenv", return_value=False):
    import cli

from src.commands import doctor
from src.commands.doctor_checks.models import DoctorReport
from src.core.exceptions import ConfigError, PersonaSchemaError
from tests.unit.test_metric_binding_probe_error_privacy import _PRIVATE, scenario


app = cli.app
_SAFE = "Cannot parse selected operator-gates YAML; check syntax and values."
_INVALID_YAML = (
    f"invalid: [{_PRIVATE}\n",
    f'invalid: "\\q{_PRIVATE}"\n',
    f'invalid: "\x01{_PRIVATE}"\n',
)


@pytest.fixture(autouse=True)
def _disable_cli_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_maybe_register_tier_decision_sink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_maybe_run_scheduled_db_maintenance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_maybe_run_session_catchup", lambda *_args, **_kwargs: None)


def _assert_safe(error: BaseException | None, path: Path | None = None) -> None:
    assert type(error) is ConfigError
    assert str(error) == _SAFE
    assert error.__cause__ is None and error.__context__ is None
    formatted = "".join(traceback.format_exception(error))
    assert _PRIVATE not in formatted
    if path is not None:
        assert str(path) not in formatted and path.as_posix() not in formatted


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("document", ["edition", "program", "workstreams", "scorecards"])
@pytest.mark.parametrize("surface", ["cli", "direct"])
def test_early_operator_gate_yaml_error_is_safe(
    scenario, document: str, surface: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    path = {
        "edition": editions_root / "demo_weekly.yaml",
        "program": programs_root / "demo" / "program.yaml",
        "workstreams": programs_root / "demo" / "workstreams.yaml",
        "scorecards": programs_root / "demo" / "scorecards.yaml",
    }[document]
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    before = store.get_metric_source_binding("synthetic-binding")

    def fail_if_reached(**_kwargs):
        pytest.fail("A failed initial resolution must not run a later stage")

    monkeypatch.setattr(doctor, "_run_auth_doctor", fail_if_reached)
    monkeypatch.setattr(doctor, "_run_channel_doctor", fail_if_reached)
    if surface == "cli":
        result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
        assert result.exit_code == 1
        error = result.exception
    else:
        with pytest.raises(ConfigError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
        error = caught.value
    _assert_safe(error, path)
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("content", _INVALID_YAML, ids=["parser", "scanner", "reader"])
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_operator_gate_yaml_error_is_safe_before_all_formats(scenario, content: str, output_format: str) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text(content, encoding="utf-8")
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--operator-gates", "--format", output_format],
    )
    assert result.exit_code == 1
    _assert_safe(result.exception, path)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_implicitly_discovered_operator_gate_edition_is_safe(scenario) -> None:
    _editions_root, programs_root, _db_root, store, calls, _source = scenario
    directory = programs_root / "demo" / "editions"
    directory.mkdir()
    path = directory / "demo_weekly.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    before = store.get_metric_source_binding("synthetic-binding")
    result = CliRunner().invoke(app, ["doctor", "--operator-gates"])
    assert result.exit_code == 1
    _assert_safe(result.exception, path)
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_operator_gate_yaml_context_is_clear_inside_an_existing_handler(scenario) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    try:
        raise RuntimeError(f"{_PRIVATE} active handler")
    except RuntimeError:
        with pytest.raises(ConfigError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
    _assert_safe(caught.value, path)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("stage", ["auth", "channel"])
@pytest.mark.parametrize("surface", ["cli", "direct"])
def test_early_stage_yaml_error_is_safe_without_changing_stage_order(
    scenario, stage: str, surface: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    before = store.get_metric_source_binding("synthetic-binding")
    stage_calls: list[str] = []
    original = PersonaSchemaError(f"{_PRIVATE} stage")
    original.__cause__ = yaml.YAMLError(f"{_PRIVATE} parser")

    def auth(**_kwargs):
        stage_calls.append("auth")
        if stage == "auth":
            raise original
        return DoctorReport("demo_weekly", ())

    def channel(**_kwargs):
        stage_calls.append("channel")
        raise original

    monkeypatch.setattr(doctor, "_run_auth_doctor", auth)
    monkeypatch.setattr(doctor, "_run_channel_doctor", channel)
    if surface == "cli":
        result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
        assert result.exit_code == 1
        error = result.exception
    else:
        with pytest.raises(ConfigError) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
            )
        error = caught.value
    _assert_safe(error)
    assert error is not original
    assert stage_calls == (["auth"] if stage == "auth" else ["auth", "channel"])
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("kind", ["config", "nested-yaml", "raw-yaml", "read", "unexpected"])
def test_non_direct_yaml_errors_keep_their_identity(scenario, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    errors = {
        "config": ConfigError(f"{_PRIVATE} config"),
        "nested-yaml": ConfigError(f"{_PRIVATE} nested"),
        "raw-yaml": yaml.YAMLError(f"{_PRIVATE} raw"),
        "read": PermissionError(f"{_PRIVATE} read"),
        "unexpected": RuntimeError(f"{_PRIVATE} unexpected"),
    }
    original = errors[kind]
    if kind == "nested-yaml":
        cause = RuntimeError(f"{_PRIVATE} intermediate")
        cause.__cause__ = yaml.YAMLError(f"{_PRIVATE} parser")
        original.__cause__ = cause

    def fail(**_kwargs):
        raise original

    monkeypatch.setattr(doctor, "_run_auth_doctor", fail)
    with pytest.raises(type(original)) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is original


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("stage", ["metric", "checkpoint"])
def test_later_operator_gate_yaml_errors_remain_unchanged(
    scenario, stage: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    original = ConfigError(f"{_PRIVATE} later")
    original.__cause__ = yaml.YAMLError(f"{_PRIVATE} parser")

    def ok(**_kwargs):
        return DoctorReport("demo_weekly", ())

    def fail(**_kwargs):
        raise original

    monkeypatch.setattr(doctor, "_run_auth_doctor", ok)
    monkeypatch.setattr(doctor, "_run_channel_doctor", ok)
    monkeypatch.setattr(doctor, "_run_metric_binding_doctor", fail if stage == "metric" else ok)
    monkeypatch.setattr(doctor, "_run_checkpoint_doctor", fail)
    with pytest.raises(ConfigError) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value is original


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_operator_gate_banner_yaml_error_remains_unchanged(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ConfigError(f"{_PRIVATE} banner")
    original.__cause__ = yaml.YAMLError(f"{_PRIVATE} parser")

    def fail(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(doctor, "_surface_alert_banner", fail)
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
    assert result.exception is original


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("mode", ["--check-auth", "--channels"])
@pytest.mark.parametrize("document", ["edition", "program"])
def test_standalone_doctor_modes_remain_unchanged(scenario, mode: str, document: str) -> None:
    editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = editions_root / "demo_weekly.yaml" if document == "edition" else programs_root / "demo" / "program.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", mode])
    assert type(result.exception) is ConfigError
    assert isinstance(result.exception.__cause__, yaml.YAMLError)
    assert _PRIVATE in "".join(traceback.format_exception(result.exception))


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_inprocess_cli_does_not_run_unrelated_startup(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, _programs_root, _db_root, _store, _calls, _source = scenario
    path = editions_root / "demo_weekly.yaml"
    path.write_text(_INVALID_YAML[0], encoding="utf-8")
    monkeypatch.setattr(cli, "_program_for_startup", lambda _ctx: "synthetic")

    def fail(*_args, **_kwargs):
        pytest.fail("Unrelated CLI startup must not access workspace state")

    monkeypatch.setattr(cli, "load_arch_data_fix", fail)
    monkeypatch.setattr(cli, "maybe_run_scheduled_compaction", fail)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: True)
    monkeypatch.setattr(cli, "maybe_catchup", fail)
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
    assert result.exit_code == 1
    _assert_safe(result.exception, path)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_operator_gate_encoding_error_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_bytes(b"\xff")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
    assert type(result.exception) is ConfigError
    assert isinstance(result.exception.__cause__, UnicodeDecodeError)
    assert str(path) in str(result.exception)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_operator_gate_read_error_remains_unchanged(scenario, monkeypatch: pytest.MonkeyPatch) -> None:
    editions_root, programs_root, db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    original = PermissionError(f"{_PRIVATE} read")
    read_text = Path.read_text

    def fail_read(self, *args, **kwargs):
        if self == path:
            raise original
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    with pytest.raises(ConfigError) as caught:
        doctor.run_doctor(
            edition_name="demo_weekly", operator_gates=True, editions_root=editions_root,
            programs_root=programs_root, reality_db_root=db_root,
        )
    assert caught.value.__cause__ is original
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_operator_gate_schema_error_remains_unchanged(scenario) -> None:
    _editions_root, programs_root, _db_root, _store, _calls, _source = scenario
    path = programs_root / "demo" / "program.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--edition", "demo_weekly", "--operator-gates"])
    assert type(result.exception) is ConfigError
    assert result.exception.__cause__ is None
    assert str(path) in str(result.exception)


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_missing_operator_gate_edition_remains_a_failed_report(scenario) -> None:
    editions_root, programs_root, db_root, store, calls, _source = scenario
    before = store.get_metric_source_binding("synthetic-binding")
    report = doctor.run_doctor(
        edition_name="not_present", operator_gates=True, editions_root=editions_root,
        programs_root=programs_root, reality_db_root=db_root,
    )
    assert len(report.checks) == 1
    assert report.checks[0].label == "Operator Gates"
    assert report.checks[0].status == "fail"
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
def test_invalid_combined_modes_keep_metric_binding_preflight_precedence(scenario) -> None:
    editions_root, _programs_root, _db_root, _store, _calls, _source = scenario
    (editions_root / "demo_weekly.yaml").write_text(_INVALID_YAML[0], encoding="utf-8")
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "demo_weekly", "--metric-bindings", "--operator-gates"],
    )
    assert result.exit_code == 1
    assert type(result.exception) is ConfigError
    assert str(result.exception) == "Cannot parse selected metric-binding edition or program YAML; check syntax and values."
    assert result.exception.__cause__ is None and result.exception.__context__ is None


def _cli_environment(tmp_path: Path, db_root: Path, programs_root: Path, traceback_style: str) -> dict[str, str]:
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
    if tmp_path.drive:
        environment["HOMEDRIVE"] = tmp_path.drive
        environment["HOMEPATH"] = str(tmp_path)[len(tmp_path.drive):]
    else:
        environment.pop("HOMEDRIVE", None)
        environment.pop("HOMEPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["VERTEX_DB_PATH"] = str(db_root)
    environment["VERTEX_PROGRAMS_ROOT"] = str(programs_root)
    if traceback_style == "standard":
        environment["TYPER_STANDARD_TRACEBACK"] = "1"
    else:
        environment.pop("TYPER_STANDARD_TRACEBACK", None)
        environment.pop("_TYPER_STANDARD_TRACEBACK", None)
    return environment


def _run_cli(script: Path, tmp_path: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, env=environment,
        capture_output=True, timeout=90, check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == 1, output
    return output


def test_cli_environment_uses_the_synthetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEDRIVE", "Z:")
    monkeypatch.setenv("HOMEPATH", r"\synthetic-host-home")
    environment = _cli_environment(tmp_path, tmp_path / "db", tmp_path / "programs", "standard")
    assert environment["HOME"] == environment["USERPROFILE"] == str(tmp_path)
    if tmp_path.drive:
        assert environment["HOMEDRIVE"] == tmp_path.drive
        assert environment["HOMEPATH"] == str(tmp_path)[len(tmp_path.drive):]
    else:
        assert "HOMEDRIVE" not in environment and "HOMEPATH" not in environment


def test_test_module_import_does_not_load_dotenv(tmp_path: Path) -> None:
    script = tmp_path / "import_test_module.py"
    script.write_text(
        "import sys\n"
        "import dotenv\n"
        "def forbid_dotenv(*args, **kwargs):\n"
        "    raise RuntimeError('dotenv loading was attempted')\n"
        "dotenv.load_dotenv = forbid_dotenv\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "import tests.unit.test_doctor_operator_gate_yaml_privacy\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path,
        env=_cli_environment(tmp_path, tmp_path / "db", tmp_path / "programs", "standard"),
        capture_output=True, timeout=90, check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8")


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("document", ["edition", "program", "workstreams", "scorecards"])
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_operator_gate_yaml_traceback_omits_parser_detail(
    scenario, document: str, traceback_style: str, tmp_path: Path,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    path = {
        "edition": editions_root / "demo_weekly.yaml",
        "program": programs_root / "demo" / "program.yaml",
        "workstreams": programs_root / "demo" / "workstreams.yaml",
        "scorecards": programs_root / "demo" / "scorecards.yaml",
    }[document]
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
        + "sys.argv = ['vertex', 'doctor', '--edition', 'demo_weekly', '--operator-gates']\n"
        + "cli.app()\n",
        encoding="utf-8",
    )
    before = store.get_metric_source_binding("synthetic-binding")
    output = _run_cli(script, tmp_path, _cli_environment(tmp_path, db_root, programs_root, traceback_style))
    assert _SAFE in " ".join(output.split())
    assert _PRIVATE not in output
    assert str(path) not in output and path.as_posix() not in output
    if traceback_style == "rich-locals":
        assert "locals" in output.lower()
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["wiql"], indirect=True)
@pytest.mark.parametrize("stage", ["auth", "channel"])
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_operator_gate_stage_traceback_omits_parser_detail(
    scenario, stage: str, traceback_style: str, tmp_path: Path,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    script = tmp_path / "synthetic_doctor_cli.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from pathlib import Path\n"
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *args, **kwargs: False\n"
        "import cli\n"
        "import yaml\n"
        "from src.commands import doctor\n"
        "from src.commands.doctor_checks.models import DoctorReport\n"
        "from src.core.exceptions import ConfigError\n"
        "cli._maybe_register_tier_decision_sink = lambda *args, **kwargs: None\n"
        "cli._maybe_run_scheduled_db_maintenance = lambda *args, **kwargs: None\n"
        "cli._maybe_run_session_catchup = lambda *args, **kwargs: None\n"
        f"doctor.EDITIONS_ROOT = Path({str(editions_root)!r})\n"
        f"doctor.PROGRAMS_ROOT = Path({str(programs_root)!r})\n"
        "def fail(**kwargs):\n"
        f"    raise ConfigError({_PRIVATE!r}) from yaml.YAMLError({_PRIVATE!r})\n"
        + ("doctor._run_auth_doctor = fail\n" if stage == "auth" else
           "doctor._run_auth_doctor = lambda **kwargs: DoctorReport('demo_weekly', ())\n")
        + "doctor._run_channel_doctor = fail\n"
        + ("cli.app.pretty_exceptions_show_locals = True\n" if traceback_style == "rich-locals" else "")
        + "sys.argv = ['vertex', 'doctor', '--edition', 'demo_weekly', '--operator-gates']\n"
        + "cli.app()\n",
        encoding="utf-8",
    )
    before = store.get_metric_source_binding("synthetic-binding")
    output = _run_cli(script, tmp_path, _cli_environment(tmp_path, db_root, programs_root, traceback_style))
    assert _SAFE in " ".join(output.split())
    assert _PRIVATE not in output
    if traceback_style == "rich-locals":
        assert "locals" in output.lower()
    assert store.get_metric_source_binding("synthetic-binding") == before
