"""Direct coverage for the extracted IcM gather signal stage."""

from __future__ import annotations

from datetime import datetime, timezone

from src.commands.gather_pipeline import icm_signal_stage
from src.core.models_v2 import ADOConfig, KustoConfig, KustoQuery, Program, Team, Workstream


def _program(*, kusto_enabled: bool = True, icm_incidents_url: str | None = None, prefer_agency: bool = True) -> Program:
    from src.core.models_v2 import M365Config

    return Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
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
