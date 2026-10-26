from __future__ import annotations

from dataclasses import replace
from itertools import product
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.commands import escalate
from src.commands.report_pipeline import assemble_stage
from src.core.config_loader import ReportBundle, ReportConfig, ScorecardDimensionSettings, ScorecardSettings
from src.core.models import RiskLevel, ScorecardEvidencePacket
from src.core.overrides_store import OverridesDocument


_RANK = {
    RiskLevel.UNKNOWN: 0,
    RiskLevel.DONE: 1,
    RiskLevel.LOW: 2,
    RiskLevel.MEDIUM: 3,
    RiskLevel.HIGH: 4,
    RiskLevel.BLOCKED: 5,
}


@pytest.mark.parametrize(("prior", "current"), list(product(_RANK, repeat=2)))
def test_report_comparison_uses_two_real_known_samples(prior: RiskLevel, current: RiskLevel) -> None:
    sparkline, label = assemble_stage._derive_risk_sparkline(current, prior)
    if RiskLevel.UNKNOWN in {prior, current}:
        assert (sparkline, label) == (None, None)
        return
    assert sparkline == assemble_stage._spark_char(prior) + assemble_stage._spark_char(current)
    if _RANK[current] > _RANK[prior]:
        assert label == "rising vs prior issue"
    elif _RANK[current] < _RANK[prior]:
        assert label == "falling vs prior issue"
    else:
        assert label == "unchanged vs prior issue"


@pytest.mark.parametrize("current", list(_RANK))
def test_report_without_prior_observation_has_no_trend(current: RiskLevel) -> None:
    assert assemble_stage._derive_risk_sparkline(current, None) == (None, None)


@pytest.mark.parametrize(
    ("history", "label"),
    [
        ((RiskLevel.LOW, RiskLevel.HIGH), "net rise across 2 observations"),
        ((RiskLevel.HIGH, RiskLevel.LOW), "net fall across 2 observations"),
        ((RiskLevel.LOW, RiskLevel.LOW), "no net change across 2 observations"),
        ((RiskLevel.HIGH, RiskLevel.HIGH), "high across 2 observations"),
        ((RiskLevel.HIGH,) * 4, "high across 4 observations"),
        ((RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.LOW), "no net change across 3 observations"),
        ((RiskLevel.DONE, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH), "net rise across 4 observations"),
        ((RiskLevel.HIGH, RiskLevel.BLOCKED), "net rise across 2 observations"),
        ((RiskLevel.BLOCKED, RiskLevel.HIGH), "net fall across 2 observations"),
    ],
)
def test_escalation_history_labels_real_observations_not_elapsed_weeks(
    history: tuple[RiskLevel, ...], label: str
) -> None:
    sparkline, actual_label = escalate._history_risk_trend(history)
    assert sparkline == "".join(assemble_stage._spark_char(level) for level in history)
    assert actual_label == label


def test_escalation_history_uses_only_the_last_four_actual_samples() -> None:
    history = (RiskLevel.HIGH, RiskLevel.LOW, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)
    sparkline, label = escalate._history_risk_trend(history)
    assert sparkline == "".join(assemble_stage._spark_char(level) for level in history[-4:])
    assert label == "net rise across 4 observations"


def test_unknown_outside_the_displayed_history_does_not_hide_known_samples() -> None:
    history = (RiskLevel.UNKNOWN, RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.BLOCKED)
    sparkline, label = escalate._history_risk_trend(history)
    assert sparkline == "".join(assemble_stage._spark_char(level) for level in history[-4:])
    assert label == "net rise across 4 observations"


@pytest.mark.parametrize(
    "history",
    [
        (), (RiskLevel.HIGH,), (RiskLevel.LOW,), (RiskLevel.UNKNOWN,),
        (RiskLevel.UNKNOWN, RiskLevel.HIGH),
        (RiskLevel.HIGH, RiskLevel.UNKNOWN),
        (RiskLevel.LOW, RiskLevel.UNKNOWN, RiskLevel.HIGH),
    ],
)
def test_escalation_does_not_infer_a_trend_from_insufficient_or_unknown_samples(
    history: tuple[RiskLevel, ...],
) -> None:
    assert escalate._history_risk_trend(history) == (None, None)


def test_report_scorecard_consumer_retains_real_pair_and_comparison_label(tmp_path: Path) -> None:
    from src.core.scorecard_engine import build_scorecard

    dimension = ScorecardDimensionSettings(name="Synthetic dimension", description=None, ado_filter="")
    settings = ScorecardSettings(name="Synthetic scorecard", dimensions=(dimension,))
    bundle = Mock(spec=ReportBundle)
    bundle.config = Mock(spec=ReportConfig)
    bundle.config.scorecards = (settings,)
    bundle.config.scorecard_sort = "risk_desc"
    bundle.slice_contracts = ()
    packet = build_scorecard(items=(), dimensions=settings.dimensions, prev_confirmed=None)[0]
    packet = replace(packet, prior_confirmed_risk=RiskLevel.LOW, derived_risk=RiskLevel.HIGH)
    packets: dict[str, dict[str, ScorecardEvidencePacket]] = {settings.name: {dimension.name: packet}}

    scorecards, dimensions, _deltas = assemble_stage._build_scorecard_data(
        bundle, (), {}, packets, OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        reports_root=tmp_path / "reports",
    )

    assert len(dimensions) == 1
    assert dimensions[0].risk_sparkline == (
        assemble_stage._spark_char(RiskLevel.LOW) + assemble_stage._spark_char(RiskLevel.HIGH)
    )
    assert dimensions[0].trend_label == "rising vs prior issue"
    assert scorecards[0].dimensions[0] == dimensions[0]


@pytest.mark.parametrize("producer", ["report", "history"])
def test_escalation_html_retains_honest_producer_output(producer: str) -> None:
    if producer == "report":
        sparkline, label = assemble_stage._derive_risk_sparkline(RiskLevel.HIGH, RiskLevel.LOW)
        expected_label = "rising vs prior issue"
    else:
        sparkline, label = escalate._history_risk_trend((RiskLevel.LOW, RiskLevel.HIGH))
        expected_label = "net rise across 2 observations"
    preview = escalate.EscalationPreview(
        rule_name="synthetic_rule", dimension_name="Synthetic dimension", consecutive_high=0,
        workstream_ids=(), workstream_names=(), recipients=(), subject="Synthetic escalation",
        md_body="", html_body="", detail_label="Synthetic detail", detail_text="Synthetic evidence",
        signal_text="", cooldown_key="synthetic", trend_label=label, risk_sparkline=sparkline,
    )
    rendered = escalate._finalize_escalation_preview(edition_name="synthetic_weekly", preview=preview)
    assert expected_label in rendered.html_body
    assert sparkline is not None and sparkline in rendered.html_body
    assert "4w" not in rendered.html_body
    assert "chronic high" not in rendered.html_body
