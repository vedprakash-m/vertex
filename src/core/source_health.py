from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from src.core.decision_source_defaults import get_legacy_decision_source_default
from src.core.gather_state_store import GatherState
from src.core.slice_contract_loader import SliceContract

_WAIVABLE_STATES = frozenset({"stale", "zero_yield", "auth_failed"})
_NEWSLETTER_EDITION_TYPES = frozenset({"detailed", "focused", "condensed", "narrative"})
_EDITION_TYPE_TO_FUNCTION_NAME: dict[str, str] = {"lookback": "review"}
_FUNCTION_REQUIRED_FALLBACK_ROLES: dict[str, tuple[str, ...]] = {
    "deck": ("decision",),
    "review": ("decision",),
}
_FUNCTION_OPTIONAL_ROLES_BY_SOURCE_OF_TRUTH: dict[str, dict[str, frozenset[str]]] = {
    "nudge": {"hybrid": frozenset({"telemetry"})},
    "review": {"hybrid": frozenset({"telemetry"})},
}


@dataclass(frozen=True, slots=True)
class SourceContract:
    contract_id: str
    function: str
    required_roles: tuple[str, ...]
    bound_sources: dict[str, tuple[str, ...]]
    min_freshness_hours: int
    min_yield: int
    criticality: str = "standard"
    decision_channels_by_source: dict[str, tuple[str, ...]] = field(default_factory=dict)
    decision_blocked_artifact_selectors_by_source: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    decision_blocked_artifact_ids_by_source: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True, slots=True)
class SourceWaiver:
    contract_id: str
    role: str
    owner: str
    reason: str
    granted: date
    expires: date


@dataclass(frozen=True, slots=True)
class SourceHealth:
    contract_id: str
    role: str
    state: str
    last_yield: int
    last_fresh: datetime | None
    waiver: SourceWaiver | None
    blocks_confirm: bool


@dataclass(frozen=True, slots=True)
class SliceTelemetryRuntimeSummary:
    failed_contracts: tuple[dict[str, object], ...]
    stale_contracts: tuple[dict[str, object], ...]
    gathered_at: datetime


@dataclass(frozen=True, slots=True)
class SourceHealthSummary:
    function: str
    contract_count: int
    healthy_contract_count: int
    waived_contract_count: int
    role_healths: tuple[SourceHealth, ...]

    @property
    def unhealthy_roles(self) -> tuple[SourceHealth, ...]:
        return tuple(role for role in self.role_healths if role.state != "healthy")


def source_health_function_name_for_edition(edition_type: str | None) -> str:
    normalized = str(edition_type or "").strip().lower()
    if not normalized or normalized in _NEWSLETTER_EDITION_TYPES:
        return "newsletter"
    mapped_function = _EDITION_TYPE_TO_FUNCTION_NAME.get(normalized)
    if mapped_function is not None:
        return mapped_function
    return normalized


def build_slice_telemetry_runtime_summary(
    slice_contracts: tuple[SliceContract, ...],
    gather_state: GatherState,
) -> SliceTelemetryRuntimeSummary | None:
    stale_contracts: list[dict[str, object]] = []
    failed_contracts: list[dict[str, object]] = []
    for contract in slice_contracts:
        telemetry_contract = contract.source_contract.telemetry
        if telemetry_contract is None:
            continue
        query_state = gather_state.query_states.get(telemetry_contract.query_id)
        if not isinstance(query_state, dict):
            continue
        if query_state.get("last_cycle_succeeded") is False:
            failed_contracts.append(
                {
                    "slice_id": contract.id,
                    "query_id": telemetry_contract.query_id,
                }
            )
            continue
        age_hours = _slice_query_state_age_hours(query_state, gathered_at=gather_state.gathered_at)
        if age_hours is None:
            continue
        if age_hours > telemetry_contract.freshness_sla_hours:
            stale_contracts.append(
                {
                    "slice_id": contract.id,
                    "query_id": telemetry_contract.query_id,
                    "age_hours": age_hours,
                    "freshness_sla_hours": telemetry_contract.freshness_sla_hours,
                }
            )

    if not stale_contracts and not failed_contracts:
        return None
    return SliceTelemetryRuntimeSummary(
        failed_contracts=tuple(failed_contracts),
        stale_contracts=tuple(stale_contracts),
        gathered_at=gather_state.gathered_at,
    )


def is_narrative_program(slice_contracts: tuple[SliceContract, ...]) -> bool:
    """Return True for programs with no structured slice contracts (narrative/onboarding programs)."""
    return len(slice_contracts) == 0


def build_narrative_program_source_health(
    gather_state: GatherState,
    *,
    function_name: str = "newsletter",
) -> SourceHealthSummary:
    """Graceful-degrade health for narrative programs that have no slice contracts.

    Instead of enforcing required roles, we evaluate against the channels that
    are actually present in gather_state — any active channel counts as healthy.
    This prevents narrative programs from being hard-blocked by source health
    gates that were designed for structured programs.
    """
    channels = getattr(gather_state, "channel_states", None) or {}
    if not isinstance(channels, dict):
        channels = {}

    role_healths: list[SourceHealth] = []
    for channel_id, channel_state in channels.items():
        if not isinstance(channel_state, dict):
            continue
        is_active = bool(channel_state.get("active") or channel_state.get("last_cycle_succeeded"))
        state = "healthy" if is_active else "zero_yield"
        role_healths.append(
            SourceHealth(
                contract_id="narrative",
                role=str(channel_id),
                state=state,
                last_yield=0,
                last_fresh=None,
                waiver=None,
                blocks_confirm=False,
            )
        )

    healthy_count = sum(1 for r in role_healths if r.state == "healthy")
    return SourceHealthSummary(
        function=function_name,
        contract_count=max(1, len(role_healths)),
        healthy_contract_count=healthy_count,
        waived_contract_count=0,
        role_healths=tuple(role_healths),
    )


def build_slice_source_health_summary(
    slice_contracts: tuple[SliceContract, ...],
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...] = (),
    *,
    function_name: str = "newsletter",
    require_structured_decision_sources: bool | None = None,
) -> SourceHealthSummary | None:
    if is_narrative_program(slice_contracts):
        return build_narrative_program_source_health(gather_state, function_name=function_name)
    return _build_slice_source_health_summary_internal(
        slice_contracts,
        gather_state,
        waivers=waivers,
        function_name=function_name,
        require_structured_decision_sources=require_structured_decision_sources,
        allow_legacy_decision_source_fallback=False,
    )


def build_slice_source_health_summary_for_legacy_compat_tests(
    slice_contracts: tuple[SliceContract, ...],
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...] = (),
    *,
    function_name: str = "newsletter",
    require_structured_decision_sources: bool | None = None,
) -> SourceHealthSummary | None:
    return _build_slice_source_health_summary_internal(
        slice_contracts,
        gather_state,
        waivers=waivers,
        function_name=function_name,
        require_structured_decision_sources=require_structured_decision_sources,
        allow_legacy_decision_source_fallback=True,
    )


def _build_slice_source_health_summary_internal(
    slice_contracts: tuple[SliceContract, ...],
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...] = (),
    *,
    function_name: str = "newsletter",
    require_structured_decision_sources: bool | None = None,
    allow_legacy_decision_source_fallback: bool,
) -> SourceHealthSummary | None:
    contracts = _derive_slice_source_contracts(slice_contracts, function_name=function_name)
    if not contracts:
        return None
    resolved_require_structured_decision_sources = (
        function_name in {"deck", "review"}
        if require_structured_decision_sources is None
        else require_structured_decision_sources
    )
    if allow_legacy_decision_source_fallback:
        resolved_require_structured_decision_sources = False

    role_healths: list[SourceHealth] = []
    healthy_contract_count = 0
    waived_contract_count = 0
    for contract in contracts:
        contract_roles: list[SourceHealth] = []
        for role in contract.required_roles:
            contract_roles.append(
                _evaluate_role_health(
                    contract=contract,
                    role=role,
                    gather_state=gather_state,
                    waivers=waivers,
                    require_structured_decision_sources=resolved_require_structured_decision_sources,
                    allow_legacy_decision_source_fallback=allow_legacy_decision_source_fallback,
                )
            )
        if contract_roles and all(role.state == "healthy" for role in contract_roles):
            healthy_contract_count += 1
        elif contract_roles and all(role.state == "healthy" or role.waiver is not None for role in contract_roles):
            waived_contract_count += 1
        role_healths.extend(contract_roles)

    return SourceHealthSummary(
        function=function_name,
        contract_count=len(contracts),
        healthy_contract_count=healthy_contract_count,
        waived_contract_count=waived_contract_count,
        role_healths=tuple(role_healths),
    )


def _derive_slice_source_contracts(
    slice_contracts: tuple[SliceContract, ...],
    *,
    function_name: str,
) -> tuple[SourceContract, ...]:
    contracts: list[SourceContract] = []
    for contract in slice_contracts:
        required_roles: list[str] = []
        bound_sources: dict[str, tuple[str, ...]] = {}
        optional_roles = _optional_roles_for_function(function_name=function_name, source_of_truth=contract.source_of_truth)
        if contract.source_of_truth in {"ado_primary", "hybrid"}:
            required_roles.append("system_of_record")
            bound_sources["system_of_record"] = _ado_binding_ids(contract)
        if contract.source_of_truth in {"telemetry_primary", "hybrid"} and "telemetry" not in optional_roles:
            required_roles.append("telemetry")
            telemetry = contract.source_contract.telemetry
            bound_sources["telemetry"] = () if telemetry is None else (telemetry.query_id,)
        for role in _FUNCTION_REQUIRED_FALLBACK_ROLES.get(function_name, ()):  # bounded per-function role slice
            required_roles.append(role)
            bound_sources[role] = _fallback_binding_ids(contract)
        if not required_roles:
            continue
        telemetry_contract = contract.source_contract.telemetry
        decision_channels_by_source = {
            entry.source_id: entry.channels
            for entry in contract.source_contract.decision_sources
        }
        decision_blocked_artifact_selectors_by_source = {
            entry.source_id: tuple((selector.workstream_id, selector.artifact_type) for selector in entry.blocked_artifact_selectors)
            for entry in contract.source_contract.decision_sources
        }
        decision_blocked_artifact_ids_by_source = {
            entry.source_id: entry.blocked_artifact_ids
            for entry in contract.source_contract.decision_sources
        }
        contracts.append(
            SourceContract(
                contract_id=contract.id,
                function=function_name,
                required_roles=tuple(required_roles),
                bound_sources=bound_sources,
                min_freshness_hours=(
                    telemetry_contract.freshness_sla_hours
                    if telemetry_contract is not None
                    else max(contract.freshness.block_days * 24, 1)
                ),
                min_yield=1,
                decision_channels_by_source=decision_channels_by_source,
                decision_blocked_artifact_selectors_by_source=decision_blocked_artifact_selectors_by_source,
                decision_blocked_artifact_ids_by_source=decision_blocked_artifact_ids_by_source,
                required=contract.required,
            )
        )
    return tuple(contracts)


def _optional_roles_for_function(*, function_name: str, source_of_truth: str) -> frozenset[str]:
    return _FUNCTION_OPTIONAL_ROLES_BY_SOURCE_OF_TRUTH.get(function_name, {}).get(source_of_truth, frozenset())


def _ado_binding_ids(contract: SliceContract) -> tuple[str, ...]:
    ado = contract.source_contract.ado
    if ado is None:
        return ()
    binding_ids = tuple(
        str(value)
        for value in (*ado.saved_queries, *(f"WI:{work_item_id}" for work_item_id in ado.explicit_work_item_ids))
    )
    if binding_ids:
        return binding_ids
    if ado.filters is not None or ado.tag_expression is not None:
        return (f"slice:{contract.id}:ado",)
    return ()


def _fallback_binding_ids(contract: SliceContract) -> tuple[str, ...]:
    return tuple(str(value) for value in contract.source_contract.fallback_sources if str(value).strip())


def _evaluate_role_health(
    *,
    contract: SourceContract,
    role: str,
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...],
    require_structured_decision_sources: bool,
    allow_legacy_decision_source_fallback: bool,
) -> SourceHealth:
    bound_sources = contract.bound_sources.get(role, ())
    if not bound_sources:
        return _build_role_health(
            contract_id=contract.contract_id,
            role=role,
            state="unbound",
            last_yield=0,
            last_fresh=None,
            waivers=waivers,
            required=_role_is_required(contract, role),
        )
    if role == "telemetry":
        return _evaluate_telemetry_role(contract=contract, source_id=bound_sources[0], gather_state=gather_state, waivers=waivers)
    if role == "decision":
        return _evaluate_decision_role(
            contract=contract,
            source_ids=bound_sources,
            gather_state=gather_state,
            waivers=waivers,
            require_structured_decision_sources=require_structured_decision_sources,
            allow_legacy_decision_source_fallback=allow_legacy_decision_source_fallback,
        )
    return _evaluate_system_of_record_role(contract=contract, gather_state=gather_state, waivers=waivers)


def _evaluate_decision_role(
    *,
    contract: SourceContract,
    source_ids: tuple[str, ...],
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...],
    require_structured_decision_sources: bool,
    allow_legacy_decision_source_fallback: bool,
) -> SourceHealth:
    if require_structured_decision_sources and not _decision_role_has_structured_bindings(contract=contract, source_ids=source_ids):
        return _build_role_health(
            contract_id=contract.contract_id,
            role="decision",
            state="unbound",
            last_yield=0,
            last_fresh=None,
            waivers=waivers,
            required=_role_is_required(contract, "decision"),
        )
    resolved_channels = _decision_role_channels(
        contract=contract,
        source_ids=source_ids,
        gather_state=gather_state,
        allow_legacy_decision_source_fallback=allow_legacy_decision_source_fallback,
    )
    if not resolved_channels:
        return _build_role_health(
            contract_id=contract.contract_id,
            role="decision",
            state="unbound",
            last_yield=0,
            last_fresh=None,
            waivers=waivers,
            required=_role_is_required(contract, "decision"),
        )

    channel_healths = tuple(
        _decision_channel_health(channel_name=channel_name, gather_state=gather_state)
        for channel_name in resolved_channels
    )
    total_yield = sum(entry[1] for entry in channel_healths)
    if any(entry[0] == "healthy" for entry in channel_healths):
        state = "healthy"
    elif any(entry[0] == "stale" for entry in channel_healths):
        state = "stale"
    elif any(entry[0] == "zero_yield" for entry in channel_healths):
        state = "zero_yield"
    else:
        state = "auth_failed"
    if state == "healthy" and _decision_role_has_missing_source_identity(
        contract=contract,
        source_ids=source_ids,
        gather_state=gather_state,
        allow_legacy_decision_source_fallback=allow_legacy_decision_source_fallback,
    ):
        state = "stale"
    return _build_role_health(
        contract_id=contract.contract_id,
        role="decision",
        state=state,
        last_yield=total_yield,
        last_fresh=gather_state.gathered_at,
        waivers=waivers,
        required=_role_is_required(contract, "decision"),
    )


def _decision_role_has_structured_bindings(*, contract: SourceContract, source_ids: tuple[str, ...]) -> bool:
    return all(bool(_configured_decision_channels(contract=contract, source_id=source_id)) for source_id in source_ids)


def _configured_decision_channels(*, contract: SourceContract, source_id: str) -> tuple[str, ...]:
    configured_channels = contract.decision_channels_by_source.get(source_id, ())
    return tuple(channel_name for channel_name in configured_channels if channel_name.strip())


def _decision_role_channels(
    *,
    contract: SourceContract,
    source_ids: tuple[str, ...],
    gather_state: GatherState,
    allow_legacy_decision_source_fallback: bool,
) -> tuple[str, ...]:
    ordered_channels: list[str] = []
    allow_legacy_channel_fallback = allow_legacy_decision_source_fallback and not any(
        _configured_decision_channels(contract=contract, source_id=source_id) for source_id in source_ids
    )
    for source_id in source_ids:
        configured_channels = _configured_decision_channels(contract=contract, source_id=source_id)
        fallback_default = (
            get_legacy_decision_source_default(source_id, program_id=gather_state.program_id)
            if allow_legacy_channel_fallback
            else None
        )
        fallback_channels = fallback_default.channels if fallback_default is not None else ()
        for channel_name in configured_channels or fallback_channels:
            if channel_name not in ordered_channels:
                ordered_channels.append(channel_name)
    return tuple(ordered_channels)


def _decision_channel_health(*, channel_name: str, gather_state: GatherState) -> tuple[str, int]:
    channel_state = gather_state.channels.get(channel_name)
    if not isinstance(channel_state, dict):
        return ("auth_failed", 0)
    if not bool(channel_state.get("active")) or _channel_last_error(channel_state) is not None:
        return ("auth_failed", int(channel_state.get("signal_count") or 0))
    signal_count = int(channel_state.get("signal_count") or 0)
    if signal_count <= 0:
        return ("zero_yield", signal_count)
    if not bool(channel_state.get("meets_expected_min", True)):
        return ("stale", signal_count)
    return ("healthy", signal_count)


def _legacy_blocked_artifact_ids(source_id: str, *, program_id: str) -> tuple[str, ...]:
    default = get_legacy_decision_source_default(source_id, program_id=program_id)
    return tuple(default.blocked_artifact_ids) if default is not None else ()


def _decision_role_has_missing_source_identity(
    *,
    contract: SourceContract,
    source_ids: tuple[str, ...],
    gather_state: GatherState,
    allow_legacy_decision_source_fallback: bool,
) -> bool:
    discovery_state = gather_state.m365_discovery
    if not isinstance(discovery_state, dict) or not bool(discovery_state.get("active")):
        return False
    blocked_artifacts = tuple(discovery_state.get("promotion_blocked_missing_id_artifacts") or ())
    selector_matches = _blocked_m365_selector_matches(
        source_ids=source_ids,
        blocked_artifacts=blocked_artifacts,
        configured_selectors_by_source=contract.decision_blocked_artifact_selectors_by_source,
    )
    if selector_matches is not None:
        return selector_matches
    blocked_ids = {
        str(value).strip()
        for value in tuple(discovery_state.get("promotion_blocked_missing_id_ids") or ())
        if str(value).strip()
    }
    if blocked_ids:
        required_blocked_ids = {
            mapped_id
            for source_id in source_ids
            for mapped_id in (
                contract.decision_blocked_artifact_ids_by_source.get(source_id)
                or (
                    _legacy_blocked_artifact_ids(
                        source_id,
                        program_id=gather_state.program_id,
                    )
                    if allow_legacy_decision_source_fallback and not blocked_artifacts
                    else ()
                )
            )
        }
        if required_blocked_ids:
            return any(mapped_id in blocked_ids for mapped_id in required_blocked_ids)
        return False
    return int(discovery_state.get("promotion_blocked_missing_id_count") or 0) > 0


def _blocked_m365_selector_matches(
    *,
    source_ids: tuple[str, ...],
    blocked_artifacts: tuple[object, ...],
    configured_selectors_by_source: dict[str, tuple[tuple[str, str], ...]],
) -> bool | None:
    blocked_selectors: set[tuple[str, str]] = set()
    for artifact in blocked_artifacts:
        if not isinstance(artifact, dict):
            continue
        workstream_id = str(artifact.get("inferred_workstream") or "").strip()
        artifact_type = str(artifact.get("artifact_type") or "").strip()
        if workstream_id and artifact_type:
            blocked_selectors.add((workstream_id, artifact_type))
    if not blocked_selectors:
        return None
    required_selectors = {
        selector
        for source_id in source_ids
        for selector in configured_selectors_by_source.get(source_id, ())
    }
    if not required_selectors:
        return None
    return any(selector in blocked_selectors for selector in required_selectors)


def _evaluate_telemetry_role(
    *,
    contract: SourceContract,
    source_id: str,
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...],
) -> SourceHealth:
    required = _role_is_required(contract, "telemetry")
    query_state = gather_state.query_states.get(source_id)
    if not isinstance(query_state, dict):
        return _build_role_health(
            contract_id=contract.contract_id,
            role="telemetry",
            state="zero_yield",
            last_yield=0,
            last_fresh=None,
            waivers=waivers,
            required=required,
        )
    if query_state.get("last_cycle_succeeded") is False:
        return _build_role_health(
            contract_id=contract.contract_id,
            role="telemetry",
            state="auth_failed",
            last_yield=0,
            last_fresh=_parse_query_state_last_fresh(query_state),
            waivers=waivers,
            required=required,
        )
    row_count = int(query_state.get("row_count") or 0)
    if row_count < contract.min_yield and not bool(query_state.get("zero_rows_ok")):
        return _build_role_health(
            contract_id=contract.contract_id,
            role="telemetry",
            state="zero_yield",
            last_yield=row_count,
            last_fresh=_parse_query_state_last_fresh(query_state),
            waivers=waivers,
            required=required,
        )
    age_hours = _slice_query_state_age_hours(query_state, gathered_at=gather_state.gathered_at)
    if age_hours is not None and age_hours > contract.min_freshness_hours:
        return _build_role_health(
            contract_id=contract.contract_id,
            role="telemetry",
            state="stale",
            last_yield=row_count,
            last_fresh=_parse_query_state_last_fresh(query_state),
            waivers=waivers,
            required=required,
        )
    return _build_role_health(
        contract_id=contract.contract_id,
        role="telemetry",
        state="healthy",
        last_yield=row_count,
        last_fresh=_parse_query_state_last_fresh(query_state),
        waivers=waivers,
        required=required,
    )


def _evaluate_system_of_record_role(
    *,
    contract: SourceContract,
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...],
) -> SourceHealth:
    required = _role_is_required(contract, "system_of_record")
    channel_state = gather_state.channels.get("ado")
    if not isinstance(channel_state, dict):
        return _build_role_health(
            contract_id=contract.contract_id,
            role="system_of_record",
            state="unbound",
            last_yield=0,
            last_fresh=None,
            waivers=waivers,
            required=required,
        )
    if not bool(channel_state.get("active")) or _channel_last_error(channel_state) is not None:
        return _build_role_health(
            contract_id=contract.contract_id,
            role="system_of_record",
            state="auth_failed",
            last_yield=int(channel_state.get("signal_count") or 0),
            last_fresh=gather_state.gathered_at,
            waivers=waivers,
            required=required,
        )
    signal_count = int(channel_state.get("signal_count") or 0)
    if signal_count < contract.min_yield:
        return _build_role_health(
            contract_id=contract.contract_id,
            role="system_of_record",
            state="zero_yield",
            last_yield=signal_count,
            last_fresh=gather_state.gathered_at,
            waivers=waivers,
            required=required,
        )
    return _build_role_health(
        contract_id=contract.contract_id,
        role="system_of_record",
        state="healthy",
        last_yield=signal_count,
        last_fresh=gather_state.gathered_at,
        waivers=waivers,
        required=required,
    )


def _build_role_health(
    *,
    contract_id: str,
    role: str,
    state: str,
    last_yield: int,
    last_fresh: datetime | None,
    waivers: tuple[SourceWaiver, ...],
    required: bool,
) -> SourceHealth:
    waiver = _find_active_waiver(contract_id=contract_id, role=role, waivers=waivers) if state in _WAIVABLE_STATES else None
    return SourceHealth(
        contract_id=contract_id,
        role=role,
        state=state,
        last_yield=last_yield,
        last_fresh=last_fresh,
        waiver=waiver,
        blocks_confirm=required and state != "healthy" and waiver is None,
    )


def _role_is_required(contract: SourceContract, role: str) -> bool:
    return role == "system_of_record" or contract.required


def _find_active_waiver(
    *,
    contract_id: str,
    role: str,
    waivers: tuple[SourceWaiver, ...],
) -> SourceWaiver | None:
    today = datetime.now(timezone.utc).date()
    active = tuple(
        waiver
        for waiver in waivers
        if waiver.contract_id == contract_id and waiver.role == role and waiver.granted <= today <= waiver.expires
    )
    if not active:
        return None
    return max(active, key=lambda waiver: (waiver.granted, waiver.expires, waiver.owner, waiver.reason))


def _slice_query_state_age_hours(query_state: dict[str, object], *, gathered_at: datetime) -> float | None:
    raw_data_age = query_state.get("data_age_hours")
    if isinstance(raw_data_age, (int, float)):
        return float(raw_data_age)
    raw_last_succeeded = query_state.get("last_succeeded_at")
    if not isinstance(raw_last_succeeded, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_last_succeeded.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
    return round((gathered_at.astimezone(timezone.utc) - parsed).total_seconds() / 3600.0, 2)


def _parse_query_state_last_fresh(query_state: dict[str, object]) -> datetime | None:
    raw_last_succeeded = query_state.get("last_succeeded_at")
    if not isinstance(raw_last_succeeded, str):
        return None
    try:
        return datetime.fromisoformat(raw_last_succeeded.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _channel_last_error(entry: dict[str, object]) -> str | None:
    last_error = entry.get("last_error")
    if isinstance(last_error, str) and last_error.strip():
        return last_error.strip()
    return None


# ── FR-SG-03: Transcript fail-loud ──────────────────────────────────────────

_TRANSCRIPT_CONTRACT_ID = "vertex/transcript"
_TRANSCRIPT_ROLE = "transcript"


def build_transcript_source_health(
    gather_state: GatherState,
    waivers: tuple[SourceWaiver, ...] = (),
) -> SourceHealth | None:
    """Return a SourceHealth for the transcript channel, or None if not configured.

    If transcript is configured (configured_series > 0) but series_id is null,
    state is 'auth_failed' (blocks_confirm=True unless waived).  Satisfies FR-SG-03.
    """
    channels = getattr(gather_state, "channels", None)
    if not isinstance(channels, dict):
        return None
    transcript_ch = channels.get("transcript")
    if not isinstance(transcript_ch, dict):
        return None
    configured_series = int(transcript_ch.get("configured_series") or 0)
    if configured_series == 0:
        return None  # transcript not configured for this program
    series_id_null = int(transcript_ch.get("series_id_null") or 0)
    signal_count = int(transcript_ch.get("signal_count") or 0)
    active = bool(transcript_ch.get("active"))
    if series_id_null > 0:
        state = "auth_failed"
    elif active and signal_count == 0:
        state = "zero_yield"
    else:
        state = "healthy"
    return _build_role_health(
        contract_id=_TRANSCRIPT_CONTRACT_ID,
        role=_TRANSCRIPT_ROLE,
        state=state,
        last_yield=signal_count,
        last_fresh=gather_state.gathered_at if state != "auth_failed" else None,
        waivers=waivers,
        required=True,
    )
