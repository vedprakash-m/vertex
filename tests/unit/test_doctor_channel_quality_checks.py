from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from src.core.models_v2 import TrajectoryPoint

from src.commands.doctor_checks.channel_quality_checks import (
    conversion_fidelity_check,
    eta_credibility_check,
)


def test_conversion_fidelity_check_warns_on_low_scores(tmp_path: Path) -> None:
    program_root = tmp_path / "programs" / "demo"
    metrics_dir = program_root / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "conversion_fidelity.yaml").write_text(
        (
            'entries:\n'
            '  - function: newsletter\n'
            '    required_inputs: 2\n'
            '    sourced_inputs: 1\n'
            '    score: 0.5\n'
            '    computed_at: "2026-06-01T00:00:00+00:00"\n'
            '  - function: deck\n'
            '    required_inputs: 1\n'
            '    sourced_inputs: 0\n'
            '    score: 0.0\n'
            '    computed_at: "2026-06-01T00:00:00+00:00"\n'
        ),
        encoding="utf-8",
    )

    check = conversion_fidelity_check("demo", tmp_path / "programs")

    assert check is not None
    assert check.status == "warn"
    assert "Low fidelity functions: deck" in check.detail
    assert check.metadata == {
        "entries": [
            {"function": "newsletter", "score": 0.5},
            {"function": "deck", "score": 0.0},
        ]
    }


def test_eta_credibility_check_reports_low_items(tmp_path: Path) -> None:
    store_dir = tmp_path / "programs" / "demo" / "trajectories"
    store_dir.mkdir(parents=True)
    (store_dir / "101.jsonl").write_text(
        '{"date":"2026-06-01","state":"active","assigned_to":"alice","target_date":"2026-06-15","risk_level":null,"risk_assessment":null,"risk_assessment_comment":null,"area_path":"demo","tags":[]}\n',
        encoding="utf-8",
    )
    (store_dir / "202.jsonl").write_text(
        '{"date":"2026-06-01","state":"active","assigned_to":"bob","target_date":"2026-06-10","risk_level":null,"risk_assessment":null,"risk_assessment_comment":null,"area_path":"demo","tags":[]}\n',
        encoding="utf-8",
    )

    observed: list[tuple[TrajectoryPoint, ...]] = []

    def compute_credibility(points: tuple[TrajectoryPoint, ...]) -> tuple[float, object]:
        observed.append(points)
        if len(observed) == 1:
            return 0.4, object()
        return 0.75, object()

    check = eta_credibility_check(
        "demo",
        tmp_path / "programs",
        compute_eta_credibility_fn=compute_credibility,
    )

    assert check is not None
    assert check.status == "warn"
    assert check.detail == "Items with low ETA credibility (< 50%): WI#101 (credibility=40%)"
    assert check.metadata == {"low_credibility_count": 1}
    assert [point.assigned_to for point in _flatten(observed)] == ["alice", "bob"]


def _flatten(batches: list[tuple[TrajectoryPoint, ...]]) -> Iterator[TrajectoryPoint]:
    for batch in batches:
        yield from batch
