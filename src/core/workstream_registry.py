from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any

import yaml

from src.core.config_loader import NarrativeProgramContext
from src.core.exceptions import ConfigError
from src.core.jinja_filters import build_anchor
from src.core.models import ScorecardEvidencePacket, WorkItem
from src.core.quality_matrix_engine import QualityMatrix
from src.core.slice_contract_loader import SliceContract
from src.core.workstream_association_store import WorkstreamAssociationRecord


@dataclass(frozen=True, slots=True)
class WorkstreamStakeholder:
    name: str
    role: str
    email: str | None = None
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class WorkstreamRegistryEntry:
    id: str
    name: str
    lifecycle_state: str
    sub_program_id: str | None = None
    aliases: tuple[str, ...] = ()
    area_paths: tuple[str, ...] = ()
    source_slice_ids: tuple[str, ...] = ()
    background: str | None = None
    history_summary: str | None = None
    reporting_guidance: str | None = None
    stakeholders: tuple[WorkstreamStakeholder, ...] = ()
    key_ado_items: tuple[int, ...] = ()
    overdue_ado_item_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class WorkstreamIssueSnapshotEntry:
    workstream_id: str
    name: str
    lifecycle_state: str
    report_relevance: str
    association_health: str
    sub_program_id: str | None
    source_slice_ids: tuple[str, ...]
    quality_state: str | None
    status: str | None
    assigned_item_count: int
    assigned_item_ids: tuple[int, ...]
    narrative_item_ids: tuple[int, ...]
    drift_item_ids: tuple[int, ...]
    issues: tuple[str, ...]
    background: str | None = None
    history_summary: str | None = None
    reporting_guidance: str | None = None
    stakeholders: tuple[WorkstreamStakeholder, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkstreamIssueSnapshot:
    schema_version: str
    program_id: str
    issue_number: int
    edition: str
    generated_at: datetime
    workstreams: tuple[WorkstreamIssueSnapshotEntry, ...]


_TITLE_NORMALIZER = re.compile(r"\s+")


def registry_path_for_program(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "workstream_registry.yaml"


def load_authored_workstream_registry(
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[WorkstreamRegistryEntry, ...]:
    """Load authored workstream registry without requiring slice_contracts.

    A missing registry file returns (). Malformed content raises ConfigError.
    """
    path = registry_path_for_program(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if str(payload.get("schema_version") or "") != "1.0":
        raise ConfigError(f"Unsupported workstream registry schema version in {path}")
    raw_workstreams = payload.get("workstreams", [])
    if not isinstance(raw_workstreams, list):
        raise ConfigError(f"workstreams must be a list in {path}")
    entries: list[WorkstreamRegistryEntry] = []
    for raw_entry in raw_workstreams:
        entries.append(_parse_registry_entry_authored(raw_entry, path))
    return tuple(entries)


def load_workstream_registry(
    *,
    program_id: str,
    slice_contracts: tuple[SliceContract, ...],
    programs_root: Path,
    program_context: NarrativeProgramContext | None = None,
) -> tuple[WorkstreamRegistryEntry, ...]:
    defaults = {entry.id: entry for entry in _derive_default_registry_entries(slice_contracts, program_context=program_context)}
    path = registry_path_for_program(program_id, programs_root=programs_root)
    if not path.exists():
        return tuple(defaults.values())

    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if str(payload.get("schema_version") or "") != "1.0":
        raise ConfigError(f"Unsupported workstream registry schema version in {path}")

    raw_workstreams = payload.get("workstreams", [])
    if not isinstance(raw_workstreams, list):
        raise ConfigError(f"workstreams must be a list in {path}")

    ordered_ids = [entry.id for entry in defaults.values()]
    merged: dict[str, WorkstreamRegistryEntry] = dict(defaults)
    for raw_entry in raw_workstreams:
        parsed = _parse_registry_entry(raw_entry, path)
        base_entry = merged.get(parsed.id)
        merged[parsed.id] = _merge_registry_entry(base_entry, parsed)
        if parsed.id not in ordered_ids:
            ordered_ids.append(parsed.id)
    return tuple(merged[workstream_id] for workstream_id in ordered_ids)


def build_workstream_issue_snapshot(
    *,
    program_id: str,
    issue_number: int,
    edition: str,
    generated_at: datetime,
    registry_entries: tuple[WorkstreamRegistryEntry, ...],
    quality_matrix: QualityMatrix,
    markdown_body: str,
    items: tuple[WorkItem, ...],
) -> WorkstreamIssueSnapshot:
    slice_rows = {row.slice_id: row for row in quality_matrix.slices}
    narrative_sections = _split_markdown_sections(markdown_body)
    snapshot_entries: list[WorkstreamIssueSnapshotEntry] = []
    for entry in registry_entries:
        source_slice_ids = entry.source_slice_ids or ((entry.id,) if entry.id in slice_rows else ())
        assigned_item_ids: list[int] = []
        issues: list[str] = []
        quality_states: list[str] = []
        statuses: list[str] = []
        for slice_id in source_slice_ids:
            row = slice_rows.get(slice_id)
            if row is None:
                continue
            assigned_item_ids.extend(row.assigned_item_ids)
            issues.extend(row.issues)
            quality_states.append(row.quality_state)
            statuses.append(row.status)
        persisted_item_ids = tuple(dict.fromkeys(sorted(assigned_item_ids)))
        narrative_item_ids = _extract_narrative_item_ids(entry, narrative_sections)
        drift_item_ids = tuple(item_id for item_id in narrative_item_ids if item_id not in persisted_item_ids)
        if drift_item_ids:
            issues.append(
                "Narrative references are not fully represented in persistent slice membership: "
                + ", ".join(f"ADO#{item_id}" for item_id in drift_item_ids)
                + "."
            )
        snapshot_entries.append(
            WorkstreamIssueSnapshotEntry(
                workstream_id=entry.id,
                name=entry.name,
                lifecycle_state=entry.lifecycle_state,
                report_relevance=_report_relevance(entry, persisted_item_ids, narrative_item_ids, issues, narrative_sections),
                association_health=_association_health(persisted_item_ids, narrative_item_ids, drift_item_ids),
                sub_program_id=entry.sub_program_id,
                source_slice_ids=source_slice_ids,
                quality_state=_pick_state(quality_states),
                status=_pick_state(statuses),
                assigned_item_count=len(persisted_item_ids),
                assigned_item_ids=persisted_item_ids,
                narrative_item_ids=narrative_item_ids,
                drift_item_ids=drift_item_ids,
                issues=tuple(dict.fromkeys(issues)),
                background=entry.background,
                history_summary=entry.history_summary,
                reporting_guidance=entry.reporting_guidance,
                stakeholders=entry.stakeholders,
            )
        )
    return WorkstreamIssueSnapshot(
        schema_version="1.0",
        program_id=program_id,
        issue_number=issue_number,
        edition=edition,
        generated_at=generated_at,
        workstreams=tuple(snapshot_entries),
    )


def build_workstream_issue_snapshot_from_packets(
    *,
    program_id: str,
    issue_number: int,
    edition: str,
    generated_at: datetime,
    registry_entries: tuple[WorkstreamRegistryEntry, ...],
    slice_contracts: tuple[SliceContract, ...],
    scorecard_packets: dict[str, dict[str, ScorecardEvidencePacket]],
    narrative_blurbs: dict[str, str],
) -> WorkstreamIssueSnapshot:
    contracts_by_id = {contract.id: contract for contract in slice_contracts}
    packets_by_id = {
        contract.id: scorecard_packets.get(contract.scorecard_name, {}).get(contract.title)
        for contract in slice_contracts
    }
    snapshot_entries: list[WorkstreamIssueSnapshotEntry] = []
    for entry in registry_entries:
        source_slice_ids = entry.source_slice_ids or ((entry.id,) if entry.id in contracts_by_id else ())
        assigned_item_ids: list[int] = []
        issues: list[str] = []
        section_ids = tuple(
            dict.fromkeys(
                section_id_for_slice_contract(contract)
                for slice_id in source_slice_ids
                for contract in ([contracts_by_id.get(slice_id)] if slice_id in contracts_by_id else [])
                if contract is not None
            )
        )
        for slice_id in source_slice_ids:
            packet = packets_by_id.get(slice_id)
            if packet is None:
                continue
            assigned_item_ids.extend(packet.item_ids)
        persisted_item_ids = tuple(dict.fromkeys(sorted(assigned_item_ids)))
        narrative_texts = [
            narrative_blurbs.get(section_id, "")
            for section_id in section_ids
            if narrative_blurbs.get(section_id, "").strip()
        ]
        narrative_item_ids = _extract_narrative_item_ids_from_texts(narrative_texts)
        drift_item_ids = tuple(item_id for item_id in narrative_item_ids if item_id not in persisted_item_ids)
        if drift_item_ids:
            issues.append(
                "Narrative references are not fully represented in persistent slice membership: "
                + ", ".join(f"ADO#{item_id}" for item_id in drift_item_ids)
                + "."
            )
        snapshot_entries.append(
            WorkstreamIssueSnapshotEntry(
                workstream_id=entry.id,
                name=entry.name,
                lifecycle_state=entry.lifecycle_state,
                report_relevance=_report_relevance_from_presence(
                    entry,
                    persisted_item_ids,
                    narrative_item_ids,
                    issues,
                    has_full_section=bool(narrative_texts),
                ),
                association_health=_association_health(persisted_item_ids, narrative_item_ids, drift_item_ids),
                sub_program_id=entry.sub_program_id,
                source_slice_ids=source_slice_ids,
                quality_state=None,
                status=None,
                assigned_item_count=len(persisted_item_ids),
                assigned_item_ids=persisted_item_ids,
                narrative_item_ids=narrative_item_ids,
                drift_item_ids=drift_item_ids,
                issues=tuple(dict.fromkeys(issues)),
                background=entry.background,
                history_summary=entry.history_summary,
                reporting_guidance=entry.reporting_guidance,
                stakeholders=entry.stakeholders,
            )
        )
    return WorkstreamIssueSnapshot(
        schema_version="1.0",
        program_id=program_id,
        issue_number=issue_number,
        edition=edition,
        generated_at=generated_at,
        workstreams=tuple(snapshot_entries),
    )


def build_workstream_association_records(
    *,
    snapshot: WorkstreamIssueSnapshot,
    slice_contracts: tuple[SliceContract, ...],
    items: tuple[WorkItem, ...],
) -> tuple[WorkstreamAssociationRecord, ...]:
    contracts_by_id = {contract.id: contract for contract in slice_contracts}
    item_lookup = {item.id: item for item in items}
    records: list[WorkstreamAssociationRecord] = []
    seen: set[tuple[str, str, str | None, str | None, int | None, str | None]] = set()
    for entry in snapshot.workstreams:
        for slice_id in entry.source_slice_ids:
            contract = contracts_by_id.get(slice_id)
            _append_association_record(
                records,
                seen,
                WorkstreamAssociationRecord(
                    recorded_at=snapshot.generated_at,
                    edition=snapshot.edition,
                    issue_number=snapshot.issue_number,
                    workstream_id=entry.workstream_id,
                    source_type="curated_slice",
                    source_slice_id=slice_id,
                    section_id=(section_id_for_slice_contract(contract) if contract is not None else None),
                    note=(contract.title if contract is not None else None),
                ),
            )

        for item_id in entry.assigned_item_ids:
            item = item_lookup.get(item_id)
            emitted = False
            if item is not None:
                for slice_id in entry.source_slice_ids:
                    contract = contracts_by_id.get(slice_id)
                    if contract is None:
                        continue
                    section_id = section_id_for_slice_contract(contract)
                    if _is_explicit_item(contract, item_id):
                        emitted = True
                        _append_association_record(
                            records,
                            seen,
                            WorkstreamAssociationRecord(
                                recorded_at=snapshot.generated_at,
                                edition=snapshot.edition,
                                issue_number=snapshot.issue_number,
                                workstream_id=entry.workstream_id,
                                source_type="curated_item",
                                source_slice_id=slice_id,
                                section_id=section_id,
                                work_item_id=item_id,
                            ),
                        )
                    if _matches_saved_query_scope(contract, item):
                        emitted = True
                        _append_association_record(
                            records,
                            seen,
                            WorkstreamAssociationRecord(
                                recorded_at=snapshot.generated_at,
                                edition=snapshot.edition,
                                issue_number=snapshot.issue_number,
                                workstream_id=entry.workstream_id,
                                source_type="query_derived",
                                source_slice_id=slice_id,
                                section_id=section_id,
                                work_item_id=item_id,
                            ),
                        )
                    if _matches_area_path_scope(contract, item):
                        emitted = True
                        _append_association_record(
                            records,
                            seen,
                            WorkstreamAssociationRecord(
                                recorded_at=snapshot.generated_at,
                                edition=snapshot.edition,
                                issue_number=snapshot.issue_number,
                                workstream_id=entry.workstream_id,
                                source_type="area_path_derived",
                                source_slice_id=slice_id,
                                section_id=section_id,
                                work_item_id=item_id,
                            ),
                        )
            if not emitted:
                _append_association_record(
                    records,
                    seen,
                    WorkstreamAssociationRecord(
                        recorded_at=snapshot.generated_at,
                        edition=snapshot.edition,
                        issue_number=snapshot.issue_number,
                        workstream_id=entry.workstream_id,
                        source_type="slice_membership",
                        work_item_id=item_id,
                    ),
                )

        for item_id in entry.narrative_item_ids:
            _append_association_record(
                records,
                seen,
                WorkstreamAssociationRecord(
                    recorded_at=snapshot.generated_at,
                    edition=snapshot.edition,
                    issue_number=snapshot.issue_number,
                    workstream_id=entry.workstream_id,
                    source_type="narrative_reference",
                    work_item_id=item_id,
                    note=("drift" if item_id in entry.drift_item_ids else None),
                ),
            )
    return tuple(records)


def section_id_for_slice_contract(contract: SliceContract) -> str:
    return build_anchor(f"{contract.scorecard_name}-{contract.title}")


def render_workstream_issue_snapshot_markdown(
    snapshot: WorkstreamIssueSnapshot,
    *,
    item_lookup: dict[int, str] | None = None,
) -> str:
    item_lookup = item_lookup or {}
    lines = [
        f"# Workstream Snapshot - {snapshot.program_id} Issue {snapshot.issue_number:03d}",
        "",
        f"Edition: {snapshot.edition}",
        f"Generated: {snapshot.generated_at.isoformat()}",
        "",
    ]
    for entry in snapshot.workstreams:
        lines.append(f"## {entry.name} ({entry.workstream_id})")
        lines.append(f"- Lifecycle: {entry.lifecycle_state}")
        lines.append(f"- Report relevance: {entry.report_relevance}")
        lines.append(f"- Association health: {entry.association_health}")
        if entry.sub_program_id:
            lines.append(f"- Sub-program: {entry.sub_program_id}")
        if entry.background:
            lines.append(f"- Background: {entry.background}")
        if entry.history_summary:
            lines.append(f"- History: {entry.history_summary}")
        if entry.reporting_guidance:
            lines.append(f"- Reporting guidance: {entry.reporting_guidance}")
        if entry.stakeholders:
            stakeholders = ", ".join(
                f"{stakeholder.name} ({stakeholder.role})" for stakeholder in entry.stakeholders
            )
            lines.append(f"- Stakeholders: {stakeholders}")
        lines.append(f"- Persisted ADO count: {entry.assigned_item_count}")
        lines.append(f"- Narrative ADO count: {len(entry.narrative_item_ids)}")
        lines.append("- Gaps / issues:")
        if entry.issues:
            for issue in entry.issues:
                lines.append(f"  - {issue}")
        else:
            lines.append("  - None detected.")
        lines.append("- Persisted ADOs:")
        if entry.assigned_item_ids:
            for item_id in entry.assigned_item_ids:
                title = item_lookup.get(item_id, "<title unavailable>")
                lines.append(f"  - ADO#{item_id} - {title}")
        else:
            lines.append("  - None currently matched.")
        lines.append("- Narrative ADOs:")
        if entry.narrative_item_ids:
            for item_id in entry.narrative_item_ids:
                title = item_lookup.get(item_id, "<title unavailable>")
                lines.append(f"  - ADO#{item_id} - {title}")
        else:
            lines.append("  - None referenced in the rendered workstream section.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _derive_default_registry_entries(
    slice_contracts: tuple[SliceContract, ...],
    *,
    program_context: NarrativeProgramContext | None,
) -> tuple[WorkstreamRegistryEntry, ...]:
    return tuple(
        WorkstreamRegistryEntry(
            id=contract.id,
            name=contract.title,
            lifecycle_state="active",
            sub_program_id=_infer_sub_program_id(contract.id),
            aliases=(),
            area_paths=_registry_area_paths(contract, program_context=program_context),
            source_slice_ids=(contract.id,),
            stakeholders=_default_stakeholders(contract),
        )
        for contract in slice_contracts
    )


def _registry_area_paths(contract: SliceContract, *, program_context: NarrativeProgramContext | None) -> tuple[str, ...]:
    if program_context is None:
        return ()
    matching: list[str] = []
    normalized_workstream = _normalize_title(contract.workstream)
    for workstream in program_context.workstreams:
        if _normalize_title(workstream.name) == normalized_workstream:
            matching.extend(workstream.area_paths)
    return tuple(dict.fromkeys(matching))


def _default_stakeholders(contract: SliceContract) -> tuple[WorkstreamStakeholder, ...]:
    stakeholders = [WorkstreamStakeholder(name=contract.owners.primary, role="primary_owner")]
    if contract.owners.support_tpm:
        stakeholders.append(WorkstreamStakeholder(name=contract.owners.support_tpm, role="support_tpm"))
    return tuple(stakeholders)


def _parse_registry_entry_authored(raw_entry: Any, path: Path) -> WorkstreamRegistryEntry:
    """Full authored-entry parser: includes key_ado_items, alias, and overdue_ado_item_ids."""
    if not isinstance(raw_entry, dict):
        raise ConfigError(f"Each workstream registry entry must be a mapping in {path}")
    workstream_id = _require_string(raw_entry.get("id"), path, "workstreams[].id")
    name = _optional_string(raw_entry.get("name")) or ""
    lifecycle_state = _optional_string(raw_entry.get("lifecycle_state")) or "active"

    stakeholders_raw = raw_entry.get("stakeholders") or []
    if not isinstance(stakeholders_raw, list):
        raise ConfigError(f"stakeholders must be a list for workstream '{workstream_id}' in {path}")
    stakeholders = tuple(
        _parse_stakeholder_authored(s, path, workstream_id) for s in stakeholders_raw
    )

    # key_ado_items: positive unique integers only
    key_items_raw = raw_entry.get("key_ado_items") or []
    key_ado_items: list[int] = []
    seen_ids: set[int] = set()
    for ki in (key_items_raw if isinstance(key_items_raw, list) else []):
        if isinstance(ki, bool):
            raise ConfigError(
                f"key_ado_items for workstream '{workstream_id}' must be integers, not booleans in {path}"
            )
        if not isinstance(ki, int):
            raise ConfigError(
                f"key_ado_items for workstream '{workstream_id}' must be integers in {path}"
            )
        if ki <= 0:
            raise ConfigError(
                f"key_ado_items for workstream '{workstream_id}' must be positive integers in {path}"
            )
        if ki not in seen_ids:
            seen_ids.add(ki)
            key_ado_items.append(ki)

    # overdue_ado_item_ids: derived from ado_live_state values
    ado_live_state_raw = raw_entry.get("ado_live_state") or {}
    overdue_ids: set[int] = set()
    if isinstance(ado_live_state_raw, dict):
        for wi_str, wi_val in ado_live_state_raw.items():
            if isinstance(wi_val, str):
                val_upper = wi_val.upper()
                if "OVERDUE" in val_upper or "NO ETA" in val_upper:
                    try:
                        overdue_ids.add(int(wi_str))
                    except (ValueError, TypeError):
                        pass

    return WorkstreamRegistryEntry(
        id=workstream_id,
        name=name,
        lifecycle_state=lifecycle_state,
        sub_program_id=_optional_string(raw_entry.get("sub_program_id")),
        aliases=_string_tuple(raw_entry.get("aliases", []), path, workstream_id, "aliases"),
        area_paths=_string_tuple(raw_entry.get("area_paths", []), path, workstream_id, "area_paths"),
        source_slice_ids=_string_tuple(raw_entry.get("source_slice_ids", []), path, workstream_id, "source_slice_ids"),
        background=_optional_string(raw_entry.get("background")),
        history_summary=_optional_string(raw_entry.get("history_summary")),
        reporting_guidance=_optional_string(raw_entry.get("reporting_guidance")),
        stakeholders=stakeholders,
        key_ado_items=tuple(key_ado_items),
        overdue_ado_item_ids=frozenset(overdue_ids),
    )


def _parse_stakeholder_authored(raw_stakeholder: Any, path: Path, workstream_id: str) -> WorkstreamStakeholder:
    """Parse stakeholder with alias support."""
    if not isinstance(raw_stakeholder, dict):
        raise ConfigError(f"stakeholders for workstream '{workstream_id}' must be mappings in {path}")
    alias_raw = _optional_string(raw_stakeholder.get("alias"))
    name_raw = _optional_string(raw_stakeholder.get("name"))
    # Per spec §24.5: if only alias exists, set both name and alias to that value
    if name_raw is None and alias_raw is not None:
        name_raw = alias_raw
    if name_raw is None:
        raise ConfigError(
            f"stakeholders for workstream '{workstream_id}' require 'name' or 'alias' in {path}"
        )
    return WorkstreamStakeholder(
        name=name_raw,
        role=_require_string(raw_stakeholder.get("role"), path, f"{workstream_id}.stakeholders[].role"),
        email=_optional_string(raw_stakeholder.get("email")),
        alias=alias_raw,
    )


def _parse_registry_entry(raw_entry: Any, path: Path) -> WorkstreamRegistryEntry:
    if not isinstance(raw_entry, dict):
        raise ConfigError(f"Each workstream registry entry must be a mapping in {path}")
    workstream_id = _require_string(raw_entry.get("id"), path, "workstreams[].id")
    name = _optional_string(raw_entry.get("name")) or ""
    lifecycle_state = _optional_string(raw_entry.get("lifecycle_state")) or "active"
    stakeholders = raw_entry.get("stakeholders", [])
    if stakeholders is None:
        stakeholders = []
    if not isinstance(stakeholders, list):
        raise ConfigError(f"stakeholders must be a list for workstream '{workstream_id}' in {path}")
    return WorkstreamRegistryEntry(
        id=workstream_id,
        name=name,
        lifecycle_state=lifecycle_state,
        sub_program_id=_optional_string(raw_entry.get("sub_program_id")),
        aliases=_string_tuple(raw_entry.get("aliases", []), path, workstream_id, "aliases"),
        area_paths=_string_tuple(raw_entry.get("area_paths", []), path, workstream_id, "area_paths"),
        source_slice_ids=_string_tuple(raw_entry.get("source_slice_ids", []), path, workstream_id, "source_slice_ids"),
        background=_optional_string(raw_entry.get("background")),
        history_summary=_optional_string(raw_entry.get("history_summary")),
        reporting_guidance=_optional_string(raw_entry.get("reporting_guidance")),
        stakeholders=tuple(_parse_stakeholder(stakeholder, path, workstream_id) for stakeholder in stakeholders),
    )


def _parse_stakeholder(raw_stakeholder: Any, path: Path, workstream_id: str) -> WorkstreamStakeholder:
    if not isinstance(raw_stakeholder, dict):
        raise ConfigError(f"stakeholders for workstream '{workstream_id}' must be mappings in {path}")
    alias_raw = _optional_string(raw_stakeholder.get("alias"))
    name_value = raw_stakeholder.get("name")
    if name_value is None:
        name_value = raw_stakeholder.get("alias")
    return WorkstreamStakeholder(
        name=_require_string(name_value, path, f"{workstream_id}.stakeholders[].name"),
        role=_require_string(raw_stakeholder.get("role"), path, f"{workstream_id}.stakeholders[].role"),
        email=_optional_string(raw_stakeholder.get("email")),
        alias=alias_raw,
    )


def _merge_registry_entry(
    base_entry: WorkstreamRegistryEntry | None,
    override_entry: WorkstreamRegistryEntry,
) -> WorkstreamRegistryEntry:
    if base_entry is None:
        if not override_entry.name:
            raise ConfigError(f"Registry entry '{override_entry.id}' must define a name when no slice-derived default exists.")
        return override_entry
    return WorkstreamRegistryEntry(
        id=base_entry.id,
        name=override_entry.name or base_entry.name,
        lifecycle_state=override_entry.lifecycle_state or base_entry.lifecycle_state,
        sub_program_id=override_entry.sub_program_id or base_entry.sub_program_id,
        aliases=override_entry.aliases or base_entry.aliases,
        area_paths=override_entry.area_paths or base_entry.area_paths,
        source_slice_ids=override_entry.source_slice_ids or base_entry.source_slice_ids,
        background=override_entry.background or base_entry.background,
        history_summary=override_entry.history_summary or base_entry.history_summary,
        reporting_guidance=override_entry.reporting_guidance or base_entry.reporting_guidance,
        stakeholders=override_entry.stakeholders or base_entry.stakeholders,
        key_ado_items=override_entry.key_ado_items or base_entry.key_ado_items,
        overdue_ado_item_ids=override_entry.overdue_ado_item_ids or base_entry.overdue_ado_item_ids,
    )


def _extract_narrative_item_ids(
    entry: WorkstreamRegistryEntry,
    sections: dict[str, str],
) -> tuple[int, ...]:
    section_text = sections.get(_normalize_title(entry.name))
    if section_text is None:
        return ()
    item_ids = [int(match) for match in re.findall(r"ADO#(\d+)", section_text)]
    return tuple(dict.fromkeys(item_ids))


def _extract_narrative_item_ids_from_texts(texts: list[str]) -> tuple[int, ...]:
    item_ids = [int(match) for text in texts for match in re.findall(r"ADO#(\d+)", text)]
    return tuple(dict.fromkeys(item_ids))


def _split_markdown_sections(markdown_body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_title: str | None = None
    buffer: list[str] = []
    for line in markdown_body.splitlines():
        if line.startswith("## "):
            if current_title is not None:
                sections[current_title] = "\n".join(buffer)
            current_title = _normalize_title(line[3:].strip())
            buffer = []
            continue
        if current_title is not None:
            buffer.append(line)
    if current_title is not None:
        sections[current_title] = "\n".join(buffer)
    return sections


def _report_relevance(
    entry: WorkstreamRegistryEntry,
    persisted_item_ids: tuple[int, ...],
    narrative_item_ids: tuple[int, ...],
    issues: list[str],
    narrative_sections: dict[str, str],
) -> str:
    return _report_relevance_from_presence(
        entry,
        persisted_item_ids,
        narrative_item_ids,
        issues,
        has_full_section=(_normalize_title(entry.name) in narrative_sections),
    )


def _report_relevance_from_presence(
    entry: WorkstreamRegistryEntry,
    persisted_item_ids: tuple[int, ...],
    narrative_item_ids: tuple[int, ...],
    issues: list[str],
    *,
    has_full_section: bool,
) -> str:
    if has_full_section:
        return "full_section"
    if persisted_item_ids or narrative_item_ids or issues:
        return "summary_only"
    if entry.lifecycle_state == "active":
        return "tracked_not_reported"
    return "dormant"


def _association_health(
    persisted_item_ids: tuple[int, ...],
    narrative_item_ids: tuple[int, ...],
    drift_item_ids: tuple[int, ...],
) -> str:
    if drift_item_ids:
        return "drift"
    if persisted_item_ids or narrative_item_ids:
        return "aligned"
    return "empty"


def _infer_sub_program_id(slice_id: str) -> str | None:
    if slice_id.startswith("dd."):
        return "dd_on_pf"
    return None


def _pick_state(values: list[str]) -> str | None:
    if not values:
        return None
    return values[0]


def _require_string(value: Any, path: Path, field_name: str) -> str:
    text = _optional_string(value)
    if text is None:
        raise ConfigError(f"{field_name} is required in {path}")
    return text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _append_association_record(
    records: list[WorkstreamAssociationRecord],
    seen: set[tuple[str, str, str | None, str | None, int | None, str | None]],
    record: WorkstreamAssociationRecord,
) -> None:
    key = (
        record.workstream_id,
        record.source_type,
        record.source_slice_id,
        record.section_id,
        record.work_item_id,
        record.note,
    )
    if key in seen:
        return
    seen.add(key)
    records.append(record)


def _is_explicit_item(contract: SliceContract, item_id: int) -> bool:
    ado_contract = contract.source_contract.ado
    if ado_contract is None:
        return False
    return item_id in ado_contract.explicit_work_item_ids


def _matches_saved_query_scope(contract: SliceContract, item: WorkItem) -> bool:
    ado_contract = contract.source_contract.ado
    if ado_contract is None or not ado_contract.saved_queries:
        return False
    item_scope = _saved_query_scope(item)
    return bool(item_scope & set(ado_contract.saved_queries))


def _matches_area_path_scope(contract: SliceContract, item: WorkItem) -> bool:
    ado_contract = contract.source_contract.ado
    if ado_contract is None or ado_contract.filters is None:
        return False
    predicates = tuple(ado_contract.filters.all_of) + tuple(ado_contract.filters.any_of)
    for predicate in predicates:
        if predicate.field.strip().lower() != "area_path":
            continue
        raw_value = predicate.value.strip()
        if not raw_value:
            continue
        operator = predicate.op.strip().lower()
        area_path = item.area_path.lower()
        candidate = raw_value.lower()
        if operator == "contains" and candidate in area_path:
            return True
        if operator == "eq" and candidate == area_path:
            return True
    return False


def _saved_query_scope(item: WorkItem) -> set[str]:
    raw_value = item.custom_fields.get("saved_query_ids")
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        return {normalized} if normalized else set()
    if isinstance(raw_value, (list, tuple, set)):
        return {str(value).strip() for value in raw_value if str(value).strip()}
    return set()


def _string_tuple(value: Any, path: Path, workstream_id: str, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list for workstream '{workstream_id}' in {path}")
    entries: list[str] = []
    for raw_item in value:
        text = _optional_string(raw_item)
        if text is None:
            continue
        entries.append(text)
    return tuple(entries)


def _normalize_title(value: str) -> str:
    return _TITLE_NORMALIZER.sub(" ", value.strip().lower())
