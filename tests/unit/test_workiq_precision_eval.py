from __future__ import annotations

from pathlib import Path

from src.core.workiq_precision_eval import (
    WorkIQPrecisionSummary,
    evaluate_workiq_precision,
    load_workiq_precision_samples,
    passes_workiq_precision_gate,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "workiq_precision_eval_samples.json"


def test_load_workiq_precision_samples_fixture() -> None:
    samples = load_workiq_precision_samples(FIXTURE_PATH)

    assert len(samples) == 10
    assert samples[0].sample_id == "wq-001"
    assert samples[4].label.value == "hallucinated"


def test_evaluate_workiq_precision_fixture_baseline() -> None:
    summary = evaluate_workiq_precision(load_workiq_precision_samples(FIXTURE_PATH))

    assert summary == WorkIQPrecisionSummary(
        total_samples=10,
        relevant_samples=7,
        irrelevant_samples=2,
        hallucinated_samples=1,
        precision=0.7,
        hallucination_rate=0.1,
        irrelevant_rate=0.2,
    )


def test_passes_workiq_precision_gate_thresholds() -> None:
    summary = evaluate_workiq_precision(load_workiq_precision_samples(FIXTURE_PATH))

    assert passes_workiq_precision_gate(summary, min_precision=0.6, max_hallucination_rate=0.1) is True
    assert passes_workiq_precision_gate(summary, min_precision=0.75, max_hallucination_rate=0.1) is False
    assert passes_workiq_precision_gate(summary, min_precision=0.6, max_hallucination_rate=0.05) is False
