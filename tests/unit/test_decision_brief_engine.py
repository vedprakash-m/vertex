from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.decision_brief_engine import (
    DecisionBrief,
    DecisionItem,
    _format_delta_lines,
    _resolve_signal,
    _section_title,
    build_decision_brief,
)
from src.core.models import Confidence
from src.core.models_v2 import (
    SectionEvidenceBrief,
    SectionRevisionProposal,
    SectionRevisionStatus,
    Signal,
)


_TS = datetime(2026, 5, 30, 10, 0, 0, tzinfo=timezone.utc)


def _make_brief(
    *,
    section_id: str = "exec_summary",
    ado_delta_summary: str = "",
    new_items: tuple[int, ...] = (),
    closed_items: tuple[int, ...] = (),
    risk_changed_items: tuple[int, ...] = (),
    eta_changed_items: tuple[int, ...] = (),
    top_signals: tuple[str, ...] = (),
    vitality_summary: str = "good",
    confidence: Confidence = Confidence.HIGH,
) -> SectionEvidenceBrief:
    return SectionEvidenceBrief(
        section_id=section_id,
        ado_delta_summary=ado_delta_summary,
        new_items=new_items,
        closed_items=closed_items,
        risk_changed_items=risk_changed_items,
        eta_changed_items=eta_changed_items,
        top_signals=top_signals,
        kpi_summary=None,
        stale_claims=(),
        vitality_summary=vitality_summary,
        confidence=confidence,
    )


def _make_proposal(
    *,
    section_id: str = "exec_summary",
    current_text: str = "Status is good.",
    proposed_text: str | None = None,
    status: SectionRevisionStatus = SectionRevisionStatus.PENDING,
    evidence_brief: SectionEvidenceBrief | None = None,
) -> SectionRevisionProposal:
    return SectionRevisionProposal(
        proposal_id=f"prop-{section_id}",
        edition_id="acme_weekly",
        issue_number=79,
        section_id=section_id,
        current_text=current_text,
        proposed_text=proposed_text,
        evidence_brief=evidence_brief or _make_brief(section_id=section_id),
        status=status,
        generated_at=_TS,
    )


def _make_signal(signal_id: str, text: str) -> Signal:
    return Signal(
        id=signal_id,
        timestamp=_TS,
        source="ado",
        program_id="acme",
        workstream_id=None,
        entity_refs=(),
        text=text,
        raw_ref=signal_id,
        confidence=Confidence.HIGH,
        metadata={},
    )


class TestSectionTitle:
    def test_exec_summary(self) -> None:
        assert _section_title("exec_summary") == "Executive Summary"

    def test_workstream_prefix_stripped(self) -> None:
        assert _section_title("ws:deployment_safety") == "Deployment Safety"

    def test_hyphens_normalized(self) -> None:
        assert _section_title("contoso-pilot-readiness") == "Contoso Pilot Readiness"

    def test_empty_falls_back_to_id(self) -> None:
        assert _section_title("") == ""


class TestFormatDeltaLines:
    def test_no_changes_gives_placeholder(self) -> None:
        lines = _format_delta_lines(
            new_items=(),
            closed_items=(),
            risk_changed_items=(),
            eta_changed_items=(),
            ado_delta_summary="",
        )
        assert lines == ("No ADO changes in evidence window.",)

    def test_new_items_listed(self) -> None:
        lines = _format_delta_lines(
            new_items=(111, 222),
            closed_items=(),
            risk_changed_items=(),
            eta_changed_items=(),
            ado_delta_summary="",
        )
        assert any("WI-111" in line for line in lines)
        assert any("2 new" in line for line in lines)

    def test_truncates_at_five_items(self) -> None:
        items = tuple(range(1, 9))
        lines = _format_delta_lines(
            new_items=items,
            closed_items=(),
            risk_changed_items=(),
            eta_changed_items=(),
            ado_delta_summary="",
        )
        new_line = next(line for line in lines if "new" in line)
        assert "..." in new_line

    def test_ado_delta_summary_appended(self) -> None:
        lines = _format_delta_lines(
            new_items=(),
            closed_items=(),
            risk_changed_items=(),
            eta_changed_items=(),
            ado_delta_summary="3 items changed state to Active",
        )
        assert "3 items changed state" in lines[-1]

    def test_multiple_change_types(self) -> None:
        lines = _format_delta_lines(
            new_items=(1,),
            closed_items=(2,),
            risk_changed_items=(3,),
            eta_changed_items=(4,),
            ado_delta_summary="",
        )
        assert len(lines) == 4


class TestResolveSignal:
    def test_signal_found(self) -> None:
        sig = _make_signal("sig-1", "WI 1234 State: Active -> Closed")
        result = _resolve_signal("sig-1", signal_map={"sig-1": sig})
        assert result.text == "WI 1234 State: Active -> Closed"
        assert result.source == "ado"
        assert result.timestamp == "2026-05-30"

    def test_signal_not_found_returns_placeholder(self) -> None:
        result = _resolve_signal("missing-sig", signal_map={})
        assert result.signal_id == "missing-sig"
        assert "unavailable" in result.text
        assert result.source is None


class TestBuildDecisionBrief:
    def test_filters_only_pending_proposals(self) -> None:
        proposals = (
            _make_proposal(section_id="exec_summary", status=SectionRevisionStatus.PENDING),
            _make_proposal(section_id="ws:dep_safety", status=SectionRevisionStatus.ACCEPTED),
            _make_proposal(section_id="ws:networking", status=SectionRevisionStatus.REJECTED),
        )
        brief = build_decision_brief(
            proposals=proposals,
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
        )
        assert brief.total_pending == 1
        assert brief.items[0].section_id == "exec_summary"

    def test_returns_all_pending(self) -> None:
        proposals = tuple(
            _make_proposal(section_id=f"ws:section_{i}")
            for i in range(5)
        )
        brief = build_decision_brief(
            proposals=proposals,
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
        )
        assert brief.total_pending == 5

    def test_ai_enriched_false_by_default(self) -> None:
        brief = build_decision_brief(
            proposals=(_make_proposal(),),
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
        )
        assert brief.ai_enriched is False

    def test_signals_resolved_to_text(self) -> None:
        sig = _make_signal("sig-abc", "WI 999 ETA changed")
        proposals = (
            _make_proposal(
                evidence_brief=_make_brief(top_signals=("sig-abc",))
            ),
        )
        brief = build_decision_brief(
            proposals=proposals,
            signal_map={"sig-abc": sig},
            edition_name="acme_weekly",
            issue_number=79,
        )
        assert brief.items[0].top_signals[0].text == "WI 999 ETA changed"

    def test_no_pending_yields_empty_brief(self) -> None:
        proposals = (
            _make_proposal(status=SectionRevisionStatus.ACCEPTED),
        )
        brief = build_decision_brief(
            proposals=proposals,
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
        )
        assert brief.total_pending == 0
        assert brief.items == ()

    def test_commands_contain_edition_and_section(self) -> None:
        proposal = _make_proposal(section_id="exec_summary")
        brief = build_decision_brief(
            proposals=(proposal,),
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
        )
        item = brief.items[0]
        assert "acme_weekly" in item.accept_command
        assert "exec_summary" in item.accept_command
        assert "acme_weekly" in item.reject_command

    def test_section_title_resolved(self) -> None:
        brief = build_decision_brief(
            proposals=(_make_proposal(section_id="exec_summary"),),
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
        )
        assert brief.items[0].section_title == "Executive Summary"

    def test_generated_at_set(self) -> None:
        ts = datetime(2026, 5, 30, 12, 0, 0)
        brief = build_decision_brief(
            proposals=(_make_proposal(),),
            signal_map={},
            edition_name="acme_weekly",
            issue_number=79,
            generated_at=ts,
        )
        assert "2026-05-30" in brief.generated_at
