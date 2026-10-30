from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.commands import escalate
from src.commands.report_pipeline import assemble_stage
from src.core.config_loader import ReportBundle, ReportConfig, ScorecardDimensionSettings, ScorecardSettings
from src.core.models import RiskLevel, ScorecardEvidencePacket
from src.core.overrides_store import OverridesDocument
from src.core.scorecard_trends import compare_dated_risk_pair


_RANK = {
    RiskLevel.UNKNOWN: 0,
    RiskLevel.DONE: 1,
    RiskLevel.LOW: 2,
    RiskLevel.MEDIUM: 3,
    RiskLevel.HIGH: 4,
    RiskLevel.BLOCKED: 5,
}
_PRIOR_AT = datetime(2040, 5, 1, 12, tzinfo=timezone.utc)
_CURRENT_AT = datetime(2040, 5, 8, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(("prior", "current"), list(product(_RANK, repeat=2)))
def test_report_comparison_uses_two_real_dated_samples(prior: RiskLevel, current: RiskLevel) -> None:
    sparkline, label = assemble_stage._derive_risk_sparkline(
        current, prior, current_at=_CURRENT_AT, prior_at=_PRIOR_AT
    )
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
    assert assemble_stage._derive_risk_sparkline(
        current, None, current_at=_CURRENT_AT, prior_at=_PRIOR_AT
    ) == (None, None)


@pytest.mark.parametrize(
    ("prior_at", "current_at"),
    [
        (None, _CURRENT_AT),
        (_PRIOR_AT, None),
        (datetime(2040, 5, 1, 12), _CURRENT_AT),
        (_PRIOR_AT, datetime(2040, 5, 8, 12)),
        (_PRIOR_AT, _PRIOR_AT),
        (_CURRENT_AT, _PRIOR_AT),
    ],
)
def test_dated_risk_pair_rejects_missing_naive_duplicate_or_reversed_clocks(
    prior_at: datetime | None, current_at: datetime | None
) -> None:
    assert compare_dated_risk_pair(
        RiskLevel.HIGH,
        RiskLevel.LOW,
        current_at=current_at,
        prior_at=prior_at,
    ) == (None, None)


@pytest.mark.parametrize(
    ("prior", "current", "label"),
    [
        (RiskLevel.LOW, RiskLevel.HIGH, "net rise across 2 observations"),
        (RiskLevel.HIGH, RiskLevel.LOW, "net fall across 2 observations"),
        (RiskLevel.LOW, RiskLevel.LOW, "no net change across 2 observations"),
        (RiskLevel.HIGH, RiskLevel.HIGH, "high across 2 observations"),
        (RiskLevel.HIGH, RiskLevel.BLOCKED, "net rise across 2 observations"),
        (RiskLevel.BLOCKED, RiskLevel.HIGH, "net fall across 2 observations"),
    ],
)
def test_escalation_labels_real_dated_publication_pair(
    prior: RiskLevel, current: RiskLevel, label: str
) -> None:
    sparkline, actual_label = escalate._history_risk_trend(
        prior, current, prior_at=_PRIOR_AT, current_at=_CURRENT_AT
    )
    assert sparkline == "".join(assemble_stage._spark_char(level) for level in (prior, current))
    assert actual_label == label


@pytest.mark.parametrize(
    ("prior", "current", "prior_at", "current_at"),
    [
        (RiskLevel.HIGH, RiskLevel.HIGH, None, _CURRENT_AT),
        (RiskLevel.HIGH, RiskLevel.HIGH, _PRIOR_AT, None),
        (RiskLevel.UNKNOWN, RiskLevel.HIGH, _PRIOR_AT, _CURRENT_AT),
        (RiskLevel.HIGH, RiskLevel.UNKNOWN, _PRIOR_AT, _CURRENT_AT),
        (RiskLevel.HIGH, RiskLevel.HIGH, _CURRENT_AT, _PRIOR_AT),
    ],
)
def test_escalation_does_not_infer_a_trend_without_two_valid_dated_known_samples(
    prior: RiskLevel, current: RiskLevel, prior_at: datetime | None, current_at: datetime | None
) -> None:
    assert escalate._history_risk_trend(
        prior, current, prior_at=prior_at, current_at=current_at
    ) == (None, None)


@pytest.mark.parametrize(
    ("history", "issue_number", "dimension_name", "expected"),
    [
        (({"issue_number": "1", "dimension": "Networking", "risk": "high"},), 1, "Networking", RiskLevel.HIGH),
        (({"issue_number": "bad", "dimension": "Networking", "risk": "low"},
          {"issue_number": 1, "dimension": "Networking", "risk": "high"}), 1, "Networking", RiskLevel.HIGH),
        (({"issue_number": None, "dimension": "Networking", "risk": "low"},
          {"issue_number": "1", "dimension": "Networking", "risk": "high"}), 1, "Networking", RiskLevel.HIGH),
        (({"issue_number": True, "dimension": "Networking", "risk": "high"},), 1, "Networking", None),
        (({"issue_number": 1, "dimension": "Other", "risk": "high"},), 1, "Networking", None),
        (({"issue_number": 2, "dimension": "Networking", "risk": "high"},), 1, "Networking", None),
        (({"issue_number": 1, "dimension": "Networking", "risk": 4},), 1, "Networking", None),
        (({"issue_number": 1, "dimension": "Networking", "risk": "invalid"},), 1, "Networking", RiskLevel.UNKNOWN),
        (({"issue_number": 1, "dimension": "Networking", "risk": "high"},
          {"issue_number": 1, "dimension": "Networking", "risk": 4}), 1, "Networking", None),
        ((), 1, "Networking", None),
    ],
)
def test_latest_confirmed_dimension_risk_is_exact_issue_dimension_and_valid_risk(
    history: tuple[dict[str, object], ...],
    issue_number: int,
    dimension_name: str,
    expected: RiskLevel | None,
) -> None:
    assert escalate._latest_confirmed_dimension_risk(
        history,
        issue_number=issue_number,
        dimension_name=dimension_name,
    ) == expected


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
        current_at=_CURRENT_AT,
        prior_at=_PRIOR_AT,
    )

    assert len(dimensions) == 1
    assert dimensions[0].risk_sparkline == (
        assemble_stage._spark_char(RiskLevel.LOW) + assemble_stage._spark_char(RiskLevel.HIGH)
    )
    assert dimensions[0].trend_label == "rising vs prior issue"
    assert scorecards[0].dimensions[0] == dimensions[0]


def test_compute_stage_uses_scorecard_snapshot_clock_when_trusted_issue_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from src.core.models import ArchiveEntry, ArchiveIndex, EditionType, Snapshot
    from src.core.overrides_store import OverridesDocument
    from src.core.pipeline import StageContext
    from src.core.stages import compute_stage as compute_stage_module

    prior_at = datetime(2040, 5, 1, 12, tzinfo=timezone.utc)
    current_at = datetime(2040, 5, 8, 12, tzinfo=timezone.utc)
    previous_snapshot = Snapshot(
        issue_number=19,
        generated_at=prior_at,
        ado_data_as_of=prior_at,
        edition_type=EditionType.DETAILED,
        items=(object(),),
        scorecards=(),
    )
    archive_index = ArchiveIndex(
        edition="synthetic_weekly",
        issues=(
            ArchiveEntry(issue_number=17, generated_at=datetime(2040, 4, 29, tzinfo=timezone.utc)),
            ArchiveEntry(issue_number=19, generated_at=prior_at),
        ),
    )
    overrides = OverridesDocument(issue_number=20, top_3_now=(), scorecards=())
    monkeypatch.setattr(compute_stage_module, "load_overrides", lambda *args, **kwargs: None)
    monkeypatch.setattr(compute_stage_module, "merge_overrides", lambda **kwargs: (overrides, None))
    monkeypatch.setattr(compute_stage_module, "save_overrides", lambda *args, **kwargs: tmp_path / "overrides.yaml")
    support = Mock()
    support.load_eta_forecasts.return_value = {}
    support.build_override_snapshot.return_value = {}
    support.build_continuity_deltas.return_value = object()
    support.build_scorecard_packets.return_value = {}
    support.build_scorecard_data.return_value = ((), (), ())
    support.load_report_signal_context.return_value = None
    support.build_exec_summary_text.return_value = "Synthetic summary"
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            ado=SimpleNamespace(date_window_days=30),
            scorecards=(),
        )
    )
    ctx = StageContext(
        edition_name="synthetic_weekly",
        reports_root=tmp_path / "reports",
        archive_root=tmp_path / "archive",
        resolved_issue_number=20,
        resolved_edition_type=EditionType.DETAILED,
        data_as_of=current_at,
        previous_snapshot=previous_snapshot,
        previous_issue_number=19,
        trusted_baseline_issue_number=17,
        archive_index=archive_index,
        bundle=bundle,
        stage_support=support,
    )

    compute_stage_module.ComputeStage().execute(ctx)

    assert support.build_scorecard_packets.call_args.args[2] is previous_snapshot
    assert support.build_scorecard_packets.call_args.kwargs["trusted_issue_number"] == 17
    assert support.build_scorecard_data.call_args.kwargs["current_at"] == current_at
    assert support.build_scorecard_data.call_args.kwargs["prior_at"] == prior_at


@pytest.mark.parametrize("producer", ["report", "history"])
def test_escalation_html_retains_honest_producer_output(producer: str) -> None:
    if producer == "report":
        sparkline, label = assemble_stage._derive_risk_sparkline(
            RiskLevel.HIGH, RiskLevel.LOW, current_at=_CURRENT_AT, prior_at=_PRIOR_AT
        )
        expected_label = "rising vs prior issue"
    else:
        sparkline, label = escalate._history_risk_trend(
            RiskLevel.LOW, RiskLevel.HIGH, prior_at=_PRIOR_AT, current_at=_CURRENT_AT
        )
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
