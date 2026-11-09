"""Direct coverage for the extracted IcM gather signal stage."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from src.commands import gather, watch_scan
from src.commands.gather_pipeline import icm_signal_stage
from src.core.catchup_scan import WatchSource
from src.core.exceptions import AuthError, CredentialExpired, QueryError, QueryTimeoutError
from src.core.journal import read_signals
from src.core.knowledge_store import KnowledgeStore
from src.core.models_v2 import ADOConfig, KustoConfig, KustoQuery, Program, Team, Workstream


def _program(*, kusto_enabled: bool = True, icm_incidents_url: str | None = None, prefer_agency: bool = True) -> Program:
    from src.core.models_v2 import M365Config

    return Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + EXD on EXN",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=kusto_enabled),
        m365=M365Config(enabled=True, prefer_agency=prefer_agency, icm_incidents_url=icm_incidents_url),
    )


def _teams() -> tuple[Team, ...]:
    return (
        Team(
            id="adventure-core",
            name="Adventure Core",
            area_paths=("One\\Adventure\\Acme",),
            programs=("acme",),
        ),
    )


def _workstreams() -> tuple[Workstream, ...]:
    return (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
        ),
    )


def _resolve_icm_workstream_id(
    *,
    owning_team: str | None,
    fallback_workstream_id: str | None,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
) -> str | None:
    del teams, workstreams
    if owning_team == "Adventure Core":
        return "acme"
    return fallback_workstream_id


def test_build_icm_signals_prefers_direct_client() -> None:
    class _FakeDirectIcmClient:
        def __init__(self, *, incidents_url: str | None = None) -> None:
            self.incidents_url = incidents_url

        def list_incidents(self) -> dict[str, object]:
            return {
                "items": [
                    {
                        "incidentId": "12345",
                        "severity": 2,
                        "status": "Active",
                        "title": "Fleet capacity alert on WI:1234",
                        "owningTeam": "Adventure Core",
                        "createDate": "2026-05-09T06:00:00Z",
                    }
                ]
            }

    signals = icm_signal_stage.build_icm_signals(
        program=_program(icm_incidents_url="https://icm.example.test/incidents"),
        program_id="acme",
        programs_root="unused",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        teams=_teams(),
        workstreams=_workstreams(),
        executor=lambda query: [],
        bridge=lambda: None,
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        load_kusto_queries_fn=lambda *args, **kwargs: (),
        icm_client_factory=_FakeDirectIcmClient,
    )

    assert len(signals) == 1
    assert signals[0].entity_refs == ("ICM:12345", "WI:1234", "WS:acme")
    assert signals[0].metadata is not None
    assert signals[0].metadata["source_path"] == "direct"


def test_build_icm_signals_prefers_agency_when_available() -> None:
    class _FakeCapabilities:
        available = True
        has_icm = True
        server_tools = {"icm": ("list_incidents",)}

    class _FakeBridge:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def probe(self) -> _FakeCapabilities:
            return _FakeCapabilities()

        def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object]) -> dict[str, object] | None:
            self.calls.append((server, tool, args))
            return {
                "items": [
                    {
                        "incidentId": "12345",
                        "severity": 2,
                        "status": "Active",
                        "title": "Fleet capacity alert on WI:1234",
                        "owningTeam": "Adventure Core",
                        "createDate": "2026-05-09T06:00:00Z",
                    }
                ]
            }

    bridge = _FakeBridge()
    signals = icm_signal_stage.build_icm_signals(
        program=_program(icm_incidents_url=None, prefer_agency=True),
        program_id="acme",
        programs_root="unused",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        teams=_teams(),
        workstreams=_workstreams(),
        executor=lambda query: [],
        bridge=lambda: bridge,
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        load_kusto_queries_fn=lambda *args, **kwargs: (),
    )

    assert bridge.calls == [("icm", "list_incidents", {})]
    assert len(signals) == 1
    assert signals[0].metadata is not None
    assert signals[0].metadata["source_path"] == "agency"


def test_build_icm_signals_falls_back_to_kusto_queries() -> None:
    query = KustoQuery(
        id="icm-active",
        cluster="https://icmcluster.kusto.windows.net",
        database="IcMDataWarehouse",
        kql="Incidents | take 1",
        section="Active Incidents",
        render_as="table",
        confidence="high",
        program_ids=("acme",),
    )

    class _UnavailableBridge:
        def probe(self) -> object:
            return type("Caps", (), {"available": False, "has_icm": False, "server_tools": {}})()

    signals = icm_signal_stage.build_icm_signals(
        program=_program(kusto_enabled=True, icm_incidents_url=None, prefer_agency=True),
        program_id="acme",
        programs_root="unused",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        teams=_teams(),
        workstreams=_workstreams(),
        executor=lambda stage_query: [
            {
                "IncidentId": "12345",
                "Severity": 2,
                "Status": "Active",
                "Title": "Fleet capacity alert on WI:1234",
                "OwningTeam": "Adventure Core",
                "Date": "2026-05-09",
            }
        ],
        bridge=lambda: _UnavailableBridge(),
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        load_kusto_queries_fn=lambda *args, **kwargs: (query,),
    )

    assert len(signals) == 1
    assert signals[0].entity_refs == ("ICM:12345", "WI:1234", "WS:acme")
    assert signals[0].metadata is not None
    assert signals[0].metadata["query_id"] == "icm-active"


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PROGRAM_ID = "synthetic-program-sentinel"
_PROVIDER_ERROR = "synthetic-provider-private-value"
_PAYLOAD_SENTINEL = "synthetic-payload-private-value"


@pytest.fixture
def icm_fallback_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
    program = replace(
        _program(icm_incidents_url="https://icm.example.test/incidents"),
        id=_PROGRAM_ID,
        name="Synthetic",
        maturity_level=2,
    )
    workstreams = (Workstream(id="alpha", name="Alpha", area_paths=("Example\\Alpha",)),)
    knowledge = KnowledgeStore(
        people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=()
    )
    state = SimpleNamespace(
        fail_at="query",
        failure=QueryError(_PROVIDER_ERROR),
        direct_payload={"items": []},
        agency_available=True,
        agency_payload={
            "items": [{
                "incidentId": "12345",
                "severity": 2,
                "status": "Active",
                "title": "Synthetic incident",
                "createDate": "2026-05-09T06:00:00Z",
            }]
        },
    )
    direct_calls: list[str] = []
    agency_calls: list[tuple[str, str, dict[str, object]]] = []

    class DirectClient:
        def __init__(self, *, incidents_url: str | None = None) -> None:
            assert incidents_url == "https://icm.example.test/incidents"
            direct_calls.append("init")
            if state.fail_at == "init":
                raise state.failure

        def list_incidents(self) -> dict[str, object]:
            direct_calls.append("query")
            if state.fail_at == "query":
                raise state.failure
            return state.direct_payload

    class Bridge:
        def probe(self) -> SimpleNamespace:
            return SimpleNamespace(
                available=state.agency_available, has_icm=True, server_tools={"icm": ("list_incidents",)}
            )

        def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, object]) -> dict[str, object] | None:
            agency_calls.append((server, tool, args))
            return state.agency_payload

    def reject_kusto(_query: KustoQuery) -> list[dict[str, object]]:
        raise AssertionError("A populated Agency fallback must not execute a Kusto query.")

    bridge = Bridge()
    monkeypatch.setattr(icm_signal_stage, "IcmClient", DirectClient)
    monkeypatch.setattr(gather, "_load_program_context", lambda *_a, **_kw: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_a, **_kw: (14, 30))
    monkeypatch.setattr(gather, "load_program_knowledge", lambda *_a, **_kw: knowledge)
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_a, **_kw: None)
    monkeypatch.setattr(watch_scan, "build_live_kusto_query_executor", lambda: reject_kusto)
    return SimpleNamespace(
        root=root,
        program=program,
        workstreams=workstreams,
        state=state,
        bridge=bridge,
        direct_calls=direct_calls,
        agency_calls=agency_calls,
        reject_kusto=reject_kusto,
    )


@pytest.mark.parametrize("caller", ["gather", "watch"])
@pytest.mark.parametrize("error_type", [AuthError, CredentialExpired, QueryError, QueryTimeoutError])
@pytest.mark.parametrize("fail_at", ["init", "query"])
def test_direct_icm_failure_warns_safely_and_preserves_actual_fallback(
    icm_fallback_context: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    caller: str,
    error_type: type[Exception],
    fail_at: str,
) -> None:
    context = icm_fallback_context
    context.state.failure = error_type(_PROVIDER_ERROR)
    context.state.fail_at = fail_at
    with caplog.at_level(logging.WARNING):
        if caller == "gather":
            result = gather.gather_program(
                _PROGRAM_ID,
                programs_root=context.root,
                as_of=_NOW,
                loader=lambda *_a, **_kw: ((), 0),
                freshness_loader=lambda *_a, **_kw: ((), 0),
                include_icm=True,
                bridge_factory=lambda: context.bridge,
                kusto_query_executor=context.reject_kusto,
            )
            assert result.discovered_signals == 1
            assert result.integration_errors == ()
        else:
            watch_result = watch_scan.watch_program_once(
                _PROGRAM_ID,
                since=_NOW - timedelta(days=1),
                as_of=_NOW,
                sources=(WatchSource.ICM,),
                programs_root=context.root,
                program_context=(context.program, context.workstreams),
                bridge_provider=lambda: context.bridge,
                auto_approve_signals=False,
            )
            assert watch_result.discovered_signals == 1
    signals = tuple(
        signal for signal in read_signals(_PROGRAM_ID, programs_root=context.root)
        if signal.source == "icm"
    )
    assert len(signals) == 1
    assert signals[0].metadata is not None
    assert signals[0].metadata["source_path"] == "agency"
    assert signals[0].metadata["incident_id"] == "12345"
    assert context.direct_calls == (["init"] if fail_at == "init" else ["init", "query"])
    assert context.agency_calls == [("icm", "list_incidents", {})]
    warnings = [record for record in caplog.records if record.getMessage().startswith("Direct IcM")]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.levelno == logging.WARNING
    assert _PROVIDER_ERROR not in warning.getMessage()
    assert _PROGRAM_ID not in warning.getMessage()
    assert ("authentication" if issubclass(error_type, AuthError) else "query") in warning.getMessage().lower()
    assert "attempting agency/kusto fallback" in warning.getMessage().lower()
    assert warning.args == ()
    assert warning.exc_info is None


@pytest.mark.parametrize("error_type", [AuthError, CredentialExpired, QueryError, QueryTimeoutError])
def test_direct_icm_failure_still_reaches_kusto_fallback(
    error_type: type[Exception], caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    program = replace(
        _program(icm_incidents_url="https://icm.example.test/incidents", prefer_agency=False),
        id=_PROGRAM_ID,
    )
    query = KustoQuery(
        id="synthetic-icm",
        cluster="https://example.test",
        database="IcMDataWarehouse",
        kql="Incidents | take 1",
        section="Incidents",
        render_as="table",
        confidence="high",
        program_ids=(_PROGRAM_ID,),
    )
    calls: list[KustoQuery] = []

    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_incidents(self) -> dict[str, object]:
            raise error_type(_PROVIDER_ERROR)

    def execute(actual_query: KustoQuery) -> list[dict[str, object]]:
        calls.append(actual_query)
        return [{"IncidentId": "12345", "Title": "Synthetic incident", "Date": "2026-05-09"}]

    def unexpected_bridge() -> NoReturn:
        raise AssertionError("Agency is disabled for this fallback.")

    with caplog.at_level(logging.WARNING):
        signals = icm_signal_stage.build_icm_signals(
            program=program,
            program_id=_PROGRAM_ID,
            programs_root=tmp_path,
            as_of=_NOW,
            teams=(),
            workstreams=_workstreams(),
            executor=execute,
            bridge=unexpected_bridge,
            resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
            load_kusto_queries_fn=lambda *_a, **_kw: (query,),
            icm_client_factory=DirectClient,
            warn_fn=logging.getLogger("icm-fallback-test").warning,
        )
    assert calls == [query]
    assert len(signals) == 1
    assert signals[0].metadata is not None
    assert signals[0].metadata["query_id"] == "synthetic-icm"
    assert _PROVIDER_ERROR not in caplog.text
    assert _PROGRAM_ID not in caplog.text


@pytest.mark.parametrize("items", [[], [{"incidentId": "12345", "createDate": "2026-05-09T06:00:00Z"}]])
def test_successful_direct_icm_never_uses_fallback_or_warns(items: list[dict[str, object]]) -> None:
    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_incidents(self) -> dict[str, object]:
            return {"items": items}

    def unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Successful direct IcM must not invoke a fallback or warning.")

    signals = icm_signal_stage.build_icm_signals(
        program=_program(icm_incidents_url="https://icm.example.test/incidents"),
        program_id="acme",
        programs_root="unused",
        as_of=_NOW,
        teams=_teams(),
        workstreams=_workstreams(),
        executor=unexpected_call,
        bridge=unexpected_call,
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        load_kusto_queries_fn=unexpected_call,
        icm_client_factory=DirectClient,
        warn_fn=unexpected_call,
    )
    assert len(signals) == len(items)


@pytest.mark.parametrize("fail_at", ["init", "query"])
def test_unexpected_direct_icm_error_propagates_without_fallback(fail_at: str) -> None:
    failure = RuntimeError("unexpected synthetic defect")

    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            if fail_at == "init":
                raise failure

        def list_incidents(self) -> dict[str, object]:
            raise failure

    def unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Unexpected direct failures must not invoke a fallback or warning.")

    with pytest.raises(RuntimeError) as caught:
        icm_signal_stage.build_icm_signals(
            program=_program(icm_incidents_url="https://icm.example.test/incidents"),
            program_id="acme",
            programs_root="unused",
            as_of=_NOW,
            teams=_teams(),
            workstreams=_workstreams(),
            executor=unexpected_call,
            bridge=unexpected_call,
            resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
            load_kusto_queries_fn=unexpected_call,
            icm_client_factory=DirectClient,
            warn_fn=unexpected_call,
        )
    assert caught.value is failure


@pytest.mark.parametrize("error_type", [AuthError, CredentialExpired, QueryError, QueryTimeoutError])
def test_direct_icm_failure_is_supported_without_a_warning_logger(error_type: type[Exception]) -> None:
    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_incidents(self) -> dict[str, object]:
            raise error_type(_PROVIDER_ERROR)

    assert icm_signal_stage.build_direct_icm_signals(
        program=_program(icm_incidents_url="https://icm.example.test/incidents"),
        program_id="acme",
        as_of=_NOW,
        teams=_teams(),
        workstreams=_workstreams(),
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        icm_client_factory=DirectClient,
        warn_fn=None,
    ) is None


def test_icm_fallback_error_propagates_after_direct_failure(caplog: pytest.LogCaptureFixture) -> None:
    failure = RuntimeError("synthetic fallback defect")

    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_incidents(self) -> dict[str, object]:
            raise QueryError(_PROVIDER_ERROR)

    class FailingBridge:
        def probe(self) -> object:
            raise failure

        def invoke_mcp_tool(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("The failed probe must not invoke a tool.")

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
        icm_signal_stage.build_icm_signals(
            program=_program(icm_incidents_url="https://icm.example.test/incidents"),
            program_id="acme",
            programs_root="unused",
            as_of=_NOW,
            teams=_teams(),
            workstreams=_workstreams(),
            executor=lambda _query: [],
            bridge=FailingBridge(),
            resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
            load_kusto_queries_fn=lambda *_a, **_kw: (),
            icm_client_factory=DirectClient,
            warn_fn=logging.getLogger("icm-fallback-test").warning,
        )
    assert caught.value is failure
    assert _PROVIDER_ERROR not in caplog.text


def test_direct_icm_failure_without_configured_fallback_is_not_reported_as_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DirectClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_incidents(self) -> dict[str, object]:
            raise QueryError(_PROVIDER_ERROR)

    def unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Unconfigured fallback must not execute.")

    with caplog.at_level(logging.WARNING):
        signals = icm_signal_stage.build_icm_signals(
            program=_program(
                kusto_enabled=False,
                icm_incidents_url="https://icm.example.test/incidents",
                prefer_agency=False,
            ),
            program_id="acme",
            programs_root="unused",
            as_of=_NOW,
            teams=_teams(),
            workstreams=_workstreams(),
            executor=unexpected_call,
            bridge=unexpected_call,
            resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
            load_kusto_queries_fn=unexpected_call,
            icm_client_factory=DirectClient,
            warn_fn=logging.getLogger("icm-fallback-test").warning,
        )
    assert signals == ()
    assert "attempting agency/kusto fallback" in caplog.text.lower()
    assert _PROVIDER_ERROR not in caplog.text


@pytest.mark.parametrize("caller", ["gather", "watch"])
@pytest.mark.parametrize(
    "mode",
    [
        "direct-invalid",
        "agency-none",
        "agency-invalid",
        "agency-none-disabled",
        "unavailable",
        "empty-direct",
        "empty-agency",
        "direct-invalid-agency-none",
        "direct-error-unavailable",
    ],
)
def test_icm_payload_and_no_signal_warnings_are_safe_and_precise(
    icm_fallback_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    caller: str,
    mode: str,
) -> None:
    context = icm_fallback_context
    direct_modes = {"direct-invalid", "empty-direct", "direct-invalid-agency-none", "direct-error-unavailable"}
    no_kusto_modes = {"agency-none-disabled", "unavailable", "empty-direct", "empty-agency", "direct-error-unavailable"}
    kusto_modes = {"agency-none", "agency-invalid", "direct-invalid-agency-none"}
    program = replace(
        context.program,
        m365=replace(
            context.program.m365,
            icm_incidents_url="https://icm.example.test/incidents" if mode in direct_modes else None,
        ),
        kusto=replace(context.program.kusto, enabled=mode not in no_kusto_modes),
    )
    context.state.fail_at = "query" if mode == "direct-error-unavailable" else "none"
    if mode in {"direct-invalid", "direct-invalid-agency-none"}:
        context.state.direct_payload = {"unexpected": _PAYLOAD_SENTINEL}
    if mode in {"agency-none", "agency-none-disabled", "direct-invalid-agency-none"}:
        context.state.agency_payload = None
    elif mode == "agency-invalid":
        context.state.agency_payload = {"unexpected": _PAYLOAD_SENTINEL}
    elif mode == "empty-agency":
        context.state.agency_payload = {"items": []}
    context.state.agency_available = mode not in {"unavailable", "direct-error-unavailable"}

    query = KustoQuery(
        id="synthetic-icm",
        cluster="https://example.test",
        database="IcMDataWarehouse",
        kql="Incidents | take 1",
        section="Incidents",
        render_as="table",
        confidence="high",
        program_ids=(_PROGRAM_ID,),
    )
    kusto_calls: list[KustoQuery] = []

    def execute(actual_query: KustoQuery) -> list[dict[str, object]]:
        kusto_calls.append(actual_query)
        return [{"IncidentId": "12345", "Title": "Synthetic incident", "Date": "2026-05-09"}]

    monkeypatch.setattr(gather, "_load_program_context", lambda *_a, **_kw: (program, context.workstreams))
    monkeypatch.setattr(gather, "_load_kusto_queries", lambda *_a, **_kw: (query,))
    monkeypatch.setattr(watch_scan, "build_live_kusto_query_executor", lambda: execute)
    expected_count = 1 if mode == "direct-invalid" or mode in kusto_modes else 0
    with caplog.at_level(logging.WARNING):
        if caller == "gather":
            gather_result = gather.gather_program(
                _PROGRAM_ID,
                programs_root=context.root,
                as_of=_NOW,
                loader=lambda *_a, **_kw: ((), 0),
                freshness_loader=lambda *_a, **_kw: ((), 0),
                include_icm=True,
                bridge_factory=lambda: context.bridge,
                kusto_query_executor=execute,
            )
            assert gather_result.discovered_signals == expected_count
            assert gather_result.integration_errors == ()
        else:
            watch_result = watch_scan.watch_program_once(
                _PROGRAM_ID,
                since=_NOW - timedelta(days=1),
                as_of=_NOW,
                sources=(WatchSource.ICM,),
                programs_root=context.root,
                program_context=(program, context.workstreams),
                bridge_provider=lambda: context.bridge,
                auto_approve_signals=False,
            )
            assert watch_result.discovered_signals == expected_count
    signals = tuple(
        signal for signal in read_signals(_PROGRAM_ID, programs_root=context.root)
        if signal.source == "icm"
    )
    assert len(signals) == expected_count
    if signals:
        assert signals[0].metadata is not None
        if mode == "direct-invalid":
            assert signals[0].metadata["source_path"] == "agency"
        else:
            assert signals[0].metadata["query_id"] == "synthetic-icm"
    assert context.direct_calls == (["init", "query"] if mode in direct_modes else [])
    agency_used = mode not in {"unavailable", "empty-direct", "direct-error-unavailable"}
    assert context.agency_calls == ([("icm", "list_incidents", {})] if agency_used else [])
    assert kusto_calls == ([query] if mode in kusto_modes else [])
    warnings = [
        record for record in caplog.records
        if record.getMessage().startswith(("Direct IcM", "IcM Agency", "IcM gather"))
    ]
    assert len(warnings) == (
        2 if mode in {"agency-none-disabled", "direct-invalid-agency-none", "direct-error-unavailable"} else 1
    )
    for warning in warnings:
        assert warning.levelno == logging.WARNING
        assert _PROGRAM_ID not in warning.getMessage()
        assert _PAYLOAD_SENTINEL not in warning.getMessage()
        assert _PROVIDER_ERROR not in warning.getMessage()
        assert warning.args == ()
        assert warning.exc_info is None
    if mode in no_kusto_modes:
        assert "returned no signals" in warnings[-1].getMessage().lower()
        assert "no kusto-backed icm queries are enabled" in warnings[-1].getMessage().lower()
        assert "unavailable" not in warnings[-1].getMessage().lower()
    if mode in {"direct-invalid", "direct-invalid-agency-none", "agency-invalid"}:
        assert "unexpected payload" in warnings[0].getMessage().lower()
    if mode in {"agency-none", "agency-none-disabled", "direct-invalid-agency-none"}:
        assert any("no payload" in warning.getMessage().lower() for warning in warnings)


@pytest.mark.parametrize("mode", ["direct-invalid", "agency-none", "agency-invalid"])
@pytest.mark.parametrize("with_logger", [False, True])
def test_icm_payload_fallback_logs_only_when_configured(
    icm_fallback_context: SimpleNamespace, tmp_path: Path, mode: str, with_logger: bool
) -> None:
    context = icm_fallback_context
    context.state.fail_at = "none"
    if mode == "direct-invalid":
        context.state.direct_payload = {"unexpected": _PAYLOAD_SENTINEL}
        program = context.program
    else:
        program = replace(
            context.program,
            m365=replace(context.program.m365, icm_incidents_url=None),
        )
        context.state.agency_payload = None if mode == "agency-none" else {"unexpected": _PAYLOAD_SENTINEL}
    query = KustoQuery(
        id="synthetic-icm",
        cluster="https://example.test",
        database="IcMDataWarehouse",
        kql="Incidents | take 1",
        section="Incidents",
        render_as="table",
        confidence="high",
        program_ids=(_PROGRAM_ID,),
    )
    calls: list[KustoQuery] = []
    warnings: list[str] = []

    def execute(actual_query: KustoQuery) -> list[dict[str, object]]:
        calls.append(actual_query)
        return [{"IncidentId": "12345", "Title": "Synthetic incident", "Date": "2026-05-09"}]

    signals = icm_signal_stage.build_icm_signals(
        program=program,
        program_id=_PROGRAM_ID,
        programs_root=tmp_path,
        as_of=_NOW,
        teams=(),
        workstreams=context.workstreams,
        executor=execute,
        bridge=context.bridge,
        resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
        load_kusto_queries_fn=lambda *_a, **_kw: (query,),
        warn_fn=warnings.append if with_logger else None,
    )
    assert len(signals) == 1
    assert signals[0].metadata is not None
    if mode == "direct-invalid":
        assert signals[0].metadata["source_path"] == "agency"
        assert calls == []
    else:
        assert signals[0].metadata["query_id"] == "synthetic-icm"
        assert calls == [query]
    expected_warning = {
        "direct-invalid": "Direct IcM incident access returned an unexpected payload; falling back to Agency/Kusto.",
        "agency-none": "IcM Agency list_incidents returned no payload; falling back to Kusto.",
        "agency-invalid": "IcM Agency list_incidents returned an unexpected payload; falling back to Kusto.",
    }[mode]
    assert warnings == ([expected_warning] if with_logger else [])


@pytest.mark.parametrize("kusto", ["enabled", "disabled", "absent"])
def test_icm_successful_empty_warning_describes_observed_result(
    icm_fallback_context: SimpleNamespace, caplog: pytest.LogCaptureFixture, kusto: str
) -> None:
    context = icm_fallback_context
    context.state.fail_at = "none"
    kusto_config = None if kusto == "absent" else replace(context.program.kusto, enabled=kusto == "enabled")
    program = replace(context.program, kusto=kusto_config)
    with caplog.at_level(logging.WARNING):
        signals = gather._build_icm_signals(
            program=program,
            program_id=_PROGRAM_ID,
            programs_root=context.root,
            as_of=_NOW,
            workstreams=context.workstreams,
            executor=context.reject_kusto,
            bridge=context.bridge,
        )
    assert signals == ()
    assert context.direct_calls == ["init", "query"]
    assert context.agency_calls == []
    warnings = [record for record in caplog.records if record.getMessage().startswith("IcM gather")]
    assert len(warnings) == (0 if kusto == "enabled" else 1)
    if warnings:
        assert warnings[0].getMessage() == "IcM gather returned no signals; no Kusto-backed IcM queries are enabled."
        assert warnings[0].args == ()
        assert warnings[0].exc_info is None


def test_icm_invalid_payload_fallback_error_propagates(
    icm_fallback_context: SimpleNamespace, tmp_path: Path
) -> None:
    context = icm_fallback_context
    context.state.agency_payload = {"unexpected": _PAYLOAD_SENTINEL}
    program = replace(
        context.program,
        m365=replace(context.program.m365, icm_incidents_url=None),
    )
    query = KustoQuery(
        id="synthetic-icm",
        cluster="https://example.test",
        database="IcMDataWarehouse",
        kql="Incidents | take 1",
        section="Incidents",
        render_as="table",
        confidence="high",
        program_ids=(_PROGRAM_ID,),
    )
    failure = RuntimeError("synthetic Kusto fallback defect")

    def execute(_query: KustoQuery) -> list[dict[str, object]]:
        raise failure

    with pytest.raises(RuntimeError) as caught:
        icm_signal_stage.build_icm_signals(
            program=program,
            program_id=_PROGRAM_ID,
            programs_root=tmp_path,
            as_of=_NOW,
            teams=(),
            workstreams=context.workstreams,
            executor=execute,
            bridge=context.bridge,
            resolve_icm_workstream_id_fn=_resolve_icm_workstream_id,
            load_kusto_queries_fn=lambda *_a, **_kw: (query,),
            warn_fn=lambda *_a, **_kw: None,
        )
    assert caught.value is failure
