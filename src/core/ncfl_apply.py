"""NCFL Plane 1 apply engine — S-NC-apply recoverable state machine.

This module implements the recoverable NCFL apply operation after ADR-0006
acceptance.  It replaces the blocked stub in ``src/commands/context.py``.

Apply strategy: ``reuse_beta_outbox_plus_minimal_apply_journal``
  - Beta outbox (S-1 / ``projection_outbox``) handles ledger idempotency.
  - A minimal per-proposal apply journal (``programs/<prog>/_ncfl_apply/``)
    covers YAML/changelog recovery if the process crashes mid-write.

State machine (§23.1.5 / ADR-0006 S-NC-apply):
  proposed → write_started → yaml_written → changelog_written
          → ledger_written → applied | needs_repair

Zone A only.  No AI imports, no external clients.

Invariants:
  - Only apply-writable target stores (from ncfl_store_policy) may be written.
  - Current-value hash check (optimistic concurrency) guards against stale apply.
  - A needs_repair journal entry is written before any exception propagates.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ncfl_apply_policy import (
    NCFL_APPLY_TRANSITIONS,
    NcflApplyState,
    is_valid_apply_transition,
)
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import update_proposal_status
from src.core.ncfl_store_policy import is_ncfl_apply_writable_target_store

log = logging.getLogger(__name__)

_APPLY_JOURNAL_DIR = "_ncfl_apply"


# ---------------------------------------------------------------------------
# Apply journal helpers
# ---------------------------------------------------------------------------

def _journal_path(program_id: str, proposal_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / _APPLY_JOURNAL_DIR / f"{proposal_id}.json"


def _write_journal(
    path: Path,
    proposal_id: str,
    state: NcflApplyState,
    *,
    note: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "proposal_id": proposal_id,
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_journal_state(
    program_id: str, proposal_id: str, *, programs_root: Path
) -> NcflApplyState | None:
    path = _journal_path(program_id, proposal_id, programs_root=programs_root)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc.get("state")
    except Exception:  # noqa: BLE001
        return None


def _clear_journal(program_id: str, proposal_id: str, *, programs_root: Path) -> None:
    path = _journal_path(program_id, proposal_id, programs_root=programs_root)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Apply result
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NcflApplyResult:
    proposal_id: str
    target_store: str
    target_key: str
    target_field: str
    action: str   # "applied" | "skipped_not_writable" | "skipped_stale" | "skipped_already_applied" | "needs_repair"
    note: str


# ---------------------------------------------------------------------------
# Core apply function
# ---------------------------------------------------------------------------

def apply_proposal(
    proposal: ContextUpdateProposal,
    *,
    actor: str,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> NcflApplyResult:
    """Apply one accepted NCFL proposal to its target Plane 1 store.

    Returns an ``NcflApplyResult`` describing what happened.  Writes a
    journal entry before each step so recovery can resume from the last
    completed state on retry.

    Raises nothing — failures are recorded in the journal + result.
    """
    pid = proposal.proposal_id
    target_store = proposal.target_store
    target_key = proposal.target_key
    target_field = proposal.target_field

    # Guard 1: only apply-writable stores
    if not is_ncfl_apply_writable_target_store(target_store):
        return NcflApplyResult(
            proposal_id=pid,
            target_store=target_store,
            target_key=target_key,
            target_field=target_field,
            action="skipped_not_writable",
            note=f"target_store {target_store!r} is not in the NCFL apply-writable set",
        )

    # Guard 2: only accepted proposals
    if proposal.status != "accepted":
        return NcflApplyResult(
            proposal_id=pid,
            target_store=target_store,
            target_key=target_key,
            target_field=target_field,
            action="skipped_not_writable",
            note=f"proposal status is {proposal.status!r}, not 'accepted'",
        )

    # Guard 3: check idempotency via journal
    prior_state = _read_journal_state(proposal.program_id, pid, programs_root=programs_root)
    if prior_state == "applied":
        return NcflApplyResult(
            proposal_id=pid,
            target_store=target_store,
            target_key=target_key,
            target_field=target_field,
            action="skipped_already_applied",
            note="apply journal shows already applied",
        )

    journal = _journal_path(proposal.program_id, pid, programs_root=programs_root)

    try:
        if not dry_run:
            _write_journal(journal, pid, "write_started", note="apply started")

        # Guard 4: optimistic concurrency — verify current_value_hash matches live YAML
        if proposal.current_value_hash is not None:
            current_raw = _read_current_value(proposal, programs_root=programs_root)
            if current_raw is not None:
                live_hash = hashlib.sha256(str(current_raw).encode()).hexdigest()
                if live_hash != proposal.current_value_hash:
                    return NcflApplyResult(
                        proposal_id=pid,
                        target_store=target_store,
                        target_key=target_key,
                        target_field=target_field,
                        action="skipped_stale",
                        note=f"current_value_hash mismatch: live={live_hash[:8]} expected={proposal.current_value_hash[:8]}",
                    )

        if dry_run:
            return NcflApplyResult(
                proposal_id=pid,
                target_store=target_store,
                target_key=target_key,
                target_field=target_field,
                action="applied",
                note="dry_run: would apply",
            )

        # Step 1: write YAML (store-specific)
        _write_to_store(proposal, programs_root=programs_root)
        _write_journal(journal, pid, "yaml_written", note="YAML updated")

        # Step 2: write changelog entry
        _write_changelog_entry(proposal, actor=actor, programs_root=programs_root)
        _write_journal(journal, pid, "changelog_written", note="changelog updated")

        # Step 3: update proposal status → applied
        update_proposal_status(
            proposal.program_id,
            proposal_id=pid,
            new_status="accepted",
            actor=actor,
            issue_number=proposal.issue_number,
            rationale="applied by ncfl_apply",
            programs_root=programs_root,
        )
        _write_journal(journal, pid, "ledger_written", note="proposal status updated")

        # Final: mark applied and clear journal
        _write_journal(journal, pid, "applied", note="complete")
        _clear_journal(proposal.program_id, pid, programs_root=programs_root)

        log.info(
            "NCFL apply: program=%s proposal=%s store=%s key=%s field=%s",
            proposal.program_id, pid, target_store, target_key, target_field,
        )
        return NcflApplyResult(
            proposal_id=pid,
            target_store=target_store,
            target_key=target_key,
            target_field=target_field,
            action="applied",
            note=f"applied {target_store}.{target_key}.{target_field}={proposal.source_value!r}",
        )

    except Exception as exc:  # noqa: BLE001
        _write_journal(journal, pid, "needs_repair", note=f"exception: {exc}")
        log.error("NCFL apply failed for %s: %s", pid, exc)
        return NcflApplyResult(
            proposal_id=pid,
            target_store=target_store,
            target_key=target_key,
            target_field=target_field,
            action="needs_repair",
            note=f"exception during apply: {exc}",
        )


def apply_proposals_batch(
    proposals: tuple[ContextUpdateProposal, ...],
    *,
    actor: str,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> tuple[NcflApplyResult, ...]:
    """Apply multiple accepted proposals. Returns one result per proposal."""
    return tuple(
        apply_proposal(p, actor=actor, programs_root=programs_root, dry_run=dry_run)
        for p in proposals
    )


# ---------------------------------------------------------------------------
# Store-specific write dispatchers
# ---------------------------------------------------------------------------

# Map each target_store to the list key in its YAML document.
_LIST_KEY_BY_STORE: dict[str, str] = {
    "assumptions": "assumptions",
    "decisions": "decisions",
    "milestones": "milestones",
    "risk_register": "risks",
}


def _read_current_value(
    proposal: ContextUpdateProposal, *, programs_root: Path
) -> str | None:
    """Read the current field value from the live YAML for optimistic concurrency.

    Each store keeps a list of records (with an ``id`` field), not a flat dict.
    For ``dimension_risk_level`` on risk_register, match by ``dimension_id``.
    """
    import yaml as _yaml
    from src.core.ncfl_store_policy import target_policy_by_store

    target_pol = target_policy_by_store().get(proposal.target_store)
    if target_pol is None or target_pol.root_yaml is None:
        return None
    yaml_path = programs_root / proposal.program_id / target_pol.root_yaml
    if not yaml_path.exists():
        return None
    try:
        doc = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            return None

        store_name = proposal.target_store

        if store_name in _LIST_KEY_BY_STORE:
            list_key = _LIST_KEY_BY_STORE[store_name]
            records: list[Any] = doc.get(list_key) or []
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                if (
                    store_name == "risk_register"
                    and proposal.target_field == "dimension_risk_level"
                ):
                    if (
                        rec.get("dimension_id") == proposal.target_key
                        or rec.get("title", "").strip() == proposal.target_key
                    ):
                        return str(rec.get("impact", ""))
                elif rec.get("id") == proposal.target_key:
                    return str(rec.get(proposal.target_field, ""))
            return None

        if store_name == "workstreams":
            for ws in doc.get("workstreams") or []:
                if isinstance(ws, dict) and ws.get("id") == proposal.target_key:
                    return str(ws.get(proposal.target_field, ""))
            return None

        if store_name == "knowledge_doc":
            # target_key is the knowledge-doc filename; target_field is "body".
            doc_path = programs_root / proposal.program_id / "knowledge" / proposal.target_key
            if not doc_path.exists():
                return None
            try:
                return doc_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                return None

        return None
    except Exception:  # noqa: BLE001
        return None


def _write_to_store(
    proposal: ContextUpdateProposal, *, programs_root: Path
) -> None:
    """Dispatch to the canonical save_* function for the target store.

    Each store has a typed load/save pair.  The apply engine:
      1. Loads existing records via load_*.
      2. Finds the target record by id (or dimension_id for risk dimension_risk_level).
      3. Applies the field update via dataclasses.replace() with type coercion.
      4. Persists via the canonical save_* (which also syncs fact stores where relevant).
    """
    dispatch: dict[str, Any] = {
        "assumptions": _apply_to_assumptions,
        "decisions": _apply_to_decisions,
        "milestones": _apply_to_milestones,
        "risk_register": _apply_to_risk_register,
        "workstreams": _apply_to_workstreams,
        "knowledge_doc": _apply_to_knowledge_doc,
    }
    fn = dispatch.get(proposal.target_store)
    if fn is None:
        raise ValueError(f"No write dispatcher for target_store={proposal.target_store!r}")
    fn(
        proposal.program_id,
        proposal.target_key,
        proposal.target_field,
        proposal.source_value,
        programs_root,
    )


def _apply_to_assumptions(
    prog_id: str, target_key: str, target_field: str, source_value: str, programs_root: Path
) -> None:
    from src.core.assumption_tracker import load_assumptions, save_assumptions
    from src.core.models_v2 import AssumptionStatus

    entries = load_assumptions(prog_id, programs_root)
    idx = next((i for i, e in enumerate(entries) if e.id == target_key), None)
    if idx is None:
        raise ValueError(f"Assumption {target_key!r} not found in program {prog_id!r}")
    entry = entries[idx]
    if target_field == "text":
        updated = dc_replace(entry, text=source_value)
    elif target_field == "owner_alias":
        updated = dc_replace(entry, owner_alias=source_value)
    elif target_field == "status":
        updated = dc_replace(entry, status=AssumptionStatus.from_string(source_value))
    else:
        raise ValueError(f"Unknown assumption target_field: {target_field!r}")
    new_entries = entries[:idx] + (updated,) + entries[idx + 1 :]
    save_assumptions(prog_id, new_entries, programs_root)


def _apply_to_decisions(
    prog_id: str, target_key: str, target_field: str, source_value: str, programs_root: Path
) -> None:
    from src.core.decision_register import load_decisions, save_decisions
    from src.core.models_v2 import DecisionStatus

    entries = load_decisions(prog_id, programs_root)
    idx = next((i for i, e in enumerate(entries) if e.id == target_key), None)
    if idx is None:
        raise ValueError(f"Decision {target_key!r} not found in program {prog_id!r}")
    entry = entries[idx]
    if target_field == "decision":
        updated = dc_replace(entry, decision=source_value)
    elif target_field == "title":
        updated = dc_replace(entry, title=source_value)
    elif target_field == "status":
        updated = dc_replace(entry, status=DecisionStatus.from_string(source_value))
    else:
        raise ValueError(f"Unknown decision target_field: {target_field!r}")
    new_entries = entries[:idx] + (updated,) + entries[idx + 1 :]
    save_decisions(prog_id, new_entries, programs_root)


def _apply_to_milestones(
    prog_id: str, target_key: str, target_field: str, source_value: str, programs_root: Path
) -> None:
    from datetime import date
    from src.core.milestone_engine import load_milestones, save_milestones
    from src.core.models_v2 import MilestoneStatus

    entries = load_milestones(prog_id, programs_root)
    idx = next((i for i, e in enumerate(entries) if e.id == target_key), None)
    if idx is None:
        raise ValueError(f"Milestone {target_key!r} not found in program {prog_id!r}")
    entry = entries[idx]
    if target_field == "status":
        updated = dc_replace(entry, status=MilestoneStatus.from_string(source_value))
    elif target_field == "owner_alias":
        updated = dc_replace(entry, owner_alias=source_value)
    elif target_field == "target_date":
        updated = dc_replace(entry, target_date=date.fromisoformat(source_value))
    else:
        raise ValueError(f"Unknown milestone target_field: {target_field!r}")
    new_entries = entries[:idx] + (updated,) + entries[idx + 1 :]
    save_milestones(prog_id, new_entries, programs_root)


def _apply_to_risk_register(
    prog_id: str, target_key: str, target_field: str, source_value: str, programs_root: Path
) -> None:
    from src.core.risk_register_engine import load_risk_register, save_risk_register
    from src.core.models_v2 import RiskImpact, RiskStatus

    entries = load_risk_register(prog_id, programs_root)

    if target_field == "dimension_risk_level":
        # Update all risks matching by dimension_id or title
        updated_entries: list[Any] = []
        found_any = False
        for e in entries:
            if e.dimension_id == target_key or e.title.strip() == target_key:
                updated_entries.append(dc_replace(e, impact=RiskImpact.from_string(source_value)))
                found_any = True
            else:
                updated_entries.append(e)
        if not found_any:
            raise ValueError(
                f"No risk with dimension_id={target_key!r} found in program {prog_id!r}"
            )
        save_risk_register(prog_id, tuple(updated_entries), programs_root)
        return

    idx = next((i for i, e in enumerate(entries) if e.id == target_key), None)
    if idx is None:
        raise ValueError(f"Risk {target_key!r} not found in program {prog_id!r}")
    entry = entries[idx]
    if target_field == "owner_alias":
        updated = dc_replace(entry, owner_alias=source_value)
    elif target_field == "status":
        updated = dc_replace(entry, status=RiskStatus.from_string(source_value))
    else:
        raise ValueError(f"Unknown risk_register target_field: {target_field!r}")
    replacement_entries = entries[:idx] + (updated,) + entries[idx + 1 :]
    save_risk_register(prog_id, replacement_entries, programs_root)


def _apply_to_workstreams(
    prog_id: str, target_key: str, target_field: str, source_value: str, programs_root: Path
) -> None:
    import yaml as _yaml
    from src.core.workstream_documents import get_workstreams_path, save_workstreams_document

    ws_path = get_workstreams_path(prog_id, programs_root)
    if not ws_path.exists():
        raise ValueError(f"workstreams.yaml not found for program {prog_id!r}")
    doc = _yaml.safe_load(ws_path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"workstreams.yaml has unexpected format for program {prog_id!r}")
    workstreams = doc.get("workstreams")
    if not isinstance(workstreams, list):
        raise ValueError(f"workstreams.yaml missing 'workstreams' list for program {prog_id!r}")
    found = False
    for ws in workstreams:
        if isinstance(ws, dict) and ws.get("id") == target_key:
            ws[target_field] = source_value
            found = True
            break
    if not found:
        raise ValueError(f"Workstream {target_key!r} not found in program {prog_id!r}")
    save_workstreams_document(prog_id, doc, programs_root=programs_root)


def _apply_to_knowledge_doc(
    prog_id: str, target_key: str, target_field: str, source_value: str, programs_root: Path
) -> None:
    """Apply a Zone B knowledge-doc synthesis proposal (§24.6 Phase 5).

    Writes ``knowledge/<target_key>`` with a dated ``.bak`` of the prior content.
    ``target_key`` is the doc filename (e.g. ``nova_program_context.md``);
    ``target_field`` is ``body``. The whole document body is replaced (it is a
    synthesized patch, not a field-level record store).
    """
    knowledge_dir = programs_root / prog_id / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    doc_path = knowledge_dir / target_key

    # INV: never write outside the knowledge dir; reject path-traversal keys.
    try:
        doc_path.resolve().relative_to(knowledge_dir.resolve())
    except ValueError as error:
        raise ValueError(
            f"knowledge_doc target_key {target_key!r} escapes the knowledge dir for program {prog_id!r}"
        ) from error

    if target_field != "body":
        raise ValueError(
            f"Unknown knowledge_doc target_field: {target_field!r} (only 'body' is supported)"
        )

    # Dated .bak of the prior content (only the last .bak pattern is kept).
    if doc_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak_path = doc_path.with_suffix(doc_path.suffix + f".{stamp}.bak")
        try:
            os.replace(doc_path, bak_path)
        except OSError:
            # If rename fails (e.g. lock), fall back to copy + truncate.
            bak_path.write_bytes(doc_path.read_bytes())

    doc_path.write_text(source_value, encoding="utf-8")


def _write_changelog_entry(
    proposal: ContextUpdateProposal, *, actor: str, programs_root: Path
) -> None:
    """Append one entry to programs/<prog>/_ncfl_apply/changelog.jsonl."""
    entry = {
        "proposal_id": proposal.proposal_id,
        "program_id": proposal.program_id,
        "issue_number": proposal.issue_number,
        "target_store": proposal.target_store,
        "target_key": proposal.target_key,
        "target_field": proposal.target_field,
        "source_value": proposal.source_value,
        "actor": actor,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    changelog_dir = programs_root / proposal.program_id / _APPLY_JOURNAL_DIR
    changelog_dir.mkdir(parents=True, exist_ok=True)
    changelog_path = changelog_dir / "changelog.jsonl"
    from src.core.jsonl_utils import append_jsonl_line
    append_jsonl_line(changelog_path, json.dumps(entry, sort_keys=True))
