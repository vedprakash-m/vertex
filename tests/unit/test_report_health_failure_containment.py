from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.commands import report_health
from src.core.exceptions import ConfigError, StateError
from src.core.models import AttributionTier, Confidence, DimensionRisk, EvidencePacket, RiskLevel
from src.core.pipeline import StageContext
from src.core.stages import render_stage
from tests.unit.test_report_deck_reality_consolidation import _risk_assessment


AS_OF = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _dimension(risk: RiskLevel) -> DimensionRisk:
    return DimensionRisk(
        name="Delivery", risk=risk, summary="Synthetic dimension",
        evidence=EvidencePacket(
            work_item_id=1, revisions=(), comments=(), enrichments=(), confidence=Confidence.HIGH,
            tier=AttributionTier.TIER1, summary_for_reviewer="Synthetic evidence",
        ),
    )


@pytest.mark.parametrize("risk", [RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.DONE])
@pytest.mark.parametrize("failure_stage", ["load", "risks", "injected"])
def test_failed_risk_access_never_returns_a_health_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, risk: RiskLevel, failure_stage: str,
) -> None:
    failure = ConfigError("Risk authority invalid; preserve state and repair configuration.")
    reality = Mock()
    reality.risks.side_effect = failure
    loader = Mock(side_effect=failure) if failure_stage == "load" else Mock(return_value=reality)
    monkeypatch.setattr(report_health.ProgramReality, "load", loader)
    with pytest.raises(ConfigError) as raised:
        report_health._build_health_summary(
            (_dimension(risk),), None, program_id="fixture", programs_root=tmp_path / "programs",
            archive_root=tmp_path / "archive", as_of=AS_OF, healthy_streak=5,
            reality=reality if failure_stage == "injected" else None,
        )
    assert raised.value is failure
    if failure_stage == "injected":
        loader.assert_not_called()
    else:
        loader.assert_called_once_with(
            "fixture", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive",
        )
    assert not list(tmp_path.iterdir())


def test_successful_empty_risk_register_preserves_valid_low_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reality = Mock()
    reality.risks.return_value = ()
    monkeypatch.setattr(report_health.ProgramReality, "load", Mock(return_value=reality))
    health = report_health._build_health_summary(
        (_dimension(RiskLevel.LOW),), None, program_id="fixture",
        programs_root=tmp_path / "programs", as_of=AS_OF, healthy_streak=5,
    )
    assert health.overall_risk is RiskLevel.LOW
    assert health.healthy_streak == 5
    reality.risks.assert_called_once_with()
    assert not list(tmp_path.iterdir())


def test_explicit_known_risk_and_assessment_do_not_trigger_an_unrelated_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = Mock(side_effect=AssertionError("Use the explicitly supplied risk assessment"))
    monkeypatch.setattr(report_health.ProgramReality, "load", loader)
    assessment = _risk_assessment()
    health = report_health._build_health_summary(
        (_dimension(RiskLevel.HIGH),), None, risks=(assessment.record,), risk_assessments=(assessment,),
        program_id="fixture", programs_root=tmp_path / "programs", as_of=AS_OF,
    )
    assert health.overall_risk is RiskLevel.HIGH
    assert health.risk_register_truth_level == assessment.truth_level.value
    assert health.risk_register_disputed is True
    loader.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error_type", [ConfigError, StateError, RuntimeError])
def test_render_health_does_not_replace_typed_risk_access_failure_with_legacy_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
) -> None:
    failure = error_type("Synthetic risk-access failure")
    monkeypatch.setattr(report_health.ProgramReality, "load", Mock(side_effect=failure))
    legacy = Mock(side_effect=AssertionError("Do not bypass failed risk authority"))
    monkeypatch.setattr(render_stage, "_load_current_risks", legacy)
    ctx = StageContext(
        programs_root=tmp_path / "programs", archive_root=tmp_path / "archive",
        data_as_of=AS_OF, risk_assessments=(),
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="fixture")),
    )
    support = SimpleNamespace(build_health_summary=report_health._build_health_summary)
    with pytest.raises(error_type) as raised:
        render_stage._build_health_summary_with_risk_fallback(
            ctx, support, (_dimension(RiskLevel.LOW),), None, program_id="fixture",
            programs_root=tmp_path / "programs", archive_root=tmp_path / "archive", as_of=AS_OF,
        )
    assert raised.value is failure
    legacy.assert_not_called()
    assert not list(tmp_path.iterdir())
