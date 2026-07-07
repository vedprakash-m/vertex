from __future__ import annotations

import subprocess
from datetime import timedelta

import pytest

from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.m365.agency_bridge import AgencyBridge


def _completed_process(*command: str, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=list(command), returncode=returncode, stdout=stdout, stderr="")


def test_probe_contract_parses_structured_server_inventory() -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(
            *command,
            stdout=(
                '{"servers": ['
                '{"name": "ado", "tools": ["get_work_items", "get_revisions", "get_comments", "query_wiql"]}, '
                '{"name": "bluebird", "tools": []}, '
                '{"name": "workiq", "tools": ["ask_work_iq", "search_emails", "search_teams", "get_meetings", "get_transcript"]}, '
                '{"name": "icm", "tools": ["list_incidents", "get_incident"]}'
                ']}'
            ),
        )

    bridge = AgencyBridge(runner=_runner)

    caps = bridge.probe()

    assert caps.available is True
    assert caps.has_ado is True
    assert caps.has_bluebird is True
    assert caps.has_workiq is True
    assert caps.has_icm is True
    assert caps.server_tools["ado"] == ("get_work_items", "get_revisions", "get_comments", "query_wiql")
    assert caps.server_tools["workiq"] == (
        "ask_work_iq",
        "search_emails",
        "search_teams",
        "get_meetings",
        "get_transcript",
    )
    assert caps.server_tools["icm"] == ("list_incidents", "get_incident")


def test_invoke_contract_restricts_server_tool_pairs() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout="{}"))

    with pytest.raises(ValueError, match="allowlist"):
        bridge.invoke_mcp_tool(server="ado", tool="search_emails", args={})


def test_invoke_contract_allows_discovered_icm_tool_payloads() -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(
            *command,
            stdout='{"servers": [{"name": "icm", "tools": ["list_incidents"]}]}',
        )

    bridge = AgencyBridge(runner=_runner)
    bridge._invoke_stdio_mcp_tool = (  # type: ignore[method-assign]
        lambda *, server, tool, args, timeout: {"items": [{"incidentId": "12345", "severity": 2, "status": "Active"}]}
    )

    result = bridge.invoke_mcp_tool(server="icm", tool="list_incidents", args={"severity": 2})

    assert result == {"items": [{"incidentId": "12345", "severity": 2, "status": "Active"}]}


def test_ask_workiq_contract_records_breaker_failures_and_short_circuits_when_open(tmp_path) -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(*command, stdout='{"servers": [{"name": "workiq", "tools": ["ask_work_iq"]}]}')

    breaker = CircuitBreaker(
        state_path=tmp_path / ".workiq_breaker.json",
        failure_threshold=3,
        recovery_timeout=timedelta(hours=4),
    )
    bridge = AgencyBridge(runner=_runner, workiq_breaker=breaker)
    bridge._invoke_stdio_mcp_tool = lambda **kwargs: None  # type: ignore[method-assign]
    bridge.inspect_workiq = lambda question, timeout_seconds=None: type(  # type: ignore[method-assign]
        "_Result",
        (),
        {
            "executable": "workiq.exe",
            "returncode": 1,
            "stdout": "",
            "stderr": "transient failure",
            "error": None,
        },
    )()

    assert bridge.ask_workiq("status?") is None
    assert bridge.ask_workiq("status? v2") is None
    assert bridge.ask_workiq("status? v3") is None
    assert breaker.get_state().state == CircuitBreakerState.OPEN

    assert bridge.ask_workiq("status? v4") is None
    assert "circuit breaker is open" in (bridge.last_mcp_error() or "").lower()


def test_ask_workiq_contract_closes_breaker_after_successful_probe(tmp_path) -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(*command, stdout='{"servers": [{"name": "workiq", "tools": ["ask_work_iq"]}]}')

    class _FakeBreaker:
        def __init__(self) -> None:
            self.success_calls: list[bool] = []

        def should_allow_request(self):
            return True, True

        def record_success(self, *, is_probe: bool = False, now=None) -> None:
            del now
            self.success_calls.append(is_probe)

        def record_failure(self, *, error=None, is_probe: bool = False, now=None) -> None:
            raise AssertionError(f"Unexpected failure recording: {error}, probe={is_probe}, now={now}")

    breaker = _FakeBreaker()
    bridge = AgencyBridge(runner=_runner, workiq_breaker=breaker)
    bridge._invoke_stdio_mcp_tool = lambda **kwargs: {"response": "ok"}  # type: ignore[method-assign]

    assert bridge.ask_workiq("status?") == {"response": "ok"}
    assert breaker.success_calls == [True]
