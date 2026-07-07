from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import portalocker

from src.core.journal import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records, validate_jsonl_row
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, WorkstreamSynthesis


# High-risk append-only file — grows with every AI proposal emitted during
# synthesis. Rotated at 10 MB (spec §11.3 Phase 5 / D-23) to bound on-disk
# footprint while preserving the full proposal history under ``journal/rotated/``.
_AI_PROPOSALS_MAX_BYTES = 10 * 1024 * 1024

# D-30: pending AI proposals that have not been reviewed within
# ``AI_PROPOSAL_TTL_DAYS`` are garbage-collected by
# ``expire_stale_ai_proposals(...)`` (called from the synthesize
# pipeline). The 14-day window matches the spec §11.5 / D-30
# ``Proposal TTL`` policy and the operator nudge cooldown. Both
# downstream consumers (the synthesize call site, the doctor
# queue check) import this constant so a future ratchet to 21d
# or 7d is a single-file change.
AI_PROPOSAL_TTL_DAYS: int = 14


def get_ai_proposals_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "ai_proposals.jsonl"


def build_ai_proposal_id(program_id: str, *, workstream_id: str, created_at: datetime) -> str:
    resolved_time = _require_utc_timestamp(created_at)
    return str(
        uuid5(
            NAMESPACE_URL,
            f"vertex:ai-proposal:{program_id}:{workstream_id}:{resolved_time.isoformat()}",
        )
    )


def load_ai_proposals(
    program_id: str,
    *,
    status: AIProposalStatus | None = None,
    workstream_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AIProposal, ...]:
    path = get_ai_proposals_path(program_id, programs_root)
    if not path.exists():
        return ()

    latest_by_id: dict[str, AIProposal] = {}
    for record in read_jsonl_records(path):
        # Strict field-presence gate: every AI proposal row must at least carry id,
        # workstream_id, synthesis payload, status, and created_at. The deeper
        # parser will then enforce type correctness on these and nested fields.
        validate_jsonl_row(
            record,
            required_fields=(
                "id",
                "workstream_id",
                "synthesis",
                "status",
                "created_at",
            ),
            field_name="AI proposal row",
        )
        proposal = _proposal_from_record(record)
        latest_by_id[proposal.id] = proposal

    proposals = tuple(sorted(latest_by_id.values(), key=lambda entry: (entry.created_at, entry.id)))
    if workstream_id is not None:
        proposals = tuple(entry for entry in proposals if entry.workstream_id == workstream_id)
    if status is not None:
        proposals = tuple(entry for entry in proposals if entry.status is status)
    return proposals


def append_ai_proposal(
    program_id: str,
    proposal: AIProposal,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_ai_proposals_path(program_id, programs_root)
    payload = json.dumps(_proposal_to_record(proposal), ensure_ascii=False) + "\n"
    append_jsonl_line(path, payload, max_bytes=_AI_PROPOSALS_MAX_BYTES)
    return path


def supersede_pending_ai_proposals(
    program_id: str,
    *,
    workstream_id: str,
    resolved_by: str,
    resolved_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AIProposal, ...]:
    timestamp = _require_utc_timestamp(resolved_at or datetime.now(timezone.utc))
    superseded: list[AIProposal] = []
    for proposal in load_ai_proposals(
        program_id,
        status=AIProposalStatus.PENDING,
        workstream_id=workstream_id,
        programs_root=programs_root,
    ):
        updated = replace(
            proposal,
            status=AIProposalStatus.SUPERSEDED,
            resolved_at=timestamp,
            resolved_by=resolved_by.strip() or None,
        )
        append_ai_proposal(program_id, updated, programs_root=programs_root)
        superseded.append(updated)
    return tuple(superseded)


def update_ai_proposal_status(
    program_id: str,
    proposal_id: str,
    *,
    new_status: AIProposalStatus,
    resolved_by: str,
    resolved_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> AIProposal:
    proposals = {proposal.id: proposal for proposal in load_ai_proposals(program_id, programs_root=programs_root)}
    proposal = proposals.get(proposal_id)
    if proposal is None:
        raise ValueError(f"AI proposal '{proposal_id}' does not exist in {program_id}.")
    updated = replace(
        proposal,
        status=new_status,
        resolved_at=_require_utc_timestamp(resolved_at or datetime.now(timezone.utc)),
        resolved_by=resolved_by.strip() or None,
    )
    append_ai_proposal(program_id, updated, programs_root=programs_root)
    return updated


def expire_stale_ai_proposals(
    program_id: str,
    *,
    ttl_days: int = AI_PROPOSAL_TTL_DAYS,
    resolved_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AIProposal, ...]:
    """Expire PENDING proposals older than ttl_days; return the expired set.

    D-30: ``ttl_days`` defaults to the module-level
    ``AI_PROPOSAL_TTL_DAYS`` constant (14 days per spec §11.5).
    Synthesize runs call this on every workstream synthesis so
    the central proposal store cannot accumulate unboundedly.
    """
    timestamp = _require_utc_timestamp(resolved_at or datetime.now(timezone.utc))
    cutoff = timestamp - timedelta(days=ttl_days)
    expired: list[AIProposal] = []
    for proposal in load_ai_proposals(
        program_id,
        status=AIProposalStatus.PENDING,
        programs_root=programs_root,
    ):
        if proposal.created_at < cutoff:
            updated = replace(
                proposal,
                status=AIProposalStatus.EXPIRED,
                resolved_at=timestamp,
                resolved_by="system:ttl",
            )
            append_ai_proposal(program_id, updated, programs_root=programs_root)
            expired.append(updated)
    return tuple(expired)


def count_pending_ai_proposals(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> int:
    """Return the number of PENDING proposals for *program_id*.

    D-30: feeds the doctor queue check so operators can see how
    many proposals are awaiting review (not just the age of the
    oldest). Counts include both fresh and stale-but-not-yet-
    expired proposals; the synthesize pipeline expires stale
    ones on its next run.
    """
    return sum(
        1
        for _ in load_ai_proposals(
            program_id,
            status=AIProposalStatus.PENDING,
            programs_root=programs_root,
        )
    )


def oldest_pending_proposal_age_days(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> int | None:
    """Return the age in days of the oldest pending proposal, or None if no pending proposals."""
    timestamp = _require_utc_timestamp(as_of or datetime.now(timezone.utc))
    pending = load_ai_proposals(program_id, status=AIProposalStatus.PENDING, programs_root=programs_root)
    if not pending:
        return None
    oldest = min(proposal.created_at for proposal in pending)
    delta = timestamp - oldest
    return delta.days


def _proposal_to_record(proposal: AIProposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "workstream_id": proposal.workstream_id,
        "status": proposal.status.value,
        "created_at": _require_utc_timestamp(proposal.created_at).isoformat(),
        "resolved_at": _require_utc_timestamp(proposal.resolved_at).isoformat() if proposal.resolved_at is not None else None,
        "resolved_by": proposal.resolved_by,
        "edition_id": proposal.edition_id,
        "issue_number": proposal.issue_number,
        "synthesis": {
            "workstream_id": proposal.synthesis.workstream_id,
            "overall_assessment": proposal.synthesis.overall_assessment,
            "proposed_risk": proposal.synthesis.proposed_risk.value,
            "confidence": proposal.synthesis.confidence.value,
            "key_findings": list(proposal.synthesis.key_findings),
            "evidence_refs": list(proposal.synthesis.evidence_refs),
            "open_questions": list(proposal.synthesis.open_questions),
            "recommended_actions": list(proposal.synthesis.recommended_actions),
        },
    }


def _proposal_from_record(record: dict[str, Any]) -> AIProposal:
    synthesis_payload = record.get("synthesis")
    if not isinstance(synthesis_payload, dict):
        raise ValueError("AI proposal record is missing a synthesis payload.")
    synthesis = WorkstreamSynthesis(
        workstream_id=_required_string(synthesis_payload.get("workstream_id"), field_name="synthesis.workstream_id").strip(),
        overall_assessment=_required_string(synthesis_payload.get("overall_assessment"), field_name="synthesis.overall_assessment").strip(),
        proposed_risk=RiskLevel.from_string(_required_string(synthesis_payload.get("proposed_risk"), field_name="synthesis.proposed_risk")),
        confidence=Confidence.from_string(_required_string(synthesis_payload.get("confidence"), field_name="synthesis.confidence")),
        key_findings=_string_tuple(synthesis_payload.get("key_findings")),
        evidence_refs=_string_tuple(synthesis_payload.get("evidence_refs")),
        open_questions=_string_tuple(synthesis_payload.get("open_questions")),
        recommended_actions=_string_tuple(synthesis_payload.get("recommended_actions")),
    )
    return AIProposal(
        id=_required_string(record.get("id"), field_name="id").strip(),
        workstream_id=_required_string(record.get("workstream_id"), field_name="workstream_id").strip(),
        synthesis=synthesis,
        status=AIProposalStatus.from_string(_required_string(record.get("status"), field_name="status")),
        created_at=_parse_datetime(record.get("created_at"), field_name="created_at"),
        resolved_at=_parse_optional_datetime(record.get("resolved_at"), field_name="resolved_at"),
        resolved_by=_optional_string(record.get("resolved_by"), field_name="resolved_by"),
        edition_id=_optional_string(record.get("edition_id"), field_name="edition_id"),
        issue_number=_optional_int(record.get("issue_number"), field_name="issue_number"),
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Expected a list of strings, found {type(value).__name__}.")
    normalized: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError("string-list field entries must be strings")
        stripped = entry.strip()
        if stripped:
            normalized.append(stripped)
    return tuple(normalized)


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    return normalized or None


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _optional_int(value: object, *, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    raise TypeError(f"{field_name} must be an integer")


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    parsed = _parse_optional_datetime(value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"missing {field_name}")
    return parsed


def _parse_optional_datetime(value: object, *, field_name: str) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO datetime string.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return parsed.astimezone(timezone.utc)


def _require_utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)