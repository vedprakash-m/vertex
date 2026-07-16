"""ADF-W3.3/ADF-W3.4 (specs/arch-data-fix.md Section 8.10.4): meeting-to-
action extraction against the spec's exact required schema:

    owner | commitment | due | linked_work_item | blocks | source_span

Follows the same "deterministic markers first, LLM extraction on residual
content, merge and deduplicate, validate, stage for review" five-step
pipeline `src/ai/claim_extractor.py` already implements for authored
narratives -- this module is the meeting-transcript-scoped counterpart with
its own schema (`ClaimEntry` has no `commitment`/`blocks`/`source_span`
fields and cannot represent 8.10.4's action shape).

This module owns steps 1, 3, and 4 (deterministic extraction, merge/dedup,
validation) -- all Zone A, deterministic, no AI call. Step 2 (LLM
extraction on residual content) is Zone B -- see
``src/ai/meeting_action_extractor.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from src.core.models import WorkItem
from src.core.proposal_audit import record_proposal_event
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES

ExtractionMethod = Literal["deterministic", "llm"]
#: "staged" = step 5's "stage proposals for review" (this module's own output);
#: "approved" = a human reviewer accepted it (ADF-W3.5's routing precondition,
#: mirrors AIProposalStatus.ACCEPTED's human-gate pattern); "rejected" = failed
#: validation (never routable) or a reviewer declined it.
MeetingActionStatus = Literal["staged", "approved", "rejected"]

_MARKER = "Action:"
_ALLOWED_MARKER_KEYS = frozenset({"owner", "due", "wi", "blocks"})


@dataclass(frozen=True, slots=True)
class MeetingAction:
    """Section 8.10.4's required action schema, verbatim, plus the
    bookkeeping fields (id/meeting_ref/extraction_method/status) every
    other proposal-shaped type in this codebase carries."""

    id: str
    program_id: str
    meeting_ref: str
    commitment: str
    owner_alias: str | None
    due_date: date | None
    linked_work_item_id: int | None
    blocks: tuple[str, ...]
    source_span: str
    extraction_method: ExtractionMethod
    status: MeetingActionStatus = "staged"
    rejection_reason: str | None = None
    # ADF-W2.11/W3.8 (ADR-0017): additive workflow-measurement timestamps.
    proposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MeetingActionExtractionResult:
    actions: tuple[MeetingAction, ...]
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Step 1: deterministic marker extraction.
# ---------------------------------------------------------------------------


def extract_deterministic_meeting_actions(
    *,
    program_id: str,
    meeting_ref: str,
    transcript_text: str,
) -> tuple[MeetingAction, ...]:
    """Parses ``Action: <commitment> | owner=<alias> | due=<YYYY-MM-DD> |
    wi=<id> | blocks=<ref,ref,...>`` lines -- the same marker-line shape
    ``claim_extractor.py``'s ``Claim:``/``Decision ask:`` markers use. The
    literal source line is the action's ``source_span`` (a deterministic
    marker line IS its own grounding -- no separate citation needed).
    A malformed marker line is skipped, not silently coerced -- callers see
    it as a residual-content line for the LLM tier to (optionally) recover.
    """
    actions: list[MeetingAction] = []
    index = 0
    for raw_line in transcript_text.splitlines():
        line = raw_line.strip()
        if not line or line[: len(_MARKER)].lower() != _MARKER.lower():
            continue
        payload = _parse_marker_payload(line)
        if payload is None:
            continue
        index += 1
        commitment = payload.get("text", "").strip()
        if not commitment:
            continue
        raw_due_date = _parse_due(payload.get("due"))
        if raw_due_date is _INVALID_DATE:
            continue
        assert raw_due_date is None or isinstance(raw_due_date, date)
        due_date = raw_due_date
        raw_linked_work_item_id = _parse_wi(payload.get("wi"))
        if raw_linked_work_item_id is _INVALID_WI:
            continue
        assert raw_linked_work_item_id is None or isinstance(raw_linked_work_item_id, int)
        linked_work_item_id = raw_linked_work_item_id
        actions.append(
            MeetingAction(
                id=f"deterministic-action-{meeting_ref}-{index}",
                program_id=program_id,
                meeting_ref=meeting_ref,
                commitment=commitment,
                owner_alias=_normalize_owner_alias(payload.get("owner")),
                due_date=due_date,
                linked_work_item_id=linked_work_item_id,
                blocks=_parse_blocks(payload.get("blocks")),
                source_span=line,
                extraction_method="deterministic",
            )
        )
    return tuple(actions)


_INVALID_DATE = object()
_INVALID_WI = object()


def _parse_marker_payload(line: str) -> dict[str, str] | None:
    body = line[len(_MARKER) :].strip()
    if not body:
        return None
    segments = [segment.strip() for segment in body.split("|")]
    if not segments or not segments[0]:
        return None
    payload = {"text": segments[0]}
    for segment in segments[1:]:
        if "=" not in segment:
            return None
        key, value = segment.split("=", 1)
        normalized_key = key.strip().lower()
        if normalized_key not in _ALLOWED_MARKER_KEYS:
            return None
        payload[normalized_key] = value.strip()
    return payload


def _parse_due(value: str | None) -> date | None | object:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return _INVALID_DATE


def _parse_wi(value: str | None) -> int | None | object:
    if value is None or not value.strip():
        return None
    normalized = value.strip().upper()
    if normalized.startswith("WI:"):
        normalized = normalized[3:]
    try:
        return int(normalized)
    except ValueError:
        return _INVALID_WI


def _parse_blocks(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    refs = [part.strip() for part in value.split(",") if part.strip()]
    return tuple(dict.fromkeys(refs))


def _normalize_owner_alias(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    normalized = "".join(character for character in normalized if character.isalnum() or character in {".", "_", "-"})
    return normalized or None


# ---------------------------------------------------------------------------
# Step 3: merge and deduplicate.
# ---------------------------------------------------------------------------


def merge_meeting_actions(
    deterministic: tuple[MeetingAction, ...],
    llm: tuple[MeetingAction, ...],
) -> MeetingActionExtractionResult:
    """Deterministic actions are structurally exempt from dedup loss (a
    marker line is unambiguous ground truth); an LLM action is dropped only
    when it duplicates a deterministic one on the same normalized
    (owner_alias, commitment) key -- the residual-content contract (Section
    8.10.4 step 2) means true duplicates should be rare, but this is the
    safety net for an LLM re-surfacing content a marker already covered."""
    seen_keys = {_dedup_key(action) for action in deterministic}
    warnings: list[str] = []
    merged: list[MeetingAction] = list(deterministic)
    for action in llm:
        key = _dedup_key(action)
        if key in seen_keys:
            warnings.append(f"Dropped duplicate LLM-extracted action already covered by a deterministic marker: {action.commitment!r}")
            continue
        seen_keys.add(key)
        merged.append(action)
    return MeetingActionExtractionResult(actions=tuple(merged), warnings=tuple(warnings))


def _dedup_key(action: MeetingAction) -> tuple[str, str]:
    normalized_commitment = " ".join(action.commitment.split()).strip().lower()
    normalized_owner = (action.owner_alias or "").strip().lower()
    return (normalized_owner, normalized_commitment)


# ---------------------------------------------------------------------------
# Step 4: validate. An invalid action is REJECTED (status="rejected",
# rejection_reason set) -- never silently dropped from the result and never
# silently passed through as if it were valid (Section 8.10.4: "invalid/
# ambiguous proposals are blocked").
# ---------------------------------------------------------------------------


def validate_meeting_actions(
    actions: tuple[MeetingAction, ...],
    *,
    transcript_text: str,
    items: tuple[WorkItem, ...],
) -> tuple[MeetingAction, ...]:
    items_by_id = {item.id: item for item in items}
    validated: list[MeetingAction] = []
    for action in actions:
        findings = _validate_one(action, transcript_text=transcript_text, items_by_id=items_by_id)
        if findings:
            validated.append(replace(action, status="rejected", rejection_reason="; ".join(findings)))
        else:
            validated.append(action)
    return tuple(validated)


def _validate_one(
    action: MeetingAction, *, transcript_text: str, items_by_id: dict[int, WorkItem]
) -> tuple[str, ...]:
    findings: list[str] = []

    if not action.source_span.strip():
        findings.append("source_span is empty.")
    elif action.source_span.strip() not in transcript_text:
        findings.append("source_span does not appear verbatim in the transcript (possible fabrication).")

    if action.owner_alias is not None and not action.owner_alias.strip():
        findings.append("owner_alias is present but empty after normalization.")

    if action.linked_work_item_id is not None:
        item = items_by_id.get(action.linked_work_item_id)
        if item is None:
            findings.append(f"linked_work_item WI:{action.linked_work_item_id} is not in the allowed work item set.")
        elif item.state.strip().lower() in TERMINAL_WORK_ITEM_STATES:
            findings.append(
                f"linked_work_item WI:{action.linked_work_item_id} is already in a terminal state ({item.state!r}) -- "
                "a new action against a closed work item is ambiguous."
            )

    for blocked_ref in action.blocks:
        normalized = blocked_ref.strip().upper()
        if normalized.startswith("WI:"):
            try:
                blocked_id = int(normalized[3:])
            except ValueError:
                findings.append(f"blocks entry {blocked_ref!r} is not a valid WI:<id> reference.")
                continue
            if blocked_id not in items_by_id:
                findings.append(f"blocks entry WI:{blocked_id} is not in the allowed work item set.")

    if not action.commitment.strip():
        findings.append("commitment text is empty.")

    return tuple(findings)


def approve_meeting_action(
    action: MeetingAction, *, approved_by: str, programs_root: Path | None = None, reviewed: bool = True
) -> MeetingAction:
    """A human reviewer accepts a staged action -- the only transition
    ``route_meeting_action_to_ado_proposal`` (ADF-W3.5) will route. A
    rejected action can never be approved (fail closed: re-validate by
    re-running the pipeline rather than overriding a rejection here)."""
    if action.status == "rejected":
        raise ValueError(f"MeetingAction {action.id!r} was rejected ({action.rejection_reason}) -- cannot approve.")
    del approved_by  # not yet persisted anywhere (no reviewer-identity store for this type yet); accepted for API clarity/future audit trail.
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=action.program_id, proposal_type="meeting_action", proposal_id=action.id,
        event="approved", programs_root=programs_root, at=decided_at, proposed_at=action.proposed_at,
        reviewed=reviewed,
    )
    return replace(action, status="approved", decided_at=decided_at)


def reject_meeting_action(
    action: MeetingAction, *, reason: str, programs_root: Path | None = None
) -> MeetingAction:
    """A human reviewer explicitly declines a staged action (distinct from
    a validation-time rejection: ``rejection_reason`` still records why)."""
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=action.program_id, proposal_type="meeting_action", proposal_id=action.id,
        event="rejected", programs_root=programs_root, at=decided_at, proposed_at=action.proposed_at,
        rejection_reason=reason,
    )
    return replace(action, status="rejected", rejection_reason=reason, decided_at=decided_at)


__all__ = [
    "MeetingAction",
    "MeetingActionExtractionResult",
    "approve_meeting_action",
    "extract_deterministic_meeting_actions",
    "merge_meeting_actions",
    "reject_meeting_action",
    "validate_meeting_actions",
]
