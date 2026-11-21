from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cli
from cli import app
from src.commands.readiness import evaluate_launch_study_readiness
from src.core.study_evaluator import (
    MatchedWeeklyCycle,
    StratumSample,
    evaluate_matched_cycles,
    evaluate_stratum_agreement,
)


def _eight_passing_cycles(*, start: date = date(2026, 11, 20), assisted: float = 50.0) -> tuple[MatchedWeeklyCycle, ...]:
    return tuple(
        MatchedWeeklyCycle(
            cycle_id=f"cycle-{idx + 1}",
            week_start=start + timedelta(days=7 * idx),
            baseline_minutes=420.0,
            assisted_minutes=assisted,
            quality_passed=True,
        )
        for idx in range(8)
    )


@pytest.mark.parametrize("case", [
    "eligible", "empty", "implicit_baseline", "string_bool", "duplicate_stratum",
    "invalid_json", "noncanonical_date", "wrong_collection", "wrong_document",
])
def test_readiness_study_evaluates_explicit_evidence_without_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    def unexpected_startup(*args, **kwargs):
        pytest.fail("Study evaluation must skip unrelated startup.")

    monkeypatch.setattr(cli, "_set_invocation_ai_mode", unexpected_startup)
    cycles = [asdict(cycle) for cycle in _eight_passing_cycles()]
    strata = [asdict(StratumSample("exec", 60, 0))]
    if case == "empty":
        cycles.clear()
    elif case == "implicit_baseline":
        del cycles[0]["baseline_minutes"]
    elif case == "string_bool":
        cycles[0]["quality_passed"] = "false"
    elif case == "duplicate_stratum":
        strata += strata
    elif case == "noncanonical_date":
        cycles[0]["week_start"] = "20261120"
    path = tmp_path / "study.json"
    document = {"cycles": cycles, "strata": {} if case == "wrong_collection" else strata}
    content = "{" if case == "invalid_json" else json.dumps([] if case == "wrong_document" else document, default=str)
    path.write_text(content, encoding="utf-8")
    result = CliRunner().invoke(app, ["readiness", "--format", "json", "study", "--evidence", str(path)])
    assert path.read_text(encoding="utf-8") == content
    assert result.exit_code == {"eligible": 0, "empty": 1}.get(case, 2), result.output
    if case in {"eligible", "empty"}:
        payload = json.loads(result.stdout)
        assert payload["accepted"] is (case == "eligible")
        assert payload["scope"] == "statistical_evaluation"
    else:
        assert "Invalid study evidence" in result.stderr


def test_stratum_agreement_decision_m_contracts() -> None:
    approved = evaluate_stratum_agreement(
        (
            StratumSample(stratum_id="exec", sample_count=60, divergence_count=1),
            StratumSample(stratum_id="workstream", sample_count=120, divergence_count=2, reverified_after_pause=True),
        )
    )
    assert all(res.approved for res in approved)
    assert approved[0].paused_for_recheck is False
    assert approved[1].paused_for_recheck is True

    blocked = evaluate_stratum_agreement(
        (
            StratumSample(stratum_id="too-few", sample_count=59, divergence_count=0),
            StratumSample(stratum_id="paused-unverified", sample_count=60, divergence_count=2, reverified_after_pause=False),
            StratumSample(stratum_id="paused-too-small", sample_count=119, divergence_count=2, reverified_after_pause=True),
        )
    )
    assert all(not res.approved for res in blocked)
    assert blocked[0].required_samples == 60
    assert blocked[1].paused_for_recheck is True
    assert blocked[2].required_samples == 120

    with pytest.raises(ValueError, match="Negative"):
        evaluate_stratum_agreement((StratumSample(stratum_id="bad", sample_count=-1, divergence_count=0),))
    with pytest.raises(ValueError, match="exceed"):
        evaluate_stratum_agreement((StratumSample(stratum_id="bad", sample_count=10, divergence_count=11),))
    with pytest.raises(ValueError, match="unique"):
        evaluate_stratum_agreement(
            (
                StratumSample(stratum_id="duplicate", sample_count=60, divergence_count=0),
                StratumSample(stratum_id="duplicate", sample_count=60, divergence_count=0),
            )
        )


def test_matched_cycles_evaluation_lcb_and_calendar_gates() -> None:
    strata = (StratumSample(stratum_id="exec", sample_count=60, divergence_count=0),)

    # 1. Empty cycles & missing strata fail closed.
    empty_res = evaluate_matched_cycles((), strata=())
    assert empty_res.accepted is False
    assert empty_res.lcb_95_saved_fraction is None
    assert len(empty_res.blocking_reasons) == 2

    # 2. Single cycle cannot compute 95% LCB.
    one_res = evaluate_matched_cycles(_eight_passing_cycles()[:1], strata=strata)
    assert one_res.accepted is False
    assert one_res.lcb_95_saved_fraction is None

    # 3. 8 cycles spanning 49 calendar days with low assisted time pass >80% 95% LCB.
    passing = evaluate_launch_study_readiness(_eight_passing_cycles(assisted=45.0), strata=strata)
    assert passing.accepted is True
    assert passing.cycle_count == 8
    assert passing.elapsed_calendar_days == 49
    assert passing.lcb_95_saved_fraction is not None and passing.lcb_95_saved_fraction > 0.80
    assert passing.blocking_reasons == ()

    # 4. Quality failure zeroes cycle contribution and drops LCB <= 0.80.
    degraded_cycles = _eight_passing_cycles(assisted=50.0)[:-1] + (
        MatchedWeeklyCycle(
            cycle_id="cycle-8",
            week_start=date(2026, 11, 20) + timedelta(days=49),
            baseline_minutes=420.0,
            assisted_minutes=50.0,
            quality_passed=False,
        ),
    )
    degraded = evaluate_matched_cycles(degraded_cycles, strata=strata)
    assert degraded.accepted is False
    assert any("95% LCB" in reason for reason in degraded.blocking_reasons)

    # 5. Compressed <49-day window fails calendar gate.
    compressed = tuple(
        MatchedWeeklyCycle(
            cycle_id=f"c-{i}",
            week_start=date(2026, 11, 20) + timedelta(days=3 * i),
            baseline_minutes=420.0,
            assisted_minutes=40.0,
            quality_passed=True,
        )
        for i in range(8)
    )
    compressed_res = evaluate_matched_cycles(compressed, strata=strata)
    assert compressed_res.accepted is False
    assert any("calendar days" in reason for reason in compressed_res.blocking_reasons)

    with pytest.raises(ValueError, match="baseline_minutes"):
        evaluate_matched_cycles(
            (MatchedWeeklyCycle(cycle_id="bad", week_start=date(2026, 11, 20), baseline_minutes=0.0, assisted_minutes=10.0, quality_passed=True),),
            strata=strata,
        )
    with pytest.raises(ValueError, match="assisted_minutes"):
        evaluate_matched_cycles(
            (MatchedWeeklyCycle(cycle_id="bad", week_start=date(2026, 11, 20), baseline_minutes=420.0, assisted_minutes=-1.0, quality_passed=True),),
            strata=strata,
        )

    # 6. A larger cohort still uses the matched-cycle bootstrap.
    thirty_two = tuple(
        MatchedWeeklyCycle(
            cycle_id=f"c32-{i}",
            week_start=date(2026, 11, 20) + timedelta(days=7 * i),
            baseline_minutes=420.0,
            assisted_minutes=40.0 + (i % 3),
            quality_passed=True,
        )
        for i in range(32)
    )
    large_sample = evaluate_matched_cycles(thirty_two, strata=strata)
    assert large_sample.accepted is True
    assert large_sample.cycle_count == 32
    assert large_sample.lcb_95_saved_fraction is not None and large_sample.lcb_95_saved_fraction > 0.80


def test_matched_cycle_bootstrap_uses_signed_savings_and_is_order_independent() -> None:
    from dataclasses import replace

    strata = (StratumSample("exec", 60, 0),)
    cycles = _eight_passing_cycles(assisted=0.0)
    cycles = (*cycles[:-1], replace(cycles[-1], assisted_minutes=840.0))
    result = evaluate_matched_cycles(cycles, strata=strata)
    assert result.mean_saved_fraction == pytest.approx(0.75)
    assert result.lcb_95_saved_fraction == pytest.approx(0.25)
    assert evaluate_matched_cycles(tuple(reversed(cycles)), strata=strata) == result
    assert not result.accepted


def test_matched_cycles_reject_duplicate_evidence_and_invalid_timings() -> None:
    from dataclasses import replace

    strata = (StratumSample("exec", 60, 0),)
    cycles = _eight_passing_cycles(assisted=0.0)
    for duplicate in (
        replace(cycles[1], cycle_id=cycles[0].cycle_id),
        replace(cycles[1], week_start=cycles[0].week_start + timedelta(days=3)),
    ):
        assert not evaluate_matched_cycles((cycles[0], duplicate, *cycles[2:]), strata=strata).accepted
    for timing, value in (
        ("baseline_minutes", float("nan")),
        ("assisted_minutes", float("inf")),
        ("quality_adjusted_minutes_saved", float("nan")),
        ("quality_adjusted_minutes_saved", 421.0),
    ):
        with pytest.raises(ValueError, match=timing):
            evaluate_matched_cycles((replace(cycles[0], **{timing: value}),), strata=strata)


def test_metric_list_resolves_workspace_registry_without_checkout_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "ext_workspace"
    programs_root = workspace / "programs"
    programs_root.mkdir(parents=True)

    # Fail closed when workspace metrics registry is absent under VERTEX_PROGRAMS_ROOT.
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(programs_root))
    missing_res = runner.invoke(app, ["admin", "metric", "list", "--format", "json"])
    assert missing_res.exit_code != 0
    assert "Workspace metrics registry directory does not exist" in missing_res.output

    # Reads workspace/knowledge/metrics once populated.
    metrics_dir = workspace / "knowledge" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "demo.yaml").write_text(
        """
metrics:
  - id: demo.synthetic_availability
    title: Synthetic Availability
    product: demo
    unit: percent
    owner: metric-owner
    description: Synthetic metric for isolated workspace parity.
    aggregation: last
    window: 7d
    source_system: kusto
    status: active
""".strip(),
        encoding="utf-8",
    )
    ok_res = runner.invoke(app, ["admin", "metric", "list", "--product", "demo", "--format", "json"])
    assert ok_res.exit_code == 0, ok_res.output
    parsed = json.loads(ok_res.stdout)
    assert [row["metric_id"] for row in parsed] == ["demo.synthetic_availability"]

    # Also resolves when passed via hidden --programs-root without env var.
    monkeypatch.delenv("VERTEX_PROGRAMS_ROOT", raising=False)
    arg_res = runner.invoke(
        app,
        ["admin", "metric", "list", "--product", "demo", "--format", "json", "--programs-root", str(programs_root)],
    )
    assert arg_res.exit_code == 0, arg_res.output
    assert [row["metric_id"] for row in json.loads(arg_res.stdout)] == ["demo.synthetic_availability"]
