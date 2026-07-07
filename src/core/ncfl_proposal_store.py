"""NCFL proposal store.

Phase 1/3 implementation of §23.4 and §24.2–24.4.

Storage:
  programs/<program_id>/context_proposals/issue_NNN.proposals.json
  programs/<program_id>/context_proposals/_conflict_index.json

Zone A only. Atomic JSON-array writes via os.replace.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ncfl_models import ContextUpdateProposal, DecisionRecord


CONFLICT_INDEX_FILENAME = "_conflict_index.json"


def get_context_proposals_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "context_proposals"


def get_proposals_path(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return get_context_proposals_dir(program_id, programs_root=programs_root) / f"issue_{issue_number:03d}.proposals.json"


def load_proposals(
    program_id: str,
    *,
    issue_number: int | None = None,
    status_filter: set[str] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ContextUpdateProposal, ...]:
    paths = (
        (get_proposals_path(program_id, issue_number, programs_root=programs_root),)
        if issue_number is not None
        else tuple(sorted(get_context_proposals_dir(program_id, programs_root=programs_root).glob("issue_*.proposals.json")))
    )
    proposals: list[ContextUpdateProposal] = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            proposal = ContextUpdateProposal.from_json(raw)
            if status_filter is not None and proposal.status not in status_filter:
                continue
            proposals.append(proposal)
    proposals.sort(key=lambda p: (p.issue_number, p.conflict_key, p.proposal_id))
    return tuple(proposals)


def save_proposals(
    program_id: str,
    issue_number: int,
    proposals: tuple[ContextUpdateProposal, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_proposals_path(program_id, issue_number, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, [proposal.to_json() for proposal in proposals])
    rebuild_conflict_index(program_id, programs_root=programs_root)
    return path


def stage_extracted_proposals(
    program_id: str,
    issue_number: int,
    proposals: tuple[ContextUpdateProposal, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ContextUpdateProposal, ...]:
    existing = list(load_proposals(program_id, issue_number=issue_number, programs_root=programs_root))
    existing_by_id = {proposal.proposal_id: proposal for proposal in existing}
    existing_by_conflict = {proposal.conflict_key: proposal for proposal in existing if proposal.status == "pending"}
    staged = list(existing)

    for proposal in proposals:
        current = existing_by_id.get(proposal.proposal_id)
        if current is not None:
            continue
        conflicting = existing_by_conflict.get(proposal.conflict_key)
        if conflicting is not None and conflicting.proposal_id != proposal.proposal_id:
            superseded = replace(
                conflicting,
                status="superseded",
                superseded_by=proposal.proposal_id,
                decision_history=conflicting.decision_history + (
                    DecisionRecord(
                        timestamp=datetime.now(timezone.utc),
                        actor="system",
                        from_status=conflicting.status,
                        to_status="superseded",
                        note=f"Superseded by {proposal.proposal_id}",
                    ),
                ),
            )
            staged = [superseded if entry.proposal_id == conflicting.proposal_id else entry for entry in staged]
        staged.append(proposal)
        existing_by_id[proposal.proposal_id] = proposal
        existing_by_conflict[proposal.conflict_key] = proposal

    staged_tuple = tuple(sorted(staged, key=lambda p: (p.conflict_key, p.proposal_id, p.status)))
    save_proposals(program_id, issue_number, staged_tuple, programs_root=programs_root)
    return staged_tuple


def update_proposal_status(
    program_id: str,
    *,
    proposal_id: str,
    new_status: str,
    actor: str,
    issue_number: int | None = None,
    rationale: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ContextUpdateProposal:
    proposals = list(load_proposals(program_id, issue_number=issue_number, programs_root=programs_root))
    for proposal in proposals:
        if proposal.proposal_id != proposal_id:
            continue
        updated = replace(
            proposal,
            status=new_status,
            rationale=rationale if new_status == "dismissed" else proposal.rationale,
            dismissed_at=(datetime.now(timezone.utc) if new_status == "dismissed" else proposal.dismissed_at),
            dismissed_by=(actor if new_status == "dismissed" else proposal.dismissed_by),
            decision_history=proposal.decision_history + (
                DecisionRecord(
                    timestamp=datetime.now(timezone.utc),
                    actor=actor,
                    from_status=proposal.status,
                    to_status=new_status,
                    note=rationale,
                ),
            ),
        )
        per_issue = tuple(
            updated if candidate.proposal_id == proposal_id else candidate
            for candidate in proposals
            if candidate.issue_number == proposal.issue_number
        )
        save_proposals(program_id, proposal.issue_number, per_issue, programs_root=programs_root)
        return updated
    raise ValueError(f"Unknown proposal_id {proposal_id!r} for program {program_id!r}.")


def rebuild_conflict_index(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for proposal in load_proposals(program_id, programs_root=programs_root):
        grouped.setdefault(proposal.conflict_key, []).append(
            {
                "proposal_id": proposal.proposal_id,
                "issue_number": proposal.issue_number,
                "status": proposal.status,
                "target_store": proposal.target_store,
                "target_key": proposal.target_key,
                "target_field": proposal.target_field,
            }
        )
    payload = {
        "schema_version": "1.0",
        "program_id": program_id,
        "conflicts": grouped,
    }
    path = get_context_proposals_dir(program_id, programs_root=programs_root) / CONFLICT_INDEX_FILENAME
    _atomic_write_json(path, payload)
    return path


def load_conflict_index(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> dict[str, list[dict[str, Any]]]:
    path = get_context_proposals_dir(program_id, programs_root=programs_root) / CONFLICT_INDEX_FILENAME
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("conflicts") if isinstance(payload, dict) else {}
    return raw if isinstance(raw, dict) else {}


def conflicting_pending_proposals(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, tuple[dict[str, Any], ...]]:
    conflicts = load_conflict_index(program_id, programs_root=programs_root)
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for conflict_key, entries in conflicts.items():
        if not isinstance(entries, list):
            continue
        pending = tuple(entry for entry in entries if isinstance(entry, dict) and entry.get("status") == "pending")
        if len(pending) > 1:
            result[conflict_key] = pending
    return result


def stale_pending_proposals(
    program_id: str,
    *,
    max_issue_lag: int = 2,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ContextUpdateProposal, ...]:
    pending = load_proposals(
        program_id,
        status_filter={"pending"},
        programs_root=programs_root,
    )
    if not pending:
        return ()
    newest_issue = max(proposal.issue_number for proposal in pending)
    return tuple(
        proposal
        for proposal in pending
        if newest_issue - proposal.issue_number > max_issue_lag
    )


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
