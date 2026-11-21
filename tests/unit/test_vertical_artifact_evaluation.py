from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.vertical_artifact_evaluation import (
    QualificationStatus,
    evaluate_vertical_artifact_protocol,
)
from scripts.evaluate_vertical_artifacts import main


def _artifact(*, cost: float, review_seconds: float) -> dict[str, object]:
    return {
        "slices": [
            {
                "kind": kind,
                "facts": [
                    {
                        "fact_id": f"{kind}-1",
                        "value": {"state": "blocked"},
                        "evidence_refs": [f"ado://{kind}-1"],
                    }
                ],
            }
            for kind in ("status", "dependency", "meeting-close")
        ],
        "metrics": {
            "cost_usd": {
                "value": cost,
                "evidence_refs": ["cost://synthetic-run"],
            },
            "review_seconds": {
                "value": review_seconds,
                "evidence_refs": ["timer://synthetic-run"],
            },
        },
    }


def _qualified_external_evidence() -> dict[str, object]:
    return {
        name: {"status": "verified", "evidence_refs": [f"evidence://{name}"]}
        for name in (
            "d9_decision",
            "owner",
            "blind_audit_protocol",
            "live_cycle",
            "real_program",
        )
    }


def test_factual_non_inferiority_is_deterministic_for_all_three_slices() -> None:
    payload = {
        "baseline": _artifact(cost=12.0, review_seconds=120.0),
        "candidate": _artifact(cost=10.0, review_seconds=90.0),
        "external_evidence": _qualified_external_evidence(),
    }

    report = evaluate_vertical_artifact_protocol(payload)

    assert [slice_.kind for slice_ in report.slices] == [
        "status",
        "dependency",
        "meeting-close",
    ]
    assert all(slice_.factual_non_inferiority for slice_ in report.slices)
    assert report.cost_non_inferiority
    assert report.review_time_non_inferiority
    assert report.qualification_status is QualificationStatus.QUALIFIED
    assert report.missing_external_evidence == ()


def test_missing_external_evidence_is_explicit_and_never_success_fallback() -> None:
    payload = {
        "baseline": _artifact(cost=12.0, review_seconds=120.0),
        "candidate": _artifact(cost=10.0, review_seconds=90.0),
        "external_evidence": {
            "d9_decision": {"status": "not_provided", "evidence_refs": []},
        },
    }

    report = evaluate_vertical_artifact_protocol(payload)

    assert report.factual_and_operational_checks_pass
    assert report.qualification_status is QualificationStatus.NOT_QUALIFIED
    assert report.missing_external_evidence == (
        "d9_decision",
        "owner",
        "blind_audit_protocol",
        "live_cycle",
        "real_program",
    )


def test_candidate_contradiction_fails_qualification() -> None:
    candidate = _artifact(cost=10.0, review_seconds=90.0)
    candidate["slices"][0]["facts"][0]["value"] = {"state": "done"}  # type: ignore[index]

    report = evaluate_vertical_artifact_protocol({
        "baseline": _artifact(cost=12.0, review_seconds=120.0),
        "candidate": candidate,
        "external_evidence": _qualified_external_evidence(),
    })

    assert report.qualification_status is QualificationStatus.NOT_QUALIFIED
    status = next(slice_ for slice_ in report.slices if slice_.kind == "status")
    assert status.contradicted_fact_ids == ("status-1",)
    assert not status.factual_non_inferiority


@pytest.mark.parametrize("metric", ["cost_usd", "review_seconds"])
def test_metric_without_evidence_fails(metric: str) -> None:
    payload = {
        "baseline": _artifact(cost=12.0, review_seconds=120.0),
        "candidate": _artifact(cost=10.0, review_seconds=90.0),
        "external_evidence": _qualified_external_evidence(),
    }
    payload["candidate"]["metrics"][metric]["evidence_refs"] = []  # type: ignore[index]

    report = evaluate_vertical_artifact_protocol(payload)

    assert report.qualification_status is QualificationStatus.NOT_QUALIFIED
    assert "missing candidate metric evidence" in " ".join(report.schema_errors)


def test_cli_returns_one_for_valid_but_unqualified_harness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({
        "baseline": _artifact(cost=12.0, review_seconds=120.0),
        "candidate": _artifact(cost=10.0, review_seconds=90.0),
        "external_evidence": {},
    }), encoding="utf-8")

    exit_code = main(["--protocol", str(protocol)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["qualification_status"] == "not_qualified"
    assert output["missing_external_evidence"]
