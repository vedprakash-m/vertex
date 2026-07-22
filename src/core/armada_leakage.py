"""specs/backlog.md BL-F1 (armada.md D-9): tag-hygiene `leakage_rate` metric
and candidate lifecycle.

D-9 defines ``leakage_rate = likely_missing_tag_items / max(authoritative_
scope_items + likely_missing_tag_items, 1)``, a 7-day owner/false-positive-
disposition SLA per candidate, and a 14-day no-unresolved-candidate rule.
Warning-only for manual-refresh MVP (D-9's own text) unless it causes a
required lane to have no authoritative scope.

This module is the candidate ledger + metric computation half. The ADO
Analytics OData fetch reuses ``ADOClient.query_work_items`` (already built
for the WorkItems/WorkItemSnapshot entity sets) against the
``armada-xhealth-catchall-ado`` golden query's ``ado_filter``/``ado_select``
-- no new query-execution engine was needed, only wiring, contrary to this
row's own original "no generic golden-query execution engine exists"
framing (that framing was about the higher-level `KustoQuery.engine`
dispatch, which indeed did not read `ado_filter`/`ado_select` before this
change; `ADOClient` itself already had everything needed).

Zone A -- no AI or M365 imports (ADOClient itself lives in src/core, not
src/m365, so this stays Zone A).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Literal

from src.core.cockpit_models import ValueConfidence
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.knowledge_store import load_program_knowledge
from src.core.models_v2 import KustoQuery

#: The one golden query this module knows how to execute today (D-9's own
#: scope). A future second ado_odata hygiene query would need its own
#: constant + a program->query_id mapping; not built speculatively.
LEAKAGE_QUERY_ID = "armada-xhealth-catchall-ado"

LeakageDisposition = Literal["unresolved", "owner_assigned", "correctly_untagged", "resolved"]
_UNSETTLED_DISPOSITIONS: frozenset[LeakageDisposition] = frozenset({"unresolved", "owner_assigned"})

LeakageEventKind = Literal["discovered", "reseen", "reopened", "disposed", "auto_resolved"]

#: BL-F1's own SLA clock semantics: 7-day owner/false-positive-disposition
#: SLA per candidate, 14-day no-unresolved-candidate rule (armada.md D-9).
OWNER_DISPOSITION_SLA_DAYS = 7
NO_UNRESOLVED_SLA_DAYS = 14

#: Matches proposal_audit.jsonl's rotation cap (jsonl_utils.py convention).
_LEDGER_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RawAdoCandidate:
    """One row from the ado_odata leakage query, before ledger folding."""

    work_item_id: int
    work_item_type: str
    title: str
    state: str
    assigned_to: str | None


@dataclass(frozen=True, slots=True)
class LeakageCandidateEvent:
    recorded_at: datetime
    program_id: str
    work_item_id: int
    org: str
    project: str
    discovery_run_id: str
    query_id: str
    # BL-F1's own data-governance acceptance criteria: "every candidate must
    # record which discovery run and which query version produced it, so a
    # leakage_rate can be attributed to a point in time rather than
    # floating." A hash of the query's filter+select text, not a semantic
    # version -- this repo has no versioning scheme for golden query text.
    query_version: str
    event: LeakageEventKind
    disposition: LeakageDisposition
    work_item_type: str
    title: str
    state: str
    assigned_to: str | None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class LeakageCandidateState:
    """Current, folded state of one candidate -- the result of replaying
    every event for its work_item_id in order."""

    work_item_id: int
    first_seen_at: datetime
    last_seen_at: datetime
    disposition: LeakageDisposition
    disposition_set_at: datetime | None
    work_item_type: str
    title: str
    state: str
    assigned_to: str | None


def leakage_query_version(ado_filter: str, ado_select: str) -> str:
    """A short, stable content hash of the query definition -- detects a
    text change (filter/select edit) independent of any manual version bump,
    since this repo's golden queries carry no version field of their own."""
    import hashlib

    return hashlib.sha256(f"{ado_filter}\n{ado_select}".encode("utf-8")).hexdigest()[:16]


def leakage_ledger_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_quality" / "leakage_candidates.jsonl"


def record_leakage_event(event: LeakageCandidateEvent, *, programs_root: Path = PROGRAMS_ROOT) -> None:
    line = json.dumps(_to_jsonable(event), sort_keys=True) + "\n"
    append_jsonl_line(leakage_ledger_path(event.program_id, programs_root=programs_root), line, max_bytes=_LEDGER_MAX_BYTES)


def _to_jsonable(event: LeakageCandidateEvent) -> dict[str, object]:
    return {
        "recorded_at": event.recorded_at.isoformat(),
        "program_id": event.program_id,
        "work_item_id": event.work_item_id,
        "org": event.org,
        "project": event.project,
        "discovery_run_id": event.discovery_run_id,
        "query_id": event.query_id,
        "query_version": event.query_version,
        "event": event.event,
        "disposition": event.disposition,
        "work_item_type": event.work_item_type,
        "title": event.title,
        "state": event.state,
        "assigned_to": event.assigned_to,
        "note": event.note,
    }


def read_leakage_events(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[LeakageCandidateEvent, ...]:
    path = leakage_ledger_path(program_id, programs_root=programs_root)
    events = []
    for raw in read_jsonl_records(path):
        events.append(
            LeakageCandidateEvent(
                recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                program_id=raw["program_id"],
                work_item_id=int(raw["work_item_id"]),
                org=raw["org"],
                project=raw["project"],
                discovery_run_id=raw["discovery_run_id"],
                query_id=raw["query_id"],
                query_version=raw["query_version"],
                event=raw["event"],
                disposition=raw["disposition"],
                work_item_type=raw["work_item_type"],
                title=raw["title"],
                state=raw["state"],
                assigned_to=raw.get("assigned_to"),
                note=raw.get("note"),
            )
        )
    return tuple(events)


def fold_leakage_candidates(events: tuple[LeakageCandidateEvent, ...]) -> dict[int, LeakageCandidateState]:
    """Replays events in recorded order into current per-candidate state.
    Canonical identity is ``work_item_id`` alone (BL-F1's own acceptance
    criteria: "ADO ID alone is insufficient if items move between projects/
    orgs" -- accepted as a known limitation for a single-org/single-project
    program like Armada; a cross-project candidate ledger is out of scope
    until a program with that shape exists)."""
    states: dict[int, LeakageCandidateState] = {}
    for event in sorted(events, key=lambda e: e.recorded_at):
        existing = states.get(event.work_item_id)
        first_seen_at = existing.first_seen_at if existing is not None else event.recorded_at
        disposition_set_at = (
            event.recorded_at if event.event in ("disposed", "auto_resolved", "reopened") else
            (existing.disposition_set_at if existing is not None else None)
        )
        states[event.work_item_id] = LeakageCandidateState(
            work_item_id=event.work_item_id,
            first_seen_at=first_seen_at,
            last_seen_at=event.recorded_at,
            disposition=event.disposition,
            disposition_set_at=disposition_set_at,
            work_item_type=event.work_item_type,
            title=event.title,
            state=event.state,
            assigned_to=event.assigned_to,
        )
    return states


@dataclass(frozen=True, slots=True)
class LeakageSyncResult:
    discovered: tuple[int, ...]
    reseen: tuple[int, ...]
    reopened: tuple[int, ...]
    auto_resolved: tuple[int, ...]


def sync_leakage_candidates(
    program_id: str,
    *,
    org: str,
    project: str,
    raw_candidates: tuple[RawAdoCandidate, ...],
    discovery_run_id: str,
    query_version: str,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> LeakageSyncResult:
    """Reconciles one freshly-fetched discovery against the ledger.

    Lifecycle transitions (BL-F1's own acceptance criteria):
    first-seen -> owner-assigned -> disposed -> reopened. A candidate that
    drops out of the query result (tagged, closed, or otherwise resolved
    itself) is auto-resolved so the metric converges instead of
    accumulating stale entries forever ("the false-positive handling"
    acceptance criterion's mirror image: a *true*-positive that got fixed
    must also durably leave the unresolved count).
    """
    resolved_at = now or datetime.now(timezone.utc)
    existing_events = read_leakage_events(program_id, programs_root=programs_root)
    current = fold_leakage_candidates(existing_events)
    seen_ids = {c.work_item_id for c in raw_candidates}

    discovered: list[int] = []
    reseen: list[int] = []
    reopened: list[int] = []
    auto_resolved: list[int] = []

    for candidate in raw_candidates:
        prior = current.get(candidate.work_item_id)
        if prior is None:
            kind: LeakageEventKind = "discovered"
            disposition: LeakageDisposition = "unresolved"
            discovered.append(candidate.work_item_id)
        elif prior.disposition not in _UNSETTLED_DISPOSITIONS:
            kind = "reopened"
            disposition = "unresolved"
            reopened.append(candidate.work_item_id)
        else:
            kind = "reseen"
            disposition = prior.disposition
            reseen.append(candidate.work_item_id)
        record_leakage_event(
            LeakageCandidateEvent(
                recorded_at=resolved_at,
                program_id=program_id,
                work_item_id=candidate.work_item_id,
                org=org,
                project=project,
                discovery_run_id=discovery_run_id,
                query_id=LEAKAGE_QUERY_ID,
                query_version=query_version,
                event=kind,
                disposition=disposition,
                work_item_type=candidate.work_item_type,
                title=candidate.title,
                state=candidate.state,
                assigned_to=candidate.assigned_to,
            ),
            programs_root=programs_root,
        )

    for work_item_id, prior in current.items():
        if prior.disposition in _UNSETTLED_DISPOSITIONS and work_item_id not in seen_ids:
            auto_resolved.append(work_item_id)
            record_leakage_event(
                LeakageCandidateEvent(
                    recorded_at=resolved_at,
                    program_id=program_id,
                    work_item_id=work_item_id,
                    org=org,
                    project=project,
                    discovery_run_id=discovery_run_id,
                    query_id=LEAKAGE_QUERY_ID,
                    query_version=query_version,
                    event="auto_resolved",
                    disposition="resolved",
                    work_item_type=prior.work_item_type,
                    title=prior.title,
                    state=prior.state,
                    assigned_to=prior.assigned_to,
                    note="no longer matched the leakage query on this discovery run",
                ),
                programs_root=programs_root,
            )

    return LeakageSyncResult(
        discovered=tuple(discovered), reseen=tuple(reseen), reopened=tuple(reopened), auto_resolved=tuple(auto_resolved),
    )


def dispose_leakage_candidate(
    program_id: str,
    work_item_id: int,
    *,
    org: str,
    project: str,
    disposition: Literal["owner_assigned", "correctly_untagged", "resolved"],
    note: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> None:
    """Records an explicit human disposition (BL-F1's "false-positive
    handling" acceptance criterion: a disposition that marks a candidate
    *correctly untagged* must durably suppress it from future leakage
    counts). Requires the candidate to already exist in the ledger."""
    existing = fold_leakage_candidates(read_leakage_events(program_id, programs_root=programs_root))
    prior = existing.get(work_item_id)
    if prior is None:
        raise ValueError(f"work_item_id {work_item_id} is not a known leakage candidate for {program_id!r}.")
    record_leakage_event(
        LeakageCandidateEvent(
            recorded_at=now or datetime.now(timezone.utc),
            program_id=program_id,
            work_item_id=work_item_id,
            org=org,
            project=project,
            discovery_run_id="manual-disposition",
            query_id=LEAKAGE_QUERY_ID,
            query_version="n/a",
            event="disposed",
            disposition=disposition,
            work_item_type=prior.work_item_type,
            title=prior.title,
            state=prior.state,
            assigned_to=prior.assigned_to,
            note=note,
        ),
        programs_root=programs_root,
    )


@dataclass(frozen=True, slots=True)
class LeakageRateResult:
    value: float | None
    confidence: ValueConfidence
    likely_missing_tag_items: int
    authoritative_scope_items: int | None
    detail: str


def compute_leakage_rate(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> LeakageRateResult:
    """D-9's formula: ``likely_missing_tag_items / max(authoritative_scope_
    items + likely_missing_tag_items, 1)``. ``authoritative_scope_items`` is
    the last committed gather run's total discovered-item count (the sum of
    ``QueryResultEntry.raw_count`` across scopes) -- the normal,
    correctly-tagged delivery scope this metric compares the untagged
    catchall against."""
    states = fold_leakage_candidates(read_leakage_events(program_id, programs_root=programs_root))
    likely_missing_tag_items = sum(1 for s in states.values() if s.disposition in _UNSETTLED_DISPOSITIONS)

    manifest = resolve_latest_committed_manifest(program_id, programs_root=programs_root)
    if manifest is None:
        return LeakageRateResult(
            value=None,
            confidence=ValueConfidence.UNAVAILABLE,
            likely_missing_tag_items=likely_missing_tag_items,
            authoritative_scope_items=None,
            detail="No committed gather run yet; authoritative_scope_items is unknown.",
        )
    authoritative_scope_items = sum(result.raw_count for result in manifest.query_results)
    denominator = max(authoritative_scope_items + likely_missing_tag_items, 1)
    rate = likely_missing_tag_items / denominator
    return LeakageRateResult(
        value=round(rate, 4),
        confidence=ValueConfidence.MEASURED,
        likely_missing_tag_items=likely_missing_tag_items,
        authoritative_scope_items=authoritative_scope_items,
        detail=(
            f"{likely_missing_tag_items} unresolved candidate(s) vs. {authoritative_scope_items} "
            f"authoritative scope item(s) from run {manifest.run_id}."
        ),
    )


@dataclass(frozen=True, slots=True)
class LeakageSlaViolation:
    work_item_id: int
    kind: Literal["owner_disposition_overdue", "no_unresolved_candidate"]
    age_days: float


def leakage_sla_violations(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT, now: datetime | None = None,
) -> tuple[LeakageSlaViolation, ...]:
    """D-9's two SLA clocks: a 7-day owner/false-positive-disposition
    deadline per candidate, and a 14-day no-unresolved-candidate rule (no
    unresolved candidate may sit longer than 14 days). Clocks run from
    ``first_seen_at`` and do not pause on reassignment (no reassignment
    concept exists in this ledger yet -- a future enhancement, not
    speculatively built)."""
    checked_at = now or datetime.now(timezone.utc)
    states = fold_leakage_candidates(read_leakage_events(program_id, programs_root=programs_root))
    violations: list[LeakageSlaViolation] = []
    for state in states.values():
        if state.disposition not in _UNSETTLED_DISPOSITIONS:
            continue
        age_days = (checked_at - state.first_seen_at).total_seconds() / 86400
        if state.disposition == "unresolved" and age_days > OWNER_DISPOSITION_SLA_DAYS:
            violations.append(LeakageSlaViolation(state.work_item_id, "owner_disposition_overdue", round(age_days, 1)))
        if age_days > NO_UNRESOLVED_SLA_DAYS:
            violations.append(LeakageSlaViolation(state.work_item_id, "no_unresolved_candidate", round(age_days, 1)))
    return tuple(sorted(violations, key=lambda v: (-v.age_days, v.work_item_id)))


def find_leakage_query(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> KustoQuery | None:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    for query in knowledge.golden_queries:
        if query.engine == "ado_odata" and query.id == LEAKAGE_QUERY_ID:
            return query
    return None


def fetch_leakage_candidates_from_ado(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    client_factory: Any = None,
) -> tuple[RawAdoCandidate, ...]:
    """Executes the ``armada-xhealth-catchall-ado`` golden query for real
    against ADO Analytics OData, via the already-built ``ADOClient.
    query_work_items``. ``client_factory`` is injectable for tests (and for
    any future non-default auth path); defaults to constructing a real
    ``ADOClient`` from the program's own ``ado.organization``/``ado.
    project``.
    """
    program = load_program(program_id, programs_root=programs_root)
    if program is None or program.ado is None:
        raise ValueError(f"Program {program_id!r} has no ado config; cannot run the leakage query.")
    query = find_leakage_query(program_id, programs_root=programs_root)
    if query is None or not query.ado_filter or not query.ado_select:
        raise ValueError(
            f"No {LEAKAGE_QUERY_ID!r} ado_odata golden query with both ado_filter and ado_select "
            f"is configured for {program_id!r}."
        )

    if client_factory is None:
        from src.core.ado_client import ADOClient

        client = ADOClient(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=program.ado.api_timeout_seconds or 30,
        )
    else:
        client = client_factory(organization=program.ado.organization, project=program.ado.project)

    select_fields = tuple(field.strip() for field in query.ado_select.split(",") if field.strip())
    rows = client.query_work_items(filter_expression=query.ado_filter, select_fields=select_fields)
    return tuple(
        RawAdoCandidate(
            work_item_id=int(row["WorkItemId"]),
            work_item_type=str(row.get("WorkItemType", "")),
            title=str(row.get("Title", "")),
            state=str(row.get("State", "")),
            assigned_to=_extract_assigned_to(row.get("AssignedTo")),
        )
        for row in rows
        if "WorkItemId" in row
    )


def _extract_assigned_to(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, dict):
        # ADO OData sometimes returns AssignedTo as an identity object.
        value = raw.get("displayName") or raw.get("uniqueName")
        return str(value) if value else None
    return None


__all__ = [
    "LEAKAGE_QUERY_ID",
    "NO_UNRESOLVED_SLA_DAYS",
    "OWNER_DISPOSITION_SLA_DAYS",
    "LeakageCandidateEvent",
    "LeakageCandidateState",
    "LeakageDisposition",
    "LeakageRateResult",
    "LeakageSlaViolation",
    "LeakageSyncResult",
    "RawAdoCandidate",
    "compute_leakage_rate",
    "dispose_leakage_candidate",
    "fetch_leakage_candidates_from_ado",
    "find_leakage_query",
    "fold_leakage_candidates",
    "leakage_ledger_path",
    "leakage_query_version",
    "leakage_sla_violations",
    "read_leakage_events",
    "record_leakage_event",
    "sync_leakage_candidates",
]
