from __future__ import annotations

import json
import logging
import os
from queue import Empty, Queue
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
import threading
from time import monotonic
from typing import Any, Callable

from src.core.circuit_breaker import CircuitBreaker


log = logging.getLogger(__name__)
SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class AgencyCapabilities:
    """What MCP servers are available via Agency CLI."""

    available: bool = False
    has_workiq: bool = False
    has_workiq_cli: bool = False
    has_ado: bool = False
    has_bluebird: bool = False
    has_icm: bool = False
    tier: str = "none"
    server_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkIQCommandResult:
    executable: str | None
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


class AgencyBridge:
    """Subprocess wrapper around Agency CLI for MCP tool invocation."""

    TIMEOUT = 30
    PROBE_TIMEOUT = 5
    WORKIQ_TIMEOUT = 120

    _SERVER_ALIASES = {
        "ado": "ado",
        "msft-ado": "ado",
        "workiq": "workiq",
        "msft-workiq": "workiq",
        "artha-work-msft": "workiq",
        "bluebird": "bluebird",
        "icm": "icm",
        "msft-icm": "icm",
    }
    _STATIC_ALLOWED_TOOLS = {
        "ado": frozenset({"get_work_items", "get_revisions", "get_comments", "query_wiql"}),
        "bluebird": frozenset(),  # ADO/code search only — no M365 personal data tools
        "workiq": frozenset(
            {
                "ask_work_iq",
                "accept_eula",
                "get_debug_link",
                "search_emails",
                "search_teams",
                "get_meetings",
                "get_transcript",
            }
        ),
        "icm": frozenset(),
    }

    def __init__(
        self,
        *,
        executable: str = "agency",
        runner: SubprocessRunner | None = None,
        workiq_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._executable = executable
        # WS-17: wrap the runner in a bounded-retry policy. The retry helper
        # is opt-out via ``runner=None`` + ``subprocess.run`` if a caller
        # passes a custom runner (test-injected runners bypass retry by
        # design — they assert call counts).
        if runner is None:
            from src.m365.retry_subprocess import build_subprocess_runner
            self._runner: SubprocessRunner = build_subprocess_runner(subprocess.run)
        else:
            self._runner = runner
        self._capabilities_cache: AgencyCapabilities | None = None
        self._last_mcp_error: str | None = None
        self._workiq_breaker = workiq_breaker
        # In-run memoization of successful WorkIQ answers. Discovery re-issues identical
        # questions within a single gather (seeded resolution runs before AND after the
        # broad pass; overlapping per-workstream queries), and each WorkIQ call is slow
        # (36-180s). Caching successful answers for the bridge instance's lifetime cuts
        # those duplicate calls without changing results (M365 data is stable within a run).
        self._ask_cache: dict[str, dict[str, Any]] = {}

    def probe(self) -> AgencyCapabilities:
        """Check Agency CLI installation and available MCP servers."""

        has_workiq_cli = self._workiq_cli_available()
        caps = AgencyCapabilities(has_workiq_cli=has_workiq_cli)
        try:
            result = self._run([self._executable, "--version"], timeout=self.PROBE_TIMEOUT)
            if result.returncode != 0:
                return caps
            caps = AgencyCapabilities(available=True, has_workiq_cli=has_workiq_cli)

            mcp_result = self._run([self._executable, "mcp", "list"], timeout=self.PROBE_TIMEOUT)
            if mcp_result.returncode != 0:
                self._capabilities_cache = caps
                return caps

            servers, server_tools = self._parse_mcp_inventory(mcp_result.stdout or "")
            has_workiq = "workiq" in servers
            has_bluebird = "bluebird" in servers
            has_ado = "ado" in servers
            has_icm = "icm" in servers

            tier = "baseline"
            if has_workiq or has_bluebird:
                tier = "msft"
            elif has_ado or has_icm:
                tier = "enterprise"

            caps = AgencyCapabilities(
                available=True,
                has_workiq=has_workiq,
                has_workiq_cli=has_workiq_cli,
                has_ado=has_ado,
                has_bluebird=has_bluebird,
                has_icm=has_icm,
                tier=tier,
                server_tools=server_tools,
            )
            self._capabilities_cache = caps
            return caps
        except FileNotFoundError:
            log.info("Agency CLI not installed — M365 features unavailable")
            self._capabilities_cache = caps
            return caps
        except Exception as exc:
            log.warning("Agency probe failed: %s", exc)
            self._capabilities_cache = caps
            return caps

    def ask_workiq(
        self,
        question: str,
        *,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
        use_cache: bool = True,
    ) -> dict[str, Any] | None:
        """Ask WorkIQ a question, memoizing successful answers for this bridge's lifetime.

        Discovery re-issues identical questions within one run (seeded resolution runs
        before and after the broad pass); each WorkIQ call is slow, so a cache hit avoids a
        duplicate round-trip. Only non-``None`` results are cached, so a transient failure
        (``None``) is retried rather than stuck.
        """

        if use_cache:
            cached = self._ask_cache.get(question)
            if cached is not None:
                return cached
        result = self._ask_workiq_uncached(
            question, timeout_seconds=timeout_seconds, allow_cli_fallback=allow_cli_fallback
        )
        if result is not None and use_cache:
            self._ask_cache[question] = result
        return result

    def _ask_workiq_uncached(
        self,
        question: str,
        *,
        timeout_seconds: int | None = None,
        allow_cli_fallback: bool = True,
    ) -> dict[str, Any] | None:
        """Ask WorkIQ a question about M365 data (no caching).

        The MCP ``ask_work_iq`` path is the fast path. The local ``workiq.exe`` CLI
        fallback is reliable but slow (often 90–180s), which blows latency-sensitive
        discovery budgets; callers in that hot path pass ``allow_cli_fallback=False`` to
        fail fast on the MCP path instead of degrading into the slow CLI.
        """

        self._last_mcp_error = None
        breaker_probe = False
        if self._workiq_breaker is not None:
            allow_request, breaker_probe = self._workiq_breaker.should_allow_request()
            if not allow_request:
                snapshot = self._workiq_breaker.get_state()
                opened_at = snapshot.last_opened_at.isoformat() if snapshot.last_opened_at is not None else "unknown"
                self._last_mcp_error = (
                    "WorkIQ circuit breaker is open; "
                    f"last_opened_at={opened_at}; "
                    "retry after the recovery timeout or reset the breaker state."
                )
                return None
        timeout = self.WORKIQ_TIMEOUT if timeout_seconds is None or timeout_seconds <= 0 else timeout_seconds
        payload: dict[str, Any] | None = None
        capabilities = self._capabilities_cache or self.probe()
        if capabilities.available and capabilities.has_workiq:
            payload = self.invoke_mcp_tool(
                "workiq",
                "ask_work_iq",
                {"question": question},
                timeout_seconds=timeout,
            )
            if payload is not None:
                if self._workiq_breaker is not None:
                    self._workiq_breaker.record_success(is_probe=breaker_probe)
                return payload

        if allow_cli_fallback:
            result = self.inspect_workiq(question, timeout_seconds=timeout_seconds)
            if result.error is not None:
                self._last_mcp_error = result.error
                log.warning("WorkIQ query failed: %s", result.error)
            elif result.returncode != 0:
                details = self._normalize_workiq_cli_output(result.stderr) or self._normalize_workiq_cli_output(result.stdout)
                if details:
                    self._last_mcp_error = details
                    log.warning(
                        "WorkIQ query failed via %s with exit code %s: %s",
                        result.executable,
                        result.returncode,
                        details,
                    )
            else:
                response = self._normalize_workiq_cli_output(result.stdout)
                if response is None:
                    stderr_text = self._normalize_workiq_cli_output(result.stderr)
                    if stderr_text:
                        self._last_mcp_error = stderr_text
                        log.warning("WorkIQ query returned no response via %s: %s", result.executable, stderr_text)
                elif self._looks_like_eula_prompt(response):
                    self._last_mcp_error = "WorkIQ CLI requires EULA acceptance (`workiq accept-eula`)."
                    log.warning("WorkIQ CLI requires EULA acceptance before use.")
                else:
                    payload = {"response": response}
                    if self._workiq_breaker is not None:
                        self._workiq_breaker.record_success(is_probe=breaker_probe)
                    return payload

        if self._workiq_breaker is not None:
            self._workiq_breaker.record_failure(error=self._last_mcp_error, is_probe=breaker_probe)
        return None

    def inspect_workiq(self, question: str, *, timeout_seconds: int | None = None) -> WorkIQCommandResult:
        """Run the local WorkIQ CLI and return raw process diagnostics."""

        executable: str | None = None
        try:
            executable = self._resolve_workiq_executable()
            resolved_timeout = self.WORKIQ_TIMEOUT if timeout_seconds is None or timeout_seconds <= 0 else timeout_seconds
            result = self._run([executable, "ask", "--question", question], timeout=resolved_timeout)
            return WorkIQCommandResult(
                executable=executable,
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
        except Exception as exc:
            return WorkIQCommandResult(
                executable=executable,
                returncode=None,
                stdout="",
                stderr="",
                error=str(exc),
            )

    def invoke_mcp_tool(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Invoke an allowlisted MCP tool through Agency CLI."""

        canonical_server = self._normalize_server(server)
        if not self._is_allowed_tool(canonical_server, tool):
            raise ValueError(f"MCP tool not in allowlist for server {canonical_server!r}: {tool!r}")
        self._last_mcp_error = None
        timeout = self.TIMEOUT if timeout_seconds is None or timeout_seconds <= 0 else timeout_seconds
        try:
            return self._invoke_stdio_mcp_tool(
                server=canonical_server,
                tool=tool,
                args=args,
                timeout=timeout,
            )
        except Exception as exc:
            self._last_mcp_error = str(exc)
            log.warning("MCP tool %s/%s failed: %s", canonical_server, tool, exc)
            return None

    def last_mcp_error(self) -> str | None:
        return self._last_mcp_error

    def workiq_cli_available(self) -> bool:
        return self._workiq_cli_available()

    def _normalize_server(self, server: str) -> str:
        canonical = self._SERVER_ALIASES.get(server.strip().lower())
        if canonical is None:
            raise ValueError(f"MCP server not in allowlist: {server!r}")
        return canonical

    def _is_allowed_tool(self, server: str, tool: str) -> bool:
        if tool in self._STATIC_ALLOWED_TOOLS.get(server, frozenset()):
            return True
        if server != "icm":
            return False

        capabilities = self._capabilities_cache or self.probe()
        return tool in capabilities.server_tools.get(server, ())

    def _parse_mcp_inventory(self, payload: str) -> tuple[set[str], dict[str, tuple[str, ...]]]:
        parsed = self._load_json_value(payload)
        if parsed is not None:
            servers, tools = self._parse_structured_inventory(parsed)
            if servers:
                return servers, tools
        return self._parse_text_inventory(payload)

    def _parse_structured_inventory(self, payload: Any) -> tuple[set[str], dict[str, tuple[str, ...]]]:
        if isinstance(payload, dict):
            if isinstance(payload.get("servers"), list):
                return self._inventory_from_entries(payload["servers"])
            if isinstance(payload.get("mcpServers"), list):
                return self._inventory_from_entries(payload["mcpServers"])
            entries: list[dict[str, Any]] = []
            for raw_server, raw_details in payload.items():
                canonical = self._SERVER_ALIASES.get(str(raw_server).strip().lower())
                if canonical is None:
                    continue
                details = raw_details if isinstance(raw_details, dict) else {"tools": raw_details}
                entry = {"name": canonical, **details}
                entries.append(entry)
            if entries:
                return self._inventory_from_entries(entries)
            return set(), {}
        if isinstance(payload, list):
            return self._inventory_from_entries(payload)
        return set(), {}

    def _inventory_from_entries(self, entries: list[Any]) -> tuple[set[str], dict[str, tuple[str, ...]]]:
        servers: set[str] = set()
        server_tools: dict[str, tuple[str, ...]] = {}
        for entry in entries:
            if isinstance(entry, str):
                canonical = self._SERVER_ALIASES.get(entry.strip().lower())
                if canonical is None:
                    continue
                servers.add(canonical)
                continue
            if not isinstance(entry, dict):
                continue
            raw_name = entry.get("server") or entry.get("name") or entry.get("id") or entry.get("alias")
            if raw_name is None:
                continue
            canonical = self._SERVER_ALIASES.get(str(raw_name).strip().lower())
            if canonical is None:
                continue
            servers.add(canonical)
            tools = self._coerce_tool_names(
                entry.get("tools")
                or entry.get("toolNames")
                or entry.get("allowed_tools")
                or entry.get("allowedTools")
            )
            if tools:
                server_tools[canonical] = tools
        return servers, server_tools

    def _parse_text_inventory(self, payload: str) -> tuple[set[str], dict[str, tuple[str, ...]]]:
        servers: set[str] = set()
        for line in payload.splitlines():
            canonical = self._SERVER_ALIASES.get(line.strip().lower().rstrip(":"))
            if canonical is not None:
                servers.add(canonical)
        return servers, {}

    def _coerce_tool_names(self, payload: Any) -> tuple[str, ...]:
        if payload in (None, ""):
            return ()
        raw_values: Any = payload
        if isinstance(payload, dict):
            raw_values = payload.keys()
        elif isinstance(payload, str):
            raw_values = (payload,)
        if not isinstance(raw_values, (list, tuple, set, frozenset, type({}.keys()))):
            return ()

        tool_names: list[str] = []
        for raw_value in raw_values:
            text = str(raw_value).strip()
            if not text or text in tool_names:
                continue
            tool_names.append(text)
        return tuple(tool_names)

    def _run(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return self._runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )

    def _resolve_workiq_executable(self) -> str:
        direct_binary = shutil.which("workiq")
        if direct_binary:
            return direct_binary

        install_root = Path.home() / ".agency" / "WorkIQ.Cli.win-x64"
        candidates = sorted(install_root.glob("*/tools/workiq.exe"), reverse=True)
        if candidates:
            return str(candidates[0])
        raise FileNotFoundError("Unable to locate workiq.exe")

    def _workiq_cli_available(self) -> bool:
        try:
            self._resolve_workiq_executable()
        except Exception:
            return False
        return True

    def _normalize_workiq_cli_output(self, payload: str) -> str | None:
        if not payload:
            return None
        lines = [line.rstrip() for line in payload.splitlines()]
        filtered_lines = [line for line in lines if not line.lstrip().startswith("request-id:")]
        response = "\n".join(line for line in filtered_lines if line.strip()).strip()
        return response or None

    def _looks_like_eula_prompt(self, payload: str) -> bool:
        normalized = payload.lower()
        return "end user license agreement" in normalized and "accept-eula" in normalized

    def _invoke_stdio_mcp_tool(
        self,
        *,
        server: str,
        tool: str,
        args: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any] | None:
        command = [self._executable, "mcp", server, "--transport", "stdio"]
        log_dir = Path(os.environ.get("AGENCY_LOG_DIR", Path.home() / ".agency" / "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.setdefault("AGENCY_LOG_DIR", str(log_dir))
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        ) as process:
            payload = self._encode_stdio_jsonrpc_messages(
                (
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "vertex", "version": "1.0"},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": args},
                    },
                )
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                return None
            process.stdin.write(payload)
            process.stdin.flush()
            messages = self._read_stdio_jsonrpc_messages(process.stdout, timeout=timeout)
            process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=1)
            stderr = process.stderr.read()

        if process.returncode not in (0, None):
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                log.warning("Agency MCP tool %s/%s exited with %s: %s", server, tool, process.returncode, stderr_text)
            return None

        call_response = next(
            (
                message
                for message in messages
                if message.get("jsonrpc") == "2.0" and message.get("id") == 2 and isinstance(message.get("result"), dict)
            ),
            None,
        )
        if call_response is None:
            return None
        return self._coerce_mcp_tool_result(call_response["result"])

    def _read_stdio_jsonrpc_messages(self, stream: Any, *, timeout: int) -> list[dict[str, Any]]:
        queue: Queue[bytes | None] = Queue()

        def _reader() -> None:
            while True:
                line = stream.readline()
                if not line:
                    break
                queue.put(line)
            queue.put(None)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

        deadline = monotonic() + timeout
        payload_parts: list[bytes] = []
        while monotonic() < deadline:
            remaining = max(0.01, deadline - monotonic())
            try:
                item = queue.get(timeout=remaining)
            except Empty:
                break
            if item is None:
                break
            payload_parts.append(item)
            messages = self._decode_mcp_messages(b"".join(payload_parts))
            if any(message.get("id") == 2 and isinstance(message.get("result"), dict) for message in messages):
                return messages
        return self._decode_mcp_messages(b"".join(payload_parts))

    def _encode_stdio_jsonrpc_messages(self, payloads: tuple[dict[str, Any], ...]) -> bytes:
        body = "\n".join(json.dumps(payload) for payload in payloads) + "\n"
        return body.encode("utf-8")

    def _decode_mcp_messages(self, payload: bytes) -> list[dict[str, Any]]:
        text = payload.decode("utf-8", errors="replace")
        json_lines: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed_line = json.loads(stripped)
            except json.JSONDecodeError:
                json_lines = []
                break
            if isinstance(parsed_line, dict):
                json_lines.append(parsed_line)
        if json_lines:
            return json_lines

        messages: list[dict[str, Any]] = []
        cursor = 0
        total_length = len(payload)
        while cursor < total_length:
            header_end = payload.find(b"\r\n\r\n", cursor)
            if header_end < 0:
                break
            header_text = payload[cursor:header_end].decode("ascii", errors="replace")
            content_length: int | None = None
            for line in header_text.split("\r\n"):
                name, separator, value = line.partition(":")
                if separator and name.strip().lower() == "content-length":
                    content_length = int(value.strip())
                    break
            if content_length is None:
                break
            body_start = header_end + 4
            body_end = body_start + content_length
            if body_end > total_length:
                break
            try:
                parsed = json.loads(payload[body_start:body_end].decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                messages.append(parsed)
            cursor = body_end
        return messages

    def _coerce_mcp_tool_result(self, result: dict[str, Any]) -> dict[str, Any] | None:
        structured_content = result.get("structuredContent")
        if isinstance(structured_content, dict):
            return structured_content

        content = result.get("content")
        if not isinstance(content, list):
            return result

        text_blocks = [
            str(entry.get("text"))
            for entry in content
            if isinstance(entry, dict) and entry.get("type") == "text" and isinstance(entry.get("text"), str)
        ]
        if not text_blocks:
            return result

        joined_text = "\n".join(block for block in text_blocks if block.strip())
        if not joined_text.strip():
            return result
        parsed = self._load_json_value(joined_text)
        if isinstance(parsed, dict):
            return parsed
        return {"response": joined_text}

    def _load_json_value(self, payload: str) -> Any | None:
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def _load_json(self, payload: str) -> dict[str, Any] | None:
        if not payload:
            return None
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
        log.warning("Agency CLI returned non-object JSON payload")
        return None
