from __future__ import annotations

from datetime import date, datetime, timezone
import os
from pathlib import Path
import shutil
from typing import Any

import portalocker
import yaml

from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import DecisionEntry, DecisionStatus
from src.core.signal_ref_utils import extract_work_item_refs


_STALE_PROPOSED_DAYS = 14


def get_decisions_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "decisions.yaml"


def load_decisions(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[DecisionEntry, ...]:
    path = get_decisions_path(program_id, programs_root)
    return _load_decisions_from_path(program_id, path)


def save_decisions(program_id: str, entries: tuple[DecisionEntry, ...], programs_root: Path = PROGRAMS_ROOT) -> None:
    with _decision_register_lock(program_id, programs_root):
        _write_decisions(program_id, entries, programs_root=programs_root)


def upsert_decisions(program_id: str, entries: tuple[DecisionEntry, ...], programs_root: Path = PROGRAMS_ROOT) -> tuple[DecisionEntry, ...]:
    with _decision_register_lock(program_id, programs_root):
        current = _load_decisions_from_path(program_id, get_decisions_path(program_id, programs_root))
        merged: dict[str, DecisionEntry] = {entry.id: entry for entry in current}
        for entry in entries:
            merged.setdefault(entry.id, entry)
        sorted_entries = sort_decisions(tuple(merged.values()))
        _write_decisions(program_id, sorted_entries, programs_root=programs_root)
        return sorted_entries


def sort_decisions(entries: tuple[DecisionEntry, ...]) -> tuple[DecisionEntry, ...]:
    today = datetime.now(timezone.utc).date()
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.status is DecisionStatus.PROPOSED else 1,
                0 if assess_proposed_decision_staleness(entry, today) else 1,
                -entry.decision_date.toordinal(),  # type: ignore[union-attr]
                entry.title.lower(),
            ),
        )
    )


def _load_decisions_from_path(program_id: str, path: Path) -> tuple[DecisionEntry, ...]:
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
        raise ConfigError(f"Unsupported decision register schema_version {schema_version!r} in {path}.")

    raw_entries = document.get("decisions")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise ConfigError(f"Expected 'decisions' list in {path}.")

    entries: list[DecisionEntry] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"Decision entry #{index} in {path} must be a mapping.")
        try:
            entry = _parse_decision_entry(program_id, raw_entry)
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"Invalid decision entry #{index} in {path}: {error}") from error
        if entry.id in seen_ids:
            raise ConfigError(f"Duplicate decision id '{entry.id}' in {path}.")
        seen_ids.add(entry.id)
        entries.append(entry)
    return tuple(entries)


def _write_decisions(program_id: str, entries: tuple[DecisionEntry, ...], programs_root: Path = PROGRAMS_ROOT) -> None:
    path = get_decisions_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))

    document = {
        "schema_version": "1.0",
        "decisions": [_decision_entry_to_record(entry) for entry in entries],
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    os.replace(temp_path, path)
    _sync_decision_facts(program_id, entries, programs_root=programs_root)


def _decision_register_lock(program_id: str, programs_root: Path) -> portalocker.Lock:
    lock_path = get_decisions_path(program_id, programs_root).with_suffix(".yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return portalocker.Lock(lock_path, mode="a+", timeout=30)


def _sync_decision_facts(
    program_id: str,
    entries: tuple[DecisionEntry, ...],
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
    active_decisions = {
        fact.natural_key: fact
        for fact in active_snapshot.facts
        if fact.fact_type == "decision.entry" and fact.lifecycle_state == FactLifecycleState.ACTIVE
    }
    current_natural_keys: set[str] = set()

    for entry in entries:
        entity_refs = tuple(entry.entity_refs) or (f"DECISION:{entry.id}",)
        natural_key = _decision_natural_key(entry)
        current_natural_keys.add(natural_key)
        store.append_fact(
            ProgramFactInput(
                fact_type="decision.entry",
                scope="program",
                entity_refs=entity_refs,
                payload=_decision_entry_to_record(entry),
                precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
                natural_key=natural_key,
                created_by="vertex.decision_register",
            ),
            recorded_at=sync_time,
        )

    for natural_key, fact in active_decisions.items():
        if natural_key in current_natural_keys:
            continue
        store.append_fact(
            ProgramFactInput(
                fact_type="decision.entry",
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
                created_by="vertex.decision_register",
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


def _decision_natural_key(entry: DecisionEntry) -> str:
    from src.core.program_fact_store import build_natural_key

    entity_refs = tuple(entry.entity_refs) or (f"DECISION:{entry.id}",)
    return build_natural_key("decision.entry", entity_refs=entity_refs, scope="program")


def assess_proposed_decision_staleness(entry: DecisionEntry, as_of: date) -> bool:
    if entry.status is not DecisionStatus.PROPOSED:
        return False
    if entry.decision_date is None:
        return False
    return (as_of - entry.decision_date).days > _STALE_PROPOSED_DAYS


def assess_decision_review_staleness(entry: DecisionEntry, as_of: date) -> bool:
    if entry.review_by is None:
        return False
    return entry.status in {DecisionStatus.DECIDED} and entry.review_by < as_of


def _parse_decision_entry(program_id: str, raw_entry: dict[str, Any]) -> DecisionEntry:
    raw_program_id = _optional_string(raw_entry.get("program_id"), field_name="program_id") or program_id
    entry_id = _required_string(raw_entry.get("id"), field_name="id").strip()
    title = _required_string(raw_entry.get("title"), field_name="title").strip()
    context = _required_string(raw_entry.get("context"), field_name="context").strip()
    decision = _required_string(raw_entry.get("decision"), field_name="decision").strip()
    decided_by = _optional_string(raw_entry.get("decided_by"), field_name="decided_by")
    if decided_by is not None:
        decided_by = decided_by.strip() or None

    return DecisionEntry(
        id=entry_id,
        program_id=raw_program_id,
        title=title,
        context=context,
        decision=decision,
        rationale=_optional_string(raw_entry.get("rationale"), field_name="rationale"),
        alternatives_considered=_parse_string_tuple(raw_entry.get("alternatives_considered"), field_name="alternatives_considered"),
        decided_by=decided_by,
        decision_date=_parse_optional_date(raw_entry.get("decision_date"), field_name="decision_date"),
        status=DecisionStatus.from_string(_required_string(raw_entry.get("status"), field_name="status")),
        superseded_by=_optional_string(raw_entry.get("superseded_by"), field_name="superseded_by"),
        linked_claim_id=_optional_string(raw_entry.get("linked_claim_id"), field_name="linked_claim_id"),
        linked_risk_id=_optional_string(raw_entry.get("linked_risk_id"), field_name="linked_risk_id"),
        linked_action_ids=_parse_string_tuple(raw_entry.get("linked_action_ids"), field_name="linked_action_ids"),
        workstream_id=_optional_string(raw_entry.get("workstream_id"), field_name="workstream_id"),
        entity_refs=_parse_string_tuple(raw_entry.get("entity_refs"), field_name="entity_refs"),
        review_by=_parse_optional_date(raw_entry.get("review_by"), field_name="review_by"),
        linked_milestone_ids=_parse_string_tuple(raw_entry.get("linked_milestone_ids"), field_name="linked_milestone_ids"),
        last_reviewed_date=_parse_optional_date(raw_entry.get("last_reviewed_date"), field_name="last_reviewed_date"),
        expected_outcome_refs=_parse_string_tuple(raw_entry.get("expected_outcome_refs"), field_name="expected_outcome_refs"),
    )


def _decision_entry_to_record(entry: DecisionEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "program_id": entry.program_id,
        "title": entry.title,
        "context": entry.context,
        "decision": entry.decision,
        "rationale": entry.rationale,
        "alternatives_considered": list(entry.alternatives_considered),
        "decided_by": entry.decided_by,
        "decision_date": entry.decision_date.isoformat() if entry.decision_date is not None else None,
        "status": entry.status.value,
        "superseded_by": entry.superseded_by,
        "linked_claim_id": entry.linked_claim_id,
        "linked_risk_id": entry.linked_risk_id,
        "linked_action_ids": list(entry.linked_action_ids),
        "workstream_id": entry.workstream_id,
        "entity_refs": list(entry.entity_refs),
        "review_by": entry.review_by.isoformat() if entry.review_by is not None else None,
        "linked_milestone_ids": list(entry.linked_milestone_ids),
        "last_reviewed_date": entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else None,
        "expected_outcome_refs": list(entry.expected_outcome_refs),
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


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def read_governance_decisions_from_overrides(
    overrides: Any, 
    program_id: str,
) -> list[DecisionEntry]:
    from src.core.overrides_store import OverridesDocument
    entries: list[DecisionEntry] = []
    if overrides is None or not isinstance(overrides, OverridesDocument):
        return entries
    if not hasattr(overrides, "decisions") or not overrides.decisions:
        return entries

    for record in overrides.decisions:
        status_str = record.status.strip().lower()
        if status_str == "active":
            status = DecisionStatus.DECIDED
        elif status_str == "superseded":
            status = DecisionStatus.SUPERSEDED
        elif status_str == "proposed":
            status = DecisionStatus.PROPOSED
        elif status_str == "reverted":
            status = DecisionStatus.REVERTED
        else:
            status = DecisionStatus.DECIDED

        entry = DecisionEntry(
            id=record.id,
            program_id=program_id,
            title=f"[{record.type.upper()}] Decision {record.id}",
            context=f"Workstream: {record.workstream}, Source: {record.source_type} ({record.source_ref})",
            decision=record.statement,
            rationale=None,
            alternatives_considered=(),
            decided_by=record.owner,
            decision_date=record.effective_date,
            status=status,
            superseded_by=None,
            linked_claim_id=None,
            linked_risk_id=None,
            linked_action_ids=(),
            workstream_id=record.workstream,
            entity_refs=extract_work_item_refs(record.source_ref),
            review_by=record.resolved_date,
        )
        entries.append(entry)
    return entries