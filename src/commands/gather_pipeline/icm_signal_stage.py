from __future__ import annotations

from datetime import datetime, time, timezone
import re
from typing import Any, Callable, Protocol, TypeGuard
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime
from src.commands.gather_pipeline.support import kusto_event_timestamp, parse_date
from src.commands.gather_workiq_helpers import _truncate_signal_text
from src.core.exceptions import AuthError, QueryError
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models import Confidence
from src.core.models_v2 import KustoQuery, Program, Signal, Team, Workstream
from src.core.signal_ref_utils import merge_entity_refs
from src.m365.icm_client import IcmClient

IcmClientFactory = Callable[..., Any]
KustoQueryExecutor = Callable[[KustoQuery], list[dict[str, Any]]]
class AgencyCapabilitiesProtocol(Protocol):
    available: bool
    has_icm: bool
    server_tools: dict[str, tuple[str, ...]]


class AgencyBridgeProtocol(Protocol):
    def probe(self) -> AgencyCapabilitiesProtocol: ...

    def invoke_mcp_tool(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any] | None: ...


AgencyBridgeFactory = object | Callable[[], object]
IcmWorkstreamResolver = Callable[..., str | None]
KustoQueryLoader = Callable[..., tuple[KustoQuery, ...]]
IcmQuerySignalBuilder = Callable[..., tuple[Signal, ...]]
WarningLogger = Callable[..., None]


def _is_agency_bridge_client(value: object) -> TypeGuard[AgencyBridgeProtocol]:
    return hasattr(value, "probe") and hasattr(value, "invoke_mcp_tool")


def build_icm_signals(
    *,
    program: Program,
    program_id: str,
    programs_root: Any,
    as_of: datetime,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
    executor: KustoQueryExecutor,
    bridge: AgencyBridgeFactory,
    resolve_icm_workstream_id_fn: IcmWorkstreamResolver,
    load_kusto_queries_fn: KustoQueryLoader,
    build_icm_query_signals_fn: IcmQuerySignalBuilder | None = None,
    icm_client_factory: IcmClientFactory | None = None,
    warn_fn: WarningLogger | None = None,
) -> tuple[Signal, ...]:
    direct_signals = build_direct_icm_signals(
        program=program,
        program_id=program_id,
        as_of=as_of,
        teams=teams,
        workstreams=workstreams,
        resolve_icm_workstream_id_fn=resolve_icm_workstream_id_fn,
        icm_client_factory=icm_client_factory,
        warn_fn=warn_fn,
    )
    if direct_signals is not None:
        return direct_signals

    agency_signals = build_agency_icm_signals(
        program=program,
        program_id=program_id,
        as_of=as_of,
        teams=teams,
        workstreams=workstreams,
        bridge=bridge,
        resolve_icm_workstream_id_fn=resolve_icm_workstream_id_fn,
        warn_fn=warn_fn,
    )
    if agency_signals is not None:
        return agency_signals

    if program.kusto is None or not program.kusto.enabled:
        return ()

    if build_icm_query_signals_fn is None:
        build_icm_query_signals_fn = build_icm_query_signals

    queries = tuple(
        query
        for query in load_kusto_queries_fn(program_id, program=program, programs_root=programs_root)
        if is_icm_query(query)
    )
    signals: list[Signal] = []
    for query in queries:
        rows = executor(query)
        signals.extend(
            build_icm_query_signals_fn(
                query=query,
                rows=rows,
                program_id=program_id,
                as_of=as_of,
                teams=teams,
                workstreams=workstreams,
                resolve_icm_workstream_id_fn=resolve_icm_workstream_id_fn,
            )
        )
    return tuple(signals)


def build_direct_icm_signals(
    *,
    program: Program,
    program_id: str,
    as_of: datetime,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
    resolve_icm_workstream_id_fn: IcmWorkstreamResolver,
    icm_client_factory: IcmClientFactory | None = None,
    warn_fn: WarningLogger | None = None,
) -> tuple[Signal, ...] | None:
    if not direct_icm_enabled(program):
        return None

    client_builder = icm_client_factory or IcmClient
    incidents_url = program.m365.icm_incidents_url if program.m365 is not None else None
    try:
        client = client_builder(incidents_url=incidents_url)
        payload = client.list_incidents()
    except (AuthError, QueryError) as exc:
        if warn_fn is not None:
            warn_fn("Direct IcM incident access unavailable for %s: %s Falling back to Agency/Kusto.", program_id, exc)
        return None

    records = agency_icm_payload_records(payload)
    if records is None:
        if warn_fn is not None:
            warn_fn(
                "Direct IcM incident access returned an unexpected payload for %s; falling back to Agency/Kusto.",
                program_id,
            )
        return None

    return build_agency_icm_record_signals(
        records=records,
        program_id=program_id,
        as_of=as_of,
        teams=teams,
        workstreams=workstreams,
        resolve_icm_workstream_id_fn=resolve_icm_workstream_id_fn,
        source_path="direct",
        tool="list_incidents",
    )


def build_agency_icm_signals(
    *,
    program: Program,
    program_id: str,
    as_of: datetime,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
    bridge: AgencyBridgeFactory,
    resolve_icm_workstream_id_fn: IcmWorkstreamResolver,
    warn_fn: WarningLogger | None = None,
) -> tuple[Signal, ...] | None:
    if not prefer_agency_icm(program):
        return None

    bridge_client = bridge() if callable(bridge) else bridge
    if not _is_agency_bridge_client(bridge_client):
        return None
    capabilities = bridge_client.probe()
    if not capabilities.available or not capabilities.has_icm:
        return None
    if "list_incidents" not in capabilities.server_tools.get("icm", ()):
        return None

    payload = bridge_client.invoke_mcp_tool("icm", "list_incidents", {})
    if payload is None:
        if warn_fn is not None:
            warn_fn("IcM Agency list_incidents returned no payload for %s; falling back to Kusto.", program_id)
        return None

    records = agency_icm_payload_records(payload)
    if records is None:
        if warn_fn is not None:
            warn_fn("IcM Agency list_incidents returned an unexpected payload for %s; falling back to Kusto.", program_id)
        return None

    return build_agency_icm_record_signals(
        records=records,
        program_id=program_id,
        as_of=as_of,
        teams=teams,
        workstreams=workstreams,
        resolve_icm_workstream_id_fn=resolve_icm_workstream_id_fn,
    )


def prefer_agency_icm(program: Program) -> bool:
    return program.m365 is None or program.m365.prefer_agency


def direct_icm_enabled(program: Program) -> bool:
    return bool(program.m365 is not None and program.m365.enabled and program.m365.icm_incidents_url)


def agency_icm_payload_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    for key in ("items", "incidents", "results", "value"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    incident = payload.get("incident")
    if isinstance(incident, dict):
        return [incident]
    return None


def build_agency_icm_record_signals(
    *,
    records: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
    resolve_icm_workstream_id_fn: IcmWorkstreamResolver,
    source_path: str = "agency",
    tool: str = "list_incidents",
) -> tuple[Signal, ...]:
    fallback_workstream_id = workstreams[0].id if len(workstreams) == 1 else None
    signals: list[Signal] = []
    for record in records:
        incident_id = extract_digits(
            record.get("incidentId")
            or record.get("incident_id")
            or record.get("id")
            or record.get("incidentUrl")
        )
        if incident_id is None:
            continue

        timestamp = agency_icm_event_timestamp(record, as_of=as_of)
        severity = severity_from_value(
            record.get("severity")
            or record.get("severityLevel")
            or record.get("sev")
        )
        owning_team = _optional_string(
            record.get("owningTeamName")
            or record.get("owningTeam")
            or record.get("teamName")
            or record.get("team")
        )
        title = _optional_string(record.get("title") or record.get("name") or record.get("summary"))
        title = title or f"IcM incident {incident_id}"
        status = _optional_string(record.get("status") or record.get("state"))
        age = _optional_string(record.get("age") or record.get("ageDisplay"))
        summary_parts = [title]
        if status is not None:
            summary_parts.append(f"status={status}")
        if age is not None:
            summary_parts.append(f"age={age}")
        workstream_id = resolve_icm_workstream_id_fn(
            owning_team=owning_team,
            fallback_workstream_id=fallback_workstream_id,
            teams=teams,
            workstreams=workstreams,
        )
        entity_refs = merge_entity_refs(
            provider_refs=merge_incident_entity_refs(incident_id, title),
            workstream_id=workstream_id,
        )

        signals.append(
            Signal(
                id=str(uuid5(NAMESPACE_URL, f"{program_id}|icm|agency|{incident_id}|{timestamp.isoformat()}")),
                timestamp=timestamp,
                source="icm",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=entity_refs,
                text=_truncate_signal_text(f"IcM {incident_id}: {'; '.join(summary_parts)}"),
                raw_ref=f"icm:{incident_id}",
                confidence=Confidence.HIGH,
                metadata={
                    "incident_id": incident_id,
                    "severity": severity,
                    "owning_team": owning_team,
                    "source_path": source_path,
                    "tool": tool,
                },
            )
        )
    return tuple(signals)


def agency_icm_event_timestamp(record: dict[str, Any], *, as_of: datetime) -> datetime:
    for key in (
        "createDate",
        "createdDateTime",
        "createdAt",
        "lastUpdatedDateTime",
        "modifiedDateTime",
        "timestamp",
    ):
        parsed = _parse_datetime(record.get(key))
        if parsed is not None:
            return parsed
        parsed_date = parse_date(record.get(key))
        if parsed_date is not None:
            return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    return as_of


def build_icm_query_signals(
    *,
    query: KustoQuery,
    rows: list[dict[str, Any]],
    program_id: str,
    as_of: datetime,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
    resolve_icm_workstream_id_fn: IcmWorkstreamResolver,
) -> tuple[Signal, ...]:
    fallback_workstream_id = query.workstream_ids[0] if len(query.workstream_ids) == 1 else None
    signals: list[Signal] = []
    for row in rows:
        incident_id = extract_digits(row.get("IncidentId") or row.get("IncidentNumber") or row.get("Id"))
        if incident_id is None:
            continue
        timestamp = kusto_event_timestamp([row], as_of=as_of)
        severity = severity_from_value(row.get("Severity"))
        owning_team = _optional_string(row.get("OwningTeam") or row.get("OwningTeamName"))
        title = _optional_string(row.get("Title")) or f"IcM incident {incident_id}"
        status = _optional_string(row.get("Status"))
        age = _optional_string(row.get("Age"))
        summary_parts = [title]
        if status is not None:
            summary_parts.append(f"status={status}")
        if age is not None:
            summary_parts.append(f"age={age}")
        workstream_id = resolve_icm_workstream_id_fn(
            owning_team=owning_team,
            fallback_workstream_id=fallback_workstream_id,
            teams=teams,
            workstreams=workstreams,
        )
        entity_refs = merge_entity_refs(
            provider_refs=merge_incident_entity_refs(incident_id, title),
            workstream_id=workstream_id,
        )
        signals.append(
            Signal(
                id=str(uuid5(NAMESPACE_URL, f"{program_id}|icm|{incident_id}|{timestamp.isoformat()}")),
                timestamp=timestamp,
                source="icm",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=entity_refs,
                text=_truncate_signal_text(f"IcM {incident_id}: {'; '.join(summary_parts)}"),
                raw_ref=f"icm:{incident_id}",
                confidence=Confidence.HIGH,
                metadata={
                    "incident_id": incident_id,
                    "severity": severity,
                    "owning_team": owning_team,
                    "query_id": query.id,
                },
            )
        )
    return tuple(signals)


def severity_from_value(value: Any) -> int:
    text = _optional_string(value)
    if text is None:
        return 0
    match = re.search(r"(\d)", text)
    if match is None:
        return 0
    return int(match.group(1))


def merge_incident_entity_refs(incident_id: str, title: str) -> tuple[str, ...]:
    return (f"ICM:{incident_id}", *extract_work_item_refs(title))


def extract_digits(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    match = re.search(r"(\d{4,})", text)
    if match is None:
        return None
    return match.group(1)


def extract_work_item_refs(text: str) -> tuple[str, ...]:
    refs = re.findall(r"\bWI:(\d+)\b", text, flags=re.IGNORECASE)
    return tuple(f"WI:{ref}" for ref in dict.fromkeys(refs))


def is_icm_query(query: KustoQuery) -> bool:
    return query.database.strip().lower() == "icmdatawarehouse" or query.cluster.strip().lower().startswith("https://icmcluster.")
