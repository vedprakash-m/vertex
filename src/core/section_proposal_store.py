from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import portalocker

from src.core.journal import PROGRAMS_ROOT
from src.core.models import Confidence
from src.core.models_v2 import SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus


@dataclass(frozen=True, slots=True)
class HintProposal:
    hint_id: str
    edition: str
    issue_number: int
    workstream_id: str
    hint_kind: str
    suggested_sentence: str
    status: str                  # "pending" | "accepted" | "rejected" | "modified"
    accepted_text: str | None = None


PROPOSALS_FILENAME = "proposals.jsonl"
ACCEPTED_PROPOSALS_FILENAME = "proposals_accepted.jsonl"


def get_proposals_path(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "narratives" / f"issue_{issue_number:03d}" / PROPOSALS_FILENAME


def build_section_revision_proposal_id(
    edition_id: str,
    issue_number: int,
    *,
    section_id: str,
    generated_at: datetime,
) -> str:
    resolved_time = _require_utc_timestamp(generated_at)
    return str(
        uuid5(
            NAMESPACE_URL,
            f"vertex:section-proposal:{edition_id}:{issue_number}:{section_id}:{resolved_time.isoformat()}",
        )
    )


def load_proposals(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    status_filter: set[SectionRevisionStatus] | None = None,
) -> tuple[SectionRevisionProposal, ...]:
    path = get_proposals_path(program_id, issue_number, programs_root=programs_root)
    if not path.exists():
        return ()

    latest_by_id: dict[str, SectionRevisionProposal] = {}
    for record in _read_jsonl(path):
        if "proposal_id" in record and "evidence_brief" in record:
            proposal = _proposal_from_record(record)
            latest_by_id[proposal.proposal_id] = proposal

    proposals = tuple(sorted(latest_by_id.values(), key=lambda entry: (entry.generated_at, entry.proposal_id)))
    if status_filter is not None:
        proposals = tuple(proposal for proposal in proposals if proposal.status in status_filter)
    return proposals
    # New function to load stale claim IDs
def load_stale_claim_ids(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    status_filter: set[SectionRevisionStatus] | None = None,
) -> tuple[str, ...]:
    stale_claim_ids: list[str] = []
    for proposal in load_proposals(
        program_id,
        issue_number,
        programs_root=programs_root,
        status_filter=status_filter,
    ):
        stale_claim_ids.extend(proposal.evidence_brief.stale_claims)
    return tuple(dict.fromkeys(claim_id for claim_id in stale_claim_ids if claim_id))


def load_archived_stale_claim_ids(archive_dir: Path) -> tuple[str, ...]:
    archive_path = archive_dir / ACCEPTED_PROPOSALS_FILENAME
    if not archive_path.exists():
        return ()
    stale_claim_ids: list[str] = []
    for record in _read_jsonl(archive_path):
        if "proposal_id" not in record or "evidence_brief" not in record:
            continue
        proposal = _proposal_from_record(record)
        stale_claim_ids.extend(proposal.evidence_brief.stale_claims)
    return tuple(dict.fromkeys(claim_id for claim_id in stale_claim_ids if claim_id))


def append_proposal(
    proposal: SectionRevisionProposal,
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_proposals_path(program_id, issue_number, programs_root=programs_root)
    _append_jsonl(path, _proposal_to_record(proposal))
    return path


def write_accepted_proposals_archive(
    proposals: tuple[SectionRevisionProposal, ...],
    archive_dir: Path,
) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / ACCEPTED_PROPOSALS_FILENAME
    payload = "".join(json.dumps(_proposal_to_record(proposal), ensure_ascii=False) + "\n" for proposal in proposals)
    temp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, archive_path)
    return archive_path


def update_proposal_status(
    proposal_id: str,
    new_status: SectionRevisionStatus,
    *,
    accepted_text: str | None = None,
    rejection_reason: str | None = None,
    resolved_at: datetime | None = None,
    program_id: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> SectionRevisionProposal:
    proposals = {
        proposal.proposal_id: proposal
        for proposal in load_proposals(program_id, issue_number, programs_root=programs_root)
    }
    proposal = proposals.get(proposal_id)
    if proposal is None:
        raise ValueError(f"Section proposal '{proposal_id}' does not exist for issue {issue_number:03d}.")

    timestamp = _require_utc_timestamp(resolved_at or datetime.now(timezone.utc))
    updated = replace(
        proposal,
        status=new_status,
        resolved_at=timestamp,
        accepted_text=(accepted_text.strip() if accepted_text is not None and accepted_text.strip() else None),
        rejection_reason=(rejection_reason.strip() if rejection_reason is not None and rejection_reason.strip() else None),
    )
    append_proposal(updated, program_id, issue_number, programs_root=programs_root)
    return updated


def supersede_pending_proposals(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    resolved_at: datetime | None = None,
) -> tuple[SectionRevisionProposal, ...]:
    timestamp = _require_utc_timestamp(resolved_at or datetime.now(timezone.utc))
    superseded: list[SectionRevisionProposal] = []
    for proposal in load_proposals(
        program_id,
        issue_number,
        programs_root=programs_root,
        status_filter={SectionRevisionStatus.PENDING},
    ):
        updated = replace(
            proposal,
            status=SectionRevisionStatus.SUPERSEDED,
            resolved_at=timestamp,
        )
        append_proposal(updated, program_id, issue_number, programs_root=programs_root)
        superseded.append(updated)
    return tuple(superseded)


def _proposal_to_record(proposal: SectionRevisionProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "edition_id": proposal.edition_id,
        "issue_number": proposal.issue_number,
        "section_id": proposal.section_id,
        "current_text": proposal.current_text,
        "proposed_text": proposal.proposed_text,
        "evidence_brief": {
            "section_id": proposal.evidence_brief.section_id,
            "ado_delta_summary": proposal.evidence_brief.ado_delta_summary,
            "new_items": list(proposal.evidence_brief.new_items),
            "closed_items": list(proposal.evidence_brief.closed_items),
            "risk_changed_items": list(proposal.evidence_brief.risk_changed_items),
            "eta_changed_items": list(proposal.evidence_brief.eta_changed_items),
            "top_signals": list(proposal.evidence_brief.top_signals),
            "kpi_summary": proposal.evidence_brief.kpi_summary,
            "stale_claims": list(proposal.evidence_brief.stale_claims),
            "vitality_summary": proposal.evidence_brief.vitality_summary,
            "confidence": proposal.evidence_brief.confidence.value,
        },
        "status": proposal.status.value,
        "generated_at": _require_utc_timestamp(proposal.generated_at).isoformat(),
        "resolved_at": _format_optional_datetime(proposal.resolved_at),
        "accepted_text": proposal.accepted_text,
        "rejection_reason": proposal.rejection_reason,
        "source_hash": proposal.source_hash,
        "ai_model_used": proposal.ai_model_used,
        "ai_cost_usd": proposal.ai_cost_usd,
    }


def _proposal_from_record(record: dict[str, Any]) -> SectionRevisionProposal:
    evidence_payload = record.get("evidence_brief")
    if not isinstance(evidence_payload, dict):
        raise ValueError("Section proposal record is missing an evidence_brief payload.")

    return SectionRevisionProposal(
        proposal_id=_required_string(record.get("proposal_id"), field_name="proposal_id").strip(),
        edition_id=_required_string(record.get("edition_id"), field_name="edition_id").strip(),
        issue_number=_required_int(record.get("issue_number"), field_name="issue_number"),
        section_id=_required_string(record.get("section_id"), field_name="section_id").strip(),
        current_text=_required_string(record.get("current_text"), field_name="current_text"),
        proposed_text=_optional_string(record.get("proposed_text")),
        evidence_brief=SectionEvidenceBrief(
            section_id=_required_string(evidence_payload.get("section_id"), field_name="evidence_brief.section_id").strip(),
            ado_delta_summary=_required_string(evidence_payload.get("ado_delta_summary"), field_name="evidence_brief.ado_delta_summary").strip(),
            new_items=_int_tuple(evidence_payload.get("new_items")),
            closed_items=_int_tuple(evidence_payload.get("closed_items")),
            risk_changed_items=_int_tuple(evidence_payload.get("risk_changed_items")),
            eta_changed_items=_int_tuple(evidence_payload.get("eta_changed_items")),
            top_signals=_string_tuple(evidence_payload.get("top_signals")),
            kpi_summary=_optional_string(evidence_payload.get("kpi_summary")),
            stale_claims=_string_tuple(evidence_payload.get("stale_claims")),
            vitality_summary=_required_string(evidence_payload.get("vitality_summary"), field_name="evidence_brief.vitality_summary").strip(),
            confidence=Confidence.from_string(_required_string(evidence_payload.get("confidence"), field_name="evidence_brief.confidence")),
        ),
        status=SectionRevisionStatus.from_string(_required_string(record.get("status"), field_name="status")),
        generated_at=_parse_datetime(record.get("generated_at"), field_name="generated_at"),
        resolved_at=_parse_optional_datetime(record.get("resolved_at"), field_name="resolved_at"),
        accepted_text=_optional_string(record.get("accepted_text")),
        rejection_reason=_optional_string(record.get("rejection_reason")),
        source_hash=_optional_string(record.get("source_hash")),
        ai_model_used=_optional_string(record.get("ai_model_used")),
        ai_cost_usd=_optional_float(record.get("ai_cost_usd")),
    )


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    with path.open("r", encoding="utf-8") as handle:
        records = []
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = parse_jsonl_line(line)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object in {path}, found {type(payload).__name__}.")
            records.append(payload)
    return tuple(records)


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Expected a list of strings, found {type(value).__name__}.")
    values: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError("Expected string entries in string list fields.")
        stripped = entry.strip()
        if stripped:
            values.append(stripped)
    return tuple(values)


def _int_tuple(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Expected a list of ints, found {type(value).__name__}.")
    values: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise TypeError("Expected integer entries in integer list fields.")
        values.append(entry)
    return tuple(values)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional string field must be a string")
    normalized = value.strip()
    return normalized or None


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Expected a float-compatible value, found bool.")
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"Expected a float-compatible value, found {type(value).__name__}.")
    return float(value)


def _required_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _require_utc_timestamp(value).isoformat()


def _require_utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_hint_proposals(
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[HintProposal, ...]:
    path = get_proposals_path(program_id, issue_number, programs_root=programs_root)
    if not path.exists():
        return ()
    
    latest_by_id: dict[str, HintProposal] = {}
    for record in _read_jsonl(path):
        if "hint_id" in record:
            hint = _hint_proposal_from_record(record)
            latest_by_id[hint.hint_id] = hint
            
    return tuple(latest_by_id.values())


def append_hint_proposal(
    proposal: HintProposal,
    program_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_proposals_path(program_id, issue_number, programs_root=programs_root)
    _append_jsonl(path, _hint_proposal_to_record(proposal))
    return path


def _hint_proposal_to_record(proposal: HintProposal) -> dict[str, Any]:
    return {
        "hint_id": proposal.hint_id,
        "edition": proposal.edition,
        "issue_number": proposal.issue_number,
        "workstream_id": proposal.workstream_id,
        "hint_kind": proposal.hint_kind,
        "suggested_sentence": proposal.suggested_sentence,
        "status": proposal.status,
        "accepted_text": proposal.accepted_text,
    }


def _hint_proposal_from_record(record: dict[str, Any]) -> HintProposal:
    return HintProposal(
        hint_id=_required_string(record.get("hint_id"), field_name="hint_id").strip(),
        edition=_required_string(record.get("edition"), field_name="edition").strip(),
        issue_number=_required_int(record.get("issue_number"), field_name="issue_number"),
        workstream_id=_required_string(record.get("workstream_id"), field_name="workstream_id").strip(),
        hint_kind=_required_string(record.get("hint_kind"), field_name="hint_kind").strip(),
        suggested_sentence=_required_string(record.get("suggested_sentence"), field_name="suggested_sentence").strip(),
        status=_required_string(record.get("status"), field_name="status").strip(),
        accepted_text=_optional_string(record.get("accepted_text")),
    )
