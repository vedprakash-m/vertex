"""specs/bklg.md BL-E3: routine people-registry freshness/enrichment.

Demand-driven, not calendar-blind: a stale field only becomes an
enrichment candidate when the person who owns it is a real stakeholder in
a program's CURRENT committed state (`load_program_stakeholder_aliases` --
no fresh gather triggered), matching specs/people.md's own "population is
demand-driven" non-goal (NG1/NG2) for the registry as a whole.

This module is Zone A: it owns the candidate ledger, selection logic, and
question text, but never calls WorkIQ itself (`AgencyBridge` lives in
`src/m365/`, Zone C). The orchestrator (`vertex kb people enrich`,
`src/commands/kb.py`) is what actually calls WorkIQ and hands the raw
answer back to `record_enrichment_event` here.

WorkIQ answers are free text, advisory, and NEVER auto-applied -- every
event lands as `status="pending"` until a human steward resolves it via
`vertex kb people enrichment resolve`, which is the only path that can set
`applied=True` (through the existing `apply_shared_registry_patch` staged
writer, not a new write path).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

from src.core.alerts import AlertSeverity, append_or_suppress_alert, entity_scoped_alert_id, resolve_alert
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.people_directory_schema import PersonDirectory, load_people_directory
from src.core.people_namespace_bridge import normalize_alias_for_lookup
from src.core.people_query import DEFAULT_STALE_FRESHNESS_DAYS, StalePersonEntry, list_stale_people
from src.core.policy_loader import load_freshness_policy
from src.core.program_context import load_program_stakeholder_aliases

#: BL-E4 activation, 2026-07-26: the operator explicitly rejected an
#: OS-level (wall-clock, Task Scheduler) cadence for the enrichment
#: reminder, wanting it tied to Vertex's own operational rhythm instead --
#: a completed nudge run or a completed (non-dry-run) report run.
CadenceTriggerKind = Literal["nudge_run", "report_run"]

#: Fixed identity for the between-runs alert (alerts.py) this trigger
#: raises -- one alert per program, not per field/person, since the
#: reminder is "some enrichment is due", not tied to a specific candidate.
_CADENCE_ALERT_CATEGORY = "people_enrichment_due"
_CADENCE_ALERT_ENTITY_TYPE = "registry"
_CADENCE_ALERT_ENTITY_ID = "people_directory"

#: specs/people.md §8.3's `_INFORMATIONAL_PERSON_FIELDS`
#: (people_registry_governance.py) minus display_name (already reliable --
#: it's how the person is referenced in the first place) and
#: exempt_from_vitality (an operator flag, not a fact WorkIQ could ever
#: answer). These three are exactly the fields worth spending a slow,
#: best-effort WorkIQ round-trip on.
ENRICHABLE_FIELDS: tuple[str, ...] = ("title", "department", "manager_entity_id")

#: Matches every other JSONL ledger's rotation cap in this codebase
#: (armada_leakage.py, proposal_audit.jsonl).
_LEDGER_MAX_BYTES = 10 * 1024 * 1024

EnrichmentEventKind = Literal["proposed", "accepted", "rejected"]


@dataclass(frozen=True, slots=True)
class EnrichmentCandidateEvent:
    recorded_at: datetime
    program_id: str
    candidate_id: str
    entity_id: str
    alias: str
    field_name: str
    current_value: str | None
    event: EnrichmentEventKind
    workiq_question: str | None = None
    workiq_answer: str | None = None
    reviewed_value: str | None = None
    reviewed_by: str | None = None
    reviewed_reason: str | None = None
    applied: bool = False


@dataclass(frozen=True, slots=True)
class EnrichmentCandidateState:
    """Current, folded state of one candidate -- the result of replaying
    every event for its candidate_id in recorded_at order."""

    candidate_id: str
    program_id: str
    entity_id: str
    alias: str
    field_name: str
    current_value: str | None
    workiq_question: str | None
    workiq_answer: str | None
    status: Literal["pending", "accepted", "rejected"]
    proposed_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_reason: str | None = None
    reviewed_value: str | None = None
    applied: bool = False


def enrichment_ledger_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_quality" / "people_enrichment_candidates.jsonl"


def _event_to_jsonable(event: EnrichmentCandidateEvent) -> dict[str, object]:
    return {
        "recorded_at": event.recorded_at.isoformat(),
        "program_id": event.program_id,
        "candidate_id": event.candidate_id,
        "entity_id": event.entity_id,
        "alias": event.alias,
        "field_name": event.field_name,
        "current_value": event.current_value,
        "event": event.event,
        "workiq_question": event.workiq_question,
        "workiq_answer": event.workiq_answer,
        "reviewed_value": event.reviewed_value,
        "reviewed_by": event.reviewed_by,
        "reviewed_reason": event.reviewed_reason,
        "applied": event.applied,
    }


def record_enrichment_event(event: EnrichmentCandidateEvent, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    line = json.dumps(_event_to_jsonable(event), sort_keys=True) + "\n"
    append_jsonl_line(enrichment_ledger_path(event.program_id, programs_root=programs_root), line, max_bytes=_LEDGER_MAX_BYTES)


def read_enrichment_events(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[EnrichmentCandidateEvent, ...]:
    path = enrichment_ledger_path(program_id, programs_root=programs_root)
    events = []
    for raw in read_jsonl_records(path):
        events.append(
            EnrichmentCandidateEvent(
                recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                program_id=raw["program_id"],
                candidate_id=raw["candidate_id"],
                entity_id=raw["entity_id"],
                alias=raw["alias"],
                field_name=raw["field_name"],
                current_value=raw.get("current_value"),
                event=raw["event"],
                workiq_question=raw.get("workiq_question"),
                workiq_answer=raw.get("workiq_answer"),
                reviewed_value=raw.get("reviewed_value"),
                reviewed_by=raw.get("reviewed_by"),
                reviewed_reason=raw.get("reviewed_reason"),
                applied=bool(raw.get("applied", False)),
            )
        )
    return tuple(events)


def fold_enrichment_candidates(events: tuple[EnrichmentCandidateEvent, ...]) -> tuple[EnrichmentCandidateState, ...]:
    """One state per candidate_id, replaying its events in recorded_at
    order. A candidate_id with only a `proposed` event is "pending"; a
    later `accepted`/`rejected` event for the same candidate_id resolves
    it -- mirrors armada_leakage.py's fold-on-read convention exactly."""
    by_id: dict[str, list[EnrichmentCandidateEvent]] = {}
    for event in sorted(events, key=lambda e: e.recorded_at):
        by_id.setdefault(event.candidate_id, []).append(event)

    states: list[EnrichmentCandidateState] = []
    for candidate_id, candidate_events in by_id.items():
        proposed = next((e for e in candidate_events if e.event == "proposed"), None)
        if proposed is None:
            continue  # malformed ledger entry -- no proposal to fold from, skip rather than fabricate one
        resolution = next((e for e in candidate_events if e.event in ("accepted", "rejected")), None)
        status: Literal["pending", "accepted", "rejected"] = "pending" if resolution is None else resolution.event  # type: ignore[assignment]
        states.append(
            EnrichmentCandidateState(
                candidate_id=candidate_id,
                program_id=proposed.program_id,
                entity_id=proposed.entity_id,
                alias=proposed.alias,
                field_name=proposed.field_name,
                current_value=proposed.current_value,
                workiq_question=proposed.workiq_question,
                workiq_answer=proposed.workiq_answer,
                status=status,
                proposed_at=proposed.recorded_at,
                reviewed_at=resolution.recorded_at if resolution else None,
                reviewed_by=resolution.reviewed_by if resolution else None,
                reviewed_reason=resolution.reviewed_reason if resolution else None,
                reviewed_value=resolution.reviewed_value if resolution else None,
                applied=resolution.applied if resolution else False,
            )
        )
    return tuple(sorted(states, key=lambda s: s.proposed_at))


def list_pending_enrichment_candidates(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[EnrichmentCandidateState, ...]:
    events = read_enrichment_events(program_id, programs_root=programs_root)
    return tuple(state for state in fold_enrichment_candidates(events) if state.status == "pending")


def select_enrichment_candidates(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    freshness_days: int | None = None,
    as_of: datetime | None = None,
    max_candidates: int | None = None,
) -> tuple[tuple[PersonDirectory, StalePersonEntry], ...]:
    """Demand-driven selection: a field only becomes a candidate if its
    person is a real stakeholder in `program_id`'s CURRENT committed
    `program.yaml` state -- `load_program_stakeholder_aliases` reads only
    that one file, no fresh gather triggered.

    Two independent reasons a field needs enrichment, both covered here:
    1. STALE -- has a `FieldVerification` older than the freshness SLA
       (`list_stale_people`, DIR-03's own definition).
    2. NEVER VERIFIED AND EMPTY -- `list_stale_people` only checks
       `person.verifications`, so a field with no verification record at
       all (every real person backfilled into this registry so far --
       none carry any `verifications` history) is invisible to it, even
       though an empty, never-checked field is at least as real a gap as
       a merely-aged one. Checked directly here rather than silently
       relying on staleness alone to cover a case it structurally cannot.

    Already-pending candidates for the same (entity_id, field_name) are
    never re-proposed. A person with `exempt_from_vitality=True` (e.g. a
    sentinel/placeholder record like "unassigned" used as a fallback owner
    elsewhere in the platform, never a real Microsoft employee) is never a
    candidate -- WorkIQ has no fact to return for it."""
    now = as_of or datetime.now(timezone.utc)
    knowledge_root = get_shared_knowledge_root(programs_root)
    resolved_freshness_days = freshness_days if freshness_days is not None else DEFAULT_STALE_FRESHNESS_DAYS

    stakeholder_aliases = {normalize_alias_for_lookup(alias) for alias in load_program_stakeholder_aliases(program_id, programs_root=programs_root)}
    if not stakeholder_aliases:
        return ()

    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    people_by_entity_id = {p.entity_id: p for p in (people_result.people if people_result else ())}
    referenced_people = tuple(
        person
        for person in people_by_entity_id.values()
        if normalize_alias_for_lookup(person.alias) in stakeholder_aliases and not person.exempt_from_vitality
    )
    if not referenced_people:
        return ()

    already_pending = {
        (state.entity_id, state.field_name) for state in list_pending_enrichment_candidates(program_id, programs_root=programs_root)
    }

    candidates: dict[tuple[str, str], StalePersonEntry] = {}

    # Reason 1: stale (has a verification, but it's old).
    stale_entries = list_stale_people(knowledge_root=knowledge_root, as_of=now, freshness_days=resolved_freshness_days)
    referenced_entity_ids = {person.entity_id for person in referenced_people}
    for entry in stale_entries:
        if entry.field_name in ENRICHABLE_FIELDS and entry.entity_id in referenced_entity_ids:
            candidates[(entry.entity_id, entry.field_name)] = entry

    # Reason 2: never verified and currently empty.
    for person in referenced_people:
        for field_name in ENRICHABLE_FIELDS:
            key = (person.entity_id, field_name)
            if key in candidates:
                continue
            if _current_field_value(person, field_name):
                continue
            has_verification = any(v.field_name == field_name for v in person.verifications)
            if has_verification:
                continue  # has a verification but isn't stale per list_stale_people -- leave it alone
            candidates[key] = StalePersonEntry(
                entity_id=person.entity_id, alias=person.alias, field_name=field_name,
                verified_at=now, age_days=0,
            )

    selected: list[tuple[PersonDirectory, StalePersonEntry]] = []
    for key, entry in candidates.items():
        if key in already_pending:
            continue
        candidate_person = people_by_entity_id.get(entry.entity_id)
        if candidate_person is None:
            continue
        selected.append((candidate_person, entry))
        if max_candidates is not None and len(selected) >= max_candidates:
            break
    return tuple(selected)


_FIELD_QUESTION_TEMPLATES: dict[str, str] = {
    "title": "What is {name}'s current job title at Microsoft?",
    "department": "What team or organization does {name} currently work in at Microsoft?",
    "manager_entity_id": "Who does {name} currently report to at Microsoft (their manager's name)?",
}


def build_workiq_question(*, display_name: str | None, alias: str, field_name: str) -> str:
    if field_name not in _FIELD_QUESTION_TEMPLATES:
        raise ValueError(f"No WorkIQ question template for field {field_name!r}; expected one of {ENRICHABLE_FIELDS}.")
    name = display_name or alias
    return _FIELD_QUESTION_TEMPLATES[field_name].format(name=name)


def _current_field_value(person: PersonDirectory, field_name: str) -> str | None:
    return getattr(person, field_name, None)


def _cadence_ledger_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_quality" / "enrichment_cadence_ticks.jsonl"


def record_cadence_tick(
    program_id: str, kind: CadenceTriggerKind, *, programs_root: Path = PROGRAMS_ROOT, now: datetime | None = None,
) -> int:
    """Append one tick for *kind* and return its new running total for this
    program. A tick is one completed (non-dry-run) nudge or report run --
    Vertex's own operational rhythm, not a wall-clock timer."""
    path = _cadence_ledger_path(program_id, programs_root=programs_root)
    line = json.dumps(
        {"recorded_at": (now or datetime.now(timezone.utc)).isoformat(), "program_id": program_id, "kind": kind},
        sort_keys=True,
    ) + "\n"
    append_jsonl_line(path, line, max_bytes=_LEDGER_MAX_BYTES)
    return sum(1 for record in read_jsonl_records(path) if record.get("kind") == kind)


def maybe_alert_enrichment_due(
    *, program_id: str, kind: CadenceTriggerKind, programs_root: Path = PROGRAMS_ROOT, now: datetime | None = None,
) -> bool:
    """BL-E4 activation: ticks a cadence counter tied to Vertex's own
    operational rhythm (a completed nudge or report run) and, every Nth
    tick (N from `freshness_policy.yaml`'s `people_registry.enrichment_trigger`,
    per *kind*; `None`/0 disables that kind), raises a between-runs alert
    (`alerts.py`) pointing at `vertex kb people enrich` -- it NEVER calls
    WorkIQ itself. This mirrors `report.py`'s own INV-ADF-2 discipline
    (report never performs WorkIQ NL discovery inline, after a historical
    >65min inline-call hang) rather than reinventing a looser rule here:
    the reminder is instant and free; the actual WorkIQ round-trip stays a
    separate, explicit, human-triggered command.

    Returns True iff an alert was raised on this call (i.e. the tick count
    just crossed a multiple of the configured threshold)."""
    policy = load_freshness_policy()
    every = policy.people_registry_enrichment_nudge_every if kind == "nudge_run" else policy.people_registry_enrichment_report_every
    if not every:
        return False
    count = record_cadence_tick(program_id, kind, programs_root=programs_root, now=now)
    if count % every != 0:
        return False
    append_or_suppress_alert(
        program_id=program_id,
        category=_CADENCE_ALERT_CATEGORY,
        entity_type=_CADENCE_ALERT_ENTITY_TYPE,
        entity_id=_CADENCE_ALERT_ENTITY_ID,
        severity=AlertSeverity.INFO,
        message=f"Routine people-registry enrichment is due ({count} {kind.replace('_', ' ')}s since last check, every {every}).",
        next_command=f"vertex kb people enrich --program {program_id}",
        programs_root=programs_root,
        now=now,
    )
    return True


def resolve_enrichment_due_alert(*, program_id: str, programs_root: Path = PROGRAMS_ROOT, now: datetime | None = None) -> bool:
    """Called from `vertex kb people enrich` itself: running the command
    satisfies whatever cadence reminder was open, regardless of how many
    (if any) candidates it finds or how the operator later resolves them."""
    alert_id = entity_scoped_alert_id(
        program_id=program_id, category=_CADENCE_ALERT_CATEGORY, entity_type=_CADENCE_ALERT_ENTITY_TYPE, entity_id=_CADENCE_ALERT_ENTITY_ID,
    )
    return resolve_alert(alert_id, program_id=program_id, programs_root=programs_root, now=now)
