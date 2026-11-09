from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.doctor_checks.channel_surface_checks import extract_kusto_failed_target
from src.core.exceptions import AuthError, QueryError
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.kusto_client import KustoClient, build_live_kusto_query_probe
from src.core.models_v2 import KustoQuery
from tests.unit.test_kusto_error_privacy import _NOW, _PROGRAM, scenario


_PRIVATE = "SYNTHETIC_PROVIDER_DETAIL"
_CLUSTER = "https://synthetic-private.example.invalid"
_DATABASE = "SyntheticPrivateDatabase"
_OTHER_CLUSTER = "https://another-private.example.invalid"
_OTHER_DATABASE = "AnotherPrivateDatabase"
_GOOD_CLUSTER = "https://healthy.example.invalid"
_WARNING = "Kusto pre-flight failed; queries on this target will be skipped."


def _query(identifier: str, cluster: str, database: str) -> KustoQuery:
    return KustoQuery(
        id=identifier, cluster=cluster, database=database,
        kql="Synthetic | take 1", section="Synthetic", render_as="table",
        confidence="high", program_ids=(_PROGRAM,), validated=True,
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    def execute(_self, cluster, database, kql, **_kwargs):
        calls.append((cluster, database, kql))
        if cluster in {_CLUSTER, _OTHER_CLUSTER}:
            raise error_type(_PRIVATE)
        return [] if kql == ".show version" else [{"Value": 1}]

    monkeypatch.setattr(KustoClient, "execute", execute)
    return calls


@pytest.mark.parametrize("error_type", [AuthError, QueryError])
@pytest.mark.parametrize("log_failures", [True, False])
def test_actual_probe_failure_warning_is_static(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error_type: type[Exception], log_failures: bool,
) -> None:
    bad = _query("failed", _CLUSTER, _DATABASE)
    other_bad = _query("failed2", _OTHER_CLUSTER, _OTHER_DATABASE)
    good = _query("healthy", _GOOD_CLUSTER, "HealthyDb")
    calls = _patch_client(monkeypatch, error_type)
    with caplog.at_level(logging.WARNING, logger="src.core.kusto_client"):
        failed = build_live_kusto_query_probe(log_failures=log_failures)((bad, bad, other_bad, good))
    assert failed == frozenset({(_CLUSTER, _DATABASE), (_OTHER_CLUSTER, _OTHER_DATABASE)})
    assert calls == [
        (_CLUSTER, _DATABASE, ".show version"),
        (_OTHER_CLUSTER, _OTHER_DATABASE, ".show version"),
        (_GOOD_CLUSTER, "HealthyDb", ".show version"),
    ]
    records = [record for record in caplog.records if record.name == "src.core.kusto_client"]
    assert len(records) == 2 * int(log_failures)
    for record in records:
        assert record.getMessage() == _WARNING
        assert record.args == () and record.exc_info is None
        assert all(
            value not in repr(record.__dict__)
            for value in (_PRIVATE, _CLUSTER, _DATABASE, _OTHER_CLUSTER, _OTHER_DATABASE)
        )


def test_actual_probe_success_and_unexpected_failure_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    good = _query("healthy", _GOOD_CLUSTER, "HealthyDb")
    _patch_client(monkeypatch, QueryError)
    with caplog.at_level(logging.WARNING, logger="src.core.kusto_client"):
        assert build_live_kusto_query_probe()((good,)) == frozenset()
    assert not [record for record in caplog.records if record.name == "src.core.kusto_client"]

    def unexpected(*_args, **_kwargs):
        raise ValueError("synthetic unexpected failure")

    monkeypatch.setattr(KustoClient, "execute", unexpected)
    with pytest.raises(ValueError, match="synthetic unexpected failure"):
        build_live_kusto_query_probe()((good,))


def _prepare_gather(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    bad = _query("failed", _CLUSTER, _DATABASE)
    good = _query("healthy", _GOOD_CLUSTER, "HealthyDb")
    calls = _patch_client(monkeypatch, QueryError)
    results: list[tuple[object, object]] = []
    monkeypatch.setattr(gather, "_load_kusto_queries", lambda *_args, **_kwargs: (bad, good))
    monkeypatch.setattr(gather, "_load_refresh_kpi_queries", lambda *_args, **_kwargs: ())

    def build_signals(*, executor, **_kwargs):
        results.append((executor(bad), executor(good)))
        return ()

    monkeypatch.setattr(gather, "_build_kusto_signals", build_signals)
    monkeypatch.setattr(gather, "_build_kusto_kpi_signals", lambda **_kwargs: ())
    return calls, results


@pytest.mark.parametrize("gather_v2", [False, True])
def test_actual_gather_retains_probe_failure_without_logging_private_data(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, gather_v2: bool,
) -> None:
    calls, results = _prepare_gather(monkeypatch)
    monkeypatch.setattr(gather, "_gather_v2_enabled", lambda: gather_v2)
    with caplog.at_level(logging.WARNING, logger="src.core.kusto_client"):
        artifacts = gather.gather_program(
            _PROGRAM, programs_root=scenario.root, as_of=_NOW,
            loader=lambda *_args, **_kwargs: ((), 0),
            freshness_loader=lambda *_args, **_kwargs: ((), 0),
            include_kusto=True,
        )
    assert calls == [
        (_CLUSTER, _DATABASE, ".show version"),
        (_GOOD_CLUSTER, "HealthyDb", ".show version"),
        (_GOOD_CLUSTER, "HealthyDb", "Synthetic | take 1"),
    ]
    assert results == [([], [{"Value": 1}])]
    assert artifacts.integration_error_count == 1
    error = artifacts.integration_errors[0]
    assert (error.source, error.stage) == ("kusto", "probe")
    assert _PRIVATE not in error.message
    assert _CLUSTER in error.message and _DATABASE in error.message
    assert extract_kusto_failed_target(error.message) == f"{_CLUSTER}/{_DATABASE}"
    assert gather._semantic_gather_exit_code(artifacts) == 2
    state = load_gather_state(_PROGRAM, programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest(_PROGRAM, programs_root=scenario.root)
    assert state is not None and error in state.integration_error_details
    assert manifest is not None
    assert next(item for item in manifest.channel_outcomes if item.channel == "kusto").degraded
    records = [record for record in caplog.records if record.name == "src.core.kusto_client"]
    assert [record.getMessage() for record in records] == [_WARNING]
    assert all(value not in repr(records[0].__dict__) for value in (_PRIVATE, _CLUSTER, _DATABASE))


def test_actual_gather_cli_preserves_probe_failure(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prepare_gather(monkeypatch)
    for name in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"):
        monkeypatch.setattr(f"cli.{name}", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    monkeypatch.setattr(
        gather, "_resolve_uil_channel_binding_for_gather",
        lambda _program, _workstreams, channel, **_kwargs: object() if channel == "ado" else None,
    )
    with caplog.at_level(logging.WARNING, logger="src.core.kusto_client"):
        result = CliRunner().invoke(app, ["gather", "--program", _PROGRAM, "--kusto"])
    assert result.exit_code == 2, result.output
    assert "kusto/probe" in result.output
    assert _CLUSTER in result.output and _DATABASE in result.output
    assert _PRIVATE not in result.output
    records = [record for record in caplog.records if record.name == "src.core.kusto_client"]
    assert [record.getMessage() for record in records] == [_WARNING]
