from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator

from src.core._db import open_program_db
from src.core.ledger.candidate_store import CandidateDecisionRecord, load_triage_decisions
from src.core.ledger.event_log import EventEnvelope, EventWriteResult, read_events
from src.core.ledger.event_index import load_entity_event_ids, load_event_entity_refs
from src.core.ledger.event_types import get_event_schema, support_table_update
from src.core.projections.program_projection import FieldCandidate, choose_field_winner
from src.core.protection.supersession import apply_supersession, find_tombstoned_targets


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
PROJECTION_SCHEMA_VERSION = "1"
# Bumped when projection logic changes (independent of schema_version).
# Incremental logic forces a full rebuild when the stored projector_version differs.
_PROJECTOR_VERSION = "2"
# Maximum events in a delta before we skip the incremental short-circuit and
# do a full rebuild.  Keeps the incremental path bounded and predictable.
_MAX_INCREMENTAL_DELTA = 50

_CONFIDENCE_STRENGTH = {
    "operator_confirmed": 4,
    "source_authoritative": 3,
    "ai_extracted": 2,
    "inferred": 1,
}

_TEMPORAL_CONFIDENCE_STRENGTH = {
    "exact": 4,
    "approximate": 3,
    "estimated": 2,
    "reconstructed": 1,
}


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    program_id: str
    projection_path: Path
    event_watermark: str
    event_count: int
    coverage_earliest: str | None
    coverage_latest: str | None


_INCREMENTAL_ENTITY_TABLES = {
    "risk": ("proj_risk", "risk_id"),
    "milestone": ("proj_milestone", "milestone_id"),
    "deliverable": ("proj_deliverable", "deliverable_id"),
    "sku_generation": ("proj_sku_generation", "sku_generation_id"),
    "decision": ("proj_decision", "decision_id"),
    "assumption": ("proj_assumption", "assumption_id"),
    "dependency": ("proj_dependency", "dependency_id"),
    "commitment": ("proj_commitment", "commitment_id"),
    "kpi": ("proj_kpi", "kpi_id"),
    "incident": ("proj_incident", "incident_id"),
    "article": ("proj_knowledge_article", "article_id"),
    "playbook": ("proj_playbook", "playbook_id"),
    "artifact": ("proj_published_artifact", "artifact_id"),
}

_FULL_REBUILD_EVENT_PREFIXES = (
    "program.",
    "schedule.",
    "workstream.",
)

_LOCKABLE_FIELDS = {
    "risk": ("proj_risk", "risk_id", frozenset({"status", "severity", "owner_person_id", "workstream_id", "likelihood"})),
    "milestone": ("proj_milestone", "milestone_id", frozenset({"target_date", "status", "completed_on", "workstream_id"})),
    "deliverable": ("proj_deliverable", "deliverable_id", frozenset({"status", "workstream_id", "due_date"})),
    "decision": ("proj_decision", "decision_id", frozenset({"decision_text", "status", "forum", "title"})),
    "assumption": ("proj_assumption", "assumption_id", frozenset({"status", "statement", "evidence", "impact"})),
    "dependency": ("proj_dependency", "dependency_id", frozenset({"status", "description", "needed_by"})),
    "commitment": ("proj_commitment", "commitment_id", frozenset({"due_date", "status", "fulfilled_on"})),
    "kpi": ("proj_kpi", "kpi_id", frozenset({"status", "name", "definition", "unit"})),
    "incident": ("proj_incident", "incident_id", frozenset({"status", "severity", "title", "resolved_on", "mttr_minutes", "root_cause"})),
    "article": ("proj_knowledge_article", "article_id", frozenset({"location", "status", "title"})),
    "playbook": ("proj_playbook", "playbook_id", frozenset({"location", "title"})),
}

_CREATION_EVENT_TYPES = {
    "risk": "risk.raised.v1",
    "milestone": "milestone.created.v1",
    "decision": "decision.made.v1",
    "assumption": "assumption.stated.v1",
    "dependency": "dependency.declared.v1",
    "workstream": "workstream.created.v1",
    "deliverable": "deliverable.created.v1",
    "commitment": "commitment.made.v1",
    "incident": "incident.opened.v1",
    "article": "knowledge.article_added.v1",
}


def project_program_events(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    projection_path: Path | None = None,
    as_of: datetime | None = None,
    knowledge_as_of: datetime | None = None,
) -> ProjectionResult:
    events = read_events(program_id, programs_root=programs_root)
    target_path = projection_path or get_current_projection_path(program_id, programs_root=programs_root)
    return project_events_to_sqlite(
        program_id,
        events,
        projection_path=target_path,
        programs_root=programs_root,
        as_of=as_of,
        knowledge_as_of=knowledge_as_of,
    )


def project_events_to_sqlite(
    program_id: str,
    events: Iterable[EventEnvelope],
    *,
    projection_path: Path,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    knowledge_as_of: datetime | None = None,
) -> ProjectionResult:
    ordered_events = tuple(sorted(events, key=lambda event: (event.recorded_at, event.event_id)))
    visible_events = _filter_events(ordered_events, as_of=as_of, knowledge_as_of=knowledge_as_of)
    effective_events = apply_supersession(visible_events)
    tombstoned_targets = find_tombstoned_targets(visible_events)
    projection_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = projection_path.with_suffix(projection_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()
    with connect_projection_db(temp_path) as connection:
        _ensure_schema(connection)
        _clear_projection_tables(connection)
        _fold_events(connection, effective_events, visible_events, as_of=as_of)
        _record_orphan_links(connection, visible_events=visible_events, effective_events=effective_events, tombstoned_targets=tombstoned_targets)
        _overlay_gap_acknowledgements(connection, program_id=program_id, programs_root=programs_root)
        watermark = visible_events[-1].event_id if visible_events else ""
        coverage_earliest, coverage_latest = _coverage_range(effective_events)
        connection.execute(
            "INSERT INTO projection_meta (schema_version, built_at, event_watermark, as_of, knowledge_as_of, coverage_earliest, coverage_latest, projector_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                PROJECTION_SCHEMA_VERSION,
                datetime.now(timezone.utc).isoformat(),
                watermark,
                as_of.isoformat() if as_of is not None else None,
                knowledge_as_of.isoformat() if knowledge_as_of is not None else None,
                coverage_earliest,
                coverage_latest,
                _PROJECTOR_VERSION,
            ),
        )
    if projection_path.exists():
        projection_path.unlink()
    temp_path.replace(projection_path)
    return ProjectionResult(
        program_id=program_id,
        projection_path=projection_path,
        event_watermark=watermark,
        event_count=len(effective_events),
        coverage_earliest=coverage_earliest,
        coverage_latest=coverage_latest,
    )


def project_events_incremental_to_sqlite(
    program_id: str,
    events: Iterable[EventEnvelope],
    *,
    projection_path: Path,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    knowledge_as_of: datetime | None = None,
) -> ProjectionResult:
    ordered_events = tuple(sorted(events, key=lambda event: (event.recorded_at, event.event_id)))
    visible_events = _filter_events(ordered_events, as_of=as_of, knowledge_as_of=knowledge_as_of)
    if not projection_path.exists():
        return project_events_to_sqlite(program_id, visible_events, projection_path=projection_path, programs_root=programs_root, as_of=as_of, knowledge_as_of=knowledge_as_of)

    existing_meta = canonical_projection_dump(projection_path)["projection_meta"]
    if not existing_meta:
        return project_events_to_sqlite(program_id, visible_events, projection_path=projection_path, programs_root=programs_root, as_of=as_of, knowledge_as_of=knowledge_as_of)

    meta_row = existing_meta[0]
    previous_watermark = meta_row.get("event_watermark", "")
    if not previous_watermark:
        return project_events_to_sqlite(program_id, visible_events, projection_path=projection_path, programs_root=programs_root, as_of=as_of, knowledge_as_of=knowledge_as_of)

    # Force full rebuild if the projector logic version has changed (rebuild-after-upgrade).
    stored_pv = meta_row.get("projector_version")
    if stored_pv != _PROJECTOR_VERSION:
        return project_events_to_sqlite(program_id, visible_events, projection_path=projection_path, programs_root=programs_root, as_of=as_of, knowledge_as_of=knowledge_as_of)

    delta_events = _events_after_watermark(visible_events, previous_watermark)
    if not delta_events:
        return ProjectionResult(
            program_id=program_id,
            projection_path=projection_path,
            event_watermark=previous_watermark,
            event_count=len(apply_supersession(visible_events)),
            coverage_earliest=meta_row.get("coverage_earliest"),
            coverage_latest=meta_row.get("coverage_latest"),
        )
    # Bounded window: if the delta is too large, full rebuild is safer.
    if len(delta_events) > _MAX_INCREMENTAL_DELTA:
        return project_events_to_sqlite(
            program_id,
            visible_events,
            projection_path=projection_path,
            programs_root=programs_root,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
        )

    # Any delta event that requires global state reconstruction → full rebuild.
    # Covers program.*, schedule.*, workstream.* which restructure the whole projection.
    if any(
        event.event_type.startswith(prefix)
        for event in delta_events
        for prefix in _FULL_REBUILD_EVENT_PREFIXES
    ):
        return project_events_to_sqlite(
            program_id,
            visible_events,
            projection_path=projection_path,
            programs_root=programs_root,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
        )

    # A correction event can retroactively change any entity's state → full rebuild.
    if any(event.event_type == "operator.correction.v1" for event in delta_events):
        return project_events_to_sqlite(
            program_id,
            visible_events,
            projection_path=projection_path,
            programs_root=programs_root,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
        )

    return _incremental_fold(
        program_id,
        visible_events=visible_events,
        delta_events=delta_events,
        projection_path=projection_path,
        programs_root=programs_root,
        as_of=as_of,
    )


def _incremental_fold(
    program_id: str,
    *,
    visible_events: tuple[EventEnvelope, ...],
    delta_events: tuple[EventEnvelope, ...],
    projection_path: Path,
    programs_root: Path,
    as_of: datetime | None,
) -> ProjectionResult:
    """Apply a small, non-correction delta directly to an existing projection DB.

    Called by ``project_events_incremental_to_sqlite`` when the delta is small
    and does not contain correction/full-rebuild events.  Only entities that
    appear in the delta are re-folded; all other entities are left unchanged.

    Supersession is still computed globally (over all visible events) so that
    a superseded delta event is correctly excluded before folding.
    """
    # Compute effective events (supersession applied globally)
    effective_events = apply_supersession(visible_events)
    effective_ids: set[str] = {e.event_id for e in effective_events}

    # Identify affected entity IDs (preserving insertion order, deduped)
    seen_ids: set[str] = set()
    affected_entity_ids: list[str] = []
    for event in delta_events:
        eid = _entity_id_from_event(event)
        if eid is not None and eid not in seen_ids:
            seen_ids.add(eid)
            affected_entity_ids.append(eid)

    # Collect ALL effective events for affected entities (pre-watermark + delta)
    entity_events: dict[str, list[EventEnvelope]] = {}
    for event in effective_events:
        eid = _entity_id_from_event(event)
        if eid in seen_ids:
            entity_events.setdefault(eid, []).append(event)

    new_watermark = visible_events[-1].event_id if visible_events else ""
    coverage_earliest, coverage_latest = _coverage_range(effective_events)

    with connect_projection_db(projection_path) as connection:
        # Wipe stale rows for affected entities so re-fold starts clean
        _delete_affected_rows(connection, affected_entity_ids)

        # Re-fold each affected entity using its complete (globally-superseded) history
        for entity_id, evts in entity_events.items():
            family = _entity_family(entity_id)
            if family == "risk":
                _fold_risk_events(connection, entity_id, tuple(evts))
            elif family == "milestone":
                _fold_milestone_events(connection, entity_id, tuple(evts))
            elif family == "deliverable":
                _fold_deliverable_events(connection, entity_id, tuple(evts))
            elif family == "sku_generation":
                _fold_sku_generation_events(connection, entity_id, tuple(evts))
            elif family == "decision":
                _fold_decision_events(connection, entity_id, tuple(evts))
            elif family == "assumption":
                _fold_assumption_events(connection, entity_id, tuple(evts))
            elif family == "dependency":
                _fold_dependency_events(connection, entity_id, tuple(evts))
            elif family == "commitment":
                _fold_commitment_events(connection, entity_id, tuple(evts))
            elif family == "kpi":
                _fold_kpi_events(connection, entity_id, tuple(evts))
            elif family == "incident":
                _fold_incident_events(connection, entity_id, tuple(evts))
            elif family == "article":
                _fold_knowledge_article_events(connection, entity_id, tuple(evts))
            elif family == "playbook":
                _fold_playbook_events(connection, entity_id, tuple(evts))
            elif family == "artifact":
                _fold_artifact_events(connection, entity_id, tuple(evts))

        # Apply control events (field_lock / field_unlock) from delta
        for event in delta_events:
            if get_event_schema(event.event_type).is_control:
                _apply_control_event(connection, event)

        # Apply support-table updates (gap events) from delta
        for event in delta_events:
            if event.event_type == "pipeline.gap_detected.v1":
                _apply_support_table_updates(connection, event)

        # Re-apply field locks (idempotent — touches all locked entities)
        _apply_field_lock_overrides(connection, as_of=as_of)

        # Re-apply gap acknowledgements (idempotent)
        _overlay_gap_acknowledgements(connection, program_id=program_id, programs_root=programs_root)

        # Update projection metadata (watermark + coverage range)
        connection.execute(
            "UPDATE projection_meta SET event_watermark = ?, built_at = ?, coverage_earliest = ?, coverage_latest = ?",
            (
                new_watermark,
                datetime.now(timezone.utc).isoformat(),
                coverage_earliest,
                coverage_latest,
            ),
        )

    return ProjectionResult(
        program_id=program_id,
        projection_path=projection_path,
        event_watermark=new_watermark,
        event_count=len(effective_events),
        coverage_earliest=coverage_earliest,
        coverage_latest=coverage_latest,
    )


def project_events_to_memory(
    program_id: str,
    events: Iterable[EventEnvelope],
    *,
    as_of: datetime | None = None,
    knowledge_as_of: datetime | None = None,
    triage_decisions: tuple[CandidateDecisionRecord, ...] = (),
) -> dict[str, list[dict[str, Any]]]:
    ordered_events = tuple(sorted(events, key=lambda event: (event.recorded_at, event.event_id)))
    visible_events = _filter_events(ordered_events, as_of=as_of, knowledge_as_of=knowledge_as_of)
    effective_events = apply_supersession(visible_events)
    tombstoned_targets = find_tombstoned_targets(visible_events)
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        _ensure_schema(connection)
        _clear_projection_tables(connection)
        _fold_events(connection, effective_events, visible_events, as_of=as_of)
        _record_orphan_links(connection, visible_events=visible_events, effective_events=effective_events, tombstoned_targets=tombstoned_targets)
        _overlay_gap_acknowledgements_from_decisions(connection, triage_decisions=triage_decisions)
        watermark = visible_events[-1].event_id if visible_events else ""
        coverage_earliest, coverage_latest = _coverage_range(effective_events)
        connection.execute(
            "INSERT INTO projection_meta (schema_version, built_at, event_watermark, as_of, knowledge_as_of, coverage_earliest, coverage_latest, projector_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                PROJECTION_SCHEMA_VERSION,
                datetime.now(timezone.utc).isoformat(),
                watermark,
                as_of.isoformat() if as_of is not None else None,
                knowledge_as_of.isoformat() if knowledge_as_of is not None else None,
                coverage_earliest,
                coverage_latest,
                _PROJECTOR_VERSION,
            ),
        )
        connection.commit()
        return {
            "proj_program": _dump_table(connection, "SELECT * FROM proj_program ORDER BY program_id"),
            "proj_phase": _dump_table(connection, "SELECT * FROM proj_phase ORDER BY phase_id"),
            "proj_schedule_baseline": _dump_table(connection, "SELECT * FROM proj_schedule_baseline ORDER BY schedule_id, baseline_name"),
            "proj_workstream": _dump_table(connection, "SELECT * FROM proj_workstream ORDER BY workstream_id"),
            "proj_risk": _dump_table(connection, "SELECT * FROM proj_risk ORDER BY risk_id"),
            "proj_milestone": _dump_table(connection, "SELECT * FROM proj_milestone ORDER BY milestone_id"),
            "proj_deliverable": _dump_table(connection, "SELECT * FROM proj_deliverable ORDER BY deliverable_id"),
            "proj_sku_generation": _dump_table(connection, "SELECT * FROM proj_sku_generation ORDER BY sku_generation_id"),
            "proj_decision": _dump_table(connection, "SELECT * FROM proj_decision ORDER BY decision_id"),
            "proj_assumption": _dump_table(connection, "SELECT * FROM proj_assumption ORDER BY assumption_id"),
            "proj_dependency": _dump_table(connection, "SELECT * FROM proj_dependency ORDER BY dependency_id"),
            "proj_commitment": _dump_table(connection, "SELECT * FROM proj_commitment ORDER BY commitment_id"),
            "proj_kpi": _dump_table(connection, "SELECT * FROM proj_kpi ORDER BY kpi_id"),
            "proj_kpi_series": _dump_table(connection, "SELECT * FROM proj_kpi_series ORDER BY kpi_id, observed_at, dimensions_hash"),
            "proj_incident": _dump_table(connection, "SELECT * FROM proj_incident ORDER BY incident_id"),
            "proj_knowledge_article": _dump_table(connection, "SELECT * FROM proj_knowledge_article ORDER BY article_id"),
            "proj_playbook": _dump_table(connection, "SELECT * FROM proj_playbook ORDER BY playbook_id"),
            "proj_published_artifact": _dump_table(connection, "SELECT * FROM proj_published_artifact ORDER BY artifact_id, published_at"),
            "entity_links": _dump_table(connection, "SELECT * FROM entity_links ORDER BY from_entity, link_kind, to_entity, event_id"),
            "event_orphan_links": _dump_table(connection, "SELECT * FROM event_orphan_links ORDER BY event_id, orphaned_by"),
            "event_shadow_links": _dump_table(connection, "SELECT * FROM event_shadow_links ORDER BY event_id, field_name, shadowed_by"),
            "field_locks": _dump_table(connection, "SELECT * FROM field_locks ORDER BY entity_id, field"),
            "gaps": _dump_table(connection, "SELECT * FROM gaps ORDER BY event_id"),
            "projection_meta": _dump_projection_meta(connection),
        }
    finally:
        connection.close()


def canonical_projection_dump(projection_path: Path) -> dict[str, list[dict[str, Any]]]:
    """INV-AF-13 (WO-2 item 6): routed through ``open_program_db()`` in
    read-only mode — this function only ever runs ``SELECT`` queries and
    never commits. The prior raw sqlite3 connect call would silently create
    an empty file if ``projection_path`` was missing (then fail on "no such
    table" from the ``SELECT``s below); ``read_only=True`` fails fast with a
    clearer "unable to open database file" instead — every caller already
    guards on ``projection_path.exists()`` first.
    """
    with open_program_db(projection_path, read_only=True) as connection:
        return {
            "proj_program": _dump_table(connection, "SELECT * FROM proj_program ORDER BY program_id"),
            "proj_phase": _dump_table(connection, "SELECT * FROM proj_phase ORDER BY phase_id"),
            "proj_schedule_baseline": _dump_table(connection, "SELECT * FROM proj_schedule_baseline ORDER BY schedule_id, baseline_name"),
            "proj_workstream": _dump_table(connection, "SELECT * FROM proj_workstream ORDER BY workstream_id"),
            "proj_risk": _dump_table(connection, "SELECT * FROM proj_risk ORDER BY risk_id"),
            "proj_milestone": _dump_table(connection, "SELECT * FROM proj_milestone ORDER BY milestone_id"),
            "proj_deliverable": _dump_table(connection, "SELECT * FROM proj_deliverable ORDER BY deliverable_id"),
            "proj_sku_generation": _dump_table(connection, "SELECT * FROM proj_sku_generation ORDER BY sku_generation_id"),
            "proj_decision": _dump_table(connection, "SELECT * FROM proj_decision ORDER BY decision_id"),
            "proj_assumption": _dump_table(connection, "SELECT * FROM proj_assumption ORDER BY assumption_id"),
            "proj_dependency": _dump_table(connection, "SELECT * FROM proj_dependency ORDER BY dependency_id"),
            "proj_commitment": _dump_table(connection, "SELECT * FROM proj_commitment ORDER BY commitment_id"),
            "proj_kpi": _dump_table(connection, "SELECT * FROM proj_kpi ORDER BY kpi_id"),
            "proj_kpi_series": _dump_table(connection, "SELECT * FROM proj_kpi_series ORDER BY kpi_id, observed_at, dimensions_hash"),
            "proj_incident": _dump_table(connection, "SELECT * FROM proj_incident ORDER BY incident_id"),
            "proj_knowledge_article": _dump_table(connection, "SELECT * FROM proj_knowledge_article ORDER BY article_id"),
            "proj_playbook": _dump_table(connection, "SELECT * FROM proj_playbook ORDER BY playbook_id"),
            "proj_published_artifact": _dump_table(connection, "SELECT * FROM proj_published_artifact ORDER BY artifact_id, published_at"),
            "entity_links": _dump_table(connection, "SELECT * FROM entity_links ORDER BY from_entity, link_kind, to_entity, event_id"),
            "event_orphan_links": _dump_table(connection, "SELECT * FROM event_orphan_links ORDER BY event_id, orphaned_by"),
            "event_shadow_links": _dump_table(connection, "SELECT * FROM event_shadow_links ORDER BY event_id, field_name, shadowed_by"),
            "field_locks": _dump_table(connection, "SELECT * FROM field_locks ORDER BY entity_id, field"),
            "gaps": _dump_table(connection, "SELECT * FROM gaps ORDER BY event_id"),
            "projection_meta": _dump_projection_meta(connection),
        }


def collapse_shadow_links(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, str | None]:
    grouped: dict[str, set[str]] = {}
    for row in rows:
        event_id = row.get("event_id")
        shadowed_by = row.get("shadowed_by")
        if not isinstance(event_id, str) or not event_id or not isinstance(shadowed_by, str) or not shadowed_by:
            continue
        grouped.setdefault(event_id, set()).add(shadowed_by)
    collapsed: dict[str, str | None] = {}
    for event_id, winners in grouped.items():
        collapsed[event_id] = next(iter(winners)) if len(winners) == 1 else None
    return collapsed


def collapse_orphan_links(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, str | None]:
    collapsed: dict[str, str | None] = {}
    for row in rows:
        event_id = row.get("event_id")
        orphaned_by = row.get("orphaned_by")
        if not isinstance(event_id, str) or not event_id or not isinstance(orphaned_by, str) or not orphaned_by:
            continue
        collapsed[event_id] = orphaned_by
    return collapsed


def get_current_projection_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / "projections" / "current.sqlite3"


@contextmanager
def connect_projection_db(path: Path) -> Iterator[sqlite3.Connection]:
    """INV-AF-13 (WO-2 item 6): connection creation routed through
    ``open_program_db()`` (``durability="balanced"`` preserves the prior
    always-``synchronous=NORMAL`` behavior on local paths), but the
    ``SQLiteUnitOfWork`` context-manager sugar is deliberately bypassed —
    this store needs its post-commit ``PRAGMA wal_checkpoint(TRUNCATE)`` to
    run *between* commit and close, which ``SQLiteUnitOfWork.__exit__``
    doesn't expose a hook for. Manual commit/rollback/checkpoint/close
    preserves the exact prior ordering.
    """
    connection = open_program_db(path).connection
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        connection.close()


def _filter_events(
    events: tuple[EventEnvelope, ...],
    *,
    as_of: datetime | None,
    knowledge_as_of: datetime | None,
) -> tuple[EventEnvelope, ...]:
    domain_cutoff = as_of or datetime.max.replace(tzinfo=timezone.utc)
    knowledge_cutoff = knowledge_as_of or datetime.max.replace(tzinfo=timezone.utc)
    visible: list[EventEnvelope] = []
    for event in events:
        schema = get_event_schema(event.event_type)
        if schema.is_control:
            if event.recorded_at <= knowledge_cutoff:
                visible.append(event)
            continue
        if event.recorded_at <= knowledge_cutoff and event.occurred_at <= domain_cutoff:
            visible.append(event)
    return tuple(visible)
def _fold_events(
    connection: sqlite3.Connection,
    effective_events: tuple[EventEnvelope, ...],
    visible_events: tuple[EventEnvelope, ...],
    *,
    as_of: datetime | None,
) -> None:
    program_events: list[EventEnvelope] = []
    phase_events: list[EventEnvelope] = []
    schedule_events: list[EventEnvelope] = []
    workstream_events: dict[str, list[EventEnvelope]] = {}
    risk_events: dict[str, list[EventEnvelope]] = {}
    milestone_events: dict[str, list[EventEnvelope]] = {}
    deliverable_events: dict[str, list[EventEnvelope]] = {}
    sku_generation_events: dict[str, list[EventEnvelope]] = {}
    decision_events: dict[str, list[EventEnvelope]] = {}
    assumption_events: dict[str, list[EventEnvelope]] = {}
    dependency_events: dict[str, list[EventEnvelope]] = {}
    commitment_events: dict[str, list[EventEnvelope]] = {}
    kpi_events: dict[str, list[EventEnvelope]] = {}
    incident_events: dict[str, list[EventEnvelope]] = {}
    knowledge_article_events: dict[str, list[EventEnvelope]] = {}
    playbook_events: dict[str, list[EventEnvelope]] = {}
    artifact_events: dict[str, list[EventEnvelope]] = {}
    for event in visible_events:
        if get_event_schema(event.event_type).is_control:
            _apply_control_event(connection, event)
    for event in effective_events:
        if event.event_type.startswith("program."):
            if event.event_type in {"program.phase_entered.v1", "program.phase_exited.v1"}:
                phase_events.append(event)
            else:
                program_events.append(event)
            continue
        if event.event_type == "schedule.baseline_set.v1":
            schedule_events.append(event)
            continue
        if event.event_type.startswith("workstream."):
            workstream_events.setdefault(event.payload["workstream_id"], []).append(event)
            continue
        if event.event_type.startswith("risk."):
            risk_events.setdefault(event.payload["risk_id"], []).append(event)
            continue
        if event.event_type.startswith("milestone."):
            milestone_events.setdefault(event.payload["milestone_id"], []).append(event)
            continue
        if event.event_type.startswith("deliverable."):
            deliverable_events.setdefault(event.payload["deliverable_id"], []).append(event)
            continue
        if event.event_type == "sku_generation.added.v1":
            sku_generation_events.setdefault(event.payload["sku_generation_id"], []).append(event)
            continue
        if event.event_type.startswith("decision."):
            decision_events.setdefault(event.payload["decision_id"], []).append(event)
            continue
        if event.event_type.startswith("assumption."):
            assumption_events.setdefault(event.payload["assumption_id"], []).append(event)
            continue
        if event.event_type.startswith("dependency."):
            dependency_events.setdefault(event.payload["dependency_id"], []).append(event)
            continue
        if event.event_type.startswith("commitment."):
            commitment_events.setdefault(event.payload["commitment_id"], []).append(event)
            continue
        if event.event_type.startswith("kpi.") or event.event_type == "metric.observed.v1":
            kpi_events.setdefault(event.payload["kpi_id"], []).append(event)
            continue
        if event.event_type.startswith("incident."):
            incident_events.setdefault(event.payload["incident_id"], []).append(event)
            continue
        if event.event_type.startswith("knowledge.article"):
            knowledge_article_events.setdefault(event.payload["article_id"], []).append(event)
            continue
        if event.event_type == "playbook.created.v1":
            playbook_events.setdefault(event.payload["playbook_id"], []).append(event)
            continue
        if event.event_type == "artifact.published.v1":
            artifact_events.setdefault(event.payload["artifact_id"], []).append(event)
            continue
        if event.event_type == "pipeline.gap_detected.v1":
            _apply_support_table_updates(connection, event)
    _fold_program_events(connection, effective_events[0].program_id if effective_events else None, tuple(program_events), tuple(phase_events), tuple(schedule_events))
    for entity_id, events in workstream_events.items():
        _fold_workstream_events(connection, entity_id, tuple(events))
    for entity_id, events in risk_events.items():
        _fold_risk_events(connection, entity_id, tuple(events))
    for entity_id, events in milestone_events.items():
        _fold_milestone_events(connection, entity_id, tuple(events))
    for entity_id, events in deliverable_events.items():
        _fold_deliverable_events(connection, entity_id, tuple(events))
    for entity_id, events in sku_generation_events.items():
        _fold_sku_generation_events(connection, entity_id, tuple(events))
    for entity_id, events in decision_events.items():
        _fold_decision_events(connection, entity_id, tuple(events))
    for entity_id, events in assumption_events.items():
        _fold_assumption_events(connection, entity_id, tuple(events))
    for entity_id, events in dependency_events.items():
        _fold_dependency_events(connection, entity_id, tuple(events))
    for entity_id, events in commitment_events.items():
        _fold_commitment_events(connection, entity_id, tuple(events))
    for entity_id, events in kpi_events.items():
        _fold_kpi_events(connection, entity_id, tuple(events))
    for entity_id, events in incident_events.items():
        _fold_incident_events(connection, entity_id, tuple(events))
    for entity_id, events in knowledge_article_events.items():
        _fold_knowledge_article_events(connection, entity_id, tuple(events))
    for entity_id, events in playbook_events.items():
        _fold_playbook_events(connection, entity_id, tuple(events))
    for entity_id, events in artifact_events.items():
        _fold_artifact_events(connection, entity_id, tuple(events))
    _apply_field_lock_overrides(connection, as_of=as_of)


def _apply_control_event(connection: sqlite3.Connection, event: EventEnvelope) -> None:
    if event.event_type == "operator.field_lock.v1":
        connection.execute(
            "INSERT OR REPLACE INTO field_locks (entity_id, field, locked_value, valid_until, lock_event_id) VALUES (?, ?, ?, ?, ?)",
            (
                event.payload["entity_id"],
                event.payload["field"],
                json.dumps(event.payload.get("locked_value"), sort_keys=True) if "locked_value" in event.payload else None,
                event.payload.get("valid_until"),
                event.event_id,
            ),
        )
    elif event.event_type == "operator.field_unlock.v1":
        connection.execute(
            "DELETE FROM field_locks WHERE entity_id = ? AND field = ?",
            (event.payload["entity_id"], event.payload["field"]),
        )


def _apply_field_lock_overrides(connection: sqlite3.Connection, *, as_of: datetime | None) -> None:
    rows = connection.execute(
        "SELECT entity_id, field, locked_value, valid_until FROM field_locks ORDER BY entity_id, field"
    ).fetchall()
    for entity_id, field_name, locked_value, valid_until in rows:
        if valid_until and as_of is not None:
            expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
            if as_of >= expiry:
                continue
        family = _entity_family(str(entity_id))
        if family is None or family not in _LOCKABLE_FIELDS:
            continue
        table, key_column, allowed_fields = _LOCKABLE_FIELDS[family]
        if str(field_name) not in allowed_fields:
            continue
        if locked_value is None:
            continue
        parsed_value = json.loads(locked_value)
        connection.execute(
            f"UPDATE {table} SET {field_name} = ? WHERE {key_column} = ?",
            (parsed_value, entity_id),
        )


def _overlay_gap_acknowledgements(
    connection: sqlite3.Connection,
    *,
    program_id: str,
    programs_root: Path,
) -> None:
    _overlay_gap_acknowledgements_from_decisions(
        connection,
        triage_decisions=load_triage_decisions(program_id, programs_root=programs_root),
    )


def _overlay_gap_acknowledgements_from_decisions(
    connection: sqlite3.Connection,
    *,
    triage_decisions: tuple[CandidateDecisionRecord, ...],
) -> None:
    acknowledged_event_ids = {
        decision.gap_event_id
        for decision in triage_decisions
        if decision.kind == "gap_acknowledged" and decision.gap_event_id is not None
    }
    for event_id in acknowledged_event_ids:
        connection.execute("UPDATE gaps SET acknowledged = 1 WHERE event_id = ?", (event_id,))


def _apply_domain_event(connection: sqlite3.Connection, event: EventEnvelope) -> None:
    return


def _fold_risk_events(connection: sqlite3.Connection, risk_id: str, events: tuple[EventEnvelope, ...]) -> None:
    raised_events = tuple(event for event in events if event.event_type == "risk.raised.v1")
    if not raised_events:
        return
    create_event = max(raised_events, key=lambda event: event.occurred_at)
    title = choose_field_winner(FieldCandidate(event, event.payload["title"]) for event in raised_events)
    severity = choose_field_winner(
        [FieldCandidate(event, event.payload["severity"]) for event in raised_events]
        + [FieldCandidate(event, event.payload["severity"]) for event in events if event.event_type == "risk.status_changed.v1" and event.payload.get("severity") is not None]
    )
    likelihood = choose_field_winner(
        FieldCandidate(event, event.payload["likelihood"]) for event in raised_events if event.payload.get("likelihood") is not None
    )
    owner_person_id = choose_field_winner(
        [FieldCandidate(event, event.payload["owner_person_id"]) for event in raised_events if event.payload.get("owner_person_id") is not None]
        + [FieldCandidate(event, event.payload["new_owner_person_id"]) for event in events if event.event_type == "risk.owner_changed.v1"]
    )
    workstream_id = choose_field_winner(
        FieldCandidate(event, event.payload["workstream_id"]) for event in raised_events if event.payload.get("workstream_id") is not None
    )
    status = choose_field_winner(
        [FieldCandidate(event, "open") for event in raised_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "risk.status_changed.v1"]
        + [FieldCandidate(event, "closed") for event in events if event.event_type == "risk.closed.v1"]
        + [FieldCandidate(event, "mitigated") for event in events if event.event_type == "risk.mitigated.v1"]
    )
    closed = choose_field_winner(
        FieldCandidate(event, (event.occurred_at.isoformat(), event.payload["closure_reason"])) for event in events if event.event_type == "risk.closed.v1"
    )
    _record_shadow_links(
        connection,
        "risk.title",
        [FieldCandidate(event, event.payload["title"]) for event in raised_events],
        title,
    )
    _record_shadow_links(
        connection,
        "risk.severity",
        [FieldCandidate(event, event.payload["severity"]) for event in raised_events]
        + [FieldCandidate(event, event.payload["severity"]) for event in events if event.event_type == "risk.status_changed.v1" and event.payload.get("severity") is not None],
        severity,
    )
    _record_shadow_links(
        connection,
        "risk.likelihood",
        [FieldCandidate(event, event.payload["likelihood"]) for event in raised_events if event.payload.get("likelihood") is not None],
        likelihood,
    )
    _record_shadow_links(
        connection,
        "risk.owner_person_id",
        [FieldCandidate(event, event.payload["owner_person_id"]) for event in raised_events if event.payload.get("owner_person_id") is not None]
        + [FieldCandidate(event, event.payload["new_owner_person_id"]) for event in events if event.event_type == "risk.owner_changed.v1"],
        owner_person_id,
    )
    _record_shadow_links(
        connection,
        "risk.workstream_id",
        [FieldCandidate(event, event.payload["workstream_id"]) for event in raised_events if event.payload.get("workstream_id") is not None],
        workstream_id,
    )
    _record_shadow_links(
        connection,
        "risk.status",
        [FieldCandidate(event, "open") for event in raised_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "risk.status_changed.v1"]
        + [FieldCandidate(event, "closed") for event in events if event.event_type == "risk.closed.v1"]
        + [FieldCandidate(event, "mitigated") for event in events if event.event_type == "risk.mitigated.v1"],
        status,
    )
    _record_shadow_links(
        connection,
        "risk.closed",
        [FieldCandidate(event, (event.occurred_at.isoformat(), event.payload["closure_reason"])) for event in events if event.event_type == "risk.closed.v1"],
        closed,
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        """
        INSERT OR REPLACE INTO proj_risk (
            risk_id, title, severity, likelihood, status, owner_person_id,
            workstream_id, raised_at, closed_at, closure_reason, derived_from_event_ids,
            min_confidence, min_temporal_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            risk_id,
            title.value if title is not None else None,
            severity.value if severity is not None else None,
            likelihood.value if likelihood is not None else None,
            status.value if status is not None else None,
            owner_person_id.value if owner_person_id is not None else None,
            workstream_id.value if workstream_id is not None else None,
            create_event.occurred_at.isoformat(),
            closed.value[0] if closed is not None else None,
            closed.value[1] if closed is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_milestone_events(connection: sqlite3.Connection, milestone_id: str, events: tuple[EventEnvelope, ...]) -> None:
    created_events = tuple(event for event in events if event.event_type == "milestone.created.v1")
    created_event = max(created_events, key=lambda event: event.occurred_at) if created_events else None
    if created_event is None:
        _ensure_milestone_stub(connection, milestone_id, events[0].event_id)
    name = choose_field_winner(FieldCandidate(event, event.payload["name"]) for event in created_events)
    target_date = choose_field_winner(
        [FieldCandidate(event, event.payload["target_date"]) for event in created_events]
        + [FieldCandidate(event, event.payload["new_target_date"]) for event in events if event.event_type == "milestone.date_revised.v1"]
    )
    workstream_id = choose_field_winner(
        FieldCandidate(event, event.payload["workstream_id"]) for event in created_events if event.payload.get("workstream_id") is not None
    )
    status = choose_field_winner(
        [FieldCandidate(event, "on_track") for event in created_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "milestone.status_changed.v1"]
        + [FieldCandidate(event, "completed") for event in events if event.event_type == "milestone.completed.v1"]
    )
    completed_on = choose_field_winner(
        FieldCandidate(event, event.payload["completed_on"]) for event in events if event.event_type == "milestone.completed.v1"
    )
    _record_shadow_links(
        connection,
        "milestone.name",
        [FieldCandidate(event, event.payload["name"]) for event in created_events],
        name,
    )
    _record_shadow_links(
        connection,
        "milestone.target_date",
        [FieldCandidate(event, event.payload["target_date"]) for event in created_events]
        + [FieldCandidate(event, event.payload["new_target_date"]) for event in events if event.event_type == "milestone.date_revised.v1"],
        target_date,
    )
    _record_shadow_links(
        connection,
        "milestone.workstream_id",
        [FieldCandidate(event, event.payload["workstream_id"]) for event in created_events if event.payload.get("workstream_id") is not None],
        workstream_id,
    )
    _record_shadow_links(
        connection,
        "milestone.status",
        [FieldCandidate(event, "on_track") for event in created_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "milestone.status_changed.v1"]
        + [FieldCandidate(event, "completed") for event in events if event.event_type == "milestone.completed.v1"],
        status,
    )
    _record_shadow_links(
        connection,
        "milestone.completed_on",
        [FieldCandidate(event, event.payload["completed_on"]) for event in events if event.event_type == "milestone.completed.v1"],
        completed_on,
    )
    revision_count = sum(1 for event in events if event.event_type == "milestone.date_revised.v1")
    original_target_date = created_event.payload["target_date"] if created_event is not None else None
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        """
        INSERT OR REPLACE INTO proj_milestone (
            milestone_id, name, target_date, original_target_date, status,
            completed_on, workstream_id, date_revision_count, derived_from_event_ids,
            min_confidence, min_temporal_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            milestone_id,
            name.value if name is not None else None,
            target_date.value if target_date is not None else None,
            original_target_date,
            status.value if status is not None else "stub",
            completed_on.value if completed_on is not None else None,
            workstream_id.value if workstream_id is not None else None,
            revision_count,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_decision_events(connection: sqlite3.Connection, decision_id: str, events: tuple[EventEnvelope, ...]) -> None:
    created_events = tuple(event for event in events if event.event_type == "decision.made.v1")
    if not created_events:
        return
    created_event = max(created_events, key=lambda event: event.occurred_at)
    title = choose_field_winner(FieldCandidate(event, event.payload["title"]) for event in created_events)
    decision_text = choose_field_winner(
        [FieldCandidate(event, event.payload["decision_text"]) for event in created_events]
        + [FieldCandidate(event, event.payload["revision_text"]) for event in events if event.event_type == "decision.revised.v1"]
    )
    forum = choose_field_winner(
        FieldCandidate(event, event.payload["forum"]) for event in created_events if event.payload.get("forum") is not None
    )
    decided_by = choose_field_winner(
        FieldCandidate(event, event.payload["decided_by"]) for event in created_events
    )
    superseded = choose_field_winner(
        FieldCandidate(event, event.payload["supersedes_decision_id"]) for event in events if event.event_type == "decision.superseded.v1"
    )
    status = choose_field_winner(
        [FieldCandidate(event, "active") for event in created_events]
        + [FieldCandidate(event, "revised") for event in events if event.event_type == "decision.revised.v1"]
        + [FieldCandidate(event, "superseded") for event in events if event.event_type == "decision.superseded.v1"]
    )
    _record_shadow_links(
        connection,
        "decision.title",
        [FieldCandidate(event, event.payload["title"]) for event in created_events],
        title,
    )
    _record_shadow_links(
        connection,
        "decision.decision_text",
        [FieldCandidate(event, event.payload["decision_text"]) for event in created_events]
        + [FieldCandidate(event, event.payload["revision_text"]) for event in events if event.event_type == "decision.revised.v1"],
        decision_text,
    )
    _record_shadow_links(
        connection,
        "decision.forum",
        [FieldCandidate(event, event.payload["forum"]) for event in created_events if event.payload.get("forum") is not None],
        forum,
    )
    _record_shadow_links(
        connection,
        "decision.decided_by",
        [FieldCandidate(event, event.payload["decided_by"]) for event in created_events],
        decided_by,
    )
    _record_shadow_links(
        connection,
        "decision.supersedes_decision_id",
        [FieldCandidate(event, event.payload["supersedes_decision_id"]) for event in events if event.event_type == "decision.superseded.v1"],
        superseded,
    )
    _record_shadow_links(
        connection,
        "decision.status",
        [FieldCandidate(event, "active") for event in created_events]
        + [FieldCandidate(event, "revised") for event in events if event.event_type == "decision.revised.v1"]
        + [FieldCandidate(event, "superseded") for event in events if event.event_type == "decision.superseded.v1"],
        status,
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        """
        INSERT OR REPLACE INTO proj_decision (
            decision_id, title, decision_text, decided_by, forum, made_at,
            status, superseded_by_decision_id, derived_from_event_ids,
            min_confidence, min_temporal_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            title.value if title is not None else None,
            decision_text.value if decision_text is not None else None,
            json.dumps(decided_by.value) if decided_by is not None else None,
            forum.value if forum is not None else None,
            created_event.occurred_at.isoformat(),
            status.value if status is not None else "active",
            superseded.value if superseded is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )
    if superseded is not None:
        _insert_entity_link(connection, decision_id, "supersedes", superseded.value, superseded.event.event_id)


def _fold_assumption_events(connection: sqlite3.Connection, assumption_id: str, events: tuple[EventEnvelope, ...]) -> None:
    stated_events = tuple(event for event in events if event.event_type == "assumption.stated.v1")
    if not stated_events:
        return
    stated_event = max(stated_events, key=lambda event: event.occurred_at)
    statement = choose_field_winner(FieldCandidate(event, event.payload["statement"]) for event in stated_events)
    evidence = choose_field_winner(
        [FieldCandidate(event, event.payload["evidence"]) for event in events if event.event_type == "assumption.validated.v1"]
        + [FieldCandidate(event, event.payload["evidence"]) for event in events if event.event_type == "assumption.invalidated.v1"]
    )
    impact = choose_field_winner(
        FieldCandidate(event, event.payload["impact"]) for event in events if event.event_type == "assumption.invalidated.v1" and event.payload.get("impact") is not None
    )
    status = choose_field_winner(
        [FieldCandidate(event, "stated") for event in stated_events]
        + [FieldCandidate(event, "validated") for event in events if event.event_type == "assumption.validated.v1"]
        + [FieldCandidate(event, "invalidated") for event in events if event.event_type == "assumption.invalidated.v1"]
    )
    resolved = choose_field_winner(
        [FieldCandidate(event, event.occurred_at.isoformat()) for event in events if event.event_type == "assumption.validated.v1"]
        + [FieldCandidate(event, event.occurred_at.isoformat()) for event in events if event.event_type == "assumption.invalidated.v1"]
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        """
        INSERT OR REPLACE INTO proj_assumption (
            assumption_id, statement, status, stated_at, resolved_at, evidence, impact, derived_from_event_ids,
            min_confidence, min_temporal_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assumption_id,
            statement.value if statement is not None else None,
            status.value if status is not None else "stated",
            stated_event.occurred_at.isoformat(),
            resolved.value if resolved is not None else None,
            evidence.value if evidence is not None else None,
            impact.value if impact is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_dependency_events(connection: sqlite3.Connection, dependency_id: str, events: tuple[EventEnvelope, ...]) -> None:
    declared_events = tuple(event for event in events if event.event_type == "dependency.declared.v1")
    if not declared_events:
        return
    declared_event = max(declared_events, key=lambda event: event.occurred_at)
    description = choose_field_winner(
        FieldCandidate(event, event.payload["description"]) for event in declared_events if event.payload.get("description") is not None
    )
    needed_by = choose_field_winner(
        FieldCandidate(event, event.payload["needed_by"]) for event in declared_events if event.payload.get("needed_by") is not None
    )
    status = choose_field_winner(
        [FieldCandidate(event, "on_track") for event in declared_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "dependency.status_changed.v1"]
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        """
        INSERT OR REPLACE INTO proj_dependency (
            dependency_id, from_entity, to_entity, description, needed_by,
            status, declared_at, derived_from_event_ids, min_confidence, min_temporal_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dependency_id,
            declared_event.payload["from_entity"],
            declared_event.payload["to_entity"],
            description.value if description is not None else None,
            needed_by.value if needed_by is not None else None,
            status.value if status is not None else "on_track",
            declared_event.occurred_at.isoformat(),
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )
    _insert_entity_link(connection, declared_event.payload["from_entity"], "depends_on", declared_event.payload["to_entity"], declared_event.event_id)


def _fold_program_events(
    connection: sqlite3.Connection,
    program_id: str | None,
    program_events: tuple[EventEnvelope, ...],
    phase_events: tuple[EventEnvelope, ...],
    schedule_events: tuple[EventEnvelope, ...],
) -> None:
    if program_id is None:
        return
    all_events = tuple(sorted((*program_events, *phase_events, *schedule_events), key=lambda item: item.recorded_at))
    charter_events = [event for event in program_events if event.event_type in {"program.charter_established.v1", "program.charter_revised.v1"}]
    charter_event = max(charter_events, key=lambda event: event.occurred_at) if charter_events else None
    sub_programs: list[dict[str, Any]] = []
    for event in sorted((item for item in program_events if item.event_type == "program.sub_program_added.v1"), key=lambda item: item.occurred_at):
        sub_programs.append(
            {
                "sub_program_id": event.payload["sub_program_id"],
                "relationship": event.payload["relationship"],
                "cadence": event.payload.get("cadence"),
            }
        )
        _insert_entity_link(connection, program_id, "sub_program", event.payload["sub_program_id"], event.event_id)
    for event in program_events:
        if event.event_type == "program.scope_changed.v1":
            for affected in event.payload.get("affected_entities", []):
                if isinstance(affected, str):
                    _insert_entity_link(connection, program_id, "scope_changed", affected, event.event_id)
    current_phase_id: str | None = None
    for event in sorted(phase_events, key=lambda item: item.occurred_at):
        if event.event_type == "program.phase_entered.v1":
            connection.execute(
                "INSERT OR REPLACE INTO proj_phase (phase_id, name, entered_at, exited_at, status, derived_from_event_ids) VALUES (?, ?, ?, NULL, 'active', ?)",
                (event.payload["phase_id"], event.payload["phase_name"], event.occurred_at.isoformat(), json.dumps([event.event_id])),
            )
            current_phase_id = event.payload["phase_id"]
        elif event.event_type == "program.phase_exited.v1":
            row = connection.execute("SELECT derived_from_event_ids FROM proj_phase WHERE phase_id = ?", (event.payload["phase_id"],)).fetchone()
            existing = [] if row is None or row[0] is None else json.loads(row[0])
            connection.execute(
                "UPDATE proj_phase SET exited_at = ?, status = 'exited', derived_from_event_ids = ? WHERE phase_id = ?",
                (event.occurred_at.isoformat(), json.dumps(existing + [event.event_id]), event.payload["phase_id"]),
            )
            if current_phase_id == event.payload["phase_id"]:
                current_phase_id = None
    connection.execute(
        "INSERT OR REPLACE INTO proj_program (program_id, current_phase_id, charter_event_id, sub_programs, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            program_id,
            current_phase_id,
            charter_event.event_id if charter_event is not None else None,
            json.dumps(sub_programs),
            _aggregate_provenance(all_events)[0],
            _aggregate_provenance(all_events)[1],
            _aggregate_provenance(all_events)[2],
        ),
    )
    for event in schedule_events:
        derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance((event,))
        connection.execute(
            "INSERT OR REPLACE INTO proj_schedule_baseline (schedule_id, baseline_name, set_at, milestone_dates, superseded, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (
                event.payload["schedule_id"],
                event.payload["baseline_name"],
                event.occurred_at.isoformat(),
                json.dumps(event.payload["milestone_dates"], sort_keys=True),
                derived_from_event_ids,
                min_confidence,
                min_temporal_confidence,
            ),
        )
        if event.payload.get("supersedes_baseline_id"):
            connection.execute(
                "UPDATE proj_schedule_baseline SET superseded = 1 WHERE schedule_id = ?",
                (event.payload["supersedes_baseline_id"],),
            )


def _fold_workstream_events(connection: sqlite3.Connection, workstream_id: str, events: tuple[EventEnvelope, ...]) -> None:
    created_events = tuple(event for event in events if event.event_type == "workstream.created.v1")
    if not created_events:
        return
    created_event = max(created_events, key=lambda event: event.occurred_at)
    owner = choose_field_winner(
        [FieldCandidate(event, event.payload["owner_person_id"]) for event in created_events if event.payload.get("owner_person_id") is not None]
        + [FieldCandidate(event, event.payload["new_owner_person_id"]) for event in events if event.event_type == "workstream.owner_changed.v1"]
    )
    status = choose_field_winner(
        [FieldCandidate(event, "active") for event in created_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "workstream.status_changed.v1"]
    )
    parent = choose_field_winner(
        FieldCandidate(event, event.payload["parent_workstream_id"]) for event in created_events if event.payload.get("parent_workstream_id") is not None
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_workstream (workstream_id, name, owner_person_id, status, parent_workstream_id, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            workstream_id,
            created_event.payload["name"],
            owner.value if owner is not None else None,
            status.value if status is not None else "active",
            parent.value if parent is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_deliverable_events(connection: sqlite3.Connection, deliverable_id: str, events: tuple[EventEnvelope, ...]) -> None:
    created_events = tuple(event for event in events if event.event_type == "deliverable.created.v1")
    if not created_events:
        return
    created_event = max(created_events, key=lambda event: event.occurred_at)
    status = choose_field_winner(
        [FieldCandidate(event, "active") for event in created_events]
        + [FieldCandidate(event, event.payload["new_status"]) for event in events if event.event_type == "deliverable.status_changed.v1"]
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_deliverable (deliverable_id, name, status, workstream_id, due_date, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            deliverable_id,
            created_event.payload["name"],
            status.value if status is not None else "active",
            created_event.payload.get("workstream_id"),
            created_event.payload.get("due_date"),
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_sku_generation_events(connection: sqlite3.Connection, sku_generation_id: str, events: tuple[EventEnvelope, ...]) -> None:
    event = max(events, key=lambda item: item.occurred_at)
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_sku_generation (sku_generation_id, name, first_deployment_date, products, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            sku_generation_id,
            event.payload["name"],
            event.payload.get("first_deployment_date"),
            json.dumps(event.payload.get("products", [])),
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )
    for product in event.payload.get("products", []):
        if isinstance(product, str):
            _insert_entity_link(connection, sku_generation_id, "product", product, event.event_id)


def _fold_commitment_events(connection: sqlite3.Connection, commitment_id: str, events: tuple[EventEnvelope, ...]) -> None:
    made_events = tuple(event for event in events if event.event_type == "commitment.made.v1")
    if not made_events:
        return
    made_event = max(made_events, key=lambda event: event.occurred_at)
    due_date = choose_field_winner(
        [FieldCandidate(event, event.payload["due_date"]) for event in made_events if event.payload.get("due_date") is not None]
        + [FieldCandidate(event, event.payload["new_due_date"]) for event in events if event.event_type == "commitment.slipped.v1" and event.payload.get("new_due_date") is not None]
    )
    status = choose_field_winner(
        [FieldCandidate(event, "open") for event in made_events]
        + [FieldCandidate(event, "slipped") for event in events if event.event_type == "commitment.slipped.v1"]
        + [FieldCandidate(event, "fulfilled") for event in events if event.event_type == "commitment.fulfilled.v1"]
    )
    fulfilled_on = choose_field_winner(
        FieldCandidate(event, event.payload["fulfilled_on"]) for event in events if event.event_type == "commitment.fulfilled.v1"
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_commitment (commitment_id, text, owner_person_id, due_date, original_due_date, status, made_in, slip_count, fulfilled_on, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            commitment_id,
            made_event.payload["text"],
            made_event.payload["owner_person_id"],
            due_date.value if due_date is not None else None,
            made_event.payload.get("due_date"),
            status.value if status is not None else "open",
            made_event.payload.get("made_in"),
            sum(1 for event in events if event.event_type == "commitment.slipped.v1"),
            fulfilled_on.value if fulfilled_on is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_kpi_events(connection: sqlite3.Connection, kpi_id: str, events: tuple[EventEnvelope, ...]) -> None:
    defined_events = tuple(event for event in events if event.event_type == "kpi.defined.v1")
    defined_provenance = _aggregate_provenance(events)
    if defined_events:
        defined_event = max(defined_events, key=lambda event: event.occurred_at)
        status = "decommissioned" if any(event.event_type == "kpi.decommissioned.v1" for event in events) else "active"
        decommissioned = next((event.occurred_at.isoformat() for event in sorted(events, key=lambda item: item.occurred_at) if event.event_type == "kpi.decommissioned.v1"), None)
        connection.execute(
            "INSERT OR REPLACE INTO proj_kpi (kpi_id, name, definition, unit, owner_person_id, thresholds, status, defined_at, decommissioned_at, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kpi_id,
                defined_event.payload["name"],
                defined_event.payload.get("definition"),
                defined_event.payload.get("unit"),
                defined_event.payload.get("owner_person_id"),
                json.dumps(defined_event.payload.get("thresholds", {}), sort_keys=True) if defined_event.payload.get("thresholds") is not None else None,
                status,
                defined_event.occurred_at.isoformat(),
                decommissioned,
                defined_provenance[0],
                defined_provenance[1],
                defined_provenance[2],
            ),
        )
    for event in sorted((item for item in events if item.event_type == "metric.observed.v1"), key=lambda item: item.occurred_at):
        derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance((event,))
        observed_at = event.payload.get("window_end") or event.occurred_at.isoformat()
        dimensions = event.payload.get("dimensions") or {}
        dimensions_hash = _dimensions_hash(dimensions)
        connection.execute(
            "INSERT OR REPLACE INTO proj_kpi_series (kpi_id, observed_at, dimensions_hash, value, unit, dimensions, source_ref_type, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kpi_id,
                observed_at,
                dimensions_hash,
                event.payload["value"],
                event.payload.get("unit"),
                json.dumps(dimensions, sort_keys=True),
                event.source_ref.ref_type,
                derived_from_event_ids,
                min_confidence,
                min_temporal_confidence,
            ),
        )


def _fold_incident_events(connection: sqlite3.Connection, incident_id: str, events: tuple[EventEnvelope, ...]) -> None:
    opened_events = tuple(event for event in events if event.event_type == "incident.opened.v1")
    if not opened_events:
        return
    opened_event = max(opened_events, key=lambda event: event.occurred_at)
    resolved = choose_field_winner(
        FieldCandidate(event, event.payload["resolved_on"]) for event in events if event.event_type == "incident.resolved.v1"
    )
    mttr = choose_field_winner(
        FieldCandidate(event, event.payload["mttr_minutes"]) for event in events if event.event_type == "incident.resolved.v1" and event.payload.get("mttr_minutes") is not None
    )
    root_cause = choose_field_winner(
        FieldCandidate(event, event.payload["root_cause"]) for event in events if event.event_type == "incident.resolved.v1" and event.payload.get("root_cause") is not None
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_incident (incident_id, severity, title, status, opened_at, impacted_entities, resolved_on, mttr_minutes, root_cause, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            incident_id,
            opened_event.payload["severity"],
            opened_event.payload["title"],
            "resolved" if resolved is not None else "open",
            opened_event.occurred_at.isoformat(),
            json.dumps(opened_event.payload.get("impacted_entities", [])),
            resolved.value if resolved is not None else None,
            mttr.value if mttr is not None else None,
            root_cause.value if root_cause is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_knowledge_article_events(connection: sqlite3.Connection, article_id: str, events: tuple[EventEnvelope, ...]) -> None:
    added_events = tuple(event for event in events if event.event_type == "knowledge.article_added.v1")
    if not added_events:
        return
    added_event = max(added_events, key=lambda event: event.occurred_at)
    revised = choose_field_winner(
        FieldCandidate(event, event.payload["location"]) for event in events if event.event_type == "knowledge.article_revised.v1"
    )
    removed = choose_field_winner(
        FieldCandidate(event, event.occurred_at.isoformat()) for event in events if event.event_type == "knowledge.article_removed.v1"
    )
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_knowledge_article (article_id, title, location, topics, status, added_at, removed_at, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            article_id,
            added_event.payload["title"],
            revised.value if revised is not None else added_event.payload["location"],
            json.dumps(added_event.payload.get("topics", [])),
            "removed" if removed is not None else "active",
            added_event.occurred_at.isoformat(),
            removed.value if removed is not None else None,
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_playbook_events(connection: sqlite3.Connection, playbook_id: str, events: tuple[EventEnvelope, ...]) -> None:
    event = max(events, key=lambda item: item.occurred_at)
    derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance(events)
    connection.execute(
        "INSERT OR REPLACE INTO proj_playbook (playbook_id, title, trigger_conditions, location, created_at, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            playbook_id,
            event.payload["title"],
            json.dumps(event.payload.get("trigger_conditions", [])),
            event.payload["location"],
            event.occurred_at.isoformat(),
            derived_from_event_ids,
            min_confidence,
            min_temporal_confidence,
        ),
    )


def _fold_artifact_events(connection: sqlite3.Connection, artifact_id: str, events: tuple[EventEnvelope, ...]) -> None:
    for event in sorted(events, key=lambda item: item.occurred_at):
        derived_from_event_ids, min_confidence, min_temporal_confidence = _aggregate_provenance((event,))
        connection.execute(
            "INSERT OR REPLACE INTO proj_published_artifact (artifact_id, artifact_kind, title, period_start, period_end, location, published_at, derived_from_event_ids, min_confidence, min_temporal_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                event.payload["artifact_kind"],
                event.payload["title"],
                event.payload.get("period_start"),
                event.payload.get("period_end"),
                event.payload["location"],
                event.occurred_at.isoformat(),
                derived_from_event_ids,
                min_confidence,
                min_temporal_confidence,
            ),
        )


def _insert_entity_link(connection: sqlite3.Connection, from_entity: str, link_kind: str, to_entity: str, event_id: str) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO entity_links (from_entity, link_kind, to_entity, event_id) VALUES (?, ?, ?, ?)",
        (from_entity, link_kind, to_entity, event_id),
    )


def _record_shadow_links(
    connection: sqlite3.Connection,
    field_name: str,
    candidates: list[FieldCandidate[Any]] | tuple[FieldCandidate[Any], ...],
    winner: FieldCandidate[Any] | None,
) -> None:
    if winner is None:
        return
    for candidate in candidates:
        if candidate.event.event_id == winner.event.event_id:
            continue
        if candidate.value == winner.value:
            continue
        connection.execute(
            "INSERT OR REPLACE INTO event_shadow_links (event_id, field_name, shadowed_by) VALUES (?, ?, ?)",
            (candidate.event.event_id, field_name, winner.event.event_id),
        )


def _record_orphan_links(
    connection: sqlite3.Connection,
    *,
    visible_events: tuple[EventEnvelope, ...],
    effective_events: tuple[EventEnvelope, ...],
    tombstoned_targets: dict[str, str],
) -> None:
    visible_by_id = {event.event_id: event for event in visible_events}
    visible_by_entity: dict[str, list[EventEnvelope]] = {}
    effective_creation_present: set[str] = set()

    for event in visible_events:
        entity_id = _entity_id_from_event(event)
        if entity_id is None:
            continue
        visible_by_entity.setdefault(entity_id, []).append(event)

    for event in effective_events:
        entity_id = _entity_id_from_event(event)
        if entity_id is None:
            continue
        family = _entity_family(entity_id)
        if family is None:
            continue
        if event.event_type == _CREATION_EVENT_TYPES.get(family):
            effective_creation_present.add(entity_id)

    for entity_id, events in visible_by_entity.items():
        family = _entity_family(entity_id)
        if family is None:
            continue
        creation_type = _CREATION_EVENT_TYPES.get(family)
        if creation_type is None or entity_id in effective_creation_present:
            continue
        creation_correction_ids = [
            tombstoned_targets[event.event_id]
            for event in events
            if event.event_type == creation_type and event.event_id in tombstoned_targets
        ]
        if not creation_correction_ids:
            continue
        orphaned_by = max(
            creation_correction_ids,
            key=lambda event_id: (visible_by_id[event_id].recorded_at, event_id),
        )
        for event in events:
            if event.event_type == creation_type:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO event_orphan_links (event_id, orphaned_by) VALUES (?, ?)",
                (event.event_id, orphaned_by),
            )


def _dimensions_hash(dimensions: dict[str, Any]) -> str:
    if not dimensions:
        return ""
    return json.dumps(dimensions, sort_keys=True, separators=(",", ":"))


def _events_after_watermark(events: tuple[EventEnvelope, ...], watermark: str) -> tuple[EventEnvelope, ...]:
    for index, event in enumerate(events):
        if event.event_id == watermark:
            return events[index + 1 :]
    return events


def _delete_affected_rows(connection: sqlite3.Connection, entity_ids: list[str]) -> None:
    families: dict[str, list[str]] = {}
    for entity_id in entity_ids:
        family = _entity_family(entity_id)
        if family is None or family not in _INCREMENTAL_ENTITY_TABLES:
            continue
        families.setdefault(family, []).append(entity_id)
    for family, ids in families.items():
        table, key_column = _INCREMENTAL_ENTITY_TABLES[family]
        placeholders = ",".join("?" for _ in ids)
        connection.execute(f"DELETE FROM {table} WHERE {key_column} IN ({placeholders})", tuple(ids))


def _entity_family(entity_id: str) -> str | None:
    if ":" not in entity_id:
        return None
    prefix = entity_id.split(":", maxsplit=1)[0]
    return {
        "risk": "risk",
        "milestone": "milestone",
        "deliverable": "deliverable",
        "sku_generation": "sku_generation",
        "decision": "decision",
        "assumption": "assumption",
        "dependency": "dependency",
        "commitment": "commitment",
        "kpi": "kpi",
        "incident": "incident",
        "article": "article",
        "playbook": "playbook",
        "artifact": "artifact",
    }.get(prefix)


def _entity_id_from_event(event: EventEnvelope) -> str | None:
    if event.event_type in {"operator.field_lock.v1", "operator.field_unlock.v1"}:
        return event.payload.get("entity_id")
    if event.event_type.startswith("risk."):
        return event.payload.get("risk_id")
    if event.event_type.startswith("milestone."):
        return event.payload.get("milestone_id")
    if event.event_type.startswith("deliverable."):
        return event.payload.get("deliverable_id")
    if event.event_type.startswith("decision."):
        return event.payload.get("decision_id")
    if event.event_type.startswith("assumption."):
        return event.payload.get("assumption_id")
    if event.event_type.startswith("dependency."):
        return event.payload.get("dependency_id")
    if event.event_type.startswith("workstream."):
        return event.payload.get("workstream_id")
    if event.event_type.startswith("commitment."):
        return event.payload.get("commitment_id")
    if event.event_type.startswith("incident."):
        return event.payload.get("incident_id")
    if event.event_type.startswith("knowledge.article"):
        return event.payload.get("article_id")
    return None


def _apply_support_table_updates(connection: sqlite3.Connection, event: EventEnvelope) -> None:
    updates = support_table_update(event)
    if "gaps" in updates:
        payload = updates["gaps"]
        connection.execute(
            "INSERT OR REPLACE INTO gaps (event_id, pipeline, gap_kind, window_start, window_end, detail, acknowledged) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                payload["event_id"],
                payload["pipeline"],
                payload["gap_kind"],
                payload.get("window_start"),
                payload.get("window_end"),
                payload["detail"],
                payload["acknowledged"],
            ),
        )


def _update_with_event_id(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    id_value: str,
    values: dict[str, Any],
    event_id: str,
) -> None:
    row = connection.execute(
        f"SELECT derived_from_event_ids FROM {table} WHERE {id_column} = ?",
        (id_value,),
    ).fetchone()
    existing = [] if row is None or row[0] is None else json.loads(row[0])
    assignments = ", ".join(f"{column} = ?" for column in values.keys())
    connection.execute(
        f"UPDATE {table} SET {assignments}, derived_from_event_ids = ? WHERE {id_column} = ?",
        (*values.values(), json.dumps(existing + [event_id]), id_value),
    )


def _ensure_milestone_stub(connection: sqlite3.Connection, milestone_id: str, event_id: str, target_date: str | None = None) -> None:
    row = connection.execute(
        "SELECT milestone_id FROM proj_milestone WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    if row is not None:
        return
    connection.execute(
        """
        INSERT INTO proj_milestone (
            milestone_id, name, target_date, original_target_date, status,
            completed_on, workstream_id, date_revision_count, derived_from_event_ids
        ) VALUES (?, NULL, ?, NULL, 'stub', NULL, NULL, 0, ?)
        """,
        (milestone_id, target_date, json.dumps([event_id])),
    )


def _coverage_range(events: tuple[EventEnvelope, ...]) -> tuple[str | None, str | None]:
    domain_events = [event for event in events if not get_event_schema(event.event_type).is_control]
    if not domain_events:
        return None, None
    occurred_values = sorted(event.occurred_at for event in domain_events)
    return occurred_values[0].isoformat(), occurred_values[-1].isoformat()


def _dump_table(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(query).fetchall()
    return [dict(row) for row in rows]


def _dump_projection_meta(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _dump_table(connection, "SELECT * FROM projection_meta")
    for row in rows:
        row.pop("built_at", None)
    return rows


def _aggregate_provenance(events: Iterable[EventEnvelope]) -> tuple[str, str | None, str | None]:
    event_tuple = tuple(sorted(events, key=lambda item: item.recorded_at))
    derived = json.dumps([event.event_id for event in event_tuple])
    if not event_tuple:
        return derived, None, None
    weakest_confidence = min(event_tuple, key=lambda event: _CONFIDENCE_STRENGTH[event.confidence.value]).confidence.value
    weakest_temporal = min(event_tuple, key=lambda event: _TEMPORAL_CONFIDENCE_STRENGTH[event.temporal_confidence.value]).temporal_confidence.value
    return derived, weakest_confidence, weakest_temporal


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS proj_program (
            program_id TEXT PRIMARY KEY,
            current_phase_id TEXT,
            charter_event_id TEXT,
            sub_programs TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_phase (
            phase_id TEXT PRIMARY KEY,
            name TEXT,
            entered_at TEXT,
            exited_at TEXT,
            status TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_schedule_baseline (
            schedule_id TEXT,
            baseline_name TEXT,
            set_at TEXT,
            milestone_dates TEXT,
            superseded INTEGER DEFAULT 0,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT,
            PRIMARY KEY (schedule_id, baseline_name)
        );
        CREATE TABLE IF NOT EXISTS proj_workstream (
            workstream_id TEXT PRIMARY KEY,
            name TEXT,
            owner_person_id TEXT,
            status TEXT,
            parent_workstream_id TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_risk (
            risk_id TEXT PRIMARY KEY,
            title TEXT,
            severity TEXT,
            likelihood TEXT,
            status TEXT,
            owner_person_id TEXT,
            workstream_id TEXT,
            raised_at TEXT,
            closed_at TEXT,
            closure_reason TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_milestone (
            milestone_id TEXT PRIMARY KEY,
            name TEXT,
            target_date TEXT,
            original_target_date TEXT,
            status TEXT,
            completed_on TEXT,
            workstream_id TEXT,
            date_revision_count INTEGER DEFAULT 0,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_deliverable (
            deliverable_id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            workstream_id TEXT,
            due_date TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_sku_generation (
            sku_generation_id TEXT PRIMARY KEY,
            name TEXT,
            first_deployment_date TEXT,
            products TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_decision (
            decision_id TEXT PRIMARY KEY,
            title TEXT,
            decision_text TEXT,
            decided_by TEXT,
            forum TEXT,
            made_at TEXT,
            status TEXT,
            superseded_by_decision_id TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_assumption (
            assumption_id TEXT PRIMARY KEY,
            statement TEXT,
            status TEXT,
            stated_at TEXT,
            resolved_at TEXT,
            evidence TEXT,
            impact TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_dependency (
            dependency_id TEXT PRIMARY KEY,
            from_entity TEXT,
            to_entity TEXT,
            description TEXT,
            needed_by TEXT,
            status TEXT,
            declared_at TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_commitment (
            commitment_id TEXT PRIMARY KEY,
            text TEXT,
            owner_person_id TEXT,
            due_date TEXT,
            original_due_date TEXT,
            status TEXT,
            made_in TEXT,
            slip_count INTEGER DEFAULT 0,
            fulfilled_on TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_kpi (
            kpi_id TEXT PRIMARY KEY,
            name TEXT,
            definition TEXT,
            unit TEXT,
            owner_person_id TEXT,
            thresholds TEXT,
            status TEXT,
            defined_at TEXT,
            decommissioned_at TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_kpi_series (
            kpi_id TEXT,
            observed_at TEXT,
            dimensions_hash TEXT NOT NULL DEFAULT '',
            value REAL,
            unit TEXT,
            dimensions TEXT,
            source_ref_type TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT,
            PRIMARY KEY (kpi_id, observed_at, dimensions_hash)
        );
        CREATE TABLE IF NOT EXISTS proj_incident (
            incident_id TEXT PRIMARY KEY,
            severity TEXT,
            title TEXT,
            status TEXT,
            opened_at TEXT,
            impacted_entities TEXT,
            resolved_on TEXT,
            mttr_minutes INTEGER,
            root_cause TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_knowledge_article (
            article_id TEXT PRIMARY KEY,
            title TEXT,
            location TEXT,
            topics TEXT,
            status TEXT,
            added_at TEXT,
            removed_at TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_playbook (
            playbook_id TEXT PRIMARY KEY,
            title TEXT,
            trigger_conditions TEXT,
            location TEXT,
            created_at TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS proj_published_artifact (
            artifact_id TEXT,
            artifact_kind TEXT,
            title TEXT,
            period_start TEXT,
            period_end TEXT,
            location TEXT,
            published_at TEXT,
            derived_from_event_ids TEXT,
            min_confidence TEXT,
            min_temporal_confidence TEXT,
            PRIMARY KEY (artifact_id, published_at)
        );
        CREATE TABLE IF NOT EXISTS entity_links (
            from_entity TEXT,
            link_kind TEXT,
            to_entity TEXT,
            event_id TEXT,
            PRIMARY KEY (from_entity, link_kind, to_entity, event_id)
        );
        CREATE TABLE IF NOT EXISTS event_shadow_links (
            event_id TEXT,
            field_name TEXT,
            shadowed_by TEXT,
            PRIMARY KEY (event_id, field_name, shadowed_by)
        );
        CREATE TABLE IF NOT EXISTS event_orphan_links (
            event_id TEXT PRIMARY KEY,
            orphaned_by TEXT
        );
        CREATE TABLE IF NOT EXISTS field_locks (
            entity_id TEXT,
            field TEXT,
            locked_value TEXT,
            valid_until TEXT,
            lock_event_id TEXT,
            PRIMARY KEY (entity_id, field)
        );
        CREATE TABLE IF NOT EXISTS gaps (
            event_id TEXT PRIMARY KEY,
            pipeline TEXT,
            gap_kind TEXT,
            window_start TEXT,
            window_end TEXT,
            detail TEXT,
            acknowledged INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS projection_meta (
            schema_version TEXT NOT NULL,
            built_at TEXT NOT NULL,
            event_watermark TEXT NOT NULL,
            as_of TEXT,
            knowledge_as_of TEXT,
            coverage_earliest TEXT,
            coverage_latest TEXT,
            projector_version TEXT
        );
        """
    )


def _clear_projection_tables(connection: sqlite3.Connection) -> None:
    for table in (
        "proj_program",
        "proj_phase",
        "proj_schedule_baseline",
        "proj_workstream",
        "proj_risk",
        "proj_milestone",
        "proj_deliverable",
        "proj_sku_generation",
        "proj_decision",
        "proj_assumption",
        "proj_dependency",
        "proj_commitment",
        "proj_kpi",
        "proj_kpi_series",
        "proj_incident",
        "proj_knowledge_article",
        "proj_playbook",
        "proj_published_artifact",
        "entity_links",
        "event_shadow_links",
        "event_orphan_links",
        "field_locks",
        "gaps",
        "projection_meta",
    ):
        connection.execute(f"DELETE FROM {table}")