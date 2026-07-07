from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ado_client import ADOClient
from src.core.ado_saved_query_helpers import append_wiql_clause as _append_wiql_clause
from src.core.ado_saved_query_helpers import extract_saved_query_wiql as _extract_saved_query_wiql
from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import QueryError
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    IntegrationError,
    ProviderCapability,
    RegistrationBinding,
    RegistrationStatus,
    ScopeStatus,
    ScopeStatusKind,
    HydrationMode,
)
from src.core.models_v2 import Program, Workstream
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES_ADO
from src.core.slice_contract_loader import SliceContract, SliceFilterDefinition, TagExpression, load_slice_contract


ADO_DISCOVERY_TOP_CAP = 10000
SETUP_DISCOVERY_TOP_CAP = 200


# ---------------------------------------------------------------------------
# Setup introspection helpers (§11.1 — read-only, for vertex setup)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkItemSample:
    """Lightweight work item sample for setup discovery."""
    id: int
    title: str
    work_item_type: str
    area_path: str
    assigned_to: str | None
    state: str
    target_date: str | None


@dataclass(frozen=True, slots=True)
class SuggestedWorkstream:
    """Deterministic workstream suggestion from area path clustering."""
    name: str
    area_paths: tuple[str, ...]
    item_count: int
    rationale: str


def list_projects(client: ADOClient) -> tuple[str, ...]:
    """Return projects visible to the authenticated user.

    Uses the ADO REST API ``_apis/projects`` endpoint.
    """
    url = f"https://dev.azure.com/{client.organization}/_apis/projects?api-version=7.1"
    try:
        response = client._request_json("GET", url)
        projects = response.get("value", [])
        return tuple(sorted(
            p["name"] for p in projects
            if isinstance(p, dict) and "name" in p
        ))
    except Exception:  # noqa: BLE001
        return ()


def list_area_paths(
    client: ADOClient,
    projects: tuple[str, ...],
    *,
    days: int = 90,
) -> tuple[str, ...]:
    """Return distinct AreaPath values from recent WorkItems.

    Queries work items changed in the last ``days`` days across the
    given projects.
    """
    all_paths: set[str] = set()
    changed_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for project in projects:
        wiql = (
            f"SELECT [System.AreaPath] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' "
            f"AND [System.ChangedDate] >= @Today - {days} "
            f"ORDER BY [System.ChangedDate] DESC"
        )
        try:
            ids = client.execute_wiql(wiql, top=SETUP_DISCOVERY_TOP_CAP)
            if ids:
                # Batch-fetch area paths from the IDs
                batch = client.query_work_items_batch(
                    list(ids)[:SETUP_DISCOVERY_TOP_CAP],
                    fields=("System.AreaPath",),
                )
                for item in batch:
                    ap = (item.get("fields") or {}).get("System.AreaPath", "")
                    if ap:
                        all_paths.add(ap)
        except Exception:  # noqa: BLE001
            continue

    return tuple(sorted(all_paths))


def get_recent_work_items(
    client: ADOClient,
    projects: tuple[str, ...],
    area_paths: tuple[str, ...] | None = None,
    *,
    days: int = 30,
    top: int = SETUP_DISCOVERY_TOP_CAP,
) -> tuple[WorkItemSample, ...]:
    """Return lightweight work item samples for setup discovery.

    Filters out terminal states and orders by ChangedDate desc for
    high-signal active items. Titles are truncated to 100 characters
    before returning (§10.3 safety).
    """
    samples: list[WorkItemSample] = []

    for project in projects:
        where_parts = [
            f"[System.TeamProject] = '{project}'",
            f"[System.ChangedDate] >= @Today - {days}",
        ]
        for state in TERMINAL_WORK_ITEM_STATES_ADO:
            where_parts.append(f"[System.State] <> '{state}'")

        if area_paths:
            ap_clauses = " OR ".join(
                f"[System.AreaPath] UNDER '{ap}'" for ap in area_paths
            )
            where_parts.append(f"({ap_clauses})")

        wiql = (
            "SELECT [System.Id] FROM WorkItems WHERE "
            + " AND ".join(where_parts)
            + " ORDER BY [System.ChangedDate] DESC"
        )
        try:
            ids = client.execute_wiql(wiql, top=top)
            if ids:
                fields = client.query_work_items_batch(
                    list(ids)[:top],
                    fields=(
                        "System.Id",
                        "System.Title",
                        "System.WorkItemType",
                        "System.AreaPath",
                        "System.AssignedTo",
                        "System.State",
                        "Microsoft.VSTS.Scheduling.TargetDate",
                    ),
                )
                for item in fields:
                    f = item.get("fields") or {}
                    title = str(f.get("System.Title", ""))[:100]
                    assigned = f.get("System.AssignedTo")
                    if isinstance(assigned, dict):
                        assigned = assigned.get("uniqueName") or assigned.get("displayName")
                    samples.append(WorkItemSample(
                        id=int(f.get("System.Id", item.get("id", 0))),
                        title=title,
                        work_item_type=str(f.get("System.WorkItemType", "")),
                        area_path=str(f.get("System.AreaPath", "")),
                        assigned_to=str(assigned) if assigned else None,
                        state=str(f.get("System.State", "")),
                        target_date=str(f.get("Microsoft.VSTS.Scheduling.TargetDate", "")) or None,
                    ))
        except Exception:  # noqa: BLE001
            continue

    return tuple(samples[:top])


def suggest_workstreams_from_samples(
    samples: tuple[WorkItemSample, ...],
) -> tuple[SuggestedWorkstream, ...]:
    """Deterministic heuristic: cluster by common area path prefix.

    Groups work items by the top 2-3 levels of their area path, then
    creates one workstream per unique group.
    """
    if not samples:
        return ()

    import re as _re

    groups: dict[str, list[WorkItemSample]] = {}
    for sample in samples:
        parts = sample.area_path.split("\\")
        key = "\\".join(parts[:min(3, len(parts))])
        groups.setdefault(key, []).append(sample)

    suggestions: list[SuggestedWorkstream] = []
    for path_prefix, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        parts = path_prefix.split("\\")
        name = parts[-1] if parts else path_prefix
        name = _re.sub(r"[_-]+", " ", name).strip().title()
        if not name:
            continue

        suggestions.append(SuggestedWorkstream(
            name=name,
            area_paths=(path_prefix,),
            item_count=len(items),
            rationale=f"Grouped {len(items)} items by area path prefix '{path_prefix}'.",
        ))

    return tuple(suggestions)


@dataclass(frozen=True, slots=True)
class ADODiscoveryConfig:
    slice_contracts: tuple[SliceContract, ...]
    provider_instance_id: str = "default"
    top: int = ADO_DISCOVERY_TOP_CAP


class ADODiscoveryProvider:
    def __init__(self, client: ADOClient):
        self._client = client

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["ADODiscoveryProvider", ADODiscoveryConfig]:
        del workstreams
        if program.ado is None:
            raise ValueError(f"Program '{program.id}' has no ADO config")
        client = ADOClient(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=program.ado.api_timeout_seconds,
        )
        config = ADODiscoveryConfig(
            slice_contracts=load_slice_contract(programs_root / program.id / "slice_contracts.yaml"),
            provider_instance_id=str((channel_config.extra or {}).get("instance_id") or "default"),
        )
        return cls(client), config

    @property
    def channel(self) -> str:
        return "ado"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="ado",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.PARTIAL),
            hydration_modes=(HydrationMode.FULL, HydrationMode.FRESHNESS_ONLY),
            supports_since=False,
            max_batch_size=200,
            rate_limit_rpm=None,
            retry_max_attempts=2,
            retry_backoff_seconds=0.5,
            privacy_class="internal_content",
            timeout_seconds=45,
        )

    def discover(
        self,
        program_id: str,
        config: ADODiscoveryConfig,
        existing: tuple[ChannelRegistration, ...],
        run_ctx: object = None,
    ) -> DiscoveryResult:
        del existing, run_ctx
        computed_at = datetime.now(timezone.utc)
        refs: list[DiscoveredRef] = []
        statuses: dict[str, ScopeStatus] = {}
        channel_errors: list[IntegrationError] = []

        query_groups: dict[tuple[str, str], list[_ADOScope]] = {}
        for scope in _discovery_scopes(config.slice_contracts):
            query_groups.setdefault((scope.query_id, scope.clause), []).append(scope)

        for group_scopes in query_groups.values():
            try:
                discovered = self._discover_scope_group(program_id, config, group_scopes, computed_at)
                refs.extend(discovered)
                for scope in group_scopes:
                    statuses[scope.scope_id] = ScopeStatus(
                        scope_id=scope.scope_id,
                        status=ScopeStatusKind.SUCCESS,
                        completeness=DiscoveryCompleteness.FULL,
                        item_count=len(discovered),
                        error_message=None,
                    )
            except QueryError as error:
                for scope in group_scopes:
                    statuses[scope.scope_id] = ScopeStatus(
                        scope_id=scope.scope_id,
                        status=ScopeStatusKind.ERROR,
                        completeness=DiscoveryCompleteness.PARTIAL,
                        item_count=0,
                        error_message=str(error),
                    )

        explicit_refs = _explicit_discovered_refs(program_id, config.slice_contracts, config.provider_instance_id, computed_at)
        refs.extend(explicit_refs)
        merged = _dedup_and_merge(refs)
        completeness = DiscoveryCompleteness.FULL
        if any(status.status is not ScopeStatusKind.SUCCESS for status in statuses.values()):
            completeness = DiscoveryCompleteness.PARTIAL
        return DiscoveryResult(
            channel="ado",
            program_id=program_id,
            discovered_refs=merged,
            completeness=completeness,
            scope_statuses=statuses,
            scope_state_updates={},
            errors=tuple(channel_errors),
            computed_at=computed_at,
            provider_instance_id=config.provider_instance_id,
        )

    def _discover_scope_group(
        self,
        program_id: str,
        config: ADODiscoveryConfig,
        scopes: list["_ADOScope"],
        computed_at: datetime,
    ) -> tuple[DiscoveredRef, ...]:
        scope = scopes[0]
        query_payload = self._client.get_saved_query(scope.query_id)
        wiql = _extract_saved_query_wiql(query_payload)
        if wiql is None:
            return ()
        bounded_wiql = _append_wiql_clause(wiql, scope.clause)
        bindings = tuple(
            RegistrationBinding(
                workstream_id=s.workstream_id,
                scope_id=s.scope_id,
                source_type=s.source_type,
                confidence=s.confidence,
                confidence_source=s.source_type,
            )
            for s in scopes
        )
        confidence = max(s.confidence for s in scopes)
        discovered: list[DiscoveredRef] = []
        for work_item_id in self._client.execute_wiql(bounded_wiql, top=config.top):
            ref_id = str(int(work_item_id))
            registration = ChannelRegistration(
                channel="ado",
                program_id=program_id,
                provider_instance_id=config.provider_instance_id,
                ref_id=ref_id,
                ref_kind="work_item",
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=computed_at,
                last_seen_at=computed_at,
                confidence=confidence,
                confidence_source=scope.source_type,
                metadata={"query_id": scope.query_id, "slice_id": scope.slice_id},
            )
            discovered.append(DiscoveredRef(registration=registration, bindings=bindings))
        return tuple(discovered)


@dataclass(frozen=True, slots=True)
class _ADOScope:
    scope_id: str
    query_id: str
    slice_id: str
    workstream_id: str | None
    clause: str
    confidence: float = 1.0
    source_type: str = "wiql_saved_query"


def _discovery_scopes(slice_contracts: tuple[SliceContract, ...]) -> tuple[_ADOScope, ...]:
    scopes: list[_ADOScope] = []
    for contract in slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            continue
        clause_parts = tuple(
            part
            for part in (
                _render_filter_clause(ado_contract.filters) if ado_contract.filters is not None else "",
                _render_tag_expression_clause(ado_contract.tag_expression),
            )
            if part
        )
        clause = " and ".join(f"({part})" for part in clause_parts)
        for query_id in ado_contract.saved_queries:
            scopes.append(
                _ADOScope(
                    scope_id=f"{contract.id}:{query_id}",
                    query_id=query_id,
                    slice_id=contract.id,
                    workstream_id=contract.id,
                    clause=clause,
                )
            )
    return tuple(scopes)


def _explicit_discovered_refs(
    program_id: str,
    slice_contracts: tuple[SliceContract, ...],
    provider_instance_id: str,
    computed_at: datetime,
) -> tuple[DiscoveredRef, ...]:
    refs: list[DiscoveredRef] = []
    for contract in slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            continue
        for work_item_id in ado_contract.explicit_work_item_ids:
            ref_id = str(work_item_id)
            refs.append(
                DiscoveredRef(
                    registration=ChannelRegistration(
                        channel="ado",
                        program_id=program_id,
                        provider_instance_id=provider_instance_id,
                        ref_id=ref_id,
                        ref_kind="work_item",
                        status=RegistrationStatus.ACTIVE,
                        first_discovered_at=computed_at,
                        last_seen_at=computed_at,
                        confidence=1.0,
                        confidence_source="manual_config",
                        metadata={"slice_id": contract.id, "explicit": True},
                    ),
                    bindings=(
                        RegistrationBinding(
                            workstream_id=contract.id,
                            scope_id=f"{contract.id}:explicit",
                            source_type="manual_config",
                            confidence=1.0,
                            confidence_source="manual_config",
                            pm_confirmed=True,
                        ),
                    ),
                )
            )
    return tuple(refs)


def _dedup_and_merge(refs: list[DiscoveredRef]) -> tuple[DiscoveredRef, ...]:
    merged: dict[tuple[str, str, str, str, str], DiscoveredRef] = {}
    for ref in refs:
        key = (
            ref.registration.channel,
            ref.registration.program_id,
            ref.registration.provider_instance_id,
            ref.registration.ref_id,
            ref.registration.ref_kind,
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = ref
            continue
        bindings = tuple(dict.fromkeys((*existing.bindings, *ref.bindings)))
        confidence = max(existing.registration.confidence, ref.registration.confidence)
        confidence_source = ref.registration.confidence_source if ref.registration.confidence >= existing.registration.confidence else existing.registration.confidence_source
        workstream_ids = tuple(dict.fromkeys(binding.workstream_id for binding in bindings if binding.workstream_id is not None))
        merged[key] = DiscoveredRef(
            registration=ChannelRegistration(
                channel=existing.registration.channel,
                program_id=existing.registration.program_id,
                provider_instance_id=existing.registration.provider_instance_id,
                ref_id=existing.registration.ref_id,
                ref_kind=existing.registration.ref_kind,
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=existing.registration.first_discovered_at,
                last_seen_at=max(existing.registration.last_seen_at, ref.registration.last_seen_at),
                confidence=confidence,
                confidence_source=confidence_source,
                pm_confirmed=existing.registration.pm_confirmed or ref.registration.pm_confirmed,
                promoted=existing.registration.promoted or ref.registration.promoted,
                metadata={**(existing.registration.metadata or {}), **(ref.registration.metadata or {})},
                workstream_ids=workstream_ids,
            ),
            bindings=bindings,
        )
    return tuple(merged[key] for key in sorted(merged))


def _render_filter_clause(filter_definition: SliceFilterDefinition) -> str:
    all_of_parts = tuple(part for part in (_render_predicate(predicate) for predicate in filter_definition.all_of) if part)
    any_of_parts = tuple(part for part in (_render_predicate(predicate) for predicate in filter_definition.any_of) if part)
    if any_of_parts:
        return " or ".join("(" + " and ".join((*all_of_parts, any_of)) + ")" for any_of in any_of_parts)
    if all_of_parts:
        return "(" + " and ".join(all_of_parts) + ")"
    return ""


def _render_predicate(predicate: Any) -> str | None:
    field_ref = {
        "title": "[System.Title]",
        "tag": "[System.Tags]",
        "area_path": "[System.AreaPath]",
    }.get(str(getattr(predicate, "field", "")).strip().lower())
    operator = str(getattr(predicate, "op", "")).strip().lower()
    raw_value = str(getattr(predicate, "value", "")).strip()
    if field_ref is None or not raw_value:
        return None
    escaped = raw_value.replace("'", "''")
    if field_ref == "[System.AreaPath]":
        if operator == "eq":
            return f"{field_ref} = '{escaped}'"
        if operator == "contains" and "\\" in raw_value:
            return f"{field_ref} under '{escaped}'"
        return None
    if operator == "eq" and field_ref == "[System.Tags]":
        return f"{field_ref} Contains Words '{escaped}'"
    if operator == "eq":
        return f"{field_ref} = '{escaped}'"
    if operator == "contains":
        return f"{field_ref} contains '{escaped}'"
    return None


def _render_tag_expression_clause(tag_expression: TagExpression | None) -> str:
    if tag_expression is None:
        return ""
    all_of_parts = tuple(_tag_clause(tag) for tag in tag_expression.all_of if tag.strip())
    any_of_parts = tuple(_tag_clause(tag) for tag in tag_expression.any_of if tag.strip())
    if any_of_parts:
        return " and ".join((*all_of_parts, "(" + " or ".join(any_of_parts) + ")"))
    return " and ".join(all_of_parts)


def _tag_clause(tag: str) -> str:
    escaped = tag.strip().replace("'", "''")
    return f"[System.Tags] Contains Words '{escaped}'"


# ---------------------------------------------------------------------------
# FR-SG-29: Graph-based scope expansion
# ---------------------------------------------------------------------------

# ADO relation types that represent Child, Related, and Predecessor links
_LINKED_RELATION_TYPES: frozenset[str] = frozenset({
    "System.LinkTypes.Hierarchy-Forward",   # Child
    "System.LinkTypes.Related",             # Related
    "System.LinkTypes.Dependency-Forward",  # Predecessor/Successor
})


def expand_with_linked_items(
    client: ADOClient,
    seed_ids: frozenset[int],
    *,
    max_depth: int = 1,
) -> frozenset[int]:
    """Return IDs of work items linked to seed_ids (Child/Related/Predecessor).

    Only performs one hop by default (max_depth=1) to avoid unbounded traversal.
    Returns ONLY the new IDs that were not in seed_ids.
    Silently suppresses errors for individual items so a partial fetch does not
    abort the gather pipeline.
    """
    if not seed_ids or max_depth < 1:
        return frozenset()

    discovered: set[int] = set()
    frontier: frozenset[int] = seed_ids

    for _ in range(max_depth):
        if not frontier:
            break
        try:
            relation_items = client.get_work_item_relations(list(frontier))
        except Exception:  # noqa: BLE001 – partial failure is acceptable
            break

        new_ids: set[int] = set()
        for item in relation_items:
            if not isinstance(item, dict):
                continue
            for relation in item.get("relations") or []:
                if not isinstance(relation, dict):
                    continue
                rel_type = str(relation.get("rel") or "")
                if rel_type not in _LINKED_RELATION_TYPES:
                    continue
                url = str(relation.get("url") or "")
                # ADO relation URLs end with the WI id: .../workItems/12345
                parts = url.rstrip("/").rsplit("/", 1)
                if len(parts) == 2 and parts[-1].isdigit():
                    linked_id = int(parts[-1])
                    if linked_id not in seed_ids and linked_id not in discovered:
                        new_ids.add(linked_id)

        discovered.update(new_ids)
        frontier = frozenset(new_ids)

    return frozenset(discovered)
