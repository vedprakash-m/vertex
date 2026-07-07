from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.core.models import Confidence
from src.core.models_v2 import SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus
from src.core.section_proposal_store import append_proposal, build_section_revision_proposal_id, get_proposals_path, load_hint_proposals, load_proposals, supersede_pending_proposals, update_proposal_status


def test_load_proposals_returns_latest_version_per_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    generated_at = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    proposal_id = build_section_revision_proposal_id(
        "acme_weekly",
        78,
        section_id="ws_nova",
        generated_at=generated_at,
    )

    proposal = _proposal(proposal_id, generated_at=generated_at)
    append_proposal(proposal, "acme", 78, programs_root=programs_root)
    append_proposal(
        SectionRevisionProposal(
            proposal_id=proposal.proposal_id,
            edition_id=proposal.edition_id,
            issue_number=proposal.issue_number,
            section_id=proposal.section_id,
            current_text=proposal.current_text,
            proposed_text=proposal.proposed_text,
            evidence_brief=proposal.evidence_brief,
            status=SectionRevisionStatus.ACCEPTED,
            generated_at=proposal.generated_at,
            resolved_at=datetime(2026, 5, 17, 10, 5, tzinfo=timezone.utc),
            accepted_text=proposal.proposed_text,
            source_hash=proposal.source_hash,
            ai_model_used=proposal.ai_model_used,
            ai_cost_usd=proposal.ai_cost_usd,
        ),
        "acme",
        78,
        programs_root=programs_root,
    )

    proposals = load_proposals("acme", 78, programs_root=programs_root)

    assert len(proposals) == 1
    assert proposals[0].status is SectionRevisionStatus.ACCEPTED
    assert proposals[0].accepted_text == "Updated narrative based on fresh evidence."


def test_load_proposals_filters_by_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_proposal(
        _proposal(
            "proposal-1",
            generated_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
            status=SectionRevisionStatus.PENDING,
        ),
        "acme",
        78,
        programs_root=programs_root,
    )
    append_proposal(
        _proposal(
            "proposal-2",
            generated_at=datetime(2026, 5, 17, 10, 1, tzinfo=timezone.utc),
            status=SectionRevisionStatus.REJECTED,
        ),
        "acme",
        78,
        programs_root=programs_root,
    )

    proposals = load_proposals(
        "acme",
        78,
        programs_root=programs_root,
        status_filter={SectionRevisionStatus.PENDING},
    )

    assert [proposal.proposal_id for proposal in proposals] == ["proposal-1"]


def test_update_proposal_status_appends_new_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _proposal(
        "proposal-1",
        generated_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
    )
    append_proposal(proposal, "acme", 78, programs_root=programs_root)

    updated = update_proposal_status(
        "proposal-1",
        SectionRevisionStatus.ACCEPTED_MODIFIED,
        accepted_text="Edited narrative after reviewer input.",
        program_id="acme",
        issue_number=78,
        resolved_at=datetime(2026, 5, 17, 10, 4, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert updated.status is SectionRevisionStatus.ACCEPTED_MODIFIED
    assert updated.accepted_text == "Edited narrative after reviewer input."
    assert load_proposals("acme", 78, programs_root=programs_root)[0].status is SectionRevisionStatus.ACCEPTED_MODIFIED


def test_supersede_pending_proposals_updates_only_pending_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_proposal(
        _proposal(
            "proposal-pending",
            generated_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
            status=SectionRevisionStatus.PENDING,
        ),
        "acme",
        78,
        programs_root=programs_root,
    )
    append_proposal(
        _proposal(
            "proposal-accepted",
            generated_at=datetime(2026, 5, 17, 10, 1, tzinfo=timezone.utc),
            status=SectionRevisionStatus.ACCEPTED,
        ),
        "acme",
        78,
        programs_root=programs_root,
    )

    superseded = supersede_pending_proposals(
        "acme",
        78,
        resolved_at=datetime(2026, 5, 17, 10, 3, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    proposals = {proposal.proposal_id: proposal for proposal in load_proposals("acme", 78, programs_root=programs_root)}

    assert [proposal.proposal_id for proposal in superseded] == ["proposal-pending"]
    assert proposals["proposal-pending"].status is SectionRevisionStatus.SUPERSEDED
    assert proposals["proposal-accepted"].status is SectionRevisionStatus.ACCEPTED


def test_load_proposals_keeps_legacy_records_without_optional_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": "proposal-legacy",
                "edition_id": "acme_weekly",
                "issue_number": 78,
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": None,
                "evidence_brief": {
                    "section_id": "ws_nova",
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": [101],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": ["sig-1"],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": "pending",
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    proposals = load_proposals("acme", 78, programs_root=programs_root)

    assert len(proposals) == 1
    assert proposals[0].accepted_text is None
    assert proposals[0].source_hash is None
    assert proposals[0].ai_model_used is None
    assert proposals[0].ai_cost_usd is None


def test_load_proposals_rejects_numeric_string_new_items(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": "proposal-bad-items",
                "edition_id": "acme_weekly",
                "issue_number": 78,
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": "Updated narrative based on fresh evidence.",
                "evidence_brief": {
                    "section_id": "ws_nova",
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": ["101"],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": ["sig-1"],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": "pending",
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="Expected integer entries in integer list fields."):
        load_proposals("acme", 78, programs_root=programs_root)


def test_load_proposals_rejects_non_string_top_signals(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": "proposal-bad-signals",
                "edition_id": "acme_weekly",
                "issue_number": 78,
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": "Updated narrative based on fresh evidence.",
                "evidence_brief": {
                    "section_id": "ws_nova",
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": [101],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": [123],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": "pending",
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="Expected string entries in string list fields."):
        load_proposals("acme", 78, programs_root=programs_root)


def test_load_proposals_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": "proposal-bad-issue",
                "edition_id": "acme_weekly",
                "issue_number": "78",
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": "Updated narrative based on fresh evidence.",
                "evidence_brief": {
                    "section_id": "ws_nova",
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": [101],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": ["sig-1"],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": "pending",
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_proposals("acme", 78, programs_root=programs_root)


def test_load_hint_proposals_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "hint_id": "hint-1",
                "edition": "acme_weekly",
                "issue_number": "78",
                "workstream_id": "acme",
                "hint_kind": "narrative",
                "suggested_sentence": "Focus on the latest risk movement.",
                "status": "pending",
                "accepted_text": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_hint_proposals("acme", 78, programs_root=programs_root)


def test_load_hint_proposals_rejects_non_string_hint_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "hint_id": 123,
                "edition": "acme_weekly",
                "issue_number": 78,
                "workstream_id": "acme",
                "hint_kind": "narrative",
                "suggested_sentence": "Focus on the latest risk movement.",
                "status": "pending",
                "accepted_text": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="hint_id must be a string"):
        load_hint_proposals("acme", 78, programs_root=programs_root)


def test_load_hint_proposals_rejects_non_string_accepted_text(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "hint_id": "hint-1",
                "edition": "acme_weekly",
                "issue_number": 78,
                "workstream_id": "acme",
                "hint_kind": "narrative",
                "suggested_sentence": "Focus on the latest risk movement.",
                "status": "pending",
                "accepted_text": 123,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="optional string field must be a string"):
        load_hint_proposals("acme", 78, programs_root=programs_root)


def test_load_proposals_rejects_non_string_proposal_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": 123,
                "edition_id": "acme_weekly",
                "issue_number": 78,
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": "Updated narrative based on fresh evidence.",
                "evidence_brief": {
                    "section_id": "ws_nova",
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": [101],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": ["sig-1"],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": "pending",
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="proposal_id must be a string"):
        load_proposals("acme", 78, programs_root=programs_root)


def test_load_proposals_rejects_non_string_evidence_section_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": "proposal-bad-evidence-section",
                "edition_id": "acme_weekly",
                "issue_number": 78,
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": "Updated narrative based on fresh evidence.",
                "evidence_brief": {
                    "section_id": 999,
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": [101],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": ["sig-1"],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": "pending",
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="evidence_brief.section_id must be a string"):
        load_proposals("acme", 78, programs_root=programs_root)


def test_load_proposals_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "proposal_id": "proposal-bad-status",
                "edition_id": "acme_weekly",
                "issue_number": 78,
                "section_id": "ws_nova",
                "current_text": "Current narrative.",
                "proposed_text": "Updated narrative based on fresh evidence.",
                "evidence_brief": {
                    "section_id": "ws_nova",
                    "ado_delta_summary": "1 risk changed.",
                    "new_items": [101],
                    "closed_items": [],
                    "risk_changed_items": [101],
                    "eta_changed_items": [],
                    "top_signals": ["sig-1"],
                    "kpi_summary": None,
                    "stale_claims": [],
                    "vitality_summary": "1 item scanned.",
                    "confidence": "high",
                },
                "status": 123,
                "generated_at": "2026-05-17T10:00:00+00:00",
                "resolved_at": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="status must be a string"):
        load_proposals("acme", 78, programs_root=programs_root)


def test_load_hint_proposals_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposals_path = get_proposals_path("acme", 78, programs_root=programs_root)
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        json.dumps(
            {
                "hint_id": "hint-1",
                "edition": "acme_weekly",
                "issue_number": 78,
                "workstream_id": "acme",
                "hint_kind": "narrative",
                "suggested_sentence": "Focus on the latest risk movement.",
                "status": 123,
                "accepted_text": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="status must be a string"):
        load_hint_proposals("acme", 78, programs_root=programs_root)


def _proposal(
    proposal_id: str,
    *,
    generated_at: datetime,
    status: SectionRevisionStatus = SectionRevisionStatus.PENDING,
) -> SectionRevisionProposal:
    return SectionRevisionProposal(
        proposal_id=proposal_id,
        edition_id="acme_weekly",
        issue_number=78,
        section_id="ws_nova",
        current_text="Current narrative.",
        proposed_text="Updated narrative based on fresh evidence.",
        evidence_brief=SectionEvidenceBrief(
            section_id="ws_nova",
            ado_delta_summary="1 risk changed, 1 ETA updated.",
            new_items=(101,),
            closed_items=(),
            risk_changed_items=(101,),
            eta_changed_items=(102,),
            top_signals=("sig-1", "sig-2"),
            kpi_summary="Deploy P50 4.2 hrs; Fleet Healthy 99.1%.",
            stale_claims=("claim-1",),
            vitality_summary="2 items scanned; 1 stale.",
            confidence=Confidence.HIGH,
        ),
        status=status,
        generated_at=generated_at,
        resolved_at=None,
        accepted_text=None,
        rejection_reason=None,
        source_hash="sha256:source",
        ai_model_used="gpt-5.4",
        ai_cost_usd=0.12,
    )
