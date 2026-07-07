from __future__ import annotations

from datetime import date
import pytest

from src.core.models import Confidence, DeltaKind, DeltaSet, ItemDelta, WorkItem, RiskLevel, EvidencePacket, AttributionTier
from src.core.forecast_engine import ETAForecast
from src.core.scorecard_trends import ScorecardTrend
from src.core.ado_narrative_hint_engine import HintKind, generate_delta_hints
from src.core.models_v2 import Program


def _empty_evidence(work_item_id: int) -> EvidencePacket:
    return EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.NONE,
        tier=AttributionTier.TIER3,
        summary_for_reviewer="Dummy",
    )


def _program(program_id: str = "acme") -> Program:
    return Program(schema_version="2.0", id=program_id, name=program_id.upper())


def test_generate_delta_hints() -> None:
    # 1. Mock Work Items
    items = {
        101: WorkItem(
            id=101,
            type="Task",
            title="Fix bios issue",
            state="Closed",
            assigned_to="Alice Testowner",
            assigned_to_email="alice.testowner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Adventure\\Iteration1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.DONE,
            tags=[],
            custom_fields={},
        ),
        102: WorkItem(
            id=102,
            type="Task",
            title="Implement Wingtip feature",
            state="In Progress",
            assigned_to="Bob Testdev",
            assigned_to_email="bob.testdev@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Adventure\\Iteration1",
            target_date=date(2026, 6, 15),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={},
        ),
        103: WorkItem(
            id=103,
            type="Task",
            title="Deploy safety check",
            state="In Progress",
            assigned_to="Carol Testeng",
            assigned_to_email="carol.testeng@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="One\\Adventure\\Iteration1",
            target_date=date(2026, 6, 20),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
        ),
    }

    # 2. Mock DeltaSet
    delta_set = DeltaSet(
        issue_number=80,
        previous_issue_number=79,
        new_items=(
            ItemDelta(
                work_item_id=103,
                kind=DeltaKind.NEW,
                field_changes={},
                old_risk=None,
                new_risk=RiskLevel.MEDIUM,
                old_eta=None,
                new_eta=date(2026, 6, 20),
                evidence=_empty_evidence(103),
            ),
        ),
        closed_items=(
            ItemDelta(
                work_item_id=101,
                kind=DeltaKind.CLOSED,
                field_changes={"state": ("In Progress", "Closed")},
                old_risk=RiskLevel.LOW,
                new_risk=RiskLevel.DONE,
                old_eta=date(2026, 6, 1),
                new_eta=date(2026, 6, 1),
                evidence=_empty_evidence(101),
            ),
        ),
        risk_changes=(
            ItemDelta(
                work_item_id=102,
                kind=DeltaKind.RISK_UP,
                field_changes={"risk_level": ("medium", "high")},
                old_risk=RiskLevel.MEDIUM,
                new_risk=RiskLevel.HIGH,
                old_eta=date(2026, 6, 15),
                new_eta=date(2026, 6, 15),
                evidence=_empty_evidence(102),
            ),
        ),
        eta_changes=(
            ItemDelta(
                work_item_id=102,
                kind=DeltaKind.ETA_CHANGED,
                field_changes={"target_date": ("2026-06-01", "2026-06-15")},
                old_risk=RiskLevel.HIGH,
                new_risk=RiskLevel.HIGH,
                old_eta=date(2026, 6, 1),
                new_eta=date(2026, 6, 15),
                evidence=_empty_evidence(102),
            ),
        ),
        owner_changes=(),
        unchanged_count=0,
    )

    # 3. Mock forecasts
    forecasts = {
        102: ETAForecast(
            work_item_id=102,
            ado_target_date=date(2026, 6, 15),
            predicted_target_date=date(2026, 6, 25),
            confidence=Confidence.MEDIUM,
            slip_probability=0.85,
            reasoning="3 prior slips",
            prior_slips=3,
        ),
    }

    # 4. Mock trends
    trends = {
        ("Acme Readiness", "Deployment Velocity"): ScorecardTrend(
            current_risk=RiskLevel.HIGH,
            prior_risk=RiskLevel.HIGH,
            history=(RiskLevel.HIGH, RiskLevel.HIGH, RiskLevel.HIGH),
            direction="worsening",
            consecutive_high_count=3,
            annotation="High for 3 consecutive issues.",
        ),
    }

    hints = generate_delta_hints(
        delta_set=delta_set,
        items=items,
        issue_number=80,
        program=_program(),
        forecasts=forecasts,
        trends=trends,
    )

    # Verify closed hint
    closed_hint = next(h for h in hints if h.hint_kind == HintKind.CLOSED)
    assert closed_hint.work_item_id == 101
    assert closed_hint.suggested_sentence == "Fix bios issue is now closed."
    assert closed_hint.confidence == Confidence.HIGH

    # Verify risk up hint
    risk_hint = next(h for h in hints if h.hint_kind == HintKind.RISK_UP)
    assert risk_hint.work_item_id == 102
    assert risk_hint.suggested_sentence == "Implement Wingtip feature elevated to high (was medium)."
    assert risk_hint.confidence == Confidence.HIGH

    # Verify eta changed hint with forecast annotation
    eta_hint = next(h for h in hints if h.hint_kind == HintKind.ETA_CHANGED)
    assert eta_hint.work_item_id == 102
    assert "Target date for Implement Wingtip feature moved from 2026-06-01 to 2026-06-15" in eta_hint.suggested_sentence
    assert "medium confidence — 3 prior slips, 85% miss probability" in eta_hint.suggested_sentence

    # Verify new hint
    new_hint = next(h for h in hints if h.hint_kind == HintKind.NEW)
    assert new_hint.work_item_id == 103
    assert new_hint.suggested_sentence == "New item tracked: Deploy safety check (risk: medium, target: 2026-06-20)."
    assert new_hint.confidence == Confidence.MEDIUM

    # Verify scorecard trend hint
    trend_hint = next(h for h in hints if h.hint_kind == HintKind.TREND_WORSENING)
    assert trend_hint.suggested_sentence == "Deployment Velocity is on a worsening trajectory (3 consecutive high)."


def test_generate_delta_hints_with_pr_and_icm_signals() -> None:
    from src.core.models_v2 import Signal
    from datetime import datetime, timezone
    
    # Mock some PR and IcM signals
    pr_merged = Signal(
        id="ado/pr/repo-adventure/124/completed/ws-y",
        timestamp=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
        source="ado",
        program_id="acme",
        workstream_id="ws-y",
        entity_refs=("pr:124", "ado/pr:124"),
        text="PR 124 Merged: Implement Wingtip speedup merged to refs/heads/release/66",
        raw_ref="ado/pr/repo-adventure/124/completed/ws-y",
        confidence=Confidence.HIGH,
        metadata={
            "pr_id": 124,
            "title": "Implement Wingtip speedup",
            "status": "completed",
            "created_by": "Bob Testdev",
            "target_ref": "refs/heads/release/66",
            "source_ref": "refs/heads/feature/wingtip",
            "url": "https://weburl/124",
            "repository_id": "repo-adventure",
            "kind": "PR_MERGED",
        }
    )
    icm_active = Signal(
        id="icm/incident/somehash/ws-z",
        timestamp=datetime(2026, 5, 24, 10, tzinfo=timezone.utc),
        source="icm",
        program_id="acme",
        workstream_id="ws-z",
        entity_refs=("icm:767811306",),
        text="[Sev 2] CA connectivity issue",
        raw_ref="icm/incident/somehash",
        confidence=Confidence.HIGH,
        metadata={
            "incident_id": "767811306",
            "severity": 2,
            "status": "active",
            "owning_team": "Adventure Contoso",
        }
    )
    
    delta_set = DeltaSet(
        issue_number=80,
        previous_issue_number=79,
        new_items=(),
        closed_items=(),
        risk_changes=(),
        eta_changes=(),
        owner_changes=(),
        unchanged_count=0,
    )
    
    hints = generate_delta_hints(
        delta_set=delta_set,
        items={},
        issue_number=80,
        program=_program(),
        signals=(pr_merged, icm_active),
    )
    
    assert len(hints) == 2
    
    pr_hint = next(h for h in hints if h.hint_kind == HintKind.PR_MERGED)
    assert pr_hint.work_item_id == 124
    assert pr_hint.suggested_sentence == "Implement Wingtip speedup merged to refs/heads/release/66."
    assert pr_hint.workstream_id == "ws-y"
    
    icm_hint = next(h for h in hints if h.hint_kind == HintKind.ICM_ACTIVE)
    assert icm_hint.work_item_id == 767811306
    assert icm_hint.suggested_sentence == "IcM 767811306 (Sev2) is active: CA connectivity issue. Workstream: ws-z."
    assert icm_hint.workstream_id == "ws-z"

def test_generate_delta_hints_skips_malformed_pr_and_icm_signals() -> None:
    from src.core.models_v2 import Signal
    from datetime import datetime, timezone

    empty_delta = DeltaSet(
        issue_number=80,
        previous_issue_number=79,
        new_items=(),
        closed_items=(),
        risk_changes=(),
        eta_changes=(),
        owner_changes=(),
        unchanged_count=0,
    )

    def _sig(source: str, metadata: dict, text: str = "x") -> Signal:
        return Signal(
            id=f"{source}/malformed",
            timestamp=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
            source=source,
            program_id="acme",
            workstream_id="ws-y",
            entity_refs=(),
            text=text,
            raw_ref=f"{source}/malformed",
            confidence=Confidence.HIGH,
            metadata=metadata,
        )

    pr_no_title = _sig("ado", {"kind": "PR_MERGED", "pr_id": 1})
    pr_no_id = _sig("ado", {"kind": "PR_MERGED", "title": "Has title"})
    icm_no_id = _sig("icm", {"status": "active", "severity": 2}, text="[Sev 2] no id")
    icm_nonstr_status = _sig("icm", {"incident_id": "9", "status": 500}, text="numeric status")

    hints = generate_delta_hints(
        delta_set=empty_delta,
        items={},
        issue_number=80,
        program=_program(),
        signals=(pr_no_title, pr_no_id, icm_no_id, icm_nonstr_status),
    )
    # All four are malformed/unactionable -> no hints, and crucially no exception raised.
    assert hints == []


def test_generate_delta_hints_valid_pr_still_emitted_alongside_malformed() -> None:
    from src.core.models_v2 import Signal
    from datetime import datetime, timezone

    empty_delta = DeltaSet(
        issue_number=80,
        previous_issue_number=79,
        new_items=(),
        closed_items=(),
        risk_changes=(),
        eta_changes=(),
        owner_changes=(),
        unchanged_count=0,
    )
    valid = Signal(
        id="ado/pr/valid",
        timestamp=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
        source="ado",
        program_id="acme",
        workstream_id="ws-y",
        entity_refs=(),
        text="ok",
        raw_ref="ado/pr/valid",
        confidence=Confidence.HIGH,
        metadata={"kind": "PR_MERGED", "pr_id": 9, "title": "Real PR", "target_ref": "main"},
    )
    malformed = Signal(
        id="ado/pr/bad",
        timestamp=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
        source="ado",
        program_id="acme",
        workstream_id="ws-y",
        entity_refs=(),
        text="bad",
        raw_ref="ado/pr/bad",
        confidence=Confidence.HIGH,
        metadata={"kind": "PR_MERGED", "title": None, "pr_id": None},
    )
    hints = generate_delta_hints(
        delta_set=empty_delta,
        items={},
        issue_number=80,
        program=_program(),
        signals=(malformed, valid),
    )
    assert len(hints) == 1
    assert hints[0].work_item_id == 9
    assert hints[0].suggested_sentence == "Real PR merged to main."
