from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.edition_resolver import (
    _optional_date,
    _optional_string,
    _parse_signal_sources,
    _required_string,
    _string_tuple,
)
from src.core.exceptions import ConfigError
from src.core.models_v2 import Workstream


def get_workstreams_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "workstreams.yaml"


def save_workstreams_document(program_id: str, document: dict[str, Any], *, programs_root: Path) -> Path:
    path = get_workstreams_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=False), encoding="utf-8")
    os.replace(temp_path, path)
    _sync_workstream_facts(program_id, document=document, programs_root=programs_root, path=path)
    return path


def load_workstreams_document(raw_workstreams: dict[str, Any], path: Path) -> tuple[Workstream, ...]:
    return _parse_workstreams(raw_workstreams, path)


def _sync_workstream_facts(
    program_id: str,
    *,
    document: dict[str, Any],
    programs_root: Path,
    path: Path,
) -> None:
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
        _serialize_workstream_signal_sources,
        build_natural_key,
    )

    workstreams = _parse_workstreams(document, path)
    store = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root))
    sync_time = datetime.now(timezone.utc)
    active_snapshot = store.snapshot(as_of=sync_time)
    active_workstreams = {
        fact.natural_key: fact
        for fact in active_snapshot.facts
        if fact.fact_type == "workstream.entry" and fact.lifecycle_state == FactLifecycleState.ACTIVE
    }
    current_natural_keys: set[str] = set()

    for entry in workstreams:
        entity_refs = (f"WS:{entry.id}",)
        natural_key = build_natural_key("workstream.entry", entity_refs=entity_refs, scope="program")
        current_natural_keys.add(natural_key)
        store.append_fact(
            ProgramFactInput(
                fact_type="workstream.entry",
                scope="program",
                entity_refs=entity_refs,
                payload={
                    "id": entry.id,
                    "name": entry.name,
                    "owner_person_id": entry.owner_person_id,
                    "status": entry.status,
                    "aliases": list(entry.aliases),
                    "area_paths": list(entry.area_paths),
                    "ado_team": entry.ado_team,
                    "ado_pipeline_ids": list(entry.ado_pipeline_ids),
                    "ado_repository_ids": list(entry.ado_repository_ids),
                    "pm_owner": entry.pm_owner,
                    "eng_owner": entry.eng_owner,
                    "accountable_owner": entry.accountable_owner,
                    "accountable_email": entry.accountable_email,
                    "responsible_owners": list(entry.responsible_owners),
                    "consulted_owners": list(entry.consulted_owners),
                    "informed_owners": list(entry.informed_owners),
                    "dri_email": entry.dri_email,
                    "alternate_owner": entry.alternate_owner,
                    "always_notify": list(entry.always_notify),
                    "description": entry.description,
                    "why_it_matters": entry.why_it_matters,
                    "history_summary": entry.history_summary,
                    "leadership_sensitivity": entry.leadership_sensitivity,
                    "current_blocker": entry.current_blocker,
                    "ado_saved_query_ids": list(entry.ado_saved_query_ids),
                    "last_reviewed_date": entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else None,
                    "signal_sources": _serialize_workstream_signal_sources(entry.signal_sources),
                },
                precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                natural_key=natural_key,
                created_by="vertex.workstream_documents",
            ),
            recorded_at=sync_time,
        )

    for natural_key, fact in active_workstreams.items():
        if natural_key in current_natural_keys:
            continue
        store.append_fact(
            ProgramFactInput(
                fact_type="workstream.entry",
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
                created_by="vertex.workstream_documents",
                privacy_classification=fact.privacy_classification,
                accepted_by=fact.accepted_by,
            ),
            recorded_at=sync_time,
        )


def _resolve_fact_db_root(programs_root: Path) -> Path | None:
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def _parse_workstreams(raw_workstreams: dict[str, Any], path: Path) -> tuple[Workstream, ...]:
    workstreams_payload = raw_workstreams.get("workstreams", [])
    if not isinstance(workstreams_payload, list):
        raise ConfigError(f"workstreams must be a list in {path}")
    return tuple(
        Workstream(
            id=_required_string(entry.get("id"), path, "workstreams[].id"),
            name=_required_string(entry.get("name"), path, "workstreams[].name"),
            aliases=_string_tuple(entry.get("aliases", [])),
            area_paths=_string_tuple(entry.get("area_paths", [])),
            ado_team=_optional_string(entry.get("ado_team")),
            ado_pipeline_ids=_string_tuple(entry.get("ado_pipeline_ids", [])),
            ado_repository_ids=_string_tuple(entry.get("ado_repository_ids", [])),
            pm_owner=_optional_string(entry.get("pm_owner")),
            eng_owner=_optional_string(entry.get("eng_owner")),
            accountable_owner=_optional_string(entry.get("raci", {}).get("accountable")) if isinstance(entry.get("raci"), dict) else None,
            accountable_email=_optional_string(entry.get("raci", {}).get("accountable_email")) if isinstance(entry.get("raci"), dict) else None,
            responsible_owners=_string_tuple(entry.get("raci", {}).get("responsible", [])) if isinstance(entry.get("raci"), dict) else (),
            consulted_owners=_string_tuple(entry.get("raci", {}).get("consulted", [])) if isinstance(entry.get("raci"), dict) else (),
            informed_owners=_string_tuple(entry.get("raci", {}).get("informed", [])) if isinstance(entry.get("raci"), dict) else (),
            dri_email=_optional_string(entry.get("dri_email")),
            alternate_owner=_optional_string(entry.get("alternate_owner")),
            always_notify=_string_tuple(entry.get("always_notify", [])),
            description=_optional_string(entry.get("description")),
            why_it_matters=_optional_string(entry.get("why_it_matters")),
            history_summary=_optional_string(entry.get("history_summary")),
            leadership_sensitivity=_optional_string(entry.get("leadership_sensitivity")),
            current_blocker=_optional_string(entry.get("current_blocker")),
            ado_saved_query_ids=_string_tuple(entry.get("ado_saved_query_ids", [])),
            signal_sources=_parse_signal_sources(entry.get("signal_sources"), path),
            last_reviewed_date=_optional_date(entry.get("last_reviewed_date"), path, "workstreams[].last_reviewed_date"),
            owner_person_id=_optional_string(entry.get("owner_person_id")),
            status=_optional_string(entry.get("status")) or "active",
        )
        for entry in workstreams_payload
        if isinstance(entry, dict)
    )
