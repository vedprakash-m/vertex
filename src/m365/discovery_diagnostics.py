from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MissingIdDiscoveryStatus:
    code: str
    detail: str


def describe_discovery_unavailable_reason(
    *,
    artifact_type: str,
    agency_available: bool,
    has_workiq: bool,
    workiq_cli_available: bool,
    available_tools: set[str],
    runtime_error: str | None = None,
) -> str | None:
    if not agency_available and not workiq_cli_available:
        return "Agency CLI unavailable"
    if not has_workiq and not workiq_cli_available:
        return "WorkIQ MCP server unavailable"
    if workiq_cli_available or not available_tools or "ask_work_iq" in available_tools:
        if runtime_error:
            if artifact_type == "meeting_series":
                return f"WorkIQ meeting discovery failed: {runtime_error}"
            if artifact_type == "email_thread":
                return f"WorkIQ email discovery failed: {runtime_error}"
            return f"WorkIQ Teams discovery failed: {runtime_error}"
        return None
    if artifact_type == "meeting_series":
        if "get_meetings" not in available_tools:
            return "WorkIQ missing get_meetings"
        if runtime_error:
            return f"WorkIQ meeting discovery failed: {runtime_error}"
        return None
    if artifact_type == "email_thread":
        if "search_emails" not in available_tools:
            return "WorkIQ missing search_emails"
        if runtime_error:
            return f"WorkIQ email discovery failed: {runtime_error}"
        return None
    # teams_channel: ONLY path is ask_work_iq NL; no structured fallback tool exists
    if "ask_work_iq" not in available_tools:
        return "WorkIQ ask_work_iq required for Teams discovery"
    if runtime_error:
        return f"WorkIQ Teams discovery failed: {runtime_error}"
    return None


def classify_missing_id_discovery_status(
    *,
    artifact_type: str,
    discovery_active: bool,
    first_discovery_completed_at: str | None,
    agency_available: bool,
    has_workiq: bool,
    workiq_cli_available: bool,
    available_tools: set[str],
    runtime_error: str | None = None,
) -> MissingIdDiscoveryStatus:
    if not discovery_active:
        return MissingIdDiscoveryStatus(
            code="discovery_inactive",
            detail="WorkIQ discovery was inactive on the latest gather.",
        )
    unavailable_reason = describe_discovery_unavailable_reason(
        artifact_type=artifact_type,
        agency_available=agency_available,
        has_workiq=has_workiq,
        workiq_cli_available=workiq_cli_available,
        available_tools=available_tools,
    )
    if unavailable_reason is not None:
        code = "tool_unavailable"
        if unavailable_reason == "Agency CLI unavailable":
            code = "agency_cli_unavailable"
        elif unavailable_reason == "WorkIQ MCP server unavailable":
            code = "workiq_unavailable"
        return MissingIdDiscoveryStatus(code=code, detail=unavailable_reason + ".")
    if runtime_error:
        return MissingIdDiscoveryStatus(
            code="runtime_blocked",
            detail=describe_discovery_unavailable_reason(
                artifact_type=artifact_type,
                agency_available=agency_available,
                has_workiq=has_workiq,
                workiq_cli_available=workiq_cli_available,
                available_tools=available_tools,
                runtime_error=runtime_error,
            )
            or f"WorkIQ discovery failed: {runtime_error}",
        )
    if first_discovery_completed_at:
        return MissingIdDiscoveryStatus(
            code="no_candidates_found",
            detail="Latest active discovery completed but returned no durable-ID candidates.",
        )
    return MissingIdDiscoveryStatus(
        code="not_probed_yet",
        detail="Awaiting the first active discovery pass.",
    )
