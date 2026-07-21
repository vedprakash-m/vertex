"""specs/people.md Phase 4, PPL-W4.4/PPL-W4.4b: `vertex kb people refresh` (§8.1, §6.7).

Orchestrates PPL-W4.1's provider port, PPL-W4.2's local file-import
adapter, and PPL-W4.3's field-allowlist/confidence-threshold validation
into one preview/apply flow, routed through the SAME canonical staged
writer (`people_registry_writer.py::apply_shared_registry_patch`) every
other shared-registry mutation already uses -- no parallel write path.

Two independent selections in one run, matching §8.1's "repeatable
--person/--team options": `--person` refreshes `display_name`/`title`/
`department`/`contacts` for named canonical people (PPL-W4.4); `--team`
diffs a COMPLETE provider membership snapshot against `memberships.yaml`
per §6.7's exact rule ("An empty complete membership snapshot may
supersede active memberships; a partial/error result may not") and
applies the diff via the writer's existing per-person `add_list_value`/
`remove_list_value` `team_ids` operations rather than reconstructing each
affected person's full membership set (PPL-W4.4b). A provider-reported
member whose alias does not resolve to any existing canonical person is
skipped, not created -- creating a new canonical person from a provider
observation is an onboarding-flow concern, out of this item's scope, and
surfaced back to the caller via `TeamMembershipDiff.unresolved_provider_aliases`
rather than silently dropped.

Only `local_directory_export` provider types have a registered adapter
(PPL-W4.2); refreshing against any other configured `provider_type`
raises a clear `ConfigError` naming that gap rather than silently no-op.

The `provider_refresh_enabled`/`VERTEX_REGISTRY_DISABLE_PROVIDER_REFRESH`
kill switch (PPL-W1.9) is checked BEFORE any provider call is made --
"a true no-op," matching this item's own verification bar -- not merely
before the write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time

from src.core.exceptions import ConfigError
from src.core.identity_provider_local_import import LocalDirectoryExportProvider
from src.core.identity_provider_port import (
    IdentityLookupRequest,
    IdentityProviderConfig,
    load_identity_providers_document,
    find_provider_config,
)
from src.core.identity_provider_validation import (
    ACCEPTED,
    QUARANTINED,
    REJECTED,
    UNRESOLVED,
    ValidatedFieldObservation,
    load_identity_source_authority_policy,
    quarantine_field_observation,
    validate_and_route_observations,
)
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.ulid import new_ulid
from src.core.people_change_journal import append_people_refresh_telemetry_record
from src.core.people_directory_schema import load_people_directory
from src.core.people_membership_schema import MembershipStatus, read_all_memberships
from src.core.people_query import find_person, find_team
from src.core.people_registry_identity import load_registry_config
from src.core.people_registry_modes import load_effective_registry_config
from src.core.people_registry_writer import RegistryPatchOperation, SharedRegistryWriteResult, apply_shared_registry_patch

_ACCEPTED_FIELD_TO_PERSON_FIELD = {
    "display_name": "display_name",
    "title": "title",
    "department": "department",
    "contacts": "email",
}


@dataclass(frozen=True, slots=True)
class TeamMembershipDiff:
    team_id: str
    team_entity_id: str
    added_person_aliases: tuple[str, ...] = ()
    removed_person_aliases: tuple[str, ...] = ()
    unresolved_provider_aliases: tuple[str, ...] = ()
    complete: bool = False


@dataclass(frozen=True, slots=True)
class RefreshResult:
    provider: str
    refresh_run_id: str
    requested_person_count: int
    kill_switch_engaged: bool
    requested_team_count: int = 0
    accepted: tuple[ValidatedFieldObservation, ...] = ()
    quarantined: tuple[ValidatedFieldObservation, ...] = ()
    rejected: tuple[ValidatedFieldObservation, ...] = ()
    unresolved: tuple[ValidatedFieldObservation, ...] = ()
    team_membership_diffs: tuple[TeamMembershipDiff, ...] = ()
    write_result: SharedRegistryWriteResult | None = None
    partial_success: bool = False


def _now(as_of: datetime | None) -> datetime:
    return (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _build_adapter(config: IdentityProviderConfig, *, import_file: Path | None) -> LocalDirectoryExportProvider:
    if config.provider_type != "local_directory_export":
        raise ConfigError(
            f"No adapter is registered for provider_type {config.provider_type!r}. Only 'local_directory_export' "
            "(PPL-W4.2) is implemented; a live adapter is future work if IT policy changes (specs/people.md §6.8)."
        )
    if import_file is None:
        raise ConfigError("--import-file is required to refresh against a 'local_directory_export' provider.")
    return LocalDirectoryExportProvider(export_path=import_file, provider_name=config.name, tenant_id=config.tenant_id)


def refresh_people_from_provider(
    *,
    programs_root: Path,
    provider_name: str,
    person_refs: tuple[str, ...],
    team_refs: tuple[str, ...] = (),
    import_file: Path | None,
    actor: str,
    reason: str,
    apply: bool,
    as_of: datetime | None = None,
) -> RefreshResult:
    if not person_refs and not team_refs:
        raise ConfigError("At least one --person or --team is required.")
    if not reason.strip():
        raise ConfigError("A non-empty --reason is required.")
    now = _now(as_of)
    knowledge_root = get_shared_knowledge_root(programs_root)
    refresh_run_id = f"refresh-{new_ulid(now)}"

    effective_config = load_effective_registry_config(knowledge_root)
    if effective_config is None or not effective_config.effective_provider_refresh_enabled:
        return RefreshResult(
            provider=provider_name, refresh_run_id=refresh_run_id, requested_person_count=len(person_refs),
            requested_team_count=len(team_refs), kill_switch_engaged=True,
        )

    providers_document = load_identity_providers_document(knowledge_root / "identity_providers.yaml")
    provider_config = find_provider_config(providers_document, provider_name)
    if provider_config is None:
        raise ConfigError(f"Identity provider {provider_name!r} is not configured in identity_providers.yaml.")
    if not provider_config.enabled:
        raise ConfigError(f"Identity provider {provider_name!r} is configured but not enabled.")
    adapter = _build_adapter(provider_config, import_file=import_file)
    if team_refs and not adapter.capabilities().supports_membership_snapshot:
        raise ConfigError(f"Identity provider {provider_name!r} does not support membership snapshots.")

    aliases_by_request_id: dict[str, str] = {}
    requests: list[IdentityLookupRequest] = []
    for index, ref in enumerate(person_refs):
        result = find_person(ref, knowledge_root=knowledge_root)
        if result is None or result.directory is None:
            raise ConfigError(f"--person {ref!r} must resolve to exactly one existing shared canonical person with a directory record.")
        request_id = f"req-{index}"
        aliases_by_request_id[request_id] = result.directory.alias
        requested_fields = tuple(
            field for field in provider_config.allowed_fields if field in adapter.capabilities().supported_fields
        )
        requests.append(
            IdentityLookupRequest(
                request_id=request_id, entity_id=result.entity.entity_id, provider_subject_id=None,
                alias_hint=result.directory.alias, requested_fields=requested_fields,
            )
        )

    run_started = time.monotonic()
    batch = adapter.fetch_people(tuple(requests))
    policy = load_identity_source_authority_policy(knowledge_root=knowledge_root)
    validated = validate_and_route_observations(batch, requests=tuple(requests), provider_config=provider_config, policy=policy)

    accepted = tuple(v for v in validated if v.outcome == ACCEPTED)
    quarantined = tuple(v for v in validated if v.outcome == QUARANTINED)
    rejected = tuple(v for v in validated if v.outcome == REJECTED)
    unresolved = tuple(v for v in validated if v.outcome == UNRESOLVED)

    if apply:
        config = load_registry_config(knowledge_root)
        if config is None:
            raise ConfigError("The shared registry has not been bootstrapped yet.")
        for observation in quarantined:
            quarantine_field_observation(
                knowledge_root, workspace_id=config.workspace_id, provider=provider_name,
                refresh_run_id=refresh_run_id, observation=observation, actor=actor, as_of=now,
            )

    fields_by_request: dict[str, list[tuple[str, object]]] = {}
    for observation in accepted:
        person_field = _ACCEPTED_FIELD_TO_PERSON_FIELD[observation.field_name]
        fields_by_request.setdefault(observation.request_id, []).append((person_field, observation.value))

    operations_list: list[RegistryPatchOperation] = [
        RegistryPatchOperation(
            relative_path="knowledge/people_directory.yaml", action="set_fields",
            match_value=aliases_by_request_id[request_id], fields=tuple(fields),
        )
        for request_id, fields in fields_by_request.items()
    ]

    team_membership_diffs: list[TeamMembershipDiff] = []
    if team_refs:
        people_result = load_people_directory(knowledge_root / "people_directory.yaml")
        people = people_result.people if people_result is not None else ()
        alias_by_entity_id = {person.entity_id: person.alias for person in people}
        entity_id_by_alias = {person.alias.casefold(): person.entity_id for person in people}
        current_memberships = read_all_memberships(knowledge_root)

        resolved_teams: list[tuple[str, str]] = []  # (team_id, team_entity_id)
        for ref in team_refs:
            team_result = find_team(ref, knowledge_root=knowledge_root)
            if team_result is None or team_result.team is None:
                raise ConfigError(f"--team {ref!r} must resolve to exactly one existing shared canonical team with a directory record.")
            resolved_teams.append((team_result.team.id, team_result.entity.entity_id))

        membership_batch = adapter.fetch_team_memberships(tuple(team_id for team_id, _ in resolved_teams))
        for team_id, team_entity_id in resolved_teams:
            if not membership_batch.complete:
                team_membership_diffs.append(TeamMembershipDiff(team_id=team_id, team_entity_id=team_entity_id, complete=False))
                continue
            provider_aliases = {
                membership.person_subject_id.casefold()
                for membership in membership_batch.memberships
                if membership.team_subject_id == team_id
            }
            current_aliases = {
                alias_by_entity_id[membership.person_entity_id].casefold()
                for membership in current_memberships
                if membership.team_entity_id == team_entity_id
                and membership.status == MembershipStatus.ACTIVE
                and membership.person_entity_id in alias_by_entity_id
            }
            to_add = sorted(provider_aliases - current_aliases)
            to_remove = sorted(current_aliases - provider_aliases)
            resolved_add = [alias for alias in to_add if alias in entity_id_by_alias]
            unresolved_add = [alias for alias in to_add if alias not in entity_id_by_alias]
            for alias in resolved_add:
                operations_list.append(
                    RegistryPatchOperation(
                        relative_path="knowledge/people_directory.yaml", action="add_list_value",
                        match_value=alias, field_name="team_ids", value=team_id,
                    )
                )
            for alias in to_remove:
                operations_list.append(
                    RegistryPatchOperation(
                        relative_path="knowledge/people_directory.yaml", action="remove_list_value",
                        match_value=alias, field_name="team_ids", value=team_id,
                    )
                )
            team_membership_diffs.append(
                TeamMembershipDiff(
                    team_id=team_id, team_entity_id=team_entity_id, added_person_aliases=tuple(resolved_add),
                    removed_person_aliases=tuple(to_remove), unresolved_provider_aliases=tuple(unresolved_add), complete=True,
                )
            )

    operations = tuple(operations_list)

    write_result: SharedRegistryWriteResult | None = None
    if operations:
        write_result = apply_shared_registry_patch(
            operations=operations, programs_root=programs_root, actor=actor, reason=reason,
            source="provider_refresh", source_ref=f"{provider_name}:{refresh_run_id}", apply=apply, as_of=now,
        )

    if apply:
        append_people_refresh_telemetry_record(
            knowledge_root, workspace_id=config.workspace_id, refresh_run_id=refresh_run_id,
            provider=provider_name, tenant_id=provider_config.tenant_id, requested_count=len(person_refs),
            observed_count=len(batch.observations), accepted_count=len(accepted), quarantined_count=len(quarantined),
            rejected_count=len(rejected), error_count=len(batch.errors),
            wall_time_seconds=time.monotonic() - run_started, kill_switch_engaged=False,
            authenticated_principal=actor, as_of=now,
        )

    return RefreshResult(
        provider=provider_name, refresh_run_id=refresh_run_id, requested_person_count=len(person_refs),
        kill_switch_engaged=False, requested_team_count=len(team_refs), accepted=accepted, quarantined=quarantined,
        rejected=rejected, unresolved=unresolved, team_membership_diffs=tuple(team_membership_diffs),
        write_result=write_result,
        partial_success=bool(quarantined or rejected) and bool(accepted),
    )
