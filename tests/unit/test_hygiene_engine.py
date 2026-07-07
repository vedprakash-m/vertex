from __future__ import annotations

from src.core.hygiene_engine import evaluate_hygiene
from src.core.models import AttributionTier, Confidence, DimensionRisk, EvidencePacket, RiskLevel
from src.core.view_models import Citation


def test_evaluate_hygiene_flags_missing_citations_and_missing_summaries() -> None:
    warnings = evaluate_hygiene(
        workstream_blurbs={"deployment": "Deployment blurb."},
        workstream_citations={"deployment": ()},
        exec_summary_text="Executive summary.",
        exec_summary_citations=(),
        scorecard=(
            DimensionRisk(
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                summary="",
                evidence=EvidencePacket(
                    work_item_id=1,
                    revisions=(),
                    comments=(),
                    enrichments=(),
                    confidence=Confidence.MEDIUM,
                    tier=AttributionTier.TIER1,
                    summary_for_reviewer="Evidence",
                ),
            ),
            DimensionRisk(
                name="SCHIE Gaps",
                risk=RiskLevel.HIGH,
                summary="Rendered summary.",
                evidence=EvidencePacket(
                    work_item_id=2,
                    revisions=(),
                    comments=(),
                    enrichments=(),
                    confidence=Confidence.NONE,
                    tier=AttributionTier.TIER3,
                    summary_for_reviewer="None",
                ),
            ),
        ),
    )

    assert "Workstream 'deployment' has prose but no citations." in warnings
    assert "Executive summary has prose but no citations." in warnings
    assert "Scorecard dimension 'Deployment Velocity' is missing a summary." in warnings
    assert any("confidence NONE" in warning for warning in warnings)
