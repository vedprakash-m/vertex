"""WI-2.7: Commitment store — commitment.entry fact type with direction + slip_history.

commitment.entry is a management fact (HUMAN_CONFIRMED truth level).
Directions: inbound (others owe us) | outbound (we owe others).

Zone A module (INV-1 applies).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import auto
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.core.config_loader import PROGRAMS_ROOT

_LOG = logging.getLogger(__name__)


class CommitmentDirection(str):
    """Direction of a commitment — inbound vs outbound.

    Closed by design (like AttentionKind): inbound and outbound have
    different projection semantics and trigger different actuation paths.
    """
    INBOUND = "inbound"    # external party owes us
    OUTBOUND = "outbound"  # we owe an external party


@dataclass(frozen=True, slots=True)
class SlipRecord:
    """One entry in the commitment slip history."""
    slipped_at: datetime
    old_due_date: str
    new_due_date: str
    reason: str


@dataclass(frozen=True, slots=True)
class CommitmentEntry:
    """A tracked commitment with direction and slip history.

    Management fact — truth_level = HUMAN_CONFIRMED in Phase 1.
    """
    commitment_id: str
    title: str
    dri: str
    due_date: str
    direction: str  # CommitmentDirection.INBOUND | OUTBOUND
    status: str
    description: str
    entity_ref: str | None
    slip_history: tuple[SlipRecord, ...]
    program_id: str

    @property
    def id(self) -> str:
        """Standard record ID used by _find_fact_for_record (S-2a)."""
        return self.commitment_id

    @property
    def is_slipped(self) -> bool:
        return len(self.slip_history) > 0

    @property
    def slip_count(self) -> int:
        return len(self.slip_history)


def build_commitment_id() -> str:
    """Generate a new unique commitment ID."""
    return f"cm-{uuid4().hex[:8]}"


def build_commitment_natural_key(commitment_id: str) -> str:
    """Build the natural key for a commitment.entry fact."""
    return f"commitment.entry|{commitment_id}"


def project_commitment_entries(snapshot: Any) -> tuple[CommitmentEntry, ...]:
    """Project commitment.entry facts from a ProgramFactSnapshot.

    S-2a / S-5d: only ACCEPTED facts are surfaced — same contract as all other project_* functions.
    """
    from src.core.program_fact_store import FactReviewState
    result: list[CommitmentEntry] = []
    for fact in snapshot.facts:
        if fact.fact_type != "commitment.entry":
            continue
        if getattr(fact, "review_state", None) != FactReviewState.ACCEPTED:
            continue
        p = fact.payload
        slip_records = tuple(
            SlipRecord(
                slipped_at=datetime.fromisoformat(s["slipped_at"]) if isinstance(s.get("slipped_at"), str) else s.get("slipped_at", datetime.now(timezone.utc)),
                old_due_date=str(s.get("old_due_date", "")),
                new_due_date=str(s.get("new_due_date", "")),
                reason=str(s.get("reason", "")),
            )
            for s in (p.get("slip_history") or [])
        )
        result.append(CommitmentEntry(
            commitment_id=str(p.get("commitment_id", "")),
            title=str(p.get("title", "")),
            dri=str(p.get("dri", "")),
            due_date=str(p.get("due_date", "")),
            direction=str(p.get("direction", CommitmentDirection.OUTBOUND)),
            status=str(p.get("status", "active")),
            description=str(p.get("description", "")),
            entity_ref=p.get("entity_ref") or None,
            slip_history=slip_records,
            program_id=fact.program_id,
        ))
    return tuple(result)


def append_commitment_slip(
    program_id: str,
    commitment_id: str,
    *,
    new_due_date: str,
    old_due_date: str,
    reason: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append a slip record to an existing commitment.entry fact.

    This adds a new entry to the slip_history in the fact payload.
    The fact is updated in-place via the fact store's revision mechanism.
    """
    from src.core.program_fact_store import (
        ProgramFactStore,
        ProgramFactInput,
        load_program_facts,
    )

    snapshot = load_program_facts(program_id, programs_root=programs_root, fact_types=("commitment.entry",))
    existing = None
    for fact in snapshot.facts:
        if fact.fact_type == "commitment.entry" and fact.payload.get("commitment_id") == commitment_id:
            existing = fact
            break

    if existing is None:
        raise ValueError(f"Commitment {commitment_id!r} not found in program {program_id!r}")

    slip_history = list(existing.payload.get("slip_history") or [])
    slip_history.append({
        "slipped_at": datetime.now(timezone.utc).isoformat(),
        "old_due_date": old_due_date,
        "new_due_date": new_due_date,
        "reason": reason,
    })
    updated_payload = dict(existing.payload)
    updated_payload["due_date"] = new_due_date
    updated_payload["slip_history"] = slip_history

    store = ProgramFactStore(program_id, home_root=programs_root)
    fact_input = ProgramFactInput(
        fact_type="commitment.entry",
        entity_refs=tuple(existing.entity_refs),
        payload=updated_payload,
        scope=existing.scope,
        source_signal_ids=tuple(existing.source_signal_ids),
        natural_key=existing.natural_key,
        created_by="commitment_store:slip",
    )
    store.append_fact(fact_input)


def save_commitment(
    program_id: str,
    entry: CommitmentEntry,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Write a commitment.entry to the fact store."""
    from src.core.program_fact_store import ProgramFactStore, ProgramFactInput

    payload: dict[str, Any] = {
        "commitment_id": entry.commitment_id,
        "title": entry.title,
        "dri": entry.dri,
        "due_date": entry.due_date,
        "direction": entry.direction,
        "status": entry.status,
        "description": entry.description,
        "slip_history": [
            {
                "slipped_at": s.slipped_at.isoformat(),
                "old_due_date": s.old_due_date,
                "new_due_date": s.new_due_date,
                "reason": s.reason,
            }
            for s in entry.slip_history
        ],
    }
    if entry.entity_ref:
        payload["entity_ref"] = entry.entity_ref

    entity_refs = (entry.entity_ref,) if entry.entity_ref else (entry.commitment_id,)
    natural_key = build_commitment_natural_key(entry.commitment_id)

    store = ProgramFactStore(program_id, home_root=programs_root)
    fact_input = ProgramFactInput(
        fact_type="commitment.entry",
        entity_refs=entity_refs,
        payload=payload,
        scope="program",
        source_signal_ids=(),
        natural_key=natural_key,
        created_by="commitment_store",
    )
    store.append_fact(fact_input)


def _commitment_entry_from_record(record: Any) -> CommitmentEntry:
    """Map a commitment domain record (or FactAssessment.record) → CommitmentEntry.

    S-8c: shared by the legacy snapshot projection (``project_commitment_entries``)
    and the ProgramReality read-path overlay (``_load_commitment_entries_via_reality``)
    so both paths produce structurally identical entries. ``record`` exposes the
    same payload fields populated by the fact bridge (``commitment_id``, ``title``,
    ``dri``, ``due_date``, ``direction``, ``status``, ``description``,
    ``entity_ref``, ``slip_history``, ``program_id``).
    """
    p = record
    slip_records = tuple(
        SlipRecord(
            slipped_at=datetime.fromisoformat(s["slipped_at"]) if isinstance(s.get("slipped_at"), str) else s.get("slipped_at", datetime.now(timezone.utc)),
            old_due_date=str(s.get("old_due_date", "")),
            new_due_date=str(s.get("new_due_date", "")),
            reason=str(s.get("reason", "")),
        )
        for s in (p.get("slip_history") if isinstance(p, dict) else getattr(p, "slip_history", None)) or []
    )
    if isinstance(p, dict):
        get = p.get
    else:
        get = lambda key, default=None: getattr(p, key, default)  # noqa: E731
    return CommitmentEntry(
        commitment_id=str(get("commitment_id", "")),
        title=str(get("title", "")),
        dri=str(get("dri", "")),
        due_date=str(get("due_date", "")),
        direction=str(get("direction", CommitmentDirection.OUTBOUND)),
        status=str(get("status", "active")),
        description=str(get("description", "")),
        entity_ref=get("entity_ref") or None,
        slip_history=slip_records,
        program_id=str(get("program_id", "")),
    )


def _load_commitment_entries_via_reality(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[CommitmentEntry, ...]:
    """S-8c: project commitments from ``ProgramReality.commitments()``.

    Active only when the ``commitment`` family SoR mode is non-legacy (resolved
    by the caller). Mirrors ``MilestoneStage._load_milestones_via_reality``:
    reads the FactAssessments exposed by the read facade and maps their
    underlying records to ``CommitmentEntry`` via ``_commitment_entry_from_record``.
    Raises on failure so the caller can apply the graceful legacy fallback.
    """
    from src.core.program_reality import ProgramReality  # noqa: PLC0415

    reality = ProgramReality.load(program_id, programs_root=programs_root)
    return tuple(_commitment_entry_from_record(fa.record) for fa in reality.commitments())


def _load_commitment_entries_legacy(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[CommitmentEntry, ...]:
    """Legacy Plane 1 shim read path (the pre-S-8c behaviour)."""
    from src.core.program_fact_store import load_program_facts  # noqa: PLC0415

    snapshot = load_program_facts(
        program_id,
        programs_root=programs_root,
        fact_types=("commitment.entry",),
    )
    return project_commitment_entries(snapshot)


def load_commitment_entries(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    direction: str | None = None,
    status: str | None = None,
) -> tuple[CommitmentEntry, ...]:
    """Load commitment entries for a program, optionally filtered.

    S-8c: when the ``commitment`` family SoR mode is non-legacy (shadow/primary),
    entries are projected from ``ProgramReality.commitments()`` instead of the
    legacy Plane 1 shim — extending the S-8a read-path slice to the
    ``commitment.date_set`` v1-authoritative family. A ProgramReality failure
    degrades gracefully to the legacy path with a WARNING (never silent, never
    breaks the read path). In ``legacy`` mode the overlay is never consulted.
    """
    from src.core.fact_sor_state import resolve_family_sor_mode  # noqa: PLC0415

    entries: tuple[CommitmentEntry, ...]
    commitment_mode = resolve_family_sor_mode(program_id, "commitment", programs_root=programs_root)
    if commitment_mode == "legacy":
        entries = _load_commitment_entries_legacy(program_id, programs_root=programs_root)
    else:
        try:
            entries = _load_commitment_entries_via_reality(program_id, programs_root=programs_root)
        except Exception as exc:  # noqa: BLE001
            # Graceful fallback — S-8c must not break the commitment read path.
            entries = _load_commitment_entries_legacy(program_id, programs_root=programs_root)
            _LOG.warning("[S-8c] commitment ProgramReality fallback for %s: %s", program_id, exc)

    if direction is not None:
        entries = tuple(e for e in entries if e.direction == direction)
    if status is not None:
        entries = tuple(e for e in entries if e.status == status)
    return entries
