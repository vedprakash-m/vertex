from __future__ import annotations

from dataclasses import fields
from typing import get_type_hints

from src.core.cascade_detector import DependencyCascade
from src.core.coverage_gap import CoverageGap
from src.core.dependency_scout import DependencyProposal
from src.core.forecast_engine import ETAForecast, ForecastAssessment
from src.core.intervention_ranker import InterventionProposal
from src.core.models import Confidence
from src.core.models_v2 import (
    CatchupEvent,
    Contradiction,
    ContradictionPacket,
    ForecastCalibrationModifier,
    HygieneCoverageAlert,
    IncidentEntry,
    MilestoneAssessment,
    SectionEvidenceBrief,
    WorkstreamSynthesis,
)
from src.core.triage import CorrelatedTriageItem, IncidentLearning, StaleNarrativeFinding


def test_zone_a_advisory_outputs_carry_standard_confidence_field() -> None:
    advisory_types = (
        CoverageGap,
        DependencyCascade,
        DependencyProposal,
        ETAForecast,
        ForecastAssessment,
        CatchupEvent,
        Contradiction,
        ContradictionPacket,
        ForecastCalibrationModifier,
        HygieneCoverageAlert,
        IncidentEntry,
        MilestoneAssessment,
        SectionEvidenceBrief,
        WorkstreamSynthesis,
        CorrelatedTriageItem,
        StaleNarrativeFinding,
        IncidentLearning,
        InterventionProposal,
    )

    for advisory_type in advisory_types:
        field_names = {field.name for field in fields(advisory_type)}
        assert "confidence" in field_names, f"{advisory_type.__name__} is missing a confidence field"
        assert get_type_hints(advisory_type).get("confidence") is Confidence

