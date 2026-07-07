from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id, expire_stale_ai_proposals, load_ai_proposals, oldest_pending_proposal_age_days, supersede_pending_ai_proposals, update_ai_proposal_status
from src.core.grounding_validator import validate_synthesis_grounding
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, Signal, WorkstreamSynthesis


def test_load_ai_proposals_returns_latest_version_per_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    created_at = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    proposal_id = build_ai_proposal_id("acme", workstream_id="networking", created_at=created_at)

    proposal = _proposal(proposal_id, created_at=created_at)
    append_ai_proposal("acme", proposal, programs_root=programs_root)
    append_ai_proposal(
        "acme",
        AIProposal(
            id=proposal.id,
            workstream_id=proposal.workstream_id,
            synthesis=proposal.synthesis,
            status=AIProposalStatus.ACCEPTED,
            created_at=proposal.created_at,
            resolved_at=datetime(2026, 5, 10, 12, 5, tzinfo=timezone.utc),
            resolved_by="operator",
        ),
        programs_root=programs_root,
    )

    proposals = load_ai_proposals("acme", programs_root=programs_root)

    assert len(proposals) == 1
    assert proposals[0].status is AIProposalStatus.ACCEPTED
    assert proposals[0].resolved_by == "operator"


def test_supersede_pending_ai_proposals_marks_matching_workstream_only(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    networking_created = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    repairs_created = datetime(2026, 5, 10, 12, 1, tzinfo=timezone.utc)
    append_ai_proposal(
        "acme",
        _proposal(
            build_ai_proposal_id("acme", workstream_id="networking", created_at=networking_created),
            workstream_id="networking",
            created_at=networking_created,
        ),
        programs_root=programs_root,
    )
    append_ai_proposal(
        "acme",
        _proposal(
            build_ai_proposal_id("acme", workstream_id="repairs", created_at=repairs_created),
            workstream_id="repairs",
            created_at=repairs_created,
        ),
        programs_root=programs_root,
    )

    superseded = supersede_pending_ai_proposals(
        "acme",
        workstream_id="networking",
        resolved_by="vertex synthesize",
        resolved_at=datetime(2026, 5, 10, 12, 10, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    proposals = {proposal.workstream_id: proposal for proposal in load_ai_proposals("acme", programs_root=programs_root)}

    assert len(superseded) == 1
    assert proposals["networking"].status is AIProposalStatus.SUPERSEDED
    assert proposals["repairs"].status is AIProposalStatus.PENDING


def test_update_ai_proposal_status_appends_new_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    created_at = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    proposal = _proposal(
        build_ai_proposal_id("acme", workstream_id="networking", created_at=created_at),
        created_at=created_at,
    )
    append_ai_proposal("acme", proposal, programs_root=programs_root)

    updated = update_ai_proposal_status(
        "acme",
        proposal.id,
        new_status=AIProposalStatus.REJECTED,
        resolved_by="operator",
        resolved_at=datetime(2026, 5, 10, 12, 4, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert updated.status is AIProposalStatus.REJECTED
    assert load_ai_proposals("acme", programs_root=programs_root)[0].status is AIProposalStatus.REJECTED


def test_validate_synthesis_grounding_drops_invalid_refs_and_flags_majority_invalid() -> None:
    synthesis = WorkstreamSynthesis(
        workstream_id="networking",
        overall_assessment="Networking remains the gating lane.",
        proposed_risk=RiskLevel.HIGH,
        confidence=Confidence.HIGH,
        key_findings=("ETA slipped again.",),
        evidence_refs=("sig-valid", "sig-missing-1", "sig-missing-2"),
        open_questions=("Who owns the next validation?",),
        recommended_actions=("Close the checkpoint.",),
    )
    signals = (
        Signal(
            id="sig-valid",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="Validated networking checkpoint moved to 2026-05-17.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )

    result = validate_synthesis_grounding(synthesis, signals=signals)

    assert result.synthesis.evidence_refs == ("sig-valid",)
    assert result.invalid_evidence_refs == ("sig-missing-1", "sig-missing-2")
    assert result.flagged_for_review is True
    assert result.synthesis.confidence is Confidence.LOW


def test_load_ai_proposals_keeps_legacy_records_without_lineage_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-legacy",
                "workstream_id": "networking",
                "status": "pending",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": None,
                "resolved_by": None,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proposals = load_ai_proposals("acme", programs_root=programs_root)

    assert len(proposals) == 1
    assert proposals[0].edition_id is None
    assert proposals[0].issue_number is None


def test_load_ai_proposals_rejects_non_string_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": 123,
                "workstream_id": "networking",
                "status": "pending",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": None,
                "resolved_by": None,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        load_ai_proposals("acme", programs_root=programs_root)
    except TypeError as exc:
        assert str(exc) == "id must be a string"
    else:
        raise AssertionError("Expected load_ai_proposals() to reject a non-string id.")


def test_load_ai_proposals_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": 123,
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": None,
                "resolved_by": None,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        load_ai_proposals("acme", programs_root=programs_root)
    except TypeError as exc:
        assert str(exc) == "status must be a string"
    else:
        raise AssertionError("Expected load_ai_proposals() to reject a non-string status.")


def test_load_ai_proposals_rejects_naive_created_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": "pending",
                "created_at": "2026-05-10T12:00:00",
                "resolved_at": None,
                "resolved_by": None,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="created_at must include timezone information"):
        load_ai_proposals("acme", programs_root=programs_root)


def test_load_ai_proposals_rejects_non_string_evidence_refs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": "pending",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": None,
                "resolved_by": None,
                "issue_number": 77,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": [123],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        load_ai_proposals("acme", programs_root=programs_root)
    except TypeError as exc:
        assert str(exc) == "string-list field entries must be strings"
    else:
        raise AssertionError("Expected load_ai_proposals() to reject non-string evidence_refs entries.")


def test_load_ai_proposals_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": "pending",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": None,
                "resolved_by": None,
                "edition_id": "acme_weekly",
                "issue_number": "77",
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        load_ai_proposals("acme", programs_root=programs_root)
    except TypeError as exc:
        assert str(exc) == "issue_number must be an integer"
    else:
        raise AssertionError("Expected load_ai_proposals() to reject a numeric-string issue_number.")


def test_load_ai_proposals_rejects_non_string_resolved_by(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": "accepted",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": "2026-05-10T13:00:00+00:00",
                "resolved_by": 123,
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        load_ai_proposals("acme", programs_root=programs_root)
    except TypeError as exc:
        assert str(exc) == "resolved_by must be a string"
    else:
        raise AssertionError("Expected load_ai_proposals() to reject a non-string resolved_by.")


def test_load_ai_proposals_rejects_naive_resolved_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": "accepted",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": "2026-05-10T13:00:00",
                "resolved_by": "owner",
                "edition_id": "acme_weekly",
                "issue_number": 77,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="resolved_at must include timezone information"):
        load_ai_proposals("acme", programs_root=programs_root)


def test_load_ai_proposals_rejects_non_string_edition_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = programs_root / "acme" / "journal" / "ai_proposals.jsonl"
    proposals_path.parent.mkdir(parents=True)
    proposals_path.write_text(
        json.dumps(
            {
                "id": "proposal-1",
                "workstream_id": "networking",
                "status": "pending",
                "created_at": "2026-05-10T12:00:00+00:00",
                "resolved_at": None,
                "resolved_by": None,
                "edition_id": 77,
                "issue_number": None,
                "synthesis": {
                    "workstream_id": "networking",
                    "overall_assessment": "Networking remains the blocking lane.",
                    "proposed_risk": "high",
                    "confidence": "high",
                    "key_findings": ["Target date slipped twice."],
                    "evidence_refs": ["sig-1"],
                    "open_questions": ["Is the servicing fix validated?"],
                    "recommended_actions": ["Close the next validation checkpoint."],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        load_ai_proposals("acme", programs_root=programs_root)
    except TypeError as exc:
        assert str(exc) == "edition_id must be a string"
    else:
        raise AssertionError("Expected load_ai_proposals() to reject a non-string edition_id.")


def _proposal(
    proposal_id: str,
    *,
    workstream_id: str = "networking",
    created_at: datetime,
) -> AIProposal:
    return AIProposal(
        id=proposal_id,
        workstream_id=workstream_id,
        synthesis=WorkstreamSynthesis(
            workstream_id=workstream_id,
            overall_assessment="Networking remains the blocking lane.",
            proposed_risk=RiskLevel.HIGH,
            confidence=Confidence.HIGH,
            key_findings=("Target date slipped twice.",),
            evidence_refs=("sig-1",),
            open_questions=("Is the servicing fix validated?",),
            recommended_actions=("Close the next validation checkpoint.",),
        ),
        status=AIProposalStatus.PENDING,
        created_at=created_at,
        resolved_at=None,
        resolved_by=None,
    )


def test_expire_stale_ai_proposals_expires_only_old_pending(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    old_created = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)  # 18 days old
    recent_created = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # 6 days old

    old_proposal = _proposal(
        build_ai_proposal_id("acme", workstream_id="networking", created_at=old_created),
        created_at=old_created,
    )
    recent_proposal = _proposal(
        build_ai_proposal_id("acme", workstream_id="repairs", created_at=recent_created),
        workstream_id="repairs",
        created_at=recent_created,
    )
    append_ai_proposal("acme", old_proposal, programs_root=programs_root)
    append_ai_proposal("acme", recent_proposal, programs_root=programs_root)

    expired = expire_stale_ai_proposals("acme", ttl_days=14, resolved_at=now, programs_root=programs_root)

    assert len(expired) == 1
    assert expired[0].id == old_proposal.id
    assert expired[0].status is AIProposalStatus.EXPIRED
    assert expired[0].resolved_by == "system:ttl"

    pending = load_ai_proposals("acme", status=AIProposalStatus.PENDING, programs_root=programs_root)
    assert len(pending) == 1
    assert pending[0].id == recent_proposal.id


def test_expire_stale_ai_proposals_leaves_no_pending_when_all_old(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    old_created = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    proposal = _proposal(
        build_ai_proposal_id("acme", workstream_id="networking", created_at=old_created),
        created_at=old_created,
    )
    append_ai_proposal("acme", proposal, programs_root=programs_root)

    expired = expire_stale_ai_proposals("acme", ttl_days=14, resolved_at=now, programs_root=programs_root)

    assert len(expired) == 1
    assert expired[0].status is AIProposalStatus.EXPIRED
    assert load_ai_proposals("acme", status=AIProposalStatus.PENDING, programs_root=programs_root) == ()


def test_oldest_pending_proposal_age_days_returns_none_when_empty(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    age = oldest_pending_proposal_age_days("acme", as_of=datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc), programs_root=programs_root)
    assert age is None


def test_oldest_pending_proposal_age_days_returns_correct_age(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)  # 8 days old

    proposal = _proposal(
        build_ai_proposal_id("acme", workstream_id="networking", created_at=created),
        created_at=created,
    )
    append_ai_proposal("acme", proposal, programs_root=programs_root)

    age = oldest_pending_proposal_age_days("acme", as_of=now, programs_root=programs_root)
    assert age == 8