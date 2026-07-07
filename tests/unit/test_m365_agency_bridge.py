from __future__ import annotations

import subprocess

import pytest

from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities


def _completed_process(
    *command: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=list(command), returncode=returncode, stdout=stdout, stderr=stderr)


def test_probe_reports_unavailable_when_agency_is_missing() -> None:
    bridge = AgencyBridge(runner=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    # Force the local WorkIQ CLI to be absent so the probe is hermetic regardless of
    # whether workiq.exe happens to be installed on the host running the test.
    bridge._resolve_workiq_executable = lambda: (_ for _ in ()).throw(FileNotFoundError())  # type: ignore[method-assign]

    caps = bridge.probe()

    assert caps == AgencyCapabilities()


def test_ask_workiq_caches_successful_answers_and_retries_failures() -> None:
    bridge = AgencyBridge(runner=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    calls: list[str] = []

    def _fake_uncached(question: str, *, timeout_seconds=None, allow_cli_fallback=True):
        calls.append(question)
        return {"response": "ok"} if question == "answered" else None

    bridge._ask_workiq_uncached = _fake_uncached  # type: ignore[method-assign]

    # Successful answers are memoized for the bridge's lifetime (one real call).
    assert bridge.ask_workiq("answered") == {"response": "ok"}
    assert bridge.ask_workiq("answered") == {"response": "ok"}
    assert calls.count("answered") == 1

    # None (failure / no result) is NOT cached -> the next identical ask retries.
    assert bridge.ask_workiq("empty") is None
    assert bridge.ask_workiq("empty") is None
    assert calls.count("empty") == 2


def test_ask_workiq_can_bypass_success_cache_for_stability_probes() -> None:
    bridge = AgencyBridge(runner=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    calls: list[str] = []

    def _fake_uncached(question: str, *, timeout_seconds=None, allow_cli_fallback=True):
        calls.append(question)
        return {"response": f"answer-{len(calls)}"}

    bridge._ask_workiq_uncached = _fake_uncached  # type: ignore[method-assign]

    assert bridge.ask_workiq("probe") == {"response": "answer-1"}
    assert bridge.ask_workiq("probe", use_cache=False) == {"response": "answer-2"}
    assert bridge.ask_workiq("probe") == {"response": "answer-1"}
    assert calls == ["probe", "probe"]


def test_probe_reports_workiq_cli_when_agency_is_missing() -> None:
    bridge = AgencyBridge(runner=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    caps = bridge.probe()

    assert caps.available is False
    assert caps.has_workiq is False
    assert caps.has_workiq_cli is True


def test_probe_parses_available_mcp_servers() -> None:
    calls: list[list[str]] = []

    def _runner(command, **kwargs):
        del kwargs
        calls.append(command)
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(*command, stdout="workiq\nbluebird\nado\nicm\n")

    bridge = AgencyBridge(runner=_runner)

    caps = bridge.probe()

    assert calls == [["agency", "--version"], ["agency", "mcp", "list"]]
    assert caps.available is True
    assert caps.has_workiq is True
    assert caps.has_bluebird is True
    assert caps.has_ado is True
    assert caps.has_icm is True
    assert caps.tier == "msft"
    assert caps.server_tools == {}


def test_probe_parses_predefined_server_inventory_with_suffixes_and_chatter() -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(
            *command,
            stdout=(
                "🤖 Agency 2026.5.6.4\n"
                "Available Predefined MCP Servers:\n"
                "=====================================\n"
                "\n"
                "bluebird:\n"
                "📦 Local (STDIO)\n"
                "LocalConfig {...}\n"
                "workiq:\n"
                "📦 Local (STDIO)\n"
                "LocalConfig {...}\n"
            ),
        )

    bridge = AgencyBridge(runner=_runner)

    caps = bridge.probe()

    assert caps.available is True
    assert caps.has_bluebird is True
    assert caps.has_workiq is True
    assert caps.tier == "msft"


def test_probe_parses_structured_inventory_and_discovers_server_tools() -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(
            *command,
            stdout='{"servers": [{"name": "ado", "tools": ["get_work_items", "query_wiql"]}, {"name": "icm", "tools": ["list_incidents", "get_incident"]}]}',
        )

    bridge = AgencyBridge(runner=_runner)

    caps = bridge.probe()

    assert caps.available is True
    assert caps.has_ado is True
    assert caps.has_icm is True
    assert caps.server_tools == {
        "ado": ("get_work_items", "query_wiql"),
        "icm": ("list_incidents", "get_incident"),
    }


def test_ask_workiq_returns_parsed_json() -> None:
    bridge = AgencyBridge(
        runner=lambda command, **kwargs: _completed_process(
            *command,
            stdout="  request-id: abc\n  request-id: def\nGrounded WorkIQ answer\n",
        )
    )
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    result = bridge.ask_workiq("find Acme Weekly threads")

    assert result == {"response": "Grounded WorkIQ answer"}


def test_ask_workiq_prefers_mcp_tool_when_available() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout="WorkIQ answer"))
    bridge._capabilities_cache = AgencyCapabilities(available=True, has_workiq=True)
    bridge.invoke_mcp_tool = lambda server, tool, args, timeout_seconds=None: {  # type: ignore[method-assign]
        "response": '{"emails":[{"id":"mail-1"}]}'
    }
    bridge._resolve_workiq_executable = lambda: (_ for _ in ()).throw(AssertionError("CLI fallback should not run"))  # type: ignore[method-assign]

    result = bridge.ask_workiq("find Acme Weekly threads")

    assert result == {"response": '{"emails":[{"id":"mail-1"}]}'}


def test_ask_workiq_invokes_direct_workiq_cli() -> None:
    captured: dict[str, object] = {}

    def _runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _completed_process(*command, stdout="WorkIQ answer")

    bridge = AgencyBridge(runner=_runner)
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    result = bridge.ask_workiq("find Acme Weekly threads")

    assert result == {"response": "WorkIQ answer"}
    assert captured["command"] == ["workiq", "ask", "--question", "find Acme Weekly threads"]
    assert captured["kwargs"] == {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 120,
        "shell": False,
    }


def test_ask_workiq_accepts_timeout_override() -> None:
    captured: dict[str, object] = {}

    def _runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _completed_process(*command, stdout="WorkIQ answer")

    bridge = AgencyBridge(runner=_runner)
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    result = bridge.ask_workiq("find Acme Weekly threads", timeout_seconds=17)

    assert result == {"response": "WorkIQ answer"}
    assert captured["kwargs"] == {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 17,
        "shell": False,
    }


def test_inspect_workiq_returns_raw_process_details() -> None:
    bridge = AgencyBridge(
        runner=lambda command, **kwargs: _completed_process(
            *command,
            returncode=1,
            stdout="request-id: abc\n",
            stderr="Error: upstream failure\n",
        )
    )
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    result = bridge.inspect_workiq("find Acme Weekly threads")

    assert result.executable == "workiq"
    assert result.returncode == 1
    assert result.stdout == "request-id: abc\n"
    assert result.stderr == "Error: upstream failure\n"


def test_ask_workiq_returns_none_on_nonzero_exit() -> None:
    bridge = AgencyBridge(
        runner=lambda command, **kwargs: _completed_process(
            *command,
            returncode=1,
            stdout="request-id: abc\n",
            stderr="Error: upstream failure\n",
        )
    )
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    result = bridge.ask_workiq("find Acme Weekly threads")

    assert result is None


def test_ask_workiq_returns_none_on_eula_prompt() -> None:
    bridge = AgencyBridge(
        runner=lambda command, **kwargs: _completed_process(
            *command,
            stdout=(
                "In order to use this tool you must accept the End User License Agreement\n"
                "workiq accept-eula\n"
            ),
        )
    )
    bridge._resolve_workiq_executable = lambda: "workiq"  # type: ignore[method-assign]

    result = bridge.ask_workiq("find Acme Weekly threads")

    assert result is None


def test_normalize_workiq_cli_output_strips_request_ids() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout='{}'))

    result = bridge._normalize_workiq_cli_output("  request-id: one\n  request-id: two\nUseful output\n\n")

    assert result == "Useful output"


def test_coerce_mcp_tool_result_prefers_structured_content() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout='{}'))

    result = bridge._coerce_mcp_tool_result(
        {
            "structuredContent": {"response": "grounded", "conversationId": "123"},
            "content": [{"type": "text", "text": "ignored"}],
        }
    )

    assert result == {"response": "grounded", "conversationId": "123"}


def test_coerce_mcp_tool_result_falls_back_to_text_blocks() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout='{}'))

    result = bridge._coerce_mcp_tool_result(
        {
            "content": [
                {"type": "text", "text": "WorkIQ summary line 1"},
                {"type": "text", "text": "WorkIQ summary line 2"},
            ]
        }
    )

    assert result == {"response": "WorkIQ summary line 1\nWorkIQ summary line 2"}


def test_decode_mcp_messages_parses_json_lines() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout='{}'))
    payload = bridge._encode_stdio_jsonrpc_messages(
        (
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            {"jsonrpc": "2.0", "id": 2, "result": {"value": 7}},
        )
    )

    messages = bridge._decode_mcp_messages(payload)

    assert messages == [
        {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        {"jsonrpc": "2.0", "id": 2, "result": {"value": 7}},
    ]


def test_invoke_mcp_tool_normalizes_server_alias_and_passes_json_args() -> None:
    captured: dict[str, object] = {}

    bridge = AgencyBridge()

    def _fake_stdio(*, server: str, tool: str, args: dict, timeout: int):
        captured["server"] = server
        captured["tool"] = tool
        captured["args"] = args
        captured["timeout"] = timeout
        return {"rows": 2}

    bridge._invoke_stdio_mcp_tool = _fake_stdio  # type: ignore[method-assign]

    result = bridge.invoke_mcp_tool(
        server="msft-ado",
        tool="query_wiql",
        args={"query": "newsletter feedback", "limit": 5},
    )

    assert result == {"rows": 2}
    assert captured["server"] == "ado"
    assert captured["tool"] == "query_wiql"
    assert captured["args"] == {"query": "newsletter feedback", "limit": 5}
    assert captured["timeout"] == 30


def test_invoke_mcp_tool_rejects_unknown_server() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout="{}"))

    with pytest.raises(ValueError, match="server"):
        bridge.invoke_mcp_tool(server="unknown-server", tool="search_emails", args={})


def test_invoke_mcp_tool_rejects_unknown_tool() -> None:
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout="{}"))

    with pytest.raises(ValueError, match="tool"):
        bridge.invoke_mcp_tool(server="bluebird", tool="drop_database", args={})


@pytest.mark.parametrize("stale_tool", ["search_calendar_events", "search_teams_messages"])
def test_invoke_mcp_tool_rejects_hallucinated_workiq_tools(stale_tool: str) -> None:
    """Ratchet: these tool names never existed and must always be rejected."""
    bridge = AgencyBridge(runner=lambda command, **kwargs: _completed_process(*command, stdout="{}"))

    with pytest.raises(ValueError, match="tool"):
        bridge.invoke_mcp_tool(server="workiq", tool=stale_tool, args={})


def test_invoke_mcp_tool_allows_discovered_icm_tool_after_probe() -> None:
    calls: list[list[str]] = []

    def _runner(command, **kwargs):
        del kwargs
        calls.append(command)
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(
            *command,
            stdout='{"servers": [{"name": "icm", "tools": ["list_incidents"]}]}',
        )

    bridge = AgencyBridge(runner=_runner)
    bridge._invoke_stdio_mcp_tool = (  # type: ignore[method-assign]
        lambda *, server, tool, args, timeout: {"items": [{"incidentId": "12345"}]}
    )

    result = bridge.invoke_mcp_tool(server="icm", tool="list_incidents", args={"severity": 2})

    assert result == {"items": [{"incidentId": "12345"}]}
    assert calls == [
        ["agency", "--version"],
        ["agency", "mcp", "list"],
    ]


def test_invoke_mcp_tool_records_last_error_on_failure_and_clears_on_success() -> None:
    bridge = AgencyBridge()
    bridge._invoke_stdio_mcp_tool = (  # type: ignore[method-assign]
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("workiq timed out"))
    )

    failed = bridge.invoke_mcp_tool(server="workiq", tool="search_emails", args={"query": "acme"})

    assert failed is None
    assert bridge.last_mcp_error() == "workiq timed out"

    bridge._invoke_stdio_mcp_tool = lambda **kwargs: {"items": []}  # type: ignore[method-assign]

    succeeded = bridge.invoke_mcp_tool(server="workiq", tool="search_emails", args={"query": "acme"})

    assert succeeded == {"items": []}
    assert bridge.last_mcp_error() is None


def test_invoke_mcp_tool_rejects_undiscovered_icm_tool() -> None:
    def _runner(command, **kwargs):
        del kwargs
        if command == ["agency", "--version"]:
            return _completed_process(*command, stdout="agency 1.2.3")
        return _completed_process(*command, stdout='{"servers": [{"name": "icm", "tools": ["get_incident"]}]}')

    bridge = AgencyBridge(runner=_runner)

    with pytest.raises(ValueError, match="tool"):
        bridge.invoke_mcp_tool(server="icm", tool="list_incidents", args={})
