from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor, metric
from src.core.ado_client import ADOClient
from src.core.exceptions import AuthError, CredentialExpired
from src.core.failure_taxonomy import classify_exception, safe_failure_suffix
from src.core.kusto_client import KustoClient, KustoColumn
from src.core.metric_binding_validator import validate_metric_source_binding
from tests.unit.test_metric_binding_probe_error_privacy import (
    _DEFINITION, _PATTERNS, _PRIVATE, _VALIDATED_AT, _binding, _raise, scenario,
)


class SpecializedAuthError(AuthError):
    pass


class SpecializedCredentialExpired(CredentialExpired):
    pass


def _original_error(error_type: type[AuthError], pattern: str) -> AuthError:
    if issubclass(error_type, CredentialExpired):
        error = error_type(
            f"{_PRIVATE} {pattern}", auth_method="synthetic-auth", connector="synthetic-connector",
        )
    else:
        error = error_type(f"{_PRIVATE} {pattern}")
    try:
        raise RuntimeError(f"{_PRIVATE} chained provider error")
    except RuntimeError as cause:
        try:
            raise error from cause
        except AuthError as captured:
            return captured


@pytest.mark.parametrize("source_kind", ["kusto", "wiql"])
@pytest.mark.parametrize("error_type", [
    AuthError, CredentialExpired, SpecializedAuthError, SpecializedCredentialExpired,
])
@pytest.mark.parametrize("pattern", _PATTERNS)
def test_selected_probe_authentication_exception_is_safe_and_unchained(
    source_kind: str, error_type: type[AuthError], pattern: str,
) -> None:
    original = _original_error(error_type, pattern)
    original_message = str(original)
    original_cause = original.__cause__
    safe_probe = metric._privacy_safe_metric_binding_probe(_raise(original))
    with pytest.raises(AuthError) as caught:
        safe_probe(_binding(source_kind))
    sanitized = caught.value
    expected_type = CredentialExpired if isinstance(original, CredentialExpired) else AuthError
    assert type(sanitized) is expected_type
    assert sanitized is not original
    assert str(sanitized) == (
        f"Metric binding probe failed{safe_failure_suffix(original_message)}. "
        "Refresh sign-in or check source access."
    )
    assert classify_exception(sanitized) == classify_exception(original)
    assert _PRIVATE not in str(sanitized)
    assert _PRIVATE not in "".join(traceback.format_exception(sanitized))
    assert sanitized.__cause__ is None and sanitized.__context__ is None
    assert str(original) == original_message and original.__cause__ is original_cause
    if isinstance(original, CredentialExpired):
        assert isinstance(sanitized, CredentialExpired)
        assert sanitized.auth_method == original.auth_method == "synthetic-auth"
        assert sanitized.connector == original.connector == "synthetic-connector"


def test_direct_validator_still_propagates_original_authentication_error() -> None:
    error = _original_error(CredentialExpired, "401")
    with pytest.raises(CredentialExpired) as caught:
        validate_metric_source_binding(
            _binding(), metric_definitions={_DEFINITION.id: _DEFINITION},
            probe=_raise(error), validated_at=_VALIDATED_AT,
        )
    assert caught.value is error
    assert _PRIVATE in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("error_type", [AuthError, CredentialExpired])
def test_original_authentication_error_is_not_retained_from_an_outer_handler(
    error_type: type[AuthError],
) -> None:
    original = _original_error(error_type, "401")
    try:
        raise original
    except AuthError:
        with pytest.raises(AuthError) as caught:
            metric._privacy_safe_metric_binding_probe(_raise(original))(_binding())
    assert caught.value is not original
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _PRIVATE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("scenario", ["kusto", "wiql"], indirect=True)
@pytest.mark.parametrize("mode", ["doctor", "admin"])
def test_actual_authentication_failure_preserves_binding_and_stops(
    scenario, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, calls, source = scenario
    if source == "wiql":
        def request(_self, method, url, **_kwargs):
            calls.append((method, url))
            return SimpleNamespace(
                status_code=401, headers={"WWW-Authenticate": _PRIVATE}, text="",
            )

        monkeypatch.setattr(ADOClient, "_request_with_progress", request)
        expected_type = CredentialExpired
    else:
        def execute(database, query, **_kwargs):
            calls.append((database, query))
            raise RuntimeError(f"aadsts {_PRIVATE}")

        monkeypatch.setattr(
            KustoClient, "_get_or_create_client",
            lambda _self, _cluster: SimpleNamespace(execute=execute),
        )
        expected_type = AuthError

    before = store.get_metric_source_binding("synthetic-binding")
    if mode == "doctor":
        with pytest.raises(expected_type) as caught:
            doctor.run_doctor(
                edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
                metric_definitions={_DEFINITION.id: _DEFINITION},
            )
        error = caught.value
    else:
        result = CliRunner().invoke(app, [
            "admin", "metric", "validate", "--program", "demo",
            "--binding-id", "synthetic-binding", "--db-root", str(db_root),
            "--programs-root", str(programs_root),
        ])
        assert result.exit_code == 1
        assert isinstance(result.exception, expected_type)
        error = result.exception

    assert type(error) is expected_type
    assert _PRIVATE not in str(error)
    assert _PRIVATE not in "".join(traceback.format_exception(error))
    assert error.__cause__ is None and error.__context__ is None
    if isinstance(error, CredentialExpired):
        assert error.connector == "ADO" and error.auth_method == "unknown"
    assert len(calls) == 1
    assert store.get_metric_source_binding("synthetic-binding") == before


@pytest.mark.parametrize("scenario", ["kusto"], indirect=True)
@pytest.mark.parametrize("mode", ["doctor", "admin"])
def test_authentication_stops_remaining_bindings_but_keeps_prior_success(
    scenario, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, _calls, _source = scenario
    store.upsert_metric_source_binding(_binding(binding_id="a-success"))
    store.upsert_metric_source_binding(_binding(binding_id="z-unattempted"))
    calls: list[str] = []

    def probe(binding):
        calls.append(binding.binding_id)
        if binding.binding_id == "synthetic-binding":
            raise AuthError(_PRIVATE)
        return [{"Count": 1}], (KustoColumn("Count", "long"),)

    original = store.get_metric_source_binding("synthetic-binding")
    untouched = store.get_metric_source_binding("z-unattempted")
    if mode == "doctor":
        with pytest.raises(AuthError):
            doctor.run_doctor(
                edition_name="demo_weekly", metric_bindings=True, editions_root=editions_root,
                programs_root=programs_root, reality_db_root=db_root,
                metric_definitions={_DEFINITION.id: _DEFINITION}, metric_binding_probe=probe,
            )
    else:
        monkeypatch.setattr(metric, "_live_metric_binding_probe", lambda: probe)
        result = CliRunner().invoke(app, [
            "admin", "metric", "validate", "--program", "demo", "--all",
            "--db-root", str(db_root), "--programs-root", str(programs_root),
        ])
        assert result.exit_code == 1 and isinstance(result.exception, AuthError)
    assert calls == ["a-success", "synthetic-binding"]
    succeeded = store.get_metric_source_binding("a-success")
    assert succeeded is not None and succeeded.validated and succeeded.last_validated_at != _VALIDATED_AT
    assert store.get_metric_source_binding("synthetic-binding") == original
    assert store.get_metric_source_binding("z-unattempted") == untouched


@pytest.mark.parametrize("scenario", ["kusto", "wiql"], indirect=True)
@pytest.mark.parametrize("mode", ["doctor", "admin"])
@pytest.mark.parametrize("traceback_style", ["default", "rich-locals", "standard"])
def test_real_cli_authentication_tracebacks_are_safe(
    scenario, mode: str, traceback_style: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    editions_root, programs_root, db_root, store, _calls, source = scenario
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "unselected-db"))
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(tmp_path / "unselected-programs"))
    monkeypatch.setenv("ADO_PAT", _PRIVATE)
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
        "cli._maybe_register_tier_decision_sink = lambda *args, **kwargs: None\n"
        "cli._maybe_run_scheduled_db_maintenance = lambda *args, **kwargs: None\n"
        "cli._maybe_run_session_catchup = lambda *args, **kwargs: None\n"
        f"doctor.EDITIONS_ROOT = Path({str(editions_root)!r})\n"
        f"doctor.PROGRAMS_ROOT = Path({str(programs_root)!r})\n"
        f"metric.METRICS_ROOT = Path({str(programs_root.parent / 'knowledge' / 'metrics')!r})\n"
        "def record_call():\n"
        "    with open(os.environ['RVX_SYNTHETIC_AUTH_CALLS'], 'a', encoding='utf-8') as stream:\n"
        "        stream.write('1')\n"
        "def request(*args, **kwargs):\n"
        "    record_call()\n"
        "    return SimpleNamespace(status_code=401, "
        "headers={'WWW-Authenticate': os.environ['RVX_SYNTHETIC_AUTH_MARKER']}, text='')\n"
        "def execute(*args, **kwargs):\n"
        "    record_call()\n"
        "    raise RuntimeError('aadsts ' + os.environ['RVX_SYNTHETIC_AUTH_MARKER'])\n"
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
    environment["RVX_SYNTHETIC_AUTH_MARKER"] = _PRIVATE
    environment["RVX_SYNTHETIC_AUTH_CALLS"] = str(tmp_path / "calls")
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
    normalized_output = " ".join(output.split())
    assert result.returncode == 1, output
    assert _PRIVATE not in output
    assert ("CredentialExpired" if source == "wiql" else "AuthError") in output
    assert "Refresh sign-in or check source access." in normalized_output
    assert ("matched pattern: '401'" if source == "wiql" else "matched pattern: 'auth'") in normalized_output
    if traceback_style == "rich-locals":
        assert "locals" in output.lower()
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "1"
    assert store.get_metric_source_binding("synthetic-binding") == before
    assert not (tmp_path / "unselected-db").exists()
    assert not (tmp_path / "unselected-programs").exists()
