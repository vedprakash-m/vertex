"""Tests for cross-source evidence conflict detection (P4-10, §7.8)."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.evidence_conflict_detector import detect_evidence_conflicts
from src.core.evidence_models import WorkstreamEvidence
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import Signal

_AS_OF = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)


def _evidence(*, lane_id: str = "deployment", risk_level: RiskLevel = RiskLevel.MEDIUM) -> WorkstreamEvidence:
    return WorkstreamEvidence(
        lane_id=lane_id,
        synthesized_at=_AS_OF,
        risk_level=risk_level,
        etas=(),
        blocking_items=(),
        owners=(),
        source_refs=(),
        raw_excerpts=(),
        confidence=0.8,
        narrative_summary="narrative",
    )


def _icm(*, incident_id: str, severity: int) -> Signal:
    return Signal(
        id=f"icm/incident/x/{incident_id}",
        timestamp=_AS_OF,
        source="icm",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(f"icm:{incident_id}",),
        text=f"[Sev {severity}] Gen9 burn-in blocked",
        raw_ref="icm:x",
        confidence=Confidence.HIGH,
        metadata={"incident_id": incident_id, "severity": severity, "owning_team": "Acme-Infra"},
    )


def _kusto(*, text: str) -> Signal:
    return Signal(
        id="kusto/x/2026-06-18",
        timestamp=_AS_OF,
        source="kusto",
        program_id="acme",
        workstream_id="deployment",
        entity_refs=(),
        text=text,
        raw_ref="kusto:x",
        confidence=Confidence.HIGH,
    )


def test_icm_sev2_vs_nonblocking_evidence_flags_conflict() -> None:
    conflicts = detect_evidence_conflicts(
        m365_evidence=_evidence(risk_level=RiskLevel.MEDIUM),
        icm_blockers=(_icm(incident_id="771996570", severity=2),),
    )
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.family == "icm_vs_evidence_risk"
    assert c.open is True
    assert "IcM:771996570" in c.entity_refs
    assert "Sev2" in c.description


def test_icm_sev1_vs_done_evidence_flags_conflict() -> None:
    conflicts = detect_evidence_conflicts(
        m365_evidence=_evidence(risk_level=RiskLevel.DONE),
        icm_blockers=(_icm(incident_id="111", severity=1),),
    )
    assert len(conflicts) == 1
    assert conflicts[0].family == "icm_vs_evidence_risk"


def test_icm_sev2_vs_already_blocked_evidence_no_conflict() -> None:
    """When evidence risk already acknowledges blocking (blocked/high), no conflict."""
    for level in (RiskLevel.BLOCKED, RiskLevel.HIGH):
        conflicts = detect_evidence_conflicts(
            m365_evidence=_evidence(risk_level=level),
            icm_blockers=(_icm(incident_id="222", severity=2),),
        )
        assert conflicts == (), f"unexpected conflict for {level}"


def test_icm_sev3_does_not_trigger_conflict() -> None:
    """Only Sev1/Sev2 are treated as authoritative blockers."""
    conflicts = detect_evidence_conflicts(
        m365_evidence=_evidence(risk_level=RiskLevel.LOW),
        icm_blockers=(_icm(incident_id="333", severity=3),),
    )
    assert conflicts == ()


def test_blocked_evidence_vs_kusto_progression_flags_conflict() -> None:
    conflicts = detect_evidence_conflicts(
        m365_evidence=_evidence(risk_level=RiskLevel.BLOCKED),
        kusto_metrics=(_kusto(text="SCHIE compliance increasing to 73.4%."),),
    )
    assert len(conflicts) == 1
    assert conflicts[0].family == "blocked_vs_kusto_progress"
    assert "progression" in conflicts[0].description


def test_blocked_evidence_vs_kusto_no_progression_no_conflict() -> None:
    conflicts = detect_evidence_conflicts(
        m365_evidence=_evidence(risk_level=RiskLevel.BLOCKED),
        kusto_metrics=(_kusto(text="Kusto query x: 0 rows observed."),),
    )
    assert conflicts == ()


def test_no_evidence_means_no_conflicts() -> None:
    assert detect_evidence_conflicts(
        m365_evidence=None,
        icm_blockers=(_icm(incident_id="444", severity=1),),
        kusto_metrics=(_kusto(text="increasing"),),
    ) == ()


def test_both_patterns_can_coexist() -> None:
    """Sev2 IcM (non-blocking risk) AND blocked-vs-kusto cannot both fire for the
    same evidence (Sev2 path requires non-blocking; kusto path requires blocked).
    But two distinct IcM blockers + a progression kusto on blocked evidence: only
    the kusto path fires (risk is blocked)."""
    conflicts = detect_evidence_conflicts(
        m365_evidence=_evidence(risk_level=RiskLevel.BLOCKED),
        icm_blockers=(_icm(incident_id="555", severity=2),),
        kusto_metrics=(_kusto(text="rollout improving"),),
    )
    # risk is blocked → IcM path does not fire; kusto progression does.
    assert len(conflicts) == 1
    assert conflicts[0].family == "blocked_vs_kusto_progress"