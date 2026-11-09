from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import traceback

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import metric
from src.commands.doctor_checks import metric_binding_checks
from src.core.exceptions import ConfigError, PersonaSchemaError, QueryError, QueryTimeoutError
from src.core.failure_taxonomy import classify_exception
from src.core.reality_store import RealityStore
from tests.unit.test_metric_binding_probe_error_privacy import (
    _PRIVATE, _binding, _expected, _raise, scenario,
)


class SpecializedQueryError(QueryError):
    pass


class SpecializedValueError(ValueError):
    pass


_ERROR_TYPES = (
    ConfigError, PersonaSchemaError, QueryError, QueryTimeoutError,
    SpecializedQueryError, ValueError, SpecializedValueError,
)


def _original_error(error_type: type[Exception]) -> tuple[Exception, Exception]:
    original = error_type(f"{_PRIVATE} 403")
    try:
        raise RuntimeError(f"{_PRIVATE} provider cause")
    except RuntimeError as cause:
        try:
            raise original from cause
        except error_type:
            return original, cause


def _normalized_type(original: Exception) -> type[Exception]:
    if isinstance(original, ConfigError):
        return ConfigError
    if isinstance(original, QueryTimeoutError):
        return QueryTimeoutError
    if isinstance(original, QueryError):
        return QueryError
    return ValueError


@pytest.mark.parametrize("source_kind", ["kusto", "wiql"])
@pytest.mark.parametrize("error_type", _ERROR_TYPES)
@pytest.mark.parametrize("outer_handler", [False, True])
def test_selected_probe_errors_have_no_original_chain(
    source_kind: str, error_type: type[Exception], outer_handler: bool,
) -> None:
    original, cause = _original_error(error_type)
    safe_probe = metric._privacy_safe_metric_binding_probe(_raise(original))
    expected_type = _normalized_type(original)

    def invoke() -> Exception:
        with pytest.raises(expected_type) as caught:
            safe_probe(_binding(source_kind))
        return caught.value

    if outer_handler:
        try:
            raise original
        except error_type:
            sanitized = invoke()
    else:
        sanitized = invoke()

    assert type(sanitized) is expected_type
    assert sanitized is not original
    assert str(sanitized) == _expected(original, source_kind)
    assert classify_exception(sanitized) == classify_exception(original)
    assert sanitized.__cause__ is None and sanitized.__context__ is None
    assert _PRIVATE not in "".join(traceback.format_exception(sanitized))
    assert original.__cause__ is cause
    assert _PRIVATE in str(original) and _PRIVATE in str(cause)


@pytest.mark.parametrize("scenario", ["kusto", "wiql"], indirect=True)
@pytest.mark.parametrize("mode", ["doctor", "admin"])
@pytest.mark.parametrize("error_type", _ERROR_TYPES)
def test_secondary_persistence_failure_does_not_expose_probe_chain(
    scenario, mode: str, error_type: type[Exception], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _editions_root, programs_root, db_root, store, calls, _source = scenario
    original, _cause = _original_error(error_type)
    probe = _raise(original)
    if mode == "doctor":
        monkeypatch.setattr(
            metric_binding_checks, "_build_program_metric_binding_probe",
            lambda *_args, **_kwargs: probe,
        )
        args = ["doctor", "--edition", "demo_weekly", "--metric-bindings"]
    else:
        monkeypatch.setattr(
            metric, "_build_program_metric_binding_probe",
            lambda *_args, **_kwargs: probe,
        )
        args = [
            "admin", "metric", "validate", "--program", "demo",
            "--binding-id", "synthetic-binding", "--db-root", str(db_root),
            "--programs-root", str(programs_root),
        ]

    persistence_error = OSError("synthetic persistence failure")

    def fail_persistence(*_args, **_kwargs):
        raise persistence_error

    before = store.get_metric_source_binding("synthetic-binding")
    monkeypatch.setattr(RealityStore, "upsert_metric_source_binding", fail_persistence)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert result.exception is persistence_error
    safe_probe_error = persistence_error.__context__
    assert type(safe_probe_error) is _normalized_type(original)
    assert safe_probe_error is not None
    assert str(safe_probe_error) == _expected(original, "kusto" if _source == "kusto" else "wiql")
    assert safe_probe_error.__cause__ is None and safe_probe_error.__context__ is None
    assert _PRIVATE not in "".join(traceback.format_exception(persistence_error))
    assert calls == []
    assert store.get_metric_source_binding("synthetic-binding") == before


def test_unexpected_probe_exception_is_not_replaced() -> None:
    original = RuntimeError(f"{_PRIVATE} unexpected")
    with pytest.raises(RuntimeError) as caught:
        metric._privacy_safe_metric_binding_probe(_raise(original))(_binding())
    assert caught.value is original
    assert _PRIVATE in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    ("scenario", "mode"),
    [
        ("kusto", "doctor"),
        ("kusto", "admin"),
        ("wiql", "doctor"),
        ("wiql", "admin"),
        ("wiql-config", "admin"),
    ],
    indirect=["scenario"],
)
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_secondary_failure_omits_original_probe_chain(
    scenario, mode: str, traceback_style: str, tmp_path: Path,
) -> None:
    editions_root, programs_root, db_root, store, _calls, source = scenario
    if source == "wiql-config":
        (programs_root / "demo" / "program.yaml").write_text(
            f"invalid: [{_PRIVATE}\n", encoding="utf-8",
        )
    script = tmp_path / "synthetic_cli.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *args, **kwargs: False\n"
        "import cli\n"
        "from src.commands import doctor, metric\n"
        "from src.core.ado_client import ADOClient\n"
        "from src.core.kusto_client import KustoClient\n"
        "from src.core.reality_store import RealityStore\n"
        "cli._maybe_register_tier_decision_sink = lambda *args, **kwargs: None\n"
        "cli._maybe_run_scheduled_db_maintenance = lambda *args, **kwargs: None\n"
        "cli._maybe_run_session_catchup = lambda *args, **kwargs: None\n"
        f"doctor.EDITIONS_ROOT = Path({str(editions_root)!r})\n"
        f"doctor.PROGRAMS_ROOT = Path({str(programs_root)!r})\n"
        f"metric.METRICS_ROOT = Path({str(programs_root.parent / 'knowledge' / 'metrics')!r})\n"
        "def record_call():\n"
        "    with open(os.environ['RVX_SYNTHETIC_PROBE_CALLS'], 'a', encoding='utf-8') as stream:\n"
        "        stream.write('1')\n"
        "def request(*args, **kwargs):\n"
        "    record_call()\n"
        "    return SimpleNamespace(status_code=403, headers={}, "
        "text=os.environ['RVX_SYNTHETIC_PROBE_MARKER'])\n"
        "def execute(*args, **kwargs):\n"
        "    record_call()\n"
        "    raise RuntimeError(os.environ['RVX_SYNTHETIC_PROBE_MARKER'])\n"
        "def fail_persistence(*args, **kwargs):\n"
        "    raise OSError('synthetic persistence failure')\n"
        "RealityStore.upsert_metric_source_binding = fail_persistence\n"
        "ADOClient._init_auth = lambda self: None\n"
        "ADOClient._request_with_progress = request\n"
        "KustoClient._get_or_create_client = lambda self, cluster: SimpleNamespace(execute=execute)\n"
        "KustoClient._build_request_properties = lambda *args, **kwargs: None\n"
        + ("cli.app.pretty_exceptions_show_locals = True\n" if traceback_style == "rich-locals" else "")
        + (
            "sys.argv = ['vertex', 'doctor', '--edition', 'demo_weekly', '--metric-bindings']\n"
            if mode == "doctor" else
            "sys.argv = ['vertex', 'admin', 'metric', 'validate', '--program', 'demo', "
            f"'--binding-id', 'synthetic-binding', '--db-root', {str(db_root)!r}, "
            f"'--programs-root', {str(programs_root)!r}]\n"
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
    environment["RVX_SYNTHETIC_PROBE_MARKER"] = _PRIVATE
    environment["RVX_SYNTHETIC_PROBE_CALLS"] = str(tmp_path / "calls")
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
    assert _PRIVATE not in output
    assert "OSError: synthetic persistence failure" in " ".join(output.split())
    assert "Metric binding probe failed" in " ".join(output.split())
    if traceback_style == "rich-locals":
        assert "locals" in output.lower()
    calls = tmp_path / "calls"
    if source == "wiql-config":
        assert not calls.exists()
    else:
        assert calls.read_text(encoding="utf-8") == "1"
    assert store.get_metric_source_binding("synthetic-binding") == before
