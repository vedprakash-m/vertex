from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import portalocker
import yaml

from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import RiskCategory, RiskDerivedLevel, RiskEntry, RiskImpact, RiskProbability, RiskStatus


_STALE_REVIEW_DAYS = 30

# High-risk append-only file — grows with every risk status change.
# Rotated at 10 MB (spec §11.3 Phase 5 / D-23) to bound on-disk footprint.
_RISK_UPDATES_MAX_BYTES = 10 * 1024 * 1024

_IMPACT_ORDINAL = {
    RiskImpact.LOW: 1,
    RiskImpact.MEDIUM: 2,
    RiskImpact.HIGH: 3,
    RiskImpact.CRITICAL: 4,
}
_PROBABILITY_ORDINAL = {
    RiskProbability.UNLIKELY: 1,
    RiskProbability.POSSIBLE: 2,
    RiskProbability.LIKELY: 3,
    RiskProbability.VERY_LIKELY: 4,
}
_ACTIVE_RISK_STATUSES = {RiskStatus.OPEN, RiskStatus.ESCALATED}


def get_risk_register_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "risk_register.yaml"


def get_risk_updates_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "risk_updates.jsonl"


def build_risk_id(
    program_id: str,
    *,
    title: str,
    description: str,
    owner_alias: str,
    entity_refs: tuple[str, ...] = (),
) -> str:
    normalized_title = " ".join(title.strip().lower().split())
    normalized_description = " ".join(description.strip().lower().split())
    normalized_owner = owner_alias.strip().lower()
    # Include sorted entity_refs so two signals with identical text but different WIs get distinct IDs.
    refs_key = "|".join(sorted(entity_refs)) if entity_refs else ""
    return str(
        uuid5(
            NAMESPACE_URL,
            f"vertex:risk:{program_id}:{normalized_title}:{normalized_description}:{normalized_owner}:{refs_key}",
        )
    )


def load_risk_register(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[RiskEntry, ...]:
    path = get_risk_register_path(program_id, programs_root)
    if not path.exists():
        return ()

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error

    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported risk register schema_version {schema_version!r} in {path}.")

    raw_entries = document.get("risks") or ()
    if not isinstance(raw_entries, list):
        raise ConfigError(f"Expected 'risks' list in {path}.")

    entries: list[RiskEntry] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"Risk entry #{index} in {path} must be a mapping.")
        try:
            entry = _parse_risk_entry(program_id, raw_entry)
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"Invalid risk entry #{index} in {path}: {error}") from error
        if entry.id in seen_ids:
            raise ConfigError(f"Duplicate risk id '{entry.id}' in {path}.")
        seen_ids.add(entry.id)
        entries.append(entry)
    return tuple(entries)


def save_risk_register(program_id: str, entries: tuple[RiskEntry, ...], programs_root: Path = PROGRAMS_ROOT) -> None:
    path = get_risk_register_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))

    document = {
        "schema_version": "1.0",
        "risks": [_risk_entry_to_record(entry) for entry in entries],
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    os.replace(temp_path, path)
    _sync_risk_facts(program_id, entries, programs_root=programs_root)


def _sync_risk_facts(
    program_id: str,
    entries: tuple[RiskEntry, ...],
    *,
    programs_root: Path,
) -> None:
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
        build_natural_key,
    )

    store = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root))
    sync_time = datetime.now(timezone.utc)
    active_snapshot = store.snapshot(as_of=sync_time)
    active_risks = {
        fact.natural_key: fact
        for fact in active_snapshot.facts
        if fact.fact_type == "risk.entry" and fact.lifecycle_state == FactLifecycleState.ACTIVE
    }
    current_natural_keys: set[str] = set()

    for entry in entries:
        entity_refs = (f"RISK:{entry.id}",)
        natural_key = build_natural_key("risk.entry", entity_refs=entity_refs, scope="program")
        current_natural_keys.add(natural_key)
        store.append_fact(
            ProgramFactInput(
                fact_type="risk.entry",
                scope="program",
                entity_refs=entity_refs,
                payload=_risk_fact_payload(entry),
                precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                natural_key=natural_key,
                created_by="vertex.risk_register",
            ),
            recorded_at=sync_time,
        )

    for natural_key, fact in active_risks.items():
        if natural_key in current_natural_keys:
            continue
        store.append_fact(
            ProgramFactInput(
                fact_type="risk.entry",
                scope=fact.scope,
                entity_refs=fact.entity_refs,
                payload=fact.payload,
                source_signal_ids=fact.source_signal_ids,
                confidence=fact.confidence,
                precedence=fact.precedence,
                review_state=fact.review_state,
                lifecycle_state=FactLifecycleState.CLOSED,
                valid_from=fact.valid_from,
                valid_until=sync_time,
                projection_history=fact.projection_history,
                natural_key=natural_key,
                created_by="vertex.risk_register",
                privacy_classification=fact.privacy_classification,
                accepted_by=fact.accepted_by,
            ),
            recorded_at=sync_time,
        )


def _resolve_fact_db_root(programs_root: Path) -> Path | None:
    if programs_root == PROGRAMS_ROOT:
        return None
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def _risk_fact_payload(entry: RiskEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "program_id": entry.program_id,
        "title": entry.title,
        "description": entry.description,
        "probability": entry.probability.value,
        "impact": entry.impact.value,
        "category": entry.category.value,
        "owner_alias": entry.owner_alias,
        "mitigation_plan": entry.mitigation_plan,
        "mitigation_due_date": entry.mitigation_due_date.isoformat() if entry.mitigation_due_date is not None else None,
        "linked_workstream_ids": list(entry.linked_workstream_ids),
        "linked_work_item_ids": list(entry.linked_work_item_ids),
        "linked_milestone_ids": list(entry.linked_milestone_ids),
        "linked_claim_ids": list(entry.linked_claim_ids),
        "linked_action_ids": list(entry.linked_action_ids),
        "status": entry.status.value,
        "identified_date": entry.identified_date.isoformat(),
        "identified_in_vertex_issue": entry.identified_in_vertex_issue,
        "last_reviewed_date": entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else None,
        "entity_refs": list(entry.entity_refs),
        "source_signal_ids": list(entry.source_signal_ids),
    }


def link_risk_action(
    program_id: str,
    risk_id: str,
    action_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> RiskEntry:
    entries = list(load_risk_register(program_id, programs_root=programs_root))
    for index, entry in enumerate(entries):
        if entry.id != risk_id:
            continue
        if action_id in entry.linked_action_ids:
            return entry
        updated = replace(entry, linked_action_ids=entry.linked_action_ids + (action_id,))
        entries[index] = updated
        save_risk_register(program_id, tuple(entries), programs_root=programs_root)
        return updated
    raise ValueError(f"Unknown risk '{risk_id}' for program '{program_id}'.")


def assess_risk_staleness(entry: RiskEntry, as_of: date) -> bool:
    if entry.status not in _ACTIVE_RISK_STATUSES:
        return False
    review_date = entry.last_reviewed_date or entry.identified_date
    return (as_of - review_date).days > _STALE_REVIEW_DAYS


def compute_risk_score(entry: RiskEntry) -> int:
    return _PROBABILITY_ORDINAL[entry.probability] * _IMPACT_ORDINAL[entry.impact]


def record_risk_update(
    program_id: str,
    risk_id: str,
    old_status: str,
    new_status: str,
    author: str,
    note: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    RiskStatus.from_string(old_status)
    RiskStatus.from_string(new_status)
    timestamp = datetime.now(timezone.utc)
    record = {
        "risk_id": risk_id,
        "old_status": old_status.strip().lower(),
        "new_status": new_status.strip().lower(),
        "timestamp": timestamp.isoformat(),
        "author": author.strip(),
        "note": note.strip() if note is not None and note.strip() else None,
    }
    path = get_risk_updates_path(program_id, programs_root)
    payload = json.dumps(record, ensure_ascii=False) + "\n"
    append_jsonl_line(path, payload, max_bytes=_RISK_UPDATES_MAX_BYTES)


def load_risk_history(program_id: str, risk_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[dict[str, Any], ...]:
    path = get_risk_updates_path(program_id, programs_root)
    if not path.exists():
        return ()
    return tuple(record for record in read_jsonl_records(path) if str(record.get("risk_id") or "") == risk_id)


def is_unsourced(entry: RiskEntry) -> bool:
    """Return True when a risk has no evidence links (signal IDs or claim IDs)."""
    return not entry.source_signal_ids and not entry.linked_claim_ids


def upsert_risk_from_signal(
    program_id: str,
    signal_id: str,
    signal_text: str,
    signal_entity_refs: tuple[str, ...],
    signal_workstream_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> RiskEntry:
    """Create or update a risk entry from a risk-class signal.

    Matches an existing risk by entity_refs overlap; if no match is found,
    creates a new OPEN risk with placeholder fields.  Either way, the
    calling signal's ID is accumulated into source_signal_ids.
    """
    today = datetime.now(timezone.utc).date()
    entries = list(load_risk_register(program_id, programs_root=programs_root))

    ref_set = frozenset(signal_entity_refs)
    matched_index: int | None = None
    for i, entry in enumerate(entries):
        if ref_set and frozenset(entry.entity_refs) & ref_set:
            matched_index = i
            break

    if matched_index is not None:
        existing = entries[matched_index]
        if signal_id not in existing.source_signal_ids:
            updated = replace(
                existing,
                source_signal_ids=existing.source_signal_ids + (signal_id,),
                last_reviewed_date=today,
            )
            entries[matched_index] = updated
            save_risk_register(program_id, tuple(entries), programs_root=programs_root)
            return updated
        return existing

    title = signal_text[:120].strip().rstrip(".,;") or "Untitled risk from signal"
    new_entry = RiskEntry(
        id=build_risk_id(program_id, title=title, description=signal_text[:500], owner_alias="unassigned", entity_refs=signal_entity_refs),
        program_id=program_id,
        title=title,
        description=signal_text[:500],
        probability=RiskProbability.POSSIBLE,
        impact=RiskImpact.MEDIUM,
        category=RiskCategory.SCHEDULE,
        owner_alias="unassigned",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(signal_workstream_id,) if signal_workstream_id else (),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=today,
        identified_in_vertex_issue=None,
        last_reviewed_date=today,
        entity_refs=signal_entity_refs,
        source_signal_ids=(signal_id,),
    )
    entries.append(new_entry)
    save_risk_register(program_id, tuple(entries), programs_root=programs_root)
    return new_entry


def _parse_risk_entry(program_id: str, raw_entry: dict[str, Any]) -> RiskEntry:
    raw_program_id = _optional_string(raw_entry.get("program_id"), field_name="program_id") or program_id
    title = _required_string(raw_entry.get("title"), field_name="title").strip()
    description = _required_string(raw_entry.get("description"), field_name="description").strip()
    owner_alias = _required_string(raw_entry.get("owner_alias"), field_name="owner_alias").strip()
    if not title:
        raise ValueError("missing title")
    if not description:
        raise ValueError("missing description")
    if not owner_alias:
        raise ValueError("missing owner_alias")

    raw_id = raw_entry.get("id")
    risk_id = (
        build_risk_id(raw_program_id, title=title, description=description, owner_alias=owner_alias)
        if raw_id in (None, "")
        else _required_string(raw_id, field_name="id").strip()
    )

    return RiskEntry(
        id=risk_id,
        program_id=raw_program_id,
        title=title,
        description=description,
        probability=RiskProbability.from_string(_required_string(raw_entry.get("probability"), field_name="probability")),
        impact=RiskImpact.from_string(_required_string(raw_entry.get("impact"), field_name="impact")),
        category=RiskCategory.from_string(_required_string(raw_entry.get("category"), field_name="category")),
        owner_alias=owner_alias,
        mitigation_plan=_optional_string(raw_entry.get("mitigation_plan"), field_name="mitigation_plan"),
        mitigation_due_date=_parse_optional_date(raw_entry.get("mitigation_due_date"), field_name="mitigation_due_date"),
        linked_workstream_ids=_parse_string_tuple(raw_entry.get("linked_workstream_ids"), field_name="linked_workstream_ids"),
        linked_work_item_ids=_parse_int_tuple(raw_entry.get("linked_work_item_ids"), field_name="linked_work_item_ids"),
        linked_milestone_ids=_parse_string_tuple(raw_entry.get("linked_milestone_ids"), field_name="linked_milestone_ids"),
        linked_claim_ids=_parse_string_tuple(raw_entry.get("linked_claim_ids"), field_name="linked_claim_ids"),
        linked_action_ids=_parse_string_tuple(raw_entry.get("linked_action_ids"), field_name="linked_action_ids"),
        status=RiskStatus.from_string(_required_string(raw_entry.get("status"), field_name="status")),
        identified_date=_parse_required_date(raw_entry.get("identified_date"), field_name="identified_date"),
        identified_in_vertex_issue=_parse_optional_int(raw_entry.get("identified_in_vertex_issue"), field_name="identified_in_vertex_issue"),
        last_reviewed_date=_parse_optional_date(raw_entry.get("last_reviewed_date"), field_name="last_reviewed_date"),
        entity_refs=_parse_string_tuple(raw_entry.get("entity_refs"), field_name="entity_refs"),
        source_signal_ids=_parse_string_tuple(raw_entry.get("source_signal_ids"), field_name="source_signal_ids"),
    )


def _risk_entry_to_record(entry: RiskEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "program_id": entry.program_id,
        "title": entry.title,
        "description": entry.description,
        "probability": entry.probability.value,
        "impact": entry.impact.value,
        "category": entry.category.value,
        "owner_alias": entry.owner_alias,
        "mitigation_plan": entry.mitigation_plan,
        "mitigation_due_date": entry.mitigation_due_date.isoformat() if entry.mitigation_due_date is not None else None,
        "linked_workstream_ids": list(entry.linked_workstream_ids),
        "linked_work_item_ids": list(entry.linked_work_item_ids),
        "linked_milestone_ids": list(entry.linked_milestone_ids),
        "linked_claim_ids": list(entry.linked_claim_ids),
        "linked_action_ids": list(entry.linked_action_ids),
        "status": entry.status.value,
        "identified_date": entry.identified_date.isoformat(),
        "identified_in_vertex_issue": entry.identified_in_vertex_issue,
        "last_reviewed_date": entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else None,
        "entity_refs": list(entry.entity_refs),
        "source_signal_ids": list(entry.source_signal_ids),
    }


def _parse_required_date(value: object, *, field_name: str) -> date:
    parsed = _parse_optional_date(value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"missing {field_name}")
    return parsed


def _parse_optional_date(value: object, *, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        raise ValueError(f"invalid {field_name}")
    return date.fromisoformat(value)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    raw_values = value or ()
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings")
    parsed: list[str] = []
    for entry in raw_values:
        if not isinstance(entry, str):
            raise TypeError(f"{field_name} must contain strings only")
        text = entry.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _parse_int_tuple(value: object, *, field_name: str) -> tuple[int, ...]:
    raw_values = value or ()
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of integers")
    parsed: list[int] = []
    for entry in raw_values:
        if not isinstance(entry, int) or isinstance(entry, bool):
            raise TypeError(f"{field_name} must contain integers only")
        parsed.append(entry)
    return tuple(parsed)


def _parse_optional_int(value: object, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"{field_name} must be an integer")


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


# ---------------------------------------------------------------------------
# FR-SG-20: Strategic risk derivation
# ---------------------------------------------------------------------------

_BLOCKED_HIGH = {RiskLevel.BLOCKED, RiskLevel.HIGH}
_MEDIUM_AND_ABOVE = {RiskLevel.BLOCKED, RiskLevel.HIGH, RiskLevel.MEDIUM}
_LOW_CREDIBILITY_THRESHOLD = 0.5


def derive_strategic_risk_level(
    workstream_id: str | None,
    items: tuple[WorkItem, ...],
    *,
    scope_delta_new_high_risk: int = 0,
    trajectory_credibility: float | None = None,
    risk_entries: tuple[RiskEntry, ...] = (),
) -> RiskDerivedLevel:
    """FR-SG-20: Propose a strategic risk level for a workstream from multi-factor inputs.

    Inputs:
    - items: current workstream work items (for item_risk_mix base level)
    - scope_delta_new_high_risk: count of newly BLOCKED/HIGH items this cycle
    - trajectory_credibility: 0.0–1.0 from trajectory_analyzer; low value → upgrade hint
    - risk_entries: program risk register (for governance / ESCALATED entries)

    Returns a RiskDerivedLevel proposal — never auto-applied, always requires
    human confirmation before use.
    """
    base_level = _compute_item_risk_mix(items)

    upgrade_reasons: list[str] = []
    downgrade_reasons: list[str] = []

    # Scope-delta upgrade: new BLOCKED/HIGH items appeared this cycle
    if scope_delta_new_high_risk > 0:
        upgrade_reasons.append(
            f"{scope_delta_new_high_risk} new BLOCKED/HIGH item(s) this cycle"
        )

    # Trajectory upgrade: low credibility signals plan instability
    if trajectory_credibility is not None and trajectory_credibility < _LOW_CREDIBILITY_THRESHOLD:
        upgrade_reasons.append(
            f"timeline credibility {trajectory_credibility:.0%} (below {_LOW_CREDIBILITY_THRESHOLD:.0%} threshold)"
        )

    # Active ESCALATED risk in this workstream → upgrade hint
    ws_escalated = [
        e for e in risk_entries
        if e.status == RiskStatus.ESCALATED
        and (workstream_id is None or workstream_id in e.linked_workstream_ids)
    ]
    if ws_escalated:
        upgrade_reasons.append(
            f"{len(ws_escalated)} escalated risk(s) in register"
        )

    # Downgrade: all active risks are MEDIUM or below with no ESCALATED
    ws_active_risks = [
        e for e in risk_entries
        if e.status in _ACTIVE_RISK_STATUSES
        and (workstream_id is None or workstream_id in e.linked_workstream_ids)
    ]
    if ws_active_risks and not ws_escalated:
        all_medium_or_below = all(
            e.impact in (RiskImpact.LOW, RiskImpact.MEDIUM) for e in ws_active_risks
        )
        if all_medium_or_below:
            downgrade_reasons.append("all active risks are MEDIUM impact or below")

    # Compute proposed level: start from base, apply upgrades
    proposed = base_level
    if upgrade_reasons and proposed in (RiskLevel.LOW, RiskLevel.MEDIUM):
        proposed = RiskLevel.HIGH if proposed == RiskLevel.MEDIUM else RiskLevel.MEDIUM
    elif downgrade_reasons and proposed == RiskLevel.HIGH and not upgrade_reasons:
        proposed = RiskLevel.MEDIUM

    return RiskDerivedLevel(
        proposed_level=proposed,
        upgrade_reason="; ".join(upgrade_reasons) if upgrade_reasons else None,
        downgrade_reason="; ".join(downgrade_reasons) if downgrade_reasons else None,
    )


def _compute_item_risk_mix(items: tuple[WorkItem, ...]) -> RiskLevel:
    """Derive a base risk level from the mix of work item risk levels."""
    if not items:
        return RiskLevel.UNKNOWN
    risk_counts: dict[RiskLevel, int] = {}
    for item in items:
        rl = getattr(item, "risk_level", None) or RiskLevel.UNKNOWN
        risk_counts[rl] = risk_counts.get(rl, 0) + 1

    if risk_counts.get(RiskLevel.BLOCKED, 0) > 0:
        return RiskLevel.BLOCKED
    if risk_counts.get(RiskLevel.HIGH, 0) > 0:
        return RiskLevel.HIGH
    if risk_counts.get(RiskLevel.MEDIUM, 0) > 0:
        return RiskLevel.MEDIUM
    if risk_counts.get(RiskLevel.LOW, 0) > 0:
        return RiskLevel.LOW
    return RiskLevel.UNKNOWN